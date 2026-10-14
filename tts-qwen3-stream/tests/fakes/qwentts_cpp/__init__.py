"""Nachbau der qwentts_cpp-API (QwenTTS) fuer Tests ohne Bibliothek, Karte und
Gewichte.

Gleiche Methoden und Argumentnamen wie qwentts-cpp-python 0.5.0
(test_real_library.py prueft das gegen das echte Paket). Jeder Aufruf wird
protokolliert - im Prozess (CALLS) und, fuer den Modell-Unterprozess, in
FAKE_QWEN_LOG."""

import json
import os
from dataclasses import dataclass

import numpy as np

CALLS: list[dict] = []


def _log(entry: dict) -> None:
    CALLS.append(entry)
    path = os.environ.get("FAKE_QWEN_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


@dataclass(frozen=True)
class VoiceRef:
    ref_spk_emb: np.ndarray
    ref_codes: np.ndarray


class QwenTTS:
    def __init__(self, talker_path, codec_path, *, library_path=None, use_fa=True, clamp_fp16=False,
                 codec_chunk_sec=24.0, log_level=None):
        _log({"call": "init", "talker": str(talker_path), "codec": str(codec_path), "use_fa": use_fa,
              "clamp_fp16": clamp_fp16, "log_level": log_level,
              "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES")})

    def model_type(self):
        return os.environ.get("FAKE_QWEN_MODEL_TYPE", "base")

    def language_names(self):
        return os.environ.get("FAKE_QWEN_LANGUAGES", "german,english").split(",")

    def extract_voice_ref(self, ref_audio_24k):
        audio = np.asarray(ref_audio_24k)
        _log({"call": "extract", "samples": int(audio.size), "dtype": str(audio.dtype),
              "peak": round(float(np.abs(audio).max()), 3) if audio.size else 0.0,
              "tail_peak": round(float(np.abs(audio[-2000:]).max()), 3) if audio.size else 0.0})
        return VoiceRef(ref_spk_emb=np.ones(4, dtype=np.float32), ref_codes=np.ones((2, 3), dtype=np.int32))

    def stream(self, *, text, lang="english", instruct=None, speaker=None, ref_audio_24k=None,
               ref_spk_emb=None, ref_codes=None, ref_text=None, seed=-1, max_new_tokens=2048,
               do_sample=True, temperature=0.9, top_k=50, top_p=1.0, repetition_penalty=1.05,
               subtalker_do_sample=None, subtalker_temperature=None, subtalker_top_k=None,
               subtalker_top_p=None, codec_chunk_sec=0.64, first_chunk_frames=4, dump_dir=None):
        _log({"call": "stream", "text": text, "lang": lang, "ref_text": ref_text,
              "icl": ref_codes is not None, "has_spk": ref_spk_emb is not None,
              "codec_chunk_sec": codec_chunk_sec, "first_chunk_frames": first_chunk_frames,
              "seed": seed, "temperature": temperature, "top_k": top_k, "top_p": top_p,
              "repetition_penalty": repetition_penalty})
        # Zwei Chunks je Satz (bei "NaN" der zweite kaputt) - 0,1 s bei 24 kHz.
        for index in range(2):
            if "NaN" in text and index == 1:
                yield np.array([np.nan, 0.1], dtype=np.float32), 24000
                return
            yield (0.25 * np.sin(np.arange(2400) / 10)).astype(np.float32), 24000
