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
    beszel,
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
    for cache in (glances._CACHE, kuma._CACHE, updates_mod._CACHE,
                  beszel._INFO_CACHE, beszel._CHARTS_CACHE, beszel._CONTAINERS_CACHE,
                  beszel._CONTAINER_HISTORY_CACHE):
        cache.invalidate()
    beszel._TOKENS.clear()
    yield
    for cache in (glances._CACHE, kuma._CACHE, updates_mod._CACHE,
                  beszel._INFO_CACHE, beszel._CHARTS_CACHE, beszel._CONTAINERS_CACHE,
                  beszel._CONTAINER_HISTORY_CACHE):
        cache.invalidate()
    beszel._TOKENS.clear()


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
async def test_kuma_monitors_and_backups(monkeypatch, tmp_path):
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "test-key")
    monkeypatch.setenv("KUMA_PUBLIC_URL", "https://uptime.example")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_kuma_handler_ok))
    mon = await kuma.monitors()
    # 6 real monitors: RSS, Recipes, Nextcloud(down), Root Pool(down), DB Backup, Verify
    assert mon.total == 6
    assert mon.up == 4  # RSS, Recipes, DB Backup, Backup Verification
    # per-monitor drill-down present, down monitors sorted first, url exposed
    assert mon.url == "https://uptime.example"
    assert len(mon.monitors) == 6
    down = [m.name for m in mon.monitors if m.status == "down"]
    assert set(down) == {"Nextcloud", "Root Pool Health"}
    assert mon.monitors[0].status == "down"  # down sorted first

    # Backups: `ok` from Kuma push monitors, freshness from the filesystem
    pg_dir = tmp_path / "pg"
    pg_dir.mkdir()
    (pg_dir / "2026-07-11-daily.sql").write_text("dump")
    kopia_log = tmp_path / "backup_run.log"
    kopia_log.write_text("log")
    monkeypatch.setenv("PG_BACKUP_DIR", str(pg_dir))
    monkeypatch.setenv("KOPIA_LOG_PATH", str(kopia_log))
    kuma._CACHE.invalidate()
    bk = await kuma.backups()
    # Database Backup + Backup Verification both up → ok
    assert bk.ok is True
    # real freshness strings + absolute tooltips from file mtimes
    assert bk.pgAgo and bk.pgAgo.endswith("ago")
    assert bk.kopiaAgo and bk.kopiaAgo.endswith("ago")
    assert bk.pgAt and bk.kopiaAt


