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
