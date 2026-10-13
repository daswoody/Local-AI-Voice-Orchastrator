import io
import math
import sys
import wave
from pathlib import Path

import pytest

FAKES = str(Path(__file__).parent / "fakes")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_faster: gegen das echte faster-qwen3-tts-Paket (nur mit --extra engine)")


@pytest.fixture(autouse=True)
def fake_libraries(request, monkeypatch):
    """torch und faster_qwen3_tts durch die Nachbauten ersetzen - auch im
    Modell-Unterprozess, der sys.path beim Start erbt. Ausnahme: Tests mit
    Marker real_faster pruefen gegen das echte Paket."""
    if request.node.get_closest_marker("real_faster"):
        yield
        return
    monkeypatch.syspath_prepend(FAKES)
    for name in ("torch", "faster_qwen3_tts"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import faster_qwen3_tts

    faster_qwen3_tts.CALLS.clear()
    yield


def wav_bytes(width: int = 2, channels: int = 1, rate: int = 24000, seconds: float = 0.5) -> bytes:
    buffer = io.BytesIO()
    full = (1 << (8 * width - 1)) - 1
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"".join(
            int(0.5 * full * math.sin(i / 7)).to_bytes(width, "little", signed=True) * channels
            for i in range(int(rate * seconds))))
    return buffer.getvalue()
