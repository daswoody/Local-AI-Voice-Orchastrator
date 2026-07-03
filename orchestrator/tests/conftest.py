import pytest
from fastapi.testclient import TestClient

from orchestrator.config import settings
from orchestrator.db import init_db
from orchestrator.main import app


@pytest.fixture(autouse=True)
def temp_environment(tmp_path, monkeypatch):
    """Jeder Test bekommt frische DB + leere Voice-/Filler-Verzeichnisse."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "filler_cache_dir", str(tmp_path / "filler-cache"))
    init_db()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers(client) -> dict:
    response = client.post(
        "/v1/auth/login",
        json={
            "username": settings.admin_username,
            "password": settings.admin_password,
            "device_name": "pytest",
        },
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}
