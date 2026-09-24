# audioop ist ab Python 3.13 aus der stdlib entfernt - hier bewusst genutzt,
# weil das Projekt auf 3.11 gepinnt ist (Docker-Image python:3.11-slim).
# Bei einem spaeteren Python-Upgrade: auf "audioop-lts" (PyPI) ausweichen.
import audioop
import base64
import io
import wave
from array import array
from collections.abc import Iterator


def pcm_to_b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def b64_to_pcm(data: str) -> bytes:
    return base64.b64decode(data)


def wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """WAV-Bytes -> (PCM16 mono, Samplerate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())

    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if channels == 2:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    elif channels != 1:
        raise ValueError(f"nur mono/stereo unterstuetzt, nicht {channels} Kanaele")

    return frames, rate


def pcm16_to_wav(pcm: bytes, rate: int) -> bytes:
    """PCM16 mono -> WAV-Bytes (Gegenstueck zu wav_to_pcm16)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    converted, _ = audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, None)
    return converted


def chunk_pcm(pcm: bytes, chunk_bytes: int = 48000) -> Iterator[bytes]:
    """Teilt PCM in Frames (Default 48000 Bytes = 1s bei 24 kHz PCM16).
    Chunk-Groesse muss gerade sein, sonst zerreisst es 16-Bit-Samples."""
    assert chunk_bytes % 2 == 0
    for offset in range(0, len(pcm), chunk_bytes):
        yield pcm[offset : offset + chunk_bytes]


# ---- Sprachsignal finden/zuschneiden (Filler-Vorgenerierung, v1.16) ---------

_FRAME_MS = 10
# Frames unter 2 % des lautesten Frames (~ -34 dB) gelten als Stille; die
# absolute Untergrenze verhindert, dass bei sehr leisem Audio schon das
# Grundrauschen als Sprache zaehlt.
_SILENCE_REL = 0.02
_SILENCE_ABS = 100
# Luecken bis zu dieser Laenge gehoeren noch zum selben Sprachstueck
# (Verschlusslaute wie das "k" in "kurz" sind kurz fast still).
_MERGE_GAP_MS = 150


def speech_segments(pcm: bytes, rate: int) -> list[tuple[float, float]]:
    """(Start, Ende) in Sekunden aller hoerbaren Abschnitte von PCM16 mono.

    Bewusst simpel (Pegel pro 10-ms-Frame): Es geht um die Frage "wo faengt
    die Sprache an, wo hoert sie auf, wie lang ist sie" - nicht um VAD."""
    pcm = pcm[: len(pcm) // 2 * 2]  # halbes Sample am Ende liesse audioop scheitern
    frame_bytes = max(2, int(rate * _FRAME_MS / 1000) * 2)
    levels = [audioop.rms(pcm[i:i + frame_bytes], 2) for i in range(0, len(pcm), frame_bytes)]
    if not levels:
        return []
    threshold = max(max(levels) * _SILENCE_REL, _SILENCE_ABS)

    segments: list[list[int]] = []
    for index, level in enumerate(levels):
        if level < threshold:
            continue
        if segments and (index - segments[-1][1]) * _FRAME_MS <= _MERGE_GAP_MS:
            segments[-1][1] = index + 1
        else:
            segments.append([index, index + 1])
    frame_s = frame_bytes / 2 / rate
    return [(start * frame_s, min(end * frame_s, len(pcm) / 2 / rate)) for start, end in segments]


def trim_silence(pcm: bytes, rate: int, lead_ms: int = 60, tail_ms: int = 200, fade_ms: int = 20) -> bytes:
    """Stille vor und nach der Sprache abschneiden, Enden weich ausblenden.

    Der Nachlauf ist bewusst grosszuegig: leise Auslaute ("ch" in "nach")
    liegen oft unter der Stille-Schwelle und sollen nicht abgehackt werden.
    Ohne erkennbare Sprache bleibt das Audio unveraendert."""
    segments = speech_segments(pcm, rate)
    if not segments:
        return pcm
    start = max(0, int((segments[0][0] - lead_ms / 1000) * rate))
    end = min(len(pcm) // 2, int((segments[-1][1] + tail_ms / 1000) * rate))
    samples = array("h", pcm[start * 2:end * 2])
    _fade(samples, int(rate * 5 / 1000), fade_in=True)
    _fade(samples, int(rate * fade_ms / 1000), fade_in=False)
    return samples.tobytes()


def _fade(samples: array, length: int, fade_in: bool) -> None:
    length = min(length, len(samples))
    for i in range(length):
        factor = (i + 1) / (length + 1)
        index = i if fade_in else len(samples) - 1 - i
        samples[index] = int(samples[index] * factor)
