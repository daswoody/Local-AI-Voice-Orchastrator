import io
import wave


def _make_wav(rate: int = 16000, channels: int = 1, seconds: float = 0.1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * channels * int(rate * seconds))
    return buffer.getvalue()


def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok"}


def test_transcribe_wav_upload(client, fake_engine):
    response = client.post(
        "/v1/transcribe",
        content=_make_wav(),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "Hallo Welt"
    assert fake_engine.received_pcm is not None


def test_transcribe_wav_gets_resampled_to_16k(client, fake_engine):
    # 44.1k-Stereo-WAV rein -> Engine muss 16k-Mono-PCM sehen:
    # 0.1s bei 16k mono = 1600 Samples = 3200 Bytes (+/- Rundung von ratecv).
    client.post("/v1/transcribe", content=_make_wav(rate=44100, channels=2))
    assert abs(len(fake_engine.received_pcm) - 3200) <= 4


def test_transcribe_raw_pcm(client, fake_engine):
    raw = b"\x00\x00" * 1600
    response = client.post("/v1/transcribe?sample_rate=16000", content=raw)
    assert response.status_code == 200
    assert fake_engine.received_pcm == raw


def test_transcribe_empty_body_rejected(client):
    assert client.post("/v1/transcribe").status_code == 400
