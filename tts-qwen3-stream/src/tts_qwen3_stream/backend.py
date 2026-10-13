"""Qwen3-TTS mit echtem Streaming und GGUF-Quantisierung als Engine nach dem
Engine-Vertrag v1 (Heim-AI v1.25).

Modell: Qwen3-TTS-12Hz-*-Base (Apache-2.0, QwenLM/Qwen3-TTS), geklont aus einem
kurzen Sample, 10 Sprachen inkl. Deutsch. Laufzeit: qwentts.cpp (ggml, MIT) ueber
`qwentts-cpp-python` - dieselbe GGUF-Technik wie audio.cpp, aber mit
Streaming: Das Audio kommt in Chunks von `QWEN3_CHUNK_STEPS` Codec-Frames
(1 Frame = 80 ms), noch waehrend das Modell rechnet. Das erste Audio kommt so
nach wenigen hundert Millisekunden statt nach dem ersten ganzen Satz.

Gewichte: GGUF-Dateien (`QWEN3_QUANT`: Q8_0 Default, Q4_K_M, BF16, F32). Kein
torch: Die frueheren float16-Abstuerze (NaN) und der doppelte VRAM von float32
auf Turing entfallen; Q8_0/Q4_K_M rechnen ueber ggml-Kernel.

Der Text wird in Saetze geteilt (Qwen3 driftet bei langen Texten), jeder Satz
aber gestreamt."""

import logging
import os
from pathlib import Path

from tts_engine_kit import Backend, EngineInfo, Voice, split_sentences
from tts_engine_kit.audio import float_to_pcm16, wav_to_float

logger = logging.getLogger(__name__)

# Base-Modell (Hugging-Face-ID, bestimmt die GGUF-Dateien). 0.6B ist der
# Streaming-Default; 1.7B klingt besser und braucht mehr VRAM.
MODEL = os.environ.get("QWEN3_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base").strip()
# Quantisierung der Gewichte: Q8_0 (Default, nahezu verlustfrei), Q4_K_M
# (kleiner, etwas schlechter), BF16, F32.
QUANT = os.environ.get("QWEN3_QUANT", "Q8_0").strip()
# Optional eine andere Stufe nur fuer den Audio-Codec (leer = wie QUANT): Der
# Codec ist klein, ein tieferes Quant spart dort kaum etwas, kostet aber Klang.
CODEC_QUANT = os.environ.get("QWEN3_CODEC_QUANT", "").strip()
GGUF_REPO = os.environ.get("QWEN3_GGUF_REPO", "Serveurperso/Qwen3-TTS-GGUF").strip()
# Eigene GGUF-Dateien im Volume statt Download (beide setzen).
TALKER_GGUF = os.environ.get("QWEN3_TALKER_GGUF", "").strip()
CODEC_GGUF = os.environ.get("QWEN3_CODEC_GGUF", "").strip()
MODELS_DIR = Path(os.environ.get("QWEN3_MODELS_DIR", "/models"))
# Codec-Frames je Chunk (1 Frame = 80 ms Audio): kleiner = frueher das naechste
# Stueck, aber die Karte muss schneller als Echtzeit bleiben, sonst ruckelt es.
CHUNK_STEPS = int(os.environ.get("QWEN3_CHUNK_STEPS", "8"))
# Frames im allerersten Chunk (1, 2, 4 oder 8): bestimmt die Zeit bis zum
# ersten Ton (4 = 0,32 s Audio).
FIRST_CHUNK_STEPS = int(os.environ.get("QWEN3_FIRST_CHUNK_STEPS", "4"))
# FlashAttention von ggml (laeuft auch auf Turing). Bei Audio-Fehlern probehalber aus.
FLASH_ATTN = os.environ.get("QWEN3_FLASH_ATTN", "true").strip().lower() not in ("0", "false", "no", "off")
# fp16-Zwischenwerte begrenzen (nur fuer F16-Pfade relevant, bei NaN-Audio probieren).
CLAMP_FP16 = os.environ.get("QWEN3_CLAMP_FP16", "false").strip().lower() in ("1", "true", "yes", "on")
LOG_LEVEL = os.environ.get("QWEN3_LOG_LEVEL", "info").strip().lower()
# Stille am Ende des Samples (ICL-Modus): sonst faerbt dessen letzter Laut den
# Anfang der Sprache (Empfehlung von faster-qwen3-tts). 0 = aus.
REF_SILENCE_S = float(os.environ.get("QWEN3_REF_SILENCE_S", "0.5"))

