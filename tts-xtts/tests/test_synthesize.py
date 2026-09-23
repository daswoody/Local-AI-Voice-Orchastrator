def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok"}


def test_list_voices(client):
    ids = {voice["id"] for voice in client.get("/v1/voices").json()}
    assert ids == {"default-de-female", "default-de-male"}


def test_synthesize_streams_pcm_with_sample_rate(client, fake_engine):
    response = client.post(
        "/v1/synthesize",
        json={"text": "Guten Morgen!", "voice_id": "default-de-female"},
    )
    assert response.status_code == 200
    assert response.headers["x-sample-rate"] == "24000"
    assert response.content == b"\x01\x02" * 100 + b"\x03\x04" * 100
    assert fake_engine.last_request == ("Guten Morgen!", "default-de-female", None)


def test_synthesize_unknown_voice_is_404(client):
    response = client.post("/v1/synthesize", json={"text": "Hi", "voice_id": "gibtsnicht"})
    assert response.status_code == 404
    assert "default-de-female" in response.json()["detail"]


def test_synthesize_rejects_empty_text(client):
    response = client.post("/v1/synthesize", json={"text": " ", "voice_id": "default-de-female"})
    assert response.status_code == 400


def test_engine_failure_becomes_clean_500_instead_of_stream_abort(client, fake_engine):
    """Stream-Priming: Fehler beim Laden/Latents/Sample muessen als 500 mit
    Fehlertext ankommen, nicht als mitten im Stream gekappte Verbindung."""

    def broken_stream(text, voice_id, language=None):
        raise RuntimeError("CUDA out of memory beim Laden der Latents")
        yield  # pragma: no cover - macht die Funktion zum Generator

    fake_engine.stream = broken_stream
    response = client.post(
        "/v1/synthesize", json={"text": "Hallo", "voice_id": "default-de-female"}
    )
    assert response.status_code == 500
    assert "CUDA out of memory" in response.json()["detail"]


# ---- Voll-Synthese fuer die Filler-Vorgenerierung (v1.16) ----------------------


def test_full_synthesis_returns_complete_wav(client, fake_engine):
    import io
    import wave

    response = client.post(
        "/v1/synthesize/full",
        json={"text": "Moment bitte.", "voice_id": "default-de-male", "temperature": 0.5},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        assert wav.readframes(wav.getnframes()) == b"\x05\x06" * 300
    assert fake_engine.last_request == ("Moment bitte.", "default-de-male", None, 0.5)


def test_full_synthesis_validates_like_streaming(client):
    assert client.post("/v1/synthesize/full", json={"text": " ", "voice_id": "default-de-female"}).status_code == 400
    assert client.post("/v1/synthesize/full", json={"text": "Hi", "voice_id": "gibtsnicht"}).status_code == 404
    assert client.post(
        "/v1/synthesize/full", json={"text": "Hi", "voice_id": "default-de-female", "temperature": 0}
    ).status_code == 422


def test_busy_engine_is_503_on_both_endpoints(client, fake_engine):
    """Laeuft schon eine Synthese laenger als erlaubt, gibt es ein klares 503
    statt eines haengenden Requests."""
    from tts_xtts.engine import EngineBusy

    def busy_stream(text, voice_id, language=None):
        raise EngineBusy("XTTS ist belegt - eine andere Synthese laeuft seit ueber 120s")
        yield  # pragma: no cover - macht die Funktion zum Generator

    def busy_synthesize(*args, **kwargs):
        raise EngineBusy("XTTS ist belegt - eine andere Synthese laeuft seit ueber 120s")

    fake_engine.stream = busy_stream
    fake_engine.synthesize = busy_synthesize
    payload = {"text": "Hallo", "voice_id": "default-de-female"}

    for path in ("/v1/synthesize", "/v1/synthesize/full"):
        response = client.post(path, json=payload)
        assert response.status_code == 503
        assert "belegt" in response.json()["detail"]
