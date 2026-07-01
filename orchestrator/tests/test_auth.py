from orchestrator.config import settings
from orchestrator.security import decode_access_token


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
    decoded = decode_access_token(body["token"])
    assert decoded["sub"] == settings.admin_username


def test_login_with_invalid_credentials_is_rejected(client):
    response = client.post(
        "/v1/auth/login",
        json={"username": "someone", "password": "wrong", "device_name": "pytest"},
    )
    assert response.status_code == 401
