"""B4 external-integration tests.

Live HTTP is faked with ``httpx.MockTransport`` (no new deps, no network) by
monkeypatching ``httpx.AsyncClient`` to use a canned transport per test. ZFS
reads a fixture JSON written to a tmp path. Everything asserts the degradation
contract: missing key / dead host / 404 / stale file → nulls, never exceptions,
and ``/api/overview`` never 500s.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard import models
from alpenglow_dashboard.integrations import (
    _common,
    glances,
    kuma,
    zfs,
)
from alpenglow_dashboard.integrations import updates as updates_mod
from alpenglow_dashboard.integrations import overview as overview_mod
from alpenglow_dashboard.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
UPDATES_SERVICES = FIXTURES / "updates_services"


# ── helpers ───────────────────────────────────────────────────────────────────


def _mock_client(handler):
    """Patch httpx.AsyncClient so every instance uses a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("timeout", None)
        real_init(self, *args, **kwargs)

    return init


@pytest.fixture(autouse=True)
def _reset_caches():
    """Each test starts with cold integration caches."""
    for cache in (glances._CACHE, kuma._CACHE, updates_mod._CACHE):
        cache.invalidate()
    yield
    for cache in (glances._CACHE, kuma._CACHE, updates_mod._CACHE):
        cache.invalidate()


@pytest.fixture()
def real_mode(monkeypatch):
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    yield


# ── _common.ago ───────────────────────────────────────────────────────────────


def test_ago_buckets():
    assert _common.ago(None) is None
    assert _common.ago(5) == "5s ago"
    assert _common.ago(120) == "2m ago"
    assert _common.ago(3 * 3600) == "3h ago"
    assert _common.ago(4 * 86400) == "4d ago"


@pytest.mark.asyncio
async def test_ttl_cache_coalesces():
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return calls["n"]

    cache = _common.AsyncTTLCache(ttl=100)
    a = await cache.get(loader)
    b = await cache.get(loader)
    assert a == b == 1  # second call served from cache
    cache.invalidate()
    c = await cache.get(loader)
    assert c == 2


# ── glances ───────────────────────────────────────────────────────────────────


GLANCES_PAYLOADS = {
    "load": {"min1": 2.26, "min5": 1.83, "min15": 1.55, "cpucore": 8},
    "cpu": {"total": 18.5},
    "mem": {"total": 33386221568, "used": 18823488264},
    "memswap": {"total": 524283904, "used": 523812864},
    "fs": [
        {"device_name": "rpool/ROOT/ubuntu", "mnt_point": "/", "size": 240000000000,
         "used": 89000000000, "percent": 37.0},
        {"device_name": "rpool/services", "mnt_point": "/svc", "size": 260000000000,
         "used": 60000000000, "percent": 23.0},
        {"device_name": "dpool/data", "mnt_point": "/data", "size": 16000000000000,
         "used": 8400000000000, "percent": 52.0},
    ],
}


def _glances_handler(request):
    name = request.url.path.rsplit("/", 1)[-1]
    if name in GLANCES_PAYLOADS:
        return httpx.Response(200, json=GLANCES_PAYLOADS[name])
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_glances_host_and_fs(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_glances_handler))
    host = await glances.host_stats()
    assert host.load1 == 2.26 and host.load15 == 1.55
    assert host.cpuPct == 18.5
    assert host.memTotal == 33386221568
    assert host.swapUsed == 523812864

    glances._CACHE.invalidate()
    fs = await glances.fs_stats()
    labels = [f.label for f in fs]
    # one bar per pool, dpool (data) first then rpool (root); largest rpool sample wins
    assert labels == ["/data (dpool)", "/ (rpool)"]
    rpool = next(f for f in fs if "rpool" in f.label)
    assert rpool.size == 260000000000  # the larger of the two rpool devices


@pytest.mark.asyncio
async def test_glances_degrades_on_dead_host(monkeypatch):
    def dead(request):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))
    host = await glances.host_stats()
    assert host.load1 is None and host.cpuPct is None and host.memTotal is None
    glances._CACHE.invalidate()
    assert await glances.fs_stats() == []


# ── uptime kuma ───────────────────────────────────────────────────────────────


