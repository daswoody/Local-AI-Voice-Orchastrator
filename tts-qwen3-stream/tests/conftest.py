import io
import math
import sys
import wave
from pathlib import Path

import pytest

FAKES = str(Path(__file__).parent / "fakes")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_qwentts: gegen das echte qwentts-cpp-python-Paket (nur mit --extra engine)")


@pytest.fixture(autouse=True)
def fake_libraries(request, monkeypatch):
    """qwentts_cpp durch den Nachbau ersetzen - auch im Modell-Unterprozess,
    der sys.path beim Start erbt. Ausnahme: Tests mit Marker real_qwentts
    pruefen gegen das echte Paket."""
    if request.node.get_closest_marker("real_qwentts"):
        yield
        return
    monkeypatch.syspath_prepend(FAKES)
    monkeypatch.delitem(sys.modules, "qwentts_cpp", raising=False)
    # load() setzt CUDA_VISIBLE_DEVICES - nach dem Test wieder zuruecknehmen
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    import qwentts_cpp

    qwentts_cpp.CALLS.clear()
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
