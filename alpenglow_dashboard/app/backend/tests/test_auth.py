"""Auth middleware behavior: forwarded headers, DEV_NO_AUTH bypass, admin gate."""

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard.main import create_app


@pytest.fixture()
def strict_client(monkeypatch) -> TestClient:
    """Client with the DEV_NO_AUTH bypass disabled (mock data still on)."""
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    return TestClient(create_app())


def test_rejects_without_forwarded_identity(strict_client):
    assert strict_client.get("/api/services").status_code == 401


def test_accepts_forwarded_identity(strict_client):
    resp = strict_client.get(
        "/api/me", headers={"X-Forwarded-User": "chris", "X-Forwarded-Groups": "admin,users"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == "chris"
    assert body["isAdmin"] is True


def test_keycloak_style_group_paths_count_as_admin(strict_client):
    resp = strict_client.get(
        "/api/me", headers={"X-Forwarded-User": "chris", "X-Forwarded-Groups": "/admin"}
    )
    assert resp.json()["isAdmin"] is True


def test_non_admin_cannot_mutate(strict_client):
    headers = {"X-Forwarded-User": "guest", "X-Forwarded-Groups": "users"}
    token = strict_client.get("/api/csrf", headers=headers).json()["token"]
    resp = strict_client.post(
        "/api/services/glances/actions",
        json={"action": "restart"},
        headers={**headers, "X-CSRF-Token": token},
    )
    assert resp.status_code == 403


def test_admin_with_csrf_can_mutate(strict_client):
    headers = {"X-Forwarded-User": "chris", "X-Forwarded-Groups": "admin"}
    token = strict_client.get("/api/csrf", headers=headers).json()["token"]
    resp = strict_client.post(
        "/api/services/glances/actions",
        json={"action": "restart"},
        headers={**headers, "X-CSRF-Token": token},
    )
    assert resp.status_code == 200


def test_csrf_mismatch_rejected(strict_client):
    headers = {"X-Forwarded-User": "chris", "X-Forwarded-Groups": "admin"}
    strict_client.get("/api/csrf", headers=headers)  # sets the cookie
    resp = strict_client.post(
        "/api/services/glances/actions",
        json={"action": "restart"},
        headers={**headers, "X-CSRF-Token": "wrong-token"},
    )
    assert resp.status_code == 403


def test_spa_paths_not_gated(strict_client):
    # No forwarded identity: static/SPA paths are not 401'd by the middleware.
    resp = strict_client.get("/")
    assert resp.status_code != 401
