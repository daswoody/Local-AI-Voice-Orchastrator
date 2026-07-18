import pytest
from fastapi.testclient import TestClient

from tts_piper.main import app


class FakeEngine:
    def __init__(self) -> None:
        self.last_text: str | None = None

    def synthesize(self, text: str) -> tuple[bytes, int]:
        self.last_text = text
        # 0.1s Stille bei 22050 Hz (typische Piper-Rate)
        return b"\x00\x00" * 2205, 22050


@pytest.fixture
def fake_engine(monkeypatch):
    from tts_piper import engine as engine_module

    fake = FakeEngine()
    monkeypatch.setattr(engine_module, "engine", fake)
    return fake


@pytest.fixture
def client(fake_engine) -> TestClient:
    return TestClient(app)