# ISO-639-1 (Orchestrator) -> Sprachname, wie ihn Qwen3-TTS erwartet.
LANGUAGES = {
    "de": "german", "en": "english", "zh": "chinese", "ja": "japanese", "ko": "korean",
    "fr": "french", "ru": "russian", "pt": "portuguese", "es": "spanish", "it": "italian",
}
# Dateinamen im GGUF-Repo (wie qwentts_cpp.models; test_real_library.py prueft das).
_TALKER_STEMS = {
    "Qwen/Qwen3-TTS-12Hz-0.6B-Base": "qwen-talker-0.6b-base",
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base": "qwen-talker-1.7b-base",
}
_QUANTS = {"F32": "F32", "FP32": "F32", "BF16": "BF16", "Q8": "Q8_0", "Q8_0": "Q8_0",
           "Q4": "Q4_K_M", "Q4_K_M": "Q4_K_M"}
# Gewichte (MB) von Talker + Codec je Stufe - Schaetzung aus der Parameterzahl, nicht gemessen.
_WEIGHTS_MB = {
    "0.6B": {"F32": 2800, "BF16": 1400, "Q8_0": 750, "Q4_K_M": 450},
    "1.7B": {"F32": 8400, "BF16": 4200, "Q8_0": 2250, "Q4_K_M": 1350},
}
_OVERHEAD_MB = 800  # KV-Cache, Rechenpuffer, CUDA-Kontext


def _model_size(model: str) -> str:
    for size in ("1.7B", "0.6B"):
        if size in model:
            return size
    return ""


def normalize_quant(name: str) -> str:
    try:
        return _QUANTS[name.strip().upper()]
    except KeyError:
        raise ValueError(f"Quantisierung {name!r}: erlaubt sind {', '.join(sorted(set(_QUANTS.values())))}") from None


def gguf_names(model: str, quant: str, codec_quant: str = "") -> tuple[str, str]:
    """(Talker-, Codec-Dateiname) im GGUF-Repo."""
    if model not in _TALKER_STEMS:
        raise ValueError(f"QWEN3_MODEL={model!r}: bekannt sind {', '.join(sorted(_TALKER_STEMS))} "
                         "(oder QWEN3_TALKER_GGUF und QWEN3_CODEC_GGUF setzen)")
    talker_quant = normalize_quant(quant)
    return (f"{_TALKER_STEMS[model]}-{talker_quant}.gguf",
            f"qwen-tokenizer-12hz-{normalize_quant(codec_quant or quant)}.gguf")


