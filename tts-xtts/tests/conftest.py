import pytest
from fastapi.testclient import TestClient

from tts_xtts.main import app


class FakeEngine:
    def __init__(self) -> None:
        self.voices = ["default-de-female", "default-de-male"]
        self.last_request: tuple | None = None

    def has_voice(self, voice_id: str) -> bool:
        return voice_id in self.voices

    def list_voices(self) -> list[str]:
        return self.voices

    def stream(self, text: str, voice_id: str, language=None):
        self.last_request = (text, voice_id, language)
        yield b"\x01\x02" * 100
        yield b"\x03\x04" * 100


@pytest.fixture
def fake_engine(monkeypatch):
    from tts_xtts import engine as engine_module

    fake = FakeEngine()
    monkeypatch.setattr(engine_module, "engine", fake)
    return fake


@pytest.fixture
def client(fake_engine) -> TestClient:
    return TestClient(app)