KUMA_METRICS = """\
# HELP monitor_status Monitor Status
# TYPE monitor_status gauge
monitor_status{monitor_id="1",monitor_name="Primary Services",monitor_type="group",monitor_url="https://"} 1
monitor_status{monitor_id="2",monitor_name="RSS",monitor_type="http",monitor_url="https://rss"} 1
monitor_status{monitor_id="3",monitor_name="Recipes",monitor_type="keyword",monitor_url="https://r"} 1
monitor_status{monitor_id="6",monitor_name="Support Services",monitor_type="group",monitor_url="https://"} 0
monitor_status{monitor_id="4",monitor_name="Nextcloud",monitor_type="http",monitor_url="https://nc"} 0
monitor_status{monitor_id="11",monitor_name="Root Pool Health",monitor_type="push",monitor_url="https://"} 0
monitor_status{monitor_id="25",monitor_name="Database Backup",monitor_type="push",monitor_url="https://"} 1
monitor_status{monitor_id="28",monitor_name="Backup Verification",monitor_type="push",monitor_url="https://"} 1
process_open_fds 42
"""


def _kuma_handler_ok(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return httpx.Response(401)
    return httpx.Response(200, text=KUMA_METRICS)


def test_parse_metrics_counts():
    mons = kuma.parse_metrics(KUMA_METRICS)
    assert len(mons) == 8  # 8 monitor_status rows, process_open_fds ignored
    real = kuma._real_monitors(mons)
    assert len(real) == 6  # two group rows dropped


@pytest.mark.asyncio
async def test_kuma_monitors_and_backups(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "test-key")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_kuma_handler_ok))
    mon = await kuma.monitors()
    # 6 real monitors: RSS, Recipes, Nextcloud(down), Root Pool(down), DB Backup, Verify
    assert mon.total == 6
    assert mon.up == 4  # RSS, Recipes, DB Backup, Backup Verification
    kuma._CACHE.invalidate()
    bk = await kuma.backups()
    # Database Backup + Backup Verification both up → ok; no freshness ages
    assert bk.ok is True
    assert bk.pgAgo is None and bk.kopiaAgo is None


@pytest.mark.asyncio
async def test_kuma_no_key_degrades(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "")
    mon = await kuma.monitors()
    assert mon.up is None and mon.total is None
    assert mon.note and "API_KEY" in mon.note.upper() or "key" in (mon.note or "")
    kuma._CACHE.invalidate()
    bk = await kuma.backups()
    assert bk.ok is None and bk.pgAgo is None


@pytest.mark.asyncio
async def test_kuma_401_degrades(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "bad-key")

    def unauth(request):
        return httpx.Response(401)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(unauth))
    mon = await kuma.monitors()
    assert mon.up is None and mon.total is None


# ── zfs ───────────────────────────────────────────────────────────────────────


def _write_zfs(tmp_path: Path, generated_at: float) -> Path:
    doc = {
        "generated_at": generated_at,
        "pools": {
            "rpool": {"name": "rpool", "state": "ONLINE", "size": 996432412672,
                      "used": 756497440768, "last_scrub": time.time() - 4 * 86400,
                      "scrub_errors": 0, "read_errors": 0, "write_errors": 0,
                      "checksum_errors": 0},
            "dpool": {"name": "dpool", "state": "ONLINE", "size": 11991548690432,
                      "used": 11157858795520, "last_scrub": time.time() - 4 * 86400,
                      "scrub_errors": 0, "read_errors": 1, "write_errors": 0,
                      "checksum_errors": 2},
        },
    }
    p = tmp_path / "zfs_status.json"
    p.write_text(json.dumps(doc))
    return p


@pytest.mark.asyncio
async def test_zfs_fresh(monkeypatch, tmp_path):
    p = _write_zfs(tmp_path, generated_at=time.time())
    monkeypatch.setenv("ZFS_STATUS_PATH", str(p))
    pools = await zfs.pools()
    assert {x.name for x in pools} == {"rpool", "dpool"}
    dpool = next(x for x in pools if x.name == "dpool")
    assert dpool.errors == 3  # 1 read + 0 write + 2 checksum
    assert dpool.size == 11991548690432
    assert dpool.scrubAgo and dpool.scrubAgo.endswith("ago")


