"""Nachbau der qwen_tts-API (Qwen3TTSModel) fuer Tests ohne torch und Gewichte.

Gleiche Methoden und Argumentnamen wie qwen-tts 0.1.1 (test_real_library.py
prueft das gegen das echte Paket). Jeder Aufruf wird protokolliert - im
Prozess (CALLS) und, fuer den Modell-Unterprozess, in FAKE_QWEN_LOG."""

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


class _Inner:
    def get_supported_languages(self):
        return os.environ.get("FAKE_QWEN_LANGUAGES", "auto,german,english").split(",")


class Qwen3TTSModel:
    def __init__(self) -> None:
        self.model = _Inner()

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, **kwargs):
        _log({"call": "from_pretrained", "path": pretrained_model_name_or_path,
              "device_map": kwargs.get("device_map"), "dtype": str(kwargs.get("dtype")),
              "attn": kwargs.get("attn_implementation")})
        return cls()

    def create_voice_clone_prompt(self, ref_audio, ref_text=None, x_vector_only_mode=False):
        samples, rate = ref_audio
        _log({"call": "prompt", "rate": rate, "samples": len(samples),
              "peak": round(float(np.abs(samples).max()), 3) if len(samples) else 0.0,
              "ref_text": ref_text, "x_vector_only_mode": x_vector_only_mode})
        return [{"prompt": ref_text or "x-vector"}]

    def generate_voice_clone(self, text, language=None, ref_audio=None, ref_text=None,
                             x_vector_only_mode=False, voice_clone_prompt=None,
                             non_streaming_mode=False, **kwargs):
        _log({"call": "generate", "text": text, "language": language, "prompt": voice_clone_prompt})
        if "NaN" in text:
            return [np.array([np.nan, 0.1], dtype=np.float32)], 24000
        return [(0.25 * np.sin(np.arange(2400) / 10)).astype(np.float32)], 24000