@pytest.mark.asyncio
async def test_kuma_backups_missing_sources_are_truthful(monkeypatch, tmp_path):
    """No key → ok None; missing backup files → ages None (renders '—')."""
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "")
    monkeypatch.setenv("PG_BACKUP_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("KOPIA_LOG_PATH", str(tmp_path / "absent.log"))
    bk = await kuma.backups()
    assert bk.ok is None
    assert bk.pgAgo is None and bk.kopiaAgo is None
    assert bk.pgAt is None and bk.kopiaAt is None


@pytest.mark.asyncio
async def test_kuma_no_key_degrades(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_API_KEY", "")
    monkeypatch.setenv("KUMA_PUBLIC_URL", "https://uptime.example")
    mon = await kuma.monitors()
    assert mon.up is None and mon.total is None
    assert mon.note and "API_KEY" in mon.note.upper() or "key" in (mon.note or "")
    # url still surfaced even when Kuma can't be read
    assert mon.url == "https://uptime.example"
    assert mon.monitors == []


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
                      "used": 756497440768,
                      "usable": {"used": 758852247552, "available": 206710087680,
                                 "total": 965562335232},
                      "last_scrub": time.time() - 4 * 86400,
                      "scrub_errors": 0, "read_errors": 0, "write_errors": 0,
                      "checksum_errors": 0},
            "dpool": {"name": "dpool", "state": "ONLINE", "size": 11991548690432,
                      "used": 2860445474816,
                      "usable": {"used": 1904991775104, "available": 5944128067200,
                                 "total": 7849119842304},
                      "last_scrub": time.time() - 4 * 86400,
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
    # `used`/`size` carry the USABLE figures (parity excluded); raw kept alongside
    assert dpool.used == 1904991775104
    assert dpool.size == 7849119842304
    assert dpool.rawUsed == 2860445474816
    assert dpool.rawSize == 11991548690432
    assert dpool.scrubAgo and dpool.scrubAgo.endswith("ago")


@pytest.mark.asyncio
async def test_zfs_falls_back_when_usable_absent(monkeypatch, tmp_path):
    """Older JSON without a `usable` block degrades to raw figures."""
    doc = {
        "generated_at": time.time(),
        "pools": {
            "dpool": {"name": "dpool", "state": "ONLINE", "size": 11991548690432,
                      "used": 2860445474816, "last_scrub": 0},
        },
    }
    p = tmp_path / "zfs_status.json"
    p.write_text(json.dumps(doc))
    monkeypatch.setenv("ZFS_STATUS_PATH", str(p))
    (dpool,) = await zfs.pools()
    assert dpool.used == 2860445474816 and dpool.size == 11991548690432
    assert dpool.rawUsed == 2860445474816 and dpool.rawSize == 11991548690432


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


# ── beszel ────────────────────────────────────────────────────────────────────

# `info` and `stats` arrive as JSON *strings* inside PocketBase records, exactly
# as the live hub returns them.
BESZEL_SYSTEM = {
    "id": "sys1",
    "name": "Alpenglow",
    "status": "up",
    "info": json.dumps({"dt": 47.85, "u": 737378, "cpu": 10.12, "mp": 59.51, "t": 8}),
}

BESZEL_STATS = [
    {"created": "2026-07-14 03:49:45.897Z",
     "stats": json.dumps({"cpu": 8.5, "mp": 52.85, "t": {"a": 27.8, "b": 46.85}, "b": [34705, 31399]})},
    {"created": "2026-07-14 03:50:45.897Z",
     "stats": json.dumps({"cpu": 9.1, "mp": 53.1, "t": {"a": 28.0, "b": 47.9}, "b": [10000, 20000]})},
]

BESZEL_CONTAINERS = [
    {"name": "immich_server", "cpu": 0.06, "memory": 933.23, "net": 5, "status": "Up 8 days", "image": "img"},
    {"name": "ollama", "cpu": 0.0, "memory": 68.99, "net": 0, "status": "Up 2 days", "image": "ollama"},
]

# container_stats: each sample's `stats` is a JSON *string* array of per-container
# {n(ame), c(pu %), m(emory MiB), b(andwidth)}.
BESZEL_CONTAINER_STATS = [
    {"created": "2026-07-14 03:49:45.897Z",
     "stats": json.dumps([{"n": "immich_server", "c": 0.05, "m": 900.0, "b": [1, 2]},
                          {"n": "ollama", "c": 0.0, "m": 68.0}])},
    {"created": "2026-07-14 03:50:45.897Z",
     "stats": json.dumps([{"n": "immich_server", "c": 0.07, "m": 933.2},
                          {"n": "ollama", "c": 0.1, "m": 69.0}])},
]


def _beszel_handler(*, auth_status=200, fail_first_get=False):
    """PocketBase-shaped handler for the Beszel hub.

    Records the Authorization header seen on each GET; can force an auth failure
    or a one-off 401 on the first GET (to exercise the re-auth path).
    """
    state = {"gets": 0, "auth_headers": []}

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/auth-with-password"):
            if auth_status != 200:
                return httpx.Response(auth_status, json={"message": "Failed to authenticate."})
            return httpx.Response(200, json={"token": "tok-123", "record": {"id": "u1"}})
        # Everything else is an authenticated GET.
        state["auth_headers"].append(request.headers.get("authorization"))
        state["gets"] += 1
        if fail_first_get and state["gets"] == 1:
            return httpx.Response(401)
        if path.endswith("/systems/records"):
            return httpx.Response(200, json={"items": [BESZEL_SYSTEM]})
        if path.endswith("/system_stats/records"):
            return httpx.Response(200, json={"items": BESZEL_STATS})
        if path.endswith("/containers/records"):
            return httpx.Response(200, json={"items": BESZEL_CONTAINERS})
        if path.endswith("/container_stats/records"):
            return httpx.Response(200, json={"items": BESZEL_CONTAINER_STATS})
        return httpx.Response(404)

    handler.state = state
    return handler


@pytest.fixture()
def beszel_creds(monkeypatch):
    monkeypatch.setenv("BESZEL_USER", "svc@example")
    monkeypatch.setenv("BESZEL_PASSWORD", "secret")
    monkeypatch.setenv("BESZEL_SYSTEM", "Alpenglow")
    yield


@pytest.mark.asyncio
async def test_beszel_host_info(monkeypatch, beszel_creds):
    h = _beszel_handler()
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(h))
    info = await beszel.host_info()
    assert info is not None
    assert info.cpu_temp == 47.85  # dt, °C
    assert info.uptime == 737378  # u, seconds
    assert info.status == "up"
    # The GET carried the raw token (no "Bearer " prefix — PocketBase convention).
    assert h.state["auth_headers"] and h.state["auth_headers"][0] == "tok-123"


@pytest.mark.asyncio
async def test_beszel_host_charts(monkeypatch, beszel_creds):
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_beszel_handler()))
    charts = await beszel.host_charts()
    assert [p.v for p in charts.cpu] == [8.5, 9.1]
    assert [p.v for p in charts.mem] == [52.85, 53.1]
    # temp = hottest sensor in each sample's `t` map
    assert [p.v for p in charts.temp] == [46.85, 47.9]
    # bandwidth = sent + received
    assert [p.v for p in charts.bandwidth] == [66104.0, 30000.0]
    # created strings parsed to ascending epoch seconds
    assert charts.cpu[0].t < charts.cpu[1].t