@pytest.mark.asyncio
async def test_zfs_stale_is_dropped(monkeypatch, tmp_path):
    p = _write_zfs(tmp_path, generated_at=time.time() - 3 * 3600)  # 3h old
    monkeypatch.setenv("ZFS_STATUS_PATH", str(p))
    assert await zfs.pools() == []


@pytest.mark.asyncio
async def test_zfs_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ZFS_STATUS_PATH", str(tmp_path / "nope.json"))
    assert await zfs.pools() == []


# ── updates image matching ────────────────────────────────────────────────────


def test_normalize_image():
    n = updates_mod.normalize_image
    assert n("ghcr.io/immich-app/immich-machine-learning:release") == \
        "ghcr.io/immich-app/immich-machine-learning"
    assert n("ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}") == \
        "ghcr.io/immich-app/immich-machine-learning"
    assert n("ollama/ollama") == "ollama/ollama"
    assert n("twentycrm/twenty:${TAG:-latest}") == "twentycrm/twenty"
    assert n("registry.example.com:5000/foo/bar:v1") == "registry.example.com:5000/foo/bar"
    assert n("nginx@sha256:abc") == "nginx"


TRACKER_PAYLOAD = {
    "last_updated": 1783793486.0,
    "refreshing": False,
    "services": [
        {"name": "Immich", "repo": "immich",
         "image": "ghcr.io/immich-app/immich-machine-learning:release",
         "current_version": "v2.7.5", "latest_version": "v3.0.2", "has_updates": True,
         "html_url": "https://github.com/immich-app/immich/releases",
         "releases": [
             {"tag": "v3.0.2", "url": "u2", "published_at": "2026-07-09T18:03:44Z"},
             {"tag": "v3.0.0", "url": "u0", "published_at": "2026-07-02T13:58:12Z"},
         ]},
        {"name": "Ollama", "repo": "ollama", "image": "ollama/ollama",
         "current_version": "0.31.1", "latest_version": "v0.31.2", "has_updates": True,
         "html_url": "https://github.com/ollama/ollama/releases",
         "releases": [{"tag": "v0.31.2", "url": "uo", "published_at": "2026-07-06T22:28:22Z"}]},
        {"name": "Twenty", "repo": "twenty", "image": "twentycrm/twenty:latest",
         "current_version": "v2.18.5", "latest_version": "twenty/v2.20.0", "has_updates": True,
         "html_url": "https://github.com/twentyhq/twenty/releases",
         "releases": [{"tag": "v2.20.0", "url": "ut", "published_at": "2026-07-10T15:55:52Z"}]},
        {"name": "Glances", "repo": "glances", "image": "nicolargo/glances:latest",
         "current_version": "v4.5.5", "latest_version": "v4.5.5", "has_updates": False,
         "html_url": "h", "releases": []},
        {"name": "Some Unmatched", "repo": "whatever",
         "image": "ghcr.io/nobody/here:latest", "current_version": "1", "latest_version": "2",
         "has_updates": True, "html_url": "h", "releases": []},
    ],
}


@pytest.fixture()
def updates_env(monkeypatch):
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(UPDATES_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(UPDATES_SERVICES / "metadata.yaml"))
    yield


def _tracker_handler(payload, status=200):
    def handler(request):
        if request.url.path.endswith("/api/updates"):
            return httpx.Response(status, json=payload)
        if request.url.path.endswith("/refresh"):
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_updates_matching_and_enrichment(monkeypatch, updates_env):
    monkeypatch.setattr(httpx.AsyncClient, "__init__",
                        _mock_client(_tracker_handler(TRACKER_PAYLOAD)))
    enrich = await updates_mod.service_enrichment()
    # immich, ollama, twenty matched; glances has no update; nobody/here unmatched
    assert set(enrich) == {"immich", "ollama", "twenty"}
    assert enrich["immich"]["latestVersion"] == "v3.0.2"
    assert enrich["immich"]["releasedAt"] == "2026-07-09T18:03:44Z"  # newest release
    assert enrich["immich"]["changelogUrl"].endswith("/releases")


