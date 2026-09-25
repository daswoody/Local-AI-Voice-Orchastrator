"""Qwen3-TTS als Engine nach dem Engine-Vertrag v1 (Heim-AI v1.20).

Modell: Qwen3-TTS-12Hz-*-Base (Apache-2.0, QwenLM/Qwen3-TTS) - klont eine
Stimme aus einem kurzen Sample und spricht 10 Sprachen, darunter Deutsch.
Mit exaktem Transkript des Samples (ICL-Modus) kommt der Klon dem Original am
naechsten; ohne Transkript nutzt Qwen3 nur den Stimm-Fingerabdruck
(x-vector) - funktioniert, klingt aber weniger aehnlich.

Das Python-Paket erzeugt ganze Aeusserungen (echtes Streaming gibt es nur
ueber vLLM) - diese Engine spricht deshalb Satz fuer Satz: Das erste Audio
kommt nach dem ersten Satz, nicht erst nach der ganzen Antwort."""

import logging
import os
from pathlib import Path

from tts_engine_kit import Backend, EngineInfo, Voice, split_sentences
from tts_engine_kit.audio import float_to_pcm16, wav_to_float

logger = logging.getLogger(__name__)

# 1.7B klingt besser, 0.6B braucht weniger VRAM (~2,5 statt ~5 GB) und ist
# schneller. Alternativ ein Pfad zu eigenen Gewichten im Volume.
MODEL = os.environ.get("QWEN3_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base").strip()
MODELS_DIR = Path(os.environ.get("QWEN3_MODELS_DIR", "/models"))
# auto = bfloat16 ab Ampere, float16 auf aelteren Karten (2080 Ti), float32
# auf der CPU. Klingt das Audio kaputt, QWEN3_DTYPE=float32 (doppeltes VRAM).
DTYPE = os.environ.get("QWEN3_DTYPE", "auto").strip().lower()
# FlashAttention 2 braucht Ampere+ und ist nicht im Image - sdpa laeuft ueberall.
ATTENTION = os.environ.get("QWEN3_ATTENTION", "sdpa").strip()

# ISO-639-1 (Orchestrator) -> Sprachname, wie ihn Qwen3-TTS erwartet.
LANGUAGES = {
    "de": "german", "en": "english", "zh": "chinese", "ja": "japanese", "ko": "korean",
    "fr": "french", "ru": "russian", "pt": "portuguese", "es": "spanish", "it": "italian",
}
_DTYPES = {"float32", "float16", "bfloat16"}


def _model_size(model: str) -> str:
    for size in ("1.7B", "0.6B"):
        if size in model:
            return size
    return ""


class Qwen3Backend(Backend):
    @classmethod
    def info(cls) -> EngineInfo:
        size = _model_size(MODEL)
        return EngineInfo(
            name=f"Qwen3-TTS {size}".strip(),
            languages=tuple(LANGUAGES),
            sample_rate=24000,
            needs_sample=True,
            uses_transcript=True,
            instructions=False,
            streaming="sentence",
            # bfloat16/float16 inkl. Audio-Tokenizer und Arbeitsspeicher; float32 ~doppelt
            vram_mb=2500 if size == "0.6B" else 5000,
            description=("Klont die Stimme aus dem Voice-Sample (mit Transkript am aehnlichsten), "
                         "10 Sprachen inkl. Deutsch, spricht Satz fuer Satz."),
            license="Apache-2.0",
        )

    @classmethod
    def prepare(cls) -> dict:
        """Gewichte einmalig ins Volume laden (Hauptprozess, beim Start) - so
        dauert das spaetere Laden auf die Karte nur Sekunden."""
        if DTYPE != "auto" and DTYPE not in _DTYPES:
            raise ValueError(f"QWEN3_DTYPE={DTYPE!r}: erlaubt sind auto, {', '.join(sorted(_DTYPES))}")
        if Path(MODEL).is_dir():
            return {"model_dir": MODEL}
        from huggingface_hub import snapshot_download

        target = MODELS_DIR / MODEL.replace("/", "--")
        marker = target / ".download-complete"
        if not marker.exists():
            target.mkdir(parents=True, exist_ok=True)
            logger.info("Lade %s nach %s (einmalig, mehrere GB) ...", MODEL, target)
            # Setzt abgebrochene Downloads beim naechsten Start fort.
            snapshot_download(repo_id=MODEL, local_dir=str(target),
                              token=os.environ.get("HF_TOKEN") or None)
            marker.touch()
        return {"model_dir": str(target)}

    def __init__(self, model_dir: str) -> None:
        self.model_dir = model_dir
        self._model = None
        self._languages: set[str] = set()

    def load(self, device: str) -> str:
        import torch
        from qwen_tts import Qwen3TTSModel

        dtype = self._dtype(torch, device)
        logger.info("Lade %s auf %s (%s, Attention %s)", self.model_dir, device, dtype, ATTENTION)
        self._model = Qwen3TTSModel.from_pretrained(
            self.model_dir, device_map=device, dtype=dtype, attn_implementation=ATTENTION)
        supported = self._model.model.get_supported_languages() or []
        self._languages = {str(language).lower() for language in supported}
        return device

    @staticmethod
    def _dtype(torch, device: str):
        if DTYPE != "auto":
            return getattr(torch, DTYPE)
        if device == "cpu":
            return torch.float32
        index = int(device.partition(":")[2] or 0)
        major, _ = torch.cuda.get_device_capability(index)
        # bfloat16 rechnen erst Ampere-Karten (8.x) nativ.
        return torch.bfloat16 if major >= 8 else torch.float16

    def prepare_voice(self, voice: Voice):
        samples, rate = wav_to_float(voice.wav)
        transcript = (voice.transcript or "").strip()
        return self._model.create_voice_clone_prompt(
            ref_audio=(samples, rate),
            ref_text=transcript or None,
            x_vector_only_mode=not transcript,
        )

    def _language(self, language: str | None) -> str:
        name = LANGUAGES.get((language or "").lower())
        if name and (not self._languages or name in self._languages):
            return name
        return "auto"  # Qwen3 erkennt die Sprache dann selbst

    def synthesize(self, text, language, voice, instruction):
        if voice is None:
            raise ValueError("Qwen3-TTS braucht ein Voice-Sample - unter 'Stimmen' hochladen")
        import numpy as np

        language_name = self._language(language)
        for sentence in split_sentences(text):
            wavs, rate = self._model.generate_voice_clone(
                text=sentence, language=language_name, voice_clone_prompt=voice)
            audio = np.asarray(wavs[0], dtype=np.float32)
            if audio.size and not np.isfinite(audio).all():
                raise RuntimeError("Qwen3 hat ungueltiges Audio (NaN) erzeugt - auf dieser Karte "
                                   "QWEN3_DTYPE=float32 setzen")
            yield int(rate), float_to_pcm16(audio)