@pytest.mark.asyncio
async def test_beszel_containers_for(monkeypatch, beszel_creds):
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_beszel_handler()))
    rows = await beszel.containers_for(["immich_server", "not-tracked", "ollama"])
    # untracked name skipped, requested order preserved
    assert [r.name for r in rows] == ["immich_server", "ollama"]
    assert rows[0].cpu == 0.06 and rows[0].memory == 933.23
    assert rows[0].status == "Up 8 days"
    # per-container history stitched from container_stats, in time order
    assert [p.v for p in rows[0].cpuHistory] == [0.05, 0.07]
    assert [p.v for p in rows[0].memHistory] == [900.0, 933.2]
    assert [p.v for p in rows[1].cpuHistory] == [0.0, 0.1]


@pytest.mark.asyncio
async def test_beszel_no_credentials_never_calls_hub(monkeypatch):
    monkeypatch.setenv("BESZEL_USER", "")
    monkeypatch.setenv("BESZEL_PASSWORD", "")

    def explode(request):
        raise AssertionError("beszel must not call the hub without credentials")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(explode))
    assert await beszel.host_info() is None
    beszel._CHARTS_CACHE.invalidate()
    assert await beszel.host_charts() == models.HostCharts(cpu=[], mem=[], temp=[], bandwidth=[])
    beszel._CONTAINERS_CACHE.invalidate()
    assert await beszel.containers_for(["immich_server"]) == []


@pytest.mark.asyncio
async def test_beszel_auth_failure_degrades(monkeypatch, beszel_creds):
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_beszel_handler(auth_status=400)))
    assert await beszel.host_info() is None
    beszel._CHARTS_CACHE.invalidate()
    assert (await beszel.host_charts()).cpu == []


@pytest.mark.asyncio
async def test_beszel_reauths_on_401(monkeypatch, beszel_creds):
    h = _beszel_handler(fail_first_get=True)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(h))
    info = await beszel.host_info()
    # first GET 401 → token cleared, re-auth, retry succeeds
    assert info is not None and info.cpu_temp == 47.85
    assert h.state["gets"] == 2  # the 401'd GET + the successful retry


@pytest.mark.asyncio
async def test_beszel_unreachable_degrades(monkeypatch, beszel_creds):
    def dead(request):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(dead))
    assert await beszel.host_info() is None
    beszel._CHARTS_CACHE.invalidate()
    assert (await beszel.host_charts()).temp == []


@pytest.mark.asyncio
async def test_beszel_wrong_system_name_yields_nothing(monkeypatch, beszel_creds):
    # Hub only knows "Alpenglow"; asking for another name returns no record.
    monkeypatch.setenv("BESZEL_SYSTEM", "Nonexistent")

    def handler(request):
        path = request.url.path
        if path.endswith("/auth-with-password"):
            return httpx.Response(200, json={"token": "tok-123"})
        return httpx.Response(200, json={"items": []})  # filter matches nothing

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(handler))
    assert await beszel.host_info() is None


def test_host_charts_route_real(monkeypatch, real_mode, beszel_creds):
    """The /api/host/charts route surfaces Beszel series and never 500s."""
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_client(_beszel_handler()))
    client = TestClient(create_app())
    resp = client.get("/api/host/charts")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["v"] for p in body["temp"]] == [46.85, 47.9]


def test_host_charts_route_degrades_without_beszel(monkeypatch, real_mode):
    """No creds → empty series, still 200."""
    monkeypatch.setenv("BESZEL_USER", "")
    monkeypatch.setenv("BESZEL_PASSWORD", "")
    client = TestClient(create_app())
    resp = client.get("/api/host/charts")
    assert resp.status_code == 200
    assert resp.json() == {"cpu": [], "mem": [], "temp": [], "bandwidth": []}


def test_beszel_routes_and_overview_temp_mock(monkeypatch):
    """MOCK_DATA=1 serves synthetic Beszel data across the new surfaces."""
    monkeypatch.setenv("MOCK_DATA", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    client = TestClient(create_app())

    charts = client.get("/api/host/charts")
    assert charts.status_code == 200
    assert len(charts.json()["cpu"]) == 120

    host = client.get("/api/overview").json()["host"]
    assert host["cpuTemp"] == 47.5 and host["uptime"] == 737378

    # per-container stats for a known mock service; unknown id → 404
    beszel_rows = client.get("/api/services/immich/beszel")
    assert beszel_rows.status_code == 200 and isinstance(beszel_rows.json(), list)
    assert client.get("/api/services/does-not-exist/beszel").status_code == 404