class Qwen3StreamBackend(Backend):
    @classmethod
    def info(cls) -> EngineInfo:
        size = _model_size(MODEL)
        try:
            quant = normalize_quant(QUANT)
        except ValueError:
            quant = QUANT
        weights = _WEIGHTS_MB.get(size, {}).get(quant)
        return EngineInfo(
            name=" ".join(part for part in ("Qwen3-TTS", size, quant, "Streaming") if part),
            languages=tuple(LANGUAGES),
            sample_rate=24000,
            needs_sample=True,
            uses_transcript=True,
            instructions=False,
            streaming="chunk",
            vram_mb=weights + _OVERHEAD_MB if weights else None,
            description=("Klont die Stimme aus dem Voice-Sample (mit Transkript am aehnlichsten), "
                         "10 Sprachen inkl. Deutsch, streamt das Audio waehrend der Erzeugung "
                         "(GGUF, quantisiert). VRAM geschaetzt."),
            license="Apache-2.0 (Modell), MIT (qwentts.cpp)",
        )

    @classmethod
    def prepare(cls) -> dict:
        """GGUF-Dateien einmalig ins Volume laden (Hauptprozess, beim Start)."""
        if CHUNK_STEPS < 1:
            raise ValueError(f"QWEN3_CHUNK_STEPS={CHUNK_STEPS}: mindestens 1")
        if FIRST_CHUNK_STEPS not in (1, 2, 4, 8):
            raise ValueError(f"QWEN3_FIRST_CHUNK_STEPS={FIRST_CHUNK_STEPS}: erlaubt sind 1, 2, 4, 8")
        if TALKER_GGUF or CODEC_GGUF:
            if not (TALKER_GGUF and CODEC_GGUF):
                raise ValueError("QWEN3_TALKER_GGUF und QWEN3_CODEC_GGUF immer zusammen setzen")
            for path in (TALKER_GGUF, CODEC_GGUF):
                if not Path(path).is_file():
                    raise ValueError(f"GGUF-Datei nicht gefunden: {path}")
            return {"talker_path": TALKER_GGUF, "codec_path": CODEC_GGUF}
        talker_name, codec_name = gguf_names(MODEL, QUANT, CODEC_QUANT)
        from huggingface_hub import hf_hub_download

        logger.info("Hole %s und %s aus %s (einmalig, setzt nach Abbruch fort) ...",
                    talker_name, codec_name, GGUF_REPO)
        common = dict(repo_id=GGUF_REPO, cache_dir=str(MODELS_DIR / "gguf"),
                      token=os.environ.get("HF_TOKEN") or None)
        return {"talker_path": str(hf_hub_download(filename=talker_name, **common)),
                "codec_path": str(hf_hub_download(filename=codec_name, **common))}

    def __init__(self, talker_path: str, codec_path: str) -> None:
        self.talker_path = talker_path
        self.codec_path = codec_path
        self._tts = None
        self._languages: set[str] = set()

    def load(self, device: str) -> str:
        # ggml waehlt die Karte nicht per Argument, sondern nimmt, was CUDA
        # sichtbar macht. Der Modell-Prozess ist frisch gestartet, CUDA noch
        # nicht initialisiert - die Zuweisung aus dem Panel ("cuda:1") wird so
        # zur einzigen sichtbaren Karte (Reihenfolge PCI_BUS_ID aus der Compose).
        # "cpu": keine Karte sichtbar, ggml rechnet auf der CPU (langsam).
        os.environ["CUDA_VISIBLE_DEVICES"] = "" if device == "cpu" else (device.partition(":")[2] or "0")
        from qwentts_cpp import QwenTTS

        logger.info("Lade %s + %s auf %s (Flash-Attention %s)", Path(self.talker_path).name,
                    Path(self.codec_path).name, device, "an" if FLASH_ATTN else "aus")
        self._tts = QwenTTS(talker_path=self.talker_path, codec_path=self.codec_path,
                            use_fa=FLASH_ATTN, clamp_fp16=CLAMP_FP16, log_level=LOG_LEVEL)
        kind = self._tts.model_type()
        if kind != "base":
            raise RuntimeError(f"Qwen3-TTS-Base-Modell erwartet, geladen ist '{kind}'")
        self._languages = {str(name).lower() for name in self._tts.language_names()}
        return device

    def prepare_voice(self, voice: Voice):
        import numpy as np

        transcript = (voice.transcript or "").strip()
        logger.info("Bereite die Referenz vor (%s)", "mit Transkript" if transcript else "ohne Transkript")
        samples, rate = wav_to_float(voice.wav)
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if rate != 24000:
            target = int(round(len(samples) * 24000 / rate))
            samples = np.interp(np.linspace(0, len(samples) - 1, target), np.arange(len(samples)),
                                samples).astype(np.float32)
        if transcript and REF_SILENCE_S > 0:
            samples = np.concatenate([samples, np.zeros(int(REF_SILENCE_S * 24000), dtype=np.float32)])
        ref = self._tts.extract_voice_ref(samples)
        # Mit Transkript: ICL (Stimme + Sprachcodes des Samples), sonst nur der
        # Stimm-Fingerabdruck.
        return {"spk": ref.ref_spk_emb, "codes": ref.ref_codes if transcript else None,
                "text": transcript or None}

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
            stream = self._tts.stream(
                text=sentence, lang=language_name, ref_spk_emb=voice["spk"], ref_codes=voice["codes"],
                ref_text=voice["text"], codec_chunk_sec=CHUNK_STEPS / 12.5,
                first_chunk_frames=FIRST_CHUNK_STEPS)
            for audio, rate in stream:
                audio = np.asarray(audio, dtype=np.float32).reshape(-1)
                if not audio.size:
                    continue
                if not np.isfinite(audio).all():
                    raise RuntimeError("Qwen3 hat ungueltiges Audio (NaN) erzeugt - QWEN3_QUANT=BF16 oder "
                                       "QWEN3_FLASH_ATTN=false probieren")
                yield int(rate), float_to_pcm16(audio)
