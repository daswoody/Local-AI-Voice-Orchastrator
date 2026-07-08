"""CPU-Fallback der Whisper-Engine: Auf der 11-GB-GPU teilen sich LLM,
XTTS und Whisper das VRAM - ist es voll, darf die Spracherkennung nicht
sterben, sondern muss auf die CPU ausweichen."""

import pytest

from stt_service.engine import WhisperEngine


class _FakeSegment:
    text = "Hallo Welt"


class _FakeInfo:
    language = "de"
    duration = 1.0


class _FakeModel:
    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with

    def transcribe(self, audio, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        return [_FakeSegment()], _FakeInfo()


@pytest.fixture
def engine(monkeypatch):
    from stt_service.config import settings

    monkeypatch.setattr(settings, "whisper_device", "cuda")
    return WhisperEngine()


def test_load_falls_back_to_cpu_when_cuda_oom(engine, monkeypatch):
    created = []

    def fake_create(device, compute_type):
        created.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA failed with out of memory")
        return _FakeModel()

    monkeypatch.setattr(engine, "_create_model", fake_create)

    result = engine.transcribe(b"\x00\x00" * 1600)

    assert result["text"] == "Hallo Welt"
    assert result["device"] == "cpu"
    assert created == [("cuda", "float16"), ("cpu", "int8")]


def test_inference_cuda_error_switches_to_cpu_and_retries(engine, monkeypatch):
    cuda_model = _FakeModel(fail_with=RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"))
    cpu_model = _FakeModel()

    def fake_create(device, compute_type):
        return cuda_model if device == "cuda" else cpu_model

    monkeypatch.setattr(engine, "_create_model", fake_create)

    result = engine.transcribe(b"\x00\x00" * 1600)

    assert result["text"] == "Hallo Welt"
    assert result["device"] == "cpu"


def test_non_cuda_error_is_not_swallowed(engine, monkeypatch):
    monkeypatch.setattr(
        engine, "_create_model",
        lambda device, compute_type: _FakeModel(fail_with=ValueError("kaputtes Audio")),
    )

    with pytest.raises(ValueError, match="kaputtes Audio"):
        engine.transcribe(b"\x00\x00" * 1600)


def test_gpu_path_stays_on_gpu_when_healthy(engine, monkeypatch):
    monkeypatch.setattr(engine, "_create_model", lambda device, compute_type: _FakeModel())

    result = engine.transcribe(b"\x00\x00" * 1600)

    assert result["device"] == "cuda"
