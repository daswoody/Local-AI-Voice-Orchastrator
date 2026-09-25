"""Audio-Helfer fuer Backends (brauchen numpy - der Kern des Kits nicht)."""

import io
import wave


def wav_to_float(wav: bytes):
    """WAV (PCM 16/24/32 Bit, mono/stereo) -> (float32-Array in [-1, 1], Samplerate)."""
    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as reader:
        rate = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        frames = reader.readframes(reader.getnframes())

    if width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        ints = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        samples = ints.astype(np.float32) / float(1 << 23)
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f"WAV mit {width * 8} Bit wird nicht unterstuetzt")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32), rate


def float_to_pcm16(samples) -> bytes:
    """Float-Samples in [-1, 1] -> PCM16 little-endian."""
    import numpy as np

    array = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (array * 32767.0).astype("<i2").tobytes()
