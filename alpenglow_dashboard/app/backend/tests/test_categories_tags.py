"""G1 tests: category management, cross-service tag management, store pruning
and normalization, and the categories/tags endpoints.

Backend-store tests run against the on-disk fixture repo (SERVICES_ROOT →
tests/fixtures/services, METADATA_PATH → tests/fixtures/metadata.yaml) with the
settings + categories stores pointed at a tmp_path, so nothing touches the live
/services repo or /data-store. Endpoint tests exercise MOCK_DATA=1 through the
TestClient so the mock code paths are covered too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard import categories, tags as settings_store
from alpenglow_dashboard.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SERVICES = FIXTURES / "services"


@pytest.fixture()
def store_env(monkeypatch, tmp_path):
    """Real (non-mock) store pointed at a tmp file + the fixture repo."""
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(FIXTURES / "metadata.yaml"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("CATEGORIES_PATH", str(tmp_path / "categories.json"))
    monkeypatch.delenv("TAGS_PATH", raising=False)
    yield tmp_path


# ── store normalization ───────────────────────────────────────────────────────


async def test_tags_normalized_on_write(store_env):
    # whitespace-runs collapse, ends strip, case-insensitive de-dupe.
    stored = await settings_store.set_tags(
        "multi", ["  Critical ", "Keycloak  SSO", "critical", "GPU"]
    )
    assert stored == ["Critical", "Keycloak SSO", "GPU"]


async def test_legacy_and_hand_edit_shapes_normalized_on_read(store_env, tmp_path):
    # a hand-edited file with near-duplicates + a legacy bare-list entry reads clean.
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "multi": {"tags": ["Beta ", " beta", "Beta"], "category": None},
                "mgmt_single": ["GPU", "gpu "],  # legacy flat shape
            }
        )
    )
    alls = await settings_store.all_settings()
    assert alls["multi"]["tags"] == ["Beta"]
    assert alls["mgmt_single"]["tags"] == ["GPU"]


# ── store pruning of unknown ids ──────────────────────────────────────────────


async def test_all_settings_prunes_unknown_ids_on_read(store_env):
    await settings_store.set_tags("multi", ["GPU"])
    await settings_store.set_tags("ghost_service", ["Phantom"])  # not a repo dir

    # read without known_ids → both present (raw)
    raw = await settings_store.all_settings()
    assert "ghost_service" in raw

    # read pruned to the real repo dirs → phantom gone
    known = {"multi", "mgmt_single"}
    pruned = await settings_store.all_settings(known_ids=known)
    assert "multi" in pruned
    assert "ghost_service" not in pruned


async def test_prune_unknown_rewrites_file(store_env, tmp_path):
    await settings_store.set_tags("multi", ["GPU"])
    await settings_store.set_tags("ghost_service", ["Phantom"])

    removed = await settings_store.prune_unknown({"multi", "mgmt_single"})
    assert removed == ["ghost_service"]

    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert "ghost_service" not in on_disk
    assert "multi" in on_disk


# ── cross-service tag rename / delete ─────────────────────────────────────────


async def test_rename_tag_across_services_atomic(store_env, tmp_path):
    await settings_store.set_tags("multi", ["Critical", "GPU"])
    await settings_store.set_tags("mgmt_single", ["Critical"])

    changed = await settings_store.rename_tag("Critical", "Essential")
    assert sorted(changed) == ["mgmt_single", "multi"]

    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert "Essential" in on_disk["multi"]["tags"]
    assert "Critical" not in on_disk["multi"]["tags"]
    assert on_disk["mgmt_single"]["tags"] == ["Essential"]


async def test_rename_tag_dedupes_into_existing(store_env):
    await settings_store.set_tags("multi", ["GPU", "Compute"])
    # rename Compute → GPU: the service already has GPU, so it de-dupes.
    changed = await settings_store.rename_tag("Compute", "GPU")
    assert changed == ["multi"]
    assert await settings_store.get_tags("multi") == ["GPU"]


async def test_rename_tag_case_insensitive_match(store_env):
    await settings_store.set_tags("multi", ["Beta"])
    changed = await settings_store.rename_tag("beta", "Stable")
    assert changed == ["multi"]
    assert await settings_store.get_tags("multi") == ["Stable"]


async def test_delete_tag_across_services(store_env, tmp_path):
    await settings_store.set_tags("multi", ["Critical", "GPU"])
    await settings_store.set_tags("mgmt_single", ["Critical"])

    changed = await settings_store.delete_tag("Critical")
    assert sorted(changed) == ["mgmt_single", "multi"]
    assert await settings_store.get_tags("multi") == ["GPU"]
    assert await settings_store.get_tags("mgmt_single") == []


async def test_delete_tag_noop_returns_empty(store_env):
    await settings_store.set_tags("multi", ["GPU"])
    assert await settings_store.delete_tag("Nonexistent") == []


# ── category rename atomicity (moves ALL members incl. metadata defaults) ──────


async def test_rename_category_moves_default_and_override_members(store_env, tmp_path):
    # mgmt_single resolves to "Monitoring" via metadata (no override).
    # Give multi an explicit override to "Monitoring" too.
    await settings_store.set_category("multi", "Monitoring")

    moved = await categories.rename_category("Monitoring", "Observability")
    assert sorted(moved) == ["mgmt_single", "multi"]

    # both members now carry an explicit override to the new name.
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert on_disk["multi"]["category"] == "Observability"
    assert on_disk["mgmt_single"]["category"] == "Observability"


async def test_rename_category_carries_metadata(store_env, tmp_path):
    await categories.set_icon("Monitoring", "gauge")
    await categories.set_order(["Monitoring"])  # order 0

    await categories.rename_category("Monitoring", "Observability")

    meta = await categories.all_meta()
    assert "Monitoring" not in meta
    assert meta["Observability"]["icon"] == "gauge"
    assert meta["Observability"]["order"] == 0


async def test_rename_category_is_atomic_single_write(store_env, tmp_path):
    # All members land in one file replace: after rename, no member is left in
    # the old category (partial move would leave a split).
    await settings_store.set_category("multi", "Infrastructure")
    # 6 fixture services default to Infrastructure + multi override = 7 members
    moved = await categories.rename_category("Infrastructure", "Core")
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    for sid in moved:
        assert on_disk[sid]["category"] == "Core"
    # none still resolve to Infrastructure
    infos = await categories.list_categories()
    names = {i.name for i in infos}
    assert "Infrastructure" not in names or next(
        i for i in infos if i.name == "Infrastructure"
    ).count == 0


# ── ordering / icon resolution ────────────────────────────────────────────────


def test_order_key_buckets():
    meta = {"Fun": {"order": 3}, "Media": {}}
    # stored-order bucket wins over known-five bucket
    assert categories.order_key("Fun", meta)[0] == 0
    assert categories.order_key("Media", meta)[0] == 1  # known, no stored order
    assert categories.order_key("Zzz", meta)[0] == 2  # unknown → alphabetical


def test_icon_fallback_chain():
    meta = {"Fun": {"icon": "coffee"}}
    assert categories.icon_for("Fun", meta) == "coffee"  # stored
    assert categories.icon_for("Media", meta) == "play-circle"  # known map
    assert categories.icon_for("Zzz", meta) == "stack"  # ph-stack fallback


def test_build_category_infos_lists_empty_metadata_categories():
    counts = {"Media": 2}
    meta = {"Retired": {"order": 9, "icon": "archive"}}
    infos = categories.build_category_infos(counts, meta)
    names = {i.name for i in infos}
    assert "Retired" in names  # empty but has metadata → still listed
    retired = next(i for i in infos if i.name == "Retired")
    assert retired.count == 0
    assert retired.icon == "archive"


# ── endpoints (MOCK_DATA=1 through TestClient) ────────────────────────────────


@pytest.fixture()
def mock_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("MOCK_DATA", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    # Snapshot the shared mock fixture + category metadata so mutating-endpoint
    # tests (rename/delete are global in mock mode) don't leak into other tests.
    from alpenglow_dashboard import mock as mock_mod

    import copy

    snapshot = copy.deepcopy(mock_mod.MOCK_SERVICES)
    meta_snapshot = dict(categories._MOCK_META)
    yield TestClient(create_app())
    mock_mod.MOCK_SERVICES.clear()
    mock_mod.MOCK_SERVICES.update(snapshot)
    categories._MOCK_META.clear()
    categories._MOCK_META.update(meta_snapshot)


def _csrf(client: TestClient) -> dict:
    token = client.get("/api/csrf").json()["token"]
    return {"X-CSRF-Token": token}


def test_categories_endpoint_shape(mock_client):
    resp = mock_client.get("/api/categories")
    assert resp.status_code == 200
    data = resp.json()
    names = [c["name"] for c in data]
    # the five known categories are present and ordered first
    assert names[:5] == ["Media", "Productivity", "Home", "Infrastructure", "Monitoring"]
    for c in data:
        assert set(c) == {"name", "icon", "order", "count"}
        assert c["count"] >= 0
    assert [c["order"] for c in data] == list(range(len(data)))


def test_tags_endpoint_shape_and_sso_flag(mock_client):
    resp = mock_client.get("/api/tags")
    assert resp.status_code == 200
    data = {t["name"]: t for t in resp.json()}
    assert "Keycloak SSO" in data
    assert data["Keycloak SSO"]["sso"] is True
    assert data["Keycloak SSO"]["count"] == len(data["Keycloak SSO"]["services"])
    # a plain tag is not flagged sso
    assert data["Critical"]["sso"] is False


def test_tag_rename_endpoint_mock(mock_client):
    hdr = _csrf(mock_client)
    before = {t["name"]: t["count"] for t in mock_client.get("/api/tags").json()}
    assert before.get("Critical", 0) >= 2

    resp = mock_client.post(
        "/api/tags/rename", json={"name": "Critical", "to": "Essential"}, headers=hdr
    )
    assert resp.status_code == 200
    assert len(resp.json()["changed"]) >= 2

    after = {t["name"]: t for t in mock_client.get("/api/tags").json()}
    assert "Critical" not in after
    assert after["Essential"]["count"] >= 2


def test_tag_delete_endpoint_mock(mock_client):
    hdr = _csrf(mock_client)
    resp = mock_client.post(
        "/api/tags/delete", json={"name": "Keycloak SSO"}, headers=hdr
    )
    assert resp.status_code == 200
    assert len(resp.json()["changed"]) >= 1
    after = {t["name"] for t in mock_client.get("/api/tags").json()}
    assert "Keycloak SSO" not in after


def test_category_rename_endpoint_mock(mock_client):
    hdr = _csrf(mock_client)
    resp = mock_client.post(
        "/api/categories/rename", json={"name": "Media", "to": "Cinema"}, headers=hdr
    )
    assert resp.status_code == 200
    assert len(resp.json()["changed"]) >= 1
    names = [c["name"] for c in mock_client.get("/api/categories").json()]
    assert "Cinema" in names
    assert "Media" not in names


def test_mutations_require_csrf(mock_client):
    # no CSRF header → 403 from the auth middleware
    resp = mock_client.post("/api/tags/rename", json={"name": "a", "to": "b"})
    assert resp.status_code == 403