@pytest.mark.asyncio
async def test_updates_collision_prefers_repo_match(monkeypatch, updates_env):
    """Two tracker entries → one dir: the one whose repo == dir id wins."""
    payload = {
        "last_updated": 1.0, "refreshing": False,
        "services": [
            {"name": "Immich Public Proxy", "repo": "immich-public-proxy",
             "image": "alangrainger/immich-public-proxy:latest",
             "current_version": "3.0.0", "latest_version": "v3.0.1", "has_updates": True,
             "html_url": "hp", "releases": [{"tag": "v3.0.1", "url": "u", "published_at": "2026-06-30T20:47:15Z"}]},
            {"name": "Immich", "repo": "immich",
             "image": "ghcr.io/immich-app/immich-machine-learning:release",
             "current_version": "v2.7.5", "latest_version": "v3.0.2", "has_updates": True,
             "html_url": "hi", "releases": [{"tag": "v3.0.2", "url": "u", "published_at": "2026-07-09T18:03:44Z"}]},
        ],
    }
    monkeypatch.setattr(httpx.AsyncClient, "__init__",
                        _mock_client(_tracker_handler(payload)))
    enrich = await updates_mod.service_enrichment()
    assert set(enrich) == {"immich"}
    # the immich-machine-learning entry (repo == "immich") wins, not the proxy
    assert enrich["immich"]["latestVersion"] == "v3.0.2"


@pytest.mark.asyncio
async def test_updates_route_builds_entries(monkeypatch, updates_env):
    monkeypatch.setattr(httpx.AsyncClient, "__init__",
                        _mock_client(_tracker_handler(TRACKER_PAYLOAD)))
    result = await updates_mod.get_updates()
    ids = {e.id for e in result.services}
    assert ids == {"immich", "ollama", "twenty"}
    assert result.trackerUnavailable is False
    immich = next(e for e in result.services if e.id == "immich")
    assert immich.name == "Immich" and immich.icon == "ph-images"
    assert immich.latestVersion == "v3.0.2"


@pytest.mark.asyncio
async def test_updates_tracker_unavailable(monkeypatch, updates_env):
    monkeypatch.setattr(httpx.AsyncClient, "__init__",
                        _mock_client(_tracker_handler({}, status=404)))
    result = await updates_mod.get_updates()
    assert result.services == []
    assert result.trackerUnavailable is True


@pytest.mark.asyncio
async def test_updates_enrichment_empty_when_unavailable(monkeypatch, updates_env):
    def dead(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))
    assert await updates_mod.service_enrichment() == {}


# ── overview never 500s ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_all_dead_degrades(monkeypatch, tmp_path):
    """Every integration down → nulls, no exception."""
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "")
    monkeypatch.setenv("ZFS_STATUS_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("SERVICES_ROOT", str(UPDATES_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(UPDATES_SERVICES / "metadata.yaml"))

    def dead(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))

    ov = await overview_mod.get_overview()
    assert isinstance(ov, models.Overview)
    assert ov.host.cpuPct is None
    assert ov.monitors.up is None
    assert ov.storage.pools == [] and ov.storage.fs == []
    assert ov.updates.count == 0
    # inventory still enumerates the fixture dirs (docker socket absent → all down)
    assert ov.services.total >= 1


def test_overview_route_never_500s(real_mode, monkeypatch, tmp_path):
    """End-to-end through FastAPI: dead deps must not 500 the route."""
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "")
    monkeypatch.setenv("ZFS_STATUS_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("SERVICES_ROOT", str(UPDATES_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(UPDATES_SERVICES / "metadata.yaml"))

    def dead(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))

    client = TestClient(create_app())
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["host"]["cpuPct"] is None
    assert body["storage"]["pools"] == []


def test_updates_route_via_app_unavailable(real_mode, monkeypatch):
    monkeypatch.setenv("SERVICES_ROOT", str(UPDATES_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(UPDATES_SERVICES / "metadata.yaml"))

    def dead(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))
    client = TestClient(create_app())
    resp = client.get("/api/updates")
    assert resp.status_code == 200
    assert resp.json()["trackerUnavailable"] is True


def test_mock_mode_updates_still_works(monkeypatch):
    """MOCK_DATA=1 must keep serving fixtures unchanged (A1 behaviour)."""
    monkeypatch.setenv("MOCK_DATA", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    client = TestClient(create_app())
    ov = client.get("/api/overview")
    assert ov.status_code == 200 and ov.json()["monitors"]["up"] == 23
    up = client.get("/api/updates")
    assert up.status_code == 200 and len(up.json()["services"]) > 0
