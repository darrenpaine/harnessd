"""Check health endpoint"""

import pytest
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


@pytest.mark.spec("SPEC-0002", "B-0006", "AT-0007")
def test_health_returns_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
