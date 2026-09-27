"""Auth middleware behavior: forwarded headers, proxy secret, DEV_NO_AUTH bypass,
admin gate."""

import base64

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard.main import create_app

SECRET = "test-proxy-secret"


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def proxied(user: str, groups: str, password: str = SECRET) -> dict[str, str]:
    """Headers as oauth2-proxy sends them: identity plus the Basic secret."""
    return {
        "Authorization": basic(user, password),
        "X-Forwarded-User": user,
        "X-Forwarded-Groups": groups,
    }


@pytest.fixture()
def strict_client(monkeypatch) -> TestClient:
    """Client with the DEV_NO_AUTH bypass disabled (mock data still on)."""
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    monkeypatch.setenv("PROXY_AUTH_SECRET", SECRET)
    return TestClient(create_app())


def test_rejects_without_forwarded_identity(strict_client):
    assert strict_client.get("/api/services").status_code == 401


def test_accepts_forwarded_identity(strict_client):
    resp = strict_client.get("/api/me", headers=proxied("chris", "admin,users"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == "chris"
    assert body["isAdmin"] is True


def test_keycloak_style_group_paths_count_as_admin(strict_client):
    resp = strict_client.get("/api/me", headers=proxied("chris", "/admin"))
    assert resp.json()["isAdmin"] is True


def test_rejects_forged_headers_without_proxy_secret(strict_client):
    # Any container on the caddy network can reach the backend directly.
    headers = {"X-Forwarded-User": "mallory", "X-Forwarded-Groups": "admin"}
    assert strict_client.get("/api/me", headers=headers).status_code == 401


def test_rejects_wrong_proxy_secret(strict_client):
    resp = strict_client.get("/api/me", headers=proxied("mallory", "admin", "guess"))
    assert resp.status_code == 401


@pytest.mark.parametrize("auth", ["Bearer abc", "Basic not-base64!", "Basic " + "x" * 3])
def test_rejects_malformed_authorization(strict_client, auth):
    headers = {**proxied("mallory", "admin"), "Authorization": auth}
    assert strict_client.get("/api/me", headers=headers).status_code == 401


def test_unset_proxy_secret_fails_closed(strict_client, monkeypatch):
    monkeypatch.delenv("PROXY_AUTH_SECRET")
    resp = strict_client.get("/api/me", headers=proxied("chris", "admin", ""))
    assert resp.status_code == 401


def test_non_admin_cannot_mutate(strict_client):
    headers = proxied("guest", "users")
    token = strict_client.get("/api/csrf", headers=headers).json()["token"]
    resp = strict_client.post(
        "/api/services/glances/actions",
        json={"action": "restart"},
        headers={**headers, "X-CSRF-Token": token},
    )
    assert resp.status_code == 403


def test_admin_with_csrf_can_mutate(strict_client):
    headers = proxied("chris", "admin")
    token = strict_client.get("/api/csrf", headers=headers).json()["token"]
    resp = strict_client.post(
        "/api/services/glances/actions",
        json={"action": "restart"},
        headers={**headers, "X-CSRF-Token": token},
    )
    assert resp.status_code == 200


def test_csrf_mismatch_rejected(strict_client):
    headers = proxied("chris", "admin")
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
