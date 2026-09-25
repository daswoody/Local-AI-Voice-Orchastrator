"""Satzaufteilung und Audio-Helfer."""

import io
import math
import wave

import numpy as np

from tts_engine_kit import split_sentences
from tts_engine_kit.audio import float_to_pcm16, wav_to_float


def test_sentences_are_split_but_abbreviations_stay_together():
    text = "Morgen ist der 3. Oktober. Das ist z. B. ein Feiertag!  Kommst du?"
    assert split_sentences(text) == ["Morgen ist der 3. Oktober.",
                                     "Das ist z. B. ein Feiertag! Kommst du?"]


def test_short_trailing_bit_joins_the_previous_sentence():
    assert split_sentences("Das ist ein ganz normaler Satz. Ja.") == [
        "Das ist ein ganz normaler Satz. Ja."]


def test_long_sentences_are_split_at_commas_then_words():
    text = ", ".join(["ein Teilsatz mit ein paar Woertern"] * 12) + "."
    pieces = split_sentences(text, max_chars=100)
    assert all(len(piece) <= 100 for piece in pieces)
    assert " ".join(pieces) == text
    no_commas = " ".join(["Wort"] * 80)
    assert all(len(piece) <= 100 for piece in split_sentences(no_commas, max_chars=100))


def test_empty_text_gives_no_sentences():
    assert split_sentences("  \n ") == []


def _wav(width: int, channels: int, rate: int, samples: list[float]) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        full = (1 << (8 * width - 1)) - 1
        writer.writeframes(b"".join(int(v * full).to_bytes(width, "little", signed=True) * channels
                                    for v in samples))
    return buffer.getvalue()


def test_wav_to_float_reads_16_and_24_bit_mono_and_stereo():
    tone = [0.5 * math.sin(i / 5) for i in range(200)]
    for width, channels in ((2, 1), (3, 2), (4, 1)):
        samples, rate = wav_to_float(_wav(width, channels, 24000, tone))
        assert rate == 24000 and samples.dtype == np.float32 and len(samples) == 200
        assert np.allclose(samples, tone, atol=1e-3)


def test_float_to_pcm16_clips():
    pcm = float_to_pcm16(np.array([0.0, 1.5, -2.0, 0.5], dtype=np.float32))
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [0, 32767, -32767, 16383]
