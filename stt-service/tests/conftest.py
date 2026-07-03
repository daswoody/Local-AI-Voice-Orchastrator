import pytest
from fastapi.testclient import TestClient

from stt_service.main import app


class FakeEngine:
    """Ersetzt die echte Whisper-Engine: gibt das Ergebnis zurueck, ohne
    faster-whisper zu brauchen, und zeichnet den Input zur Pruefung auf."""

    def __init__(self) -> None:
        self.received_pcm: bytes | None = None

    def transcribe(self, pcm16: bytes) -> dict:
        self.received_pcm = pcm16
        return {
            "text": "Hallo Welt",
            "language": "de",
            "audio_duration_ms": 1000,
            "processing_ms": 5,
        }


@pytest.fixture
def fake_engine(monkeypatch):
    from stt_service import engine as engine_module

    fake = FakeEngine()
    monkeypatch.setattr(engine_module, "engine", fake)
    return fake


@pytest.fixture
def client(fake_engine) -> TestClient:
    return TestClient(app)
