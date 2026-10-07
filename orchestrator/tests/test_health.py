def test_health_returns_ok(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["name"] == "heim-ai-orchestrator"


def test_admin_panel_is_revalidated_on_every_load(client):
    # Sonst nimmt der Browser nach einem Deploy noch das alte app.js aus dem
    # Cache (v1.22: ausgebauter Breeze-Abschnitt blieb sichtbar).
    for path in ("/admin/", "/admin/app.js", "/admin/styles.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
    # Unveraendert = 304 ohne Inhalt, das Nachfragen kostet also kaum etwas.
    etag = client.get("/admin/app.js").headers["etag"]
    cached = client.get("/admin/app.js", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
