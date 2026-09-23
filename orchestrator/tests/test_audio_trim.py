"""Sprachsignal finden und Stille abschneiden (v1.16, Filler-Vorgenerierung)."""

import math
from array import array

import pytest

from orchestrator.audio import speech_segments, trim_silence

RATE = 24000


def tone(seconds: float) -> bytes:
    return array("h", (int(8000 * math.sin(2 * math.pi * 220 * i / RATE))
                       for i in range(int(seconds * RATE)))).tobytes()


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def test_segments_mark_where_speech_starts_and_ends():
    pcm = silence(0.5) + tone(1.0) + silence(0.3) + tone(0.4) + silence(0.5)

    segments = speech_segments(pcm, RATE)

    assert len(segments) == 2
    assert segments[0] == pytest.approx((0.5, 1.5), abs=0.02)
    assert segments[1] == pytest.approx((1.8, 2.2), abs=0.02)


def test_short_gaps_belong_to_the_same_segment():
    # Verschlusslaute sind kurz fast still - das ist keine Pause
    assert len(speech_segments(tone(0.5) + silence(0.08) + tone(0.5), RATE)) == 1


def test_silence_has_no_segments():
    assert speech_segments(silence(1.0), RATE) == []
    assert speech_segments(b"", RATE) == []


def test_odd_byte_count_does_not_break_the_analysis():
    assert len(speech_segments(tone(0.2) + b"\x01", RATE)) == 1


def test_trim_keeps_short_margins_around_the_speech():
    trimmed = trim_silence(silence(0.5) + tone(1.0) + silence(1.0), RATE)

    # 60 ms Vorlauf + 1 s Sprache + 200 ms Nachlauf (leise Auslaute)
    assert len(trimmed) / 2 / RATE == pytest.approx(1.26, abs=0.02)


def test_trim_fades_hard_cuts_to_avoid_clicks():
    pcm = tone(1.0)
    samples = array("h", trim_silence(pcm, RATE))

    assert len(samples) * 2 == len(pcm)
    assert abs(samples[-1]) < 100
    # Mitte bleibt unangetastet
    assert samples[len(samples) // 2] == array("h", pcm)[len(samples) // 2]


def test_trim_leaves_silence_alone():
    pcm = silence(0.5)
    assert trim_silence(pcm, RATE) == pcm
