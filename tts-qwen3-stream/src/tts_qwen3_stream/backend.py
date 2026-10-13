"""Qwen3-TTS mit echtem Streaming als Engine nach dem Engine-Vertrag v1 (v1.22).

Modell: Qwen3-TTS-12Hz-*-Base (Apache-2.0, QwenLM/Qwen3-TTS), geklont aus
einem kurzen Sample, 10 Sprachen inkl. Deutsch. Das Paket `qwen-tts` erzeugt
nur ganze Aeusserungen, und audio.cpp kann Qwen3 nur offline; deshalb fuehrt
diese Engine das Modell ueber faster-qwen3-tts (MIT, CUDA-Graphen) und
liefert das Audio waehrend der Erzeugung in Chunks von `QWEN3_CHUNK_STEPS`
Codec-Schritten (1 Schritt = 1/12 s Audio). Das erste Audio kommt so nach
wenigen hundert Millisekunden statt nach dem ersten ganzen Satz.

Der Text wird trotzdem in Saetze geteilt: Qwen3 driftet bei langen Texten,
und der statische Cache fasst nur `QWEN3_MAX_SEQ_LEN` Positionen (Referenz +
Text + Audio). Pro Satz kostet das nur einen schnellen Prefill.

Eigenes Image: faster-qwen3-tts bringt einen Fork von qwen-tts (`qwen-tts-hf`,
gleicher Modulname `qwen_tts`) und neuere transformers mit - nicht neben dem
Original `qwen-tts` installierbar."""

import logging
import os
from pathlib import Path

from tts_engine_kit import Backend, EngineInfo, Voice, split_sentences
from tts_engine_kit.audio import float_to_pcm16, wav_to_float

logger = logging.getLogger(__name__)

# 0.6B: ~2,5 GB VRAM und schnell genug fuer Echtzeit auf Turing (T4: Faktor ~1);
# 1.7B klingt besser, braucht ~5 GB. Alternativ ein Pfad zu eigenen Gewichten.
MODEL = os.environ.get("QWEN3_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base").strip()
MODELS_DIR = Path(os.environ.get("QWEN3_MODELS_DIR", "/models"))
# auto = bfloat16 ab Ampere, float16 auf aelteren Karten (2080 Ti). Klingt das
# Audio kaputt, QWEN3_DTYPE=float32 (doppeltes VRAM). Kein CPU-Betrieb: die
# CUDA-Graphen brauchen eine Nvidia-Karte.
DTYPE = os.environ.get("QWEN3_DTYPE", "auto").strip().lower()
ATTENTION = os.environ.get("QWEN3_ATTENTION", "sdpa").strip()
# Codec-Schritte je Chunk: 4 ~ 0,33 s Audio (frueh, aber mehr Overhead je
# Sekunde Audio), 8 ~ 0,67 s, 12 ~ 1 s. Kleiner = erstes Audio frueher, aber
# die Karte muss schneller als Echtzeit bleiben, sonst ruckelt es.
CHUNK_STEPS = int(os.environ.get("QWEN3_CHUNK_STEPS", "8"))
# Laenge des statischen Caches (Referenz-Prompt + Text + erzeugte Schritte).
MAX_SEQ_LEN = int(os.environ.get("QWEN3_MAX_SEQ_LEN", "2048"))
# Stille am Ende des Samples (ICL-Modus): sonst faerbt dessen letzter Laut den
# Anfang der Sprache (Empfehlung von faster-qwen3-tts). 0 = aus.
REF_SILENCE_S = float(os.environ.get("QWEN3_REF_SILENCE_S", "0.5"))
# CUDA-Graphen sind der Grund fuer das Tempo. Stuerzt der Modell-Prozess damit
# ab (Exit-Code -11), mit QWEN3_CUDA_GRAPHS=false pruefen, ob sie die Ursache
# sind: dann rechnet das Modell ohne Graphen (Pfad "parity" von
# faster-qwen3-tts) - deutlich langsamer, eher zum Eingrenzen als zum Sprechen.
CUDA_GRAPHS = os.environ.get("QWEN3_CUDA_GRAPHS", "true").strip().lower() not in ("0", "false", "no", "off")

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


