# audioop ist ab Python 3.13 aus der stdlib entfernt - hier bewusst genutzt,
# weil das Projekt auf 3.11 gepinnt ist (Docker-Image python:3.11-slim).
# Bei einem spaeteren Python-Upgrade: auf "audioop-lts" (PyPI) ausweichen.
import audioop
import base64
import io
import wave
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
