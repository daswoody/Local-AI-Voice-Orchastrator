# audioop ist ab Python 3.13 aus der stdlib entfernt - hier bewusst genutzt,
# weil das Projekt auf 3.11 gepinnt ist (Docker-Image python:3.11-slim).
# Bei einem spaeteren Python-Upgrade: auf "audioop-lts" (PyPI) ausweichen.
import audioop
import io
import wave

TARGET_RATE = 16000  # Whisper-Eingabeformat (4.1: PCM16/16k)


def wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Liest WAV-Bytes und liefert (PCM16 mono, Original-Samplerate)."""
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


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = TARGET_RATE) -> bytes:
    if src_rate == dst_rate:
        return pcm
    converted, _ = audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, None)
    return converted