class Qwen3StreamBackend(Backend):
    @classmethod
    def info(cls) -> EngineInfo:
        size = _model_size(MODEL)
        return EngineInfo(
            name=" ".join(part for part in ("Qwen3-TTS", size, "Streaming") if part),
            languages=tuple(LANGUAGES),
            sample_rate=24000,
            needs_sample=True,
            uses_transcript=True,
            instructions=False,
            streaming="chunk",
            # Modell + Audio-Tokenizer + CUDA-Graphen und statischer Cache
            vram_mb=3500 if size == "0.6B" else 6500,
            description=("Klont die Stimme aus dem Voice-Sample (mit Transkript am aehnlichsten), "
                         "10 Sprachen inkl. Deutsch, streamt das Audio waehrend der Erzeugung."),
            license="Apache-2.0 (Modell), MIT (faster-qwen3-tts)",
        )

    @classmethod
    def prepare(cls) -> dict:
        """Gewichte einmalig ins Volume laden (Hauptprozess, beim Start)."""
        if DTYPE != "auto" and DTYPE not in _DTYPES:
            raise ValueError(f"QWEN3_DTYPE={DTYPE!r}: erlaubt sind auto, {', '.join(sorted(_DTYPES))}")
        if CHUNK_STEPS < 1:
            raise ValueError(f"QWEN3_CHUNK_STEPS={CHUNK_STEPS}: mindestens 1")
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
        from faster_qwen3_tts import FasterQwen3TTS

        if not device.startswith("cuda"):
            raise RuntimeError("Qwen3-TTS Streaming braucht eine Nvidia-Karte (CUDA-Graphen) - "
                               "fuer die CPU Qwen3-TTS ueber audio.cpp nehmen")
        dtype = self._dtype(torch, device)
        logger.info("Lade %s auf %s (%s, Attention %s)", self.model_dir, device, dtype, ATTENTION)
        self._model = FasterQwen3TTS.from_pretrained(
            self.model_dir, device=device, dtype=dtype,
            attn_implementation=ATTENTION, max_seq_len=MAX_SEQ_LEN)
        supported = self._model.model.model.get_supported_languages() or []
        self._languages = {str(language).lower() for language in supported}
        logger.info("Gewichte geladen")
        if CUDA_GRAPHS:
            # CUDA-Graphen jetzt aufnehmen (dauert einige Sekunden) statt beim
            # ersten Satz: Der Live-Turn wartet nie auf das Laden.
            logger.info("Nehme CUDA-Graphen auf ...")
            self._model.warmup()
            logger.info("CUDA-Graphen bereit")
        else:
            # "Schon aufgenommen" vortaeuschen, sonst holt das Paket es beim
            # ersten Satz nach.
            self._model._warmed_up = True
            logger.warning("QWEN3_CUDA_GRAPHS=false: ohne CUDA-Graphen, deutlich langsamer")
        return device

    @staticmethod
    def _dtype(torch, device: str):
        if DTYPE != "auto":
            return getattr(torch, DTYPE)
        index = int(device.partition(":")[2] or 0)
        major, _ = torch.cuda.get_device_capability(index)
        # bfloat16 rechnen erst Ampere-Karten (8.x) nativ.
        return torch.bfloat16 if major >= 8 else torch.float16

    def prepare_voice(self, voice: Voice):
        import numpy as np

        logger.info("Bereite die Referenz vor (%s)", "mit Transkript" if voice.transcript else "ohne Transkript")
        samples, rate = wav_to_float(voice.wav)
        transcript = (voice.transcript or "").strip()
        if transcript and REF_SILENCE_S > 0:
            samples = np.concatenate([samples, np.zeros(int(REF_SILENCE_S * rate), dtype=samples.dtype)])
        return self._model.model.create_voice_clone_prompt(
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
            stream = self._model.generate_voice_clone_streaming(
                text=sentence, language=language_name, voice_clone_prompt=voice,
                chunk_size=CHUNK_STEPS, parity_mode=not CUDA_GRAPHS)
            for audio, rate, _timing in stream:
                audio = np.asarray(audio, dtype=np.float32).reshape(-1)
                if not audio.size:
                    continue
                if not np.isfinite(audio).all():
                    raise RuntimeError("Qwen3 hat ungueltiges Audio (NaN) erzeugt - auf dieser Karte "
                                       "QWEN3_DTYPE=float32 setzen")
                yield int(rate), float_to_pcm16(audio)
