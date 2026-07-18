def test_list_voices_returns_stub_entries(client):
    response = client.get("/v1/voices")
    assert response.status_code == 200
    ids = {voice["id"] for voice in response.json()["voices"]}
    assert "default-de-female" in ids
