from orchestrator.config import settings
from orchestrator.security import resolve_token


def test_login_with_valid_credentials_returns_admin_tier(client):
    response = client.post(
        "/v1/auth/login",
        json={
            "username": settings.admin_username,
            "password": settings.admin_password,
            "device_name": "pytest",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["tier"] == 3
    # Seit v1.13.1 ein Geraete-Token (langlebig, widerrufbar) statt JWT
    assert body["token"].startswith("hda_")
    resolved = resolve_token(body["token"])
    assert resolved["sub"] == settings.admin_username
    assert resolved["tier"] == 3


def test_login_with_invalid_credentials_is_rejected(client):
    response = client.post(
        "/v1/auth/login",
        json={"username": "someone", "password": "wrong", "device_name": "pytest"},
    )
    assert response.status_code == 401


# ---- Geraete-Tokens + einheitliche Auth (v1.13.1) -----------------------------------


def _login(client, device_name="handy"):
    response = client.post("/v1/auth/login", json={
        "username": settings.admin_username, "password": settings.admin_password,
        "device_name": device_name,
    })
    return response.json()["token"]


def test_device_token_works_for_rest_and_is_revocable(client):
    token = _login(client, "wohnzimmer-tablet")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/v1/conversations", headers=headers).status_code == 200
    devices = client.get("/v1/admin/devices", headers=headers).json()
    device = next(d for d in devices if d["device_name"] == "wohnzimmer-tablet")
    assert device["username"] == settings.admin_username
    assert device["last_seen_at"]

    # Widerruf -> Token sofort ungueltig (der grosse Vorteil gegenueber JWT)
    assert client.delete(f"/v1/admin/devices/{device['id']}",
                         headers={"Authorization": f"Bearer {_login(client)}"}
                         ).status_code == 204
    assert client.get("/v1/conversations", headers=headers).status_code == 401


def test_device_token_tier_is_read_fresh_from_db(client):
    """JWTs backen ihr Tier ein - Geraete-Tokens lesen es pro Request:
    Zurueckstufen wirkt sofort."""
    from orchestrator import repos
    from orchestrator.security import hash_password

    admin_headers = {"Authorization": f"Bearer {_login(client)}"}
    client.post("/v1/admin/users", headers=admin_headers,
                json={"username": "kind", "password": "pw", "tier": 3})
    login = client.post("/v1/auth/login", json={
        "username": "kind", "password": "pw", "device_name": "handy"})
    kind_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/v1/admin/users", headers=kind_headers).status_code == 200

    user = next(u for u in repos.list_users() if u["username"] == "kind")
    client.put(f"/v1/admin/users/{user['id']}", headers=admin_headers, json={"tier": 1})
    assert client.get("/v1/admin/users", headers=kind_headers).status_code == 403


def test_legacy_jwt_still_accepted_until_expiry(client):
    """Bestandsgeraete mit altem 7-Tage-JWT fallen nicht sofort raus."""
    from orchestrator.security import create_access_token

    jwt_token = create_access_token(settings.admin_username, 3)
    headers = {"Authorization": f"Bearer {jwt_token}"}
    assert client.get("/v1/admin/users", headers=headers).status_code == 200


def test_password_reset_revokes_all_devices_of_user(client):
    from orchestrator import repos

    admin_headers = {"Authorization": f"Bearer {_login(client)}"}
    client.post("/v1/admin/users", headers=admin_headers,
                json={"username": "mia", "password": "alt", "tier": 2})
    login = client.post("/v1/auth/login", json={
        "username": "mia", "password": "alt", "device_name": "handy"})
    mia_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/v1/conversations", headers=mia_headers).status_code == 200

    user = next(u for u in repos.list_users() if u["username"] == "mia")
    client.put(f"/v1/admin/users/{user['id']}", headers=admin_headers,
               json={"password": "neu"})
    assert client.get("/v1/conversations", headers=mia_headers).status_code == 401


def test_deleting_user_revokes_their_devices(client):
    from orchestrator import repos

    admin_headers = {"Authorization": f"Bearer {_login(client)}"}
    client.post("/v1/admin/users", headers=admin_headers,
                json={"username": "gast2", "password": "pw", "tier": 1})
    client.post("/v1/auth/login", json={
        "username": "gast2", "password": "pw", "device_name": "handy"})
    assert any(d["username"] == "gast2" for d in repos.list_device_tokens())

    user = next(u for u in repos.list_users() if u["username"] == "gast2")
    client.delete(f"/v1/admin/users/{user['id']}", headers=admin_headers)
    assert not any(d["username"] == "gast2" for d in repos.list_device_tokens())


def test_ws_rejects_invalid_token_like_rest(client):
    """Einheitliche Auth (v1.13.1): mitgeschicktes, aber ungueltiges Token
    -> Fehler-Frame + Verbindungsabbau statt stillem Gast-Fallback (vorher
    liefen Dialoge als unauffindbare Gast-Gespraeche weiter)."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(
        "/v1/assistant/stream", headers={"Authorization": "Bearer kaputt"}
    ) as ws:
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "neu anmelden" in error["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_without_token_stays_guest(client, monkeypatch):
    """OHNE Token bleibt der Gast-Zugang offen (Satelliten-Szenario 4.4)."""
    from unittest.mock import AsyncMock

    from orchestrator import graph as graph_module

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message",
                        AsyncMock(return_value={"role": "assistant", "content": "Hallo Gast"}))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Hi"})
        frame = ws.receive_json()
        while frame["type"] not in ("assistant_text",):
            frame = ws.receive_json()
        assert frame["text"] == "Hallo Gast"
