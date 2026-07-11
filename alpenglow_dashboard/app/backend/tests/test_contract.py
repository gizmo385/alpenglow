"""Contract smoke tests: every response model instantiates from mock fixtures,
and every API route serves payloads that validate against models.py."""

from pathlib import Path

import pytest

from alpenglow_dashboard import mock, models


# ── fixtures instantiate every response model ─────────────────────────────────


def test_mock_services_are_service_summaries():
    services = mock.mock_services()
    assert len(services) >= 30
    for s in services:
        assert isinstance(s, models.ServiceSummary)
        models.ServiceSummary.model_validate(s.model_dump())


def test_mock_service_ids_are_real_repo_dirs():
    """Mock ids must mirror the actual /services/<dir> layout when available."""
    root = Path("/services")
    if not root.is_dir():
        pytest.skip("repo checkout not mounted at /services")
    for sid in mock.MOCK_SERVICES:
        assert (root / sid).is_dir(), f"mock id '{sid}' is not a /services directory"


def test_mock_detail_and_stats_for_every_service():
    for sid in mock.MOCK_SERVICES:
        detail = mock.mock_service_detail(sid)
        assert isinstance(detail, models.ServiceDetail)
        assert detail.id == sid
        stats = mock.mock_stats(sid)
        assert isinstance(stats, models.Stats)
        assert len(stats.history.cpu) == 60
        assert len(stats.history.mem) == 60


def test_mock_overview_and_updates_models():
    overview = mock.mock_overview()
    assert isinstance(overview, models.Overview)
    assert overview.services.total == len(mock.MOCK_SERVICES)
    assert {p.name for p in overview.storage.pools} == {"rpool", "dpool"}

    updates = mock.mock_updates()
    assert isinstance(updates, models.Updates)
    assert updates.services, "mock mode should have pending updates"
    assert overview.updates.count == len(updates.services)


def test_action_and_misc_models():
    models.Health(status="ok", version="0.1.0")
    models.Me(user="x@y", groups=["admin"], isAdmin=True)
    models.Csrf(token="tok")
    models.TagsPayload(tags=["Critical"])
    models.ActionRequest(action="restart", confirm=True)
    assert mock.mock_action("glances", "restart").accepted


# ── routes serve payloads that validate against the contract ──────────────────


def test_health_route(client):
    body = client.get("/api/health").json()
    models.Health.model_validate(body)


def test_me_route(client):
    resp = client.get("/api/me")
    assert resp.status_code == 200
    me = models.Me.model_validate(resp.json())
    assert me.isAdmin


def test_csrf_route(client):
    resp = client.get("/api/csrf")
    assert resp.status_code == 200
    models.Csrf.model_validate(resp.json())
    assert "alpenglow_csrf" in resp.cookies


def test_overview_route(client):
    models.Overview.model_validate(client.get("/api/overview").json())


def test_services_routes(client):
    body = client.get("/api/services").json()
    assert isinstance(body, list) and body
    for item in body:
        models.ServiceSummary.model_validate(item)

    detail = client.get("/api/services/glances")
    assert detail.status_code == 200
    models.ServiceDetail.model_validate(detail.json())

    assert client.get("/api/services/does_not_exist").status_code == 404


def test_stats_route(client):
    models.Stats.model_validate(client.get("/api/services/glances/stats").json())


def test_compose_route(client):
    resp = client.get("/api/services/glances/compose")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "services:" in resp.text


def test_logs_route_streams_sse(client):
    with client.stream("GET", "/api/services/glances/logs") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunk = next(resp.iter_text())
        assert "log" in chunk or "data:" in chunk


def test_updates_routes(client):
    models.Updates.model_validate(client.get("/api/updates").json())

    token = client.get("/api/csrf").json()["token"]
    resp = client.post("/api/updates/refresh", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert models.Updates.model_validate(resp.json()).refreshing


def test_tags_route(client):
    token = client.get("/api/csrf").json()["token"]
    resp = client.put(
        "/api/services/glances/tags",
        json={"tags": ["Critical", "Critical", "  "]},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert models.TagsPayload.model_validate(resp.json()).tags == ["Critical"]


def test_action_route_and_guardrail(client):
    token = client.get("/api/csrf").json()["token"]
    headers = {"X-CSRF-Token": token}

    resp = client.post("/api/services/glances/actions", json={"action": "restart"}, headers=headers)
    assert resp.status_code == 200
    models.ActionResponse.model_validate(resp.json())

    # guarded service without confirm → 409
    resp = client.post("/api/services/caddy/actions", json={"action": "restart"}, headers=headers)
    assert resp.status_code == 409
    resp = client.post(
        "/api/services/caddy/actions", json={"action": "restart", "confirm": True}, headers=headers
    )
    assert resp.status_code == 200


def test_mutating_requires_csrf(client):
    resp = client.post("/api/services/glances/actions", json={"action": "restart"})
    assert resp.status_code == 403
