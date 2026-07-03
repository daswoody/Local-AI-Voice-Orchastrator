from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.config import settings
from orchestrator.db import init_db
from orchestrator.main import app
from orchestrator.services import mcp_gateway as mcp_gateway_module


@pytest.fixture(autouse=True)
def temp_environment(tmp_path, monkeypatch):
    """Jeder Test bekommt frische DB + leere Voice-/Filler-Verzeichnisse.
    Das MCP-Gateway wird global gemockt (kein Netzwerk in Tests) -
    einzelne Tests ueberschreiben es bei Bedarf."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "filler_cache_dir", str(tmp_path / "filler-cache"))
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "list_openai_tools", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "call_tool",
        AsyncMock(return_value="Tool-Fehler: in Tests nicht verfuegbar"),
    )
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
