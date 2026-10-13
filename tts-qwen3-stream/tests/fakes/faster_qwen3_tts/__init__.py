"""Nachbau der faster_qwen3_tts-API (FasterQwen3TTS) fuer Tests ohne torch und
Gewichte.

Gleiche Methoden und Argumentnamen wie faster-qwen3-tts 0.5.4
(test_real_library.py prueft das gegen das echte Paket). Jeder Aufruf wird
protokolliert - im Prozess (CALLS) und, fuer den Modell-Unterprozess, in
FAKE_QWEN_LOG."""

import json
import os

import numpy as np

CALLS: list[dict] = []


def _log(entry: dict) -> None:
    CALLS.append(entry)
    path = os.environ.get("FAKE_QWEN_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


class _Tts:
    """Das darunterliegende qwen_tts.Qwen3TTSModel (`.model` des Wrappers)."""

    def create_voice_clone_prompt(self, ref_audio, ref_text=None, x_vector_only_mode=False):
        samples, rate = ref_audio
        _log({"call": "prompt", "rate": rate, "samples": len(samples),
              "peak": round(float(np.abs(samples).max()), 3) if len(samples) else 0.0,
              "tail_peak": round(float(np.abs(samples[-2000:]).max()), 3) if len(samples) else 0.0,
              "ref_text": ref_text, "x_vector_only_mode": x_vector_only_mode})
        return [{"prompt": ref_text or "x-vector"}]


class _Base:
    """`.model` des Qwen3TTSModel (HF-Modell)."""

    def get_supported_languages(self):
        return os.environ.get("FAKE_QWEN_LANGUAGES", "auto,german,english").split(",")


class FasterQwen3TTS:
    def __init__(self) -> None:
        self.model = _Tts()
        self.model.model = _Base()

    @classmethod
    def from_pretrained(cls, model_name, device="cuda", dtype=None, attn_implementation="sdpa",
                        max_seq_len=2048, **kwargs):
        _log({"call": "from_pretrained", "path": model_name, "device": device, "dtype": str(dtype),
              "attn": attn_implementation, "max_seq_len": max_seq_len})
        return cls()

    _warmed_up = False

    def warmup(self, prefill_len=100):
        _log({"call": "warmup"})

    def generate_voice_clone_streaming(self, text, language, ref_audio=None, ref_text="",
                                       chunk_size=12, voice_clone_prompt=None, **kwargs):
        _log({"call": "generate", "text": text, "language": language,
              "chunk_size": chunk_size, "prompt": voice_clone_prompt,
              "parity": kwargs.get("parity_mode", False)})
        # Zwei Chunks je Satz (bei "Lang" drei) - 0,1 s bei 24 kHz, wie ein Stream.
        for index in range(3 if "Lang" in text else 2):
            if "NaN" in text and index == 1:
                yield np.array([np.nan, 0.1], dtype=np.float32), 24000, {}
                return
            yield (0.25 * np.sin(np.arange(2400) / 10)).astype(np.float32), 24000, {"step": index}
