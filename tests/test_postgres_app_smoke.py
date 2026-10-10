"""End-to-end PostgreSQL smoke test, executed by the PostgreSQL CI matrix."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from src.main import app

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# Run this in a fresh pytest process: importing the full app starts shared
# schedulers that can interact with other integration tests in the same session.
@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_APP_SMOKE") != "1"
    or not DATABASE_URL.startswith("postgresql+psycopg://"),
    reason="requires RUN_POSTGRES_APP_SMOKE=1 and PostgreSQL DATABASE_URL",
)
def test_postgres_registration_and_ready() -> None:
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        email = f"pg-smoke-{uuid.uuid4()}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "password123",
                "client_type": "app",
                "device_name": "pytest-pg",
                "platform": "ios",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["user"]["email"] == email
        assert payload["access_token"]
        assert payload["refresh_token"]
