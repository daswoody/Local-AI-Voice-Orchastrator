import pytest
from fastapi.testclient import TestClient

from orchestrator.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
