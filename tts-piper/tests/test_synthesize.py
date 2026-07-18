import io
import wave


def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok"}


def test_synthesize_returns_wav_with_sample_rate(client, fake_engine):
    response = client.post("/v1/synthesize", json={"text": "Lass mich kurz nachdenken."})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-sample-rate"] == "22050"
    assert fake_engine.last_text == "Lass mich kurz nachdenken."

    with wave.open(io.BytesIO(response.content), "rb") as wav:
        assert wav.getframerate() == 22050
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 2205


def test_synthesize_rejects_empty_text(client):
    assert client.post("/v1/synthesize", json={"text": "   "}).status_code == 400


def test_entrypoint_builds_correct_hf_url():
    from tts_piper.entrypoint import _voice_url

    url = _voice_url("de_DE-thorsten-medium.onnx")
    assert url == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
        "/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"
    )
