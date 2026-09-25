"""Test-Backend: laedt sofort (oder wie per Env verlangt) und liefert pro Satz
eine JSON-Zeile statt Audio - so sehen die Tests, was im Modell-Prozess
ankam. Verhalten per Umgebungsvariable, die der Unterprozess beim Start erbt."""

import json
import os
import time

from tts_engine_kit import Backend, EngineInfo, split_sentences


class FakeBackend(Backend):
    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(name="Fake-TTS", languages=("de", "en"), sample_rate=16000,
                          needs_sample=False, uses_transcript=True)

    @classmethod
    def prepare(cls) -> dict:
        time.sleep(float(os.environ.get("FAKE_PREPARE_S", "0")))
        if os.environ.get("FAKE_PREPARE_FAIL"):
            raise RuntimeError("Download kaputt")
        return {"tag": "vorbereitet"}

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.prepare_calls = 0

    def load(self, device: str) -> str:
        time.sleep(float(os.environ.get("FAKE_LOAD_S", "0")))
        if os.environ.get("FAKE_LOAD_FAIL"):
            raise RuntimeError("CUDA out of memory (fake)")
        return "cuda:0" if device == "cuda" else device

    def prepare_voice(self, voice):
        self.prepare_calls += 1
        return {"transcript": voice.transcript, "bytes": len(voice.wav)}

    def synthesize(self, text, language, voice, instruction):
        if text.startswith("boom"):
            raise RuntimeError("Modell kaputt")
        if text.startswith("crash"):
            os._exit(3)  # wie ein Segfault/OOM-Kill: kein sauberes Ende
        if language == "xx":
            raise ValueError("Sprache xx wird nicht unterstuetzt")
        for index, sentence in enumerate(split_sentences(text)):
            time.sleep(float(os.environ.get("FAKE_SENTENCE_S", "0")))
            line = json.dumps({"i": index, "s": sentence, "lang": language, "voice": voice,
                               "prep": self.prepare_calls, "pid": os.getpid(),
                               "instr": instruction, "tag": self.tag}).encode() + b"\n"
            if len(line) % 2:  # PCM16 hat gerade Laengen
                line = b" " + line
            yield 16000, line
