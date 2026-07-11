"""B3 tests: stats derivation/humanization, ring buffer, poller lifecycle, and
SSE log framing — all with fakes, no docker socket or real sockets involved.

The mock-mode SSE + stats routes (A1 behaviour) are also asserted to still work
through the TestClient, since B3 must not regress them.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from alpenglow_dashboard import models, streams

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SERVICES = FIXTURES / "services"


# ── humanization ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0B"),
        (None, "0B"),
        (512, "512B"),
        (1200, "1.2kB"),
        (340_000, "340kB"),
        (1_200_000, "1.2MB"),
        (18_000_000, "18MB"),
        (2_500_000_000, "2.5GB"),
    ],
)
def test_humanize_bytes(n, expected):
    assert streams._humanize_bytes(n) == expected


def test_io_pair_format():
    assert streams._io_pair(1_200_000, 340_000) == "1.2MB / 340kB"


# ── stats derivation from a docker /stats payload ─────────────────────────────


def _stats_payload(**over):
    """A representative one-shot docker /stats sample (cgroup v2 shaped)."""
    payload = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2_000_000_000},
            "system_cpu_usage": 100_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 90_000_000_000,
        },
        "memory_stats": {
            "usage": 600 * 2**20,
            "limit": 2 * 2**30,
            "stats": {"inactive_file": 100 * 2**20},
        },
        "networks": {
            "eth0": {"rx_bytes": 1_200_000, "tx_bytes": 340_000},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 18_000_000},
                {"op": "Write", "value": 4_000},
            ]
        },
        "pids_stats": {"current": 6},
    }
    payload.update(over)
    return payload


def test_cpu_percent():
    # cpu_delta=1e9, system_delta=1e10, ncpus=4 → 0.1 * 4 * 100 = 40.0
    assert streams._cpu_percent(_stats_payload()) == 40.0


def test_cpu_percent_missing_fields_is_none():
    assert streams._cpu_percent({}) is None


def test_cpu_percent_nonpositive_system_delta_is_zero():
    p = _stats_payload()
    p["precpu_stats"]["system_cpu_usage"] = p["cpu_stats"]["system_cpu_usage"]
    assert streams._cpu_percent(p) == 0.0


def test_cpu_percent_falls_back_to_percpu_len():
    p = _stats_payload()
    del p["cpu_stats"]["online_cpus"]
    p["cpu_stats"]["cpu_usage"]["percpu_usage"] = [0, 0]  # 2 cpus
    assert streams._cpu_percent(p) == 20.0


def test_mem_used_subtracts_inactive_file():
    # 600MB usage - 100MB inactive_file = 500MB
    assert streams._mem_used(_stats_payload()) == float(500 * 2**20)


def test_mem_used_cgroup_v1_key():
    p = _stats_payload()
    p["memory_stats"]["stats"] = {"total_inactive_file": 100 * 2**20}
    assert streams._mem_used(p) == float(500 * 2**20)


def test_mem_limit():
    assert streams._mem_limit(_stats_payload()) == float(2 * 2**30)


def test_net_and_block_io():
    assert streams._net_io(_stats_payload()) == (1_200_000.0, 340_000.0)
    assert streams._block_io(_stats_payload()) == (18_000_000.0, 4_000.0)


def test_net_io_absent_is_none_pair():
    assert streams._net_io({}) == (None, None)


def test_pids():
    assert streams._pids(_stats_payload()) == 6


def test_sample_from_stats_roundtrip():
    s = streams._sample_from_stats(_stats_payload(), restarts=2)
    assert s.cpu_pct == 40.0
    assert s.mem_used == float(500 * 2**20)
    assert s.mem_limit == float(2 * 2**30)
    assert s.net_read == 1_200_000.0 and s.net_write == 340_000.0
    assert s.blk_read == 18_000_000.0 and s.blk_write == 4_000.0
    assert s.pids == 6
    assert s.restarts == 2


# ── multi-container aggregation ───────────────────────────────────────────────


def test_aggregate_sums_metrics():
    a = streams._sample_from_stats(_stats_payload(), restarts=1)
    b = streams._sample_from_stats(_stats_payload(), restarts=2)
    agg = streams._aggregate([a, b])
    assert agg.cpu_pct == 80.0
    assert agg.mem_used == float(1000 * 2**20)
    assert agg.mem_limit == float(4 * 2**30)
    assert agg.net_read == 2_400_000.0
    assert agg.pids == 12
    assert agg.restarts == 3


def test_aggregate_none_when_all_none():
    empty = streams.Sample(
        t=time.time(), cpu_pct=None, mem_used=None, mem_limit=None,
        net_read=None, net_write=None, blk_read=None, blk_write=None, pids=None,
    )
    agg = streams._aggregate([empty, empty])
    assert agg.cpu_pct is None
    assert agg.mem_used is None
    assert agg.pids is None


# ── ring buffer → Stats contract shape ────────────────────────────────────────


def test_stats_from_ring_builds_contract_shape():
    now = time.time()
    hist = [
        streams.Sample(
            t=now - (5 - i) * 15,
            cpu_pct=float(i),
            mem_used=float(i * 2**20),
            mem_limit=float(2 * 2**30),
            net_read=1_200_000.0, net_write=340_000.0,
            blk_read=18_000_000.0, blk_write=4_000.0,
            pids=6, restarts=0,
        )
        for i in range(5)
    ]
    stats = streams._stats_from_ring(hist)
    assert isinstance(stats, models.Stats)
    assert len(stats.history.cpu) == 5
    assert len(stats.history.mem) == 5
    assert stats.history.cpu[0].v == 0.0
    assert stats.current.cpuPct == 4.0
    assert stats.current.memLimit == float(2 * 2**30)
    assert stats.current.netIO == "1.2MB / 340kB"
    assert stats.current.blockIO == "18MB / 4.0kB"
    assert stats.current.pids == 6
    assert stats.current.restarts == 0


def test_stats_from_ring_empty_is_current_only():
    stats = streams._stats_from_ring([])
    assert stats.history.cpu == []
    assert stats.history.mem == []
    assert stats.current.cpuPct is None
    assert stats.current.restarts is None


def test_stats_from_ring_skips_none_metric_points():
    now = time.time()
    hist = [
        streams.Sample(t=now, cpu_pct=None, mem_used=5.0, mem_limit=None,
                       net_read=None, net_write=None, blk_read=None, blk_write=None,
                       pids=None, restarts=0),
        streams.Sample(t=now + 15, cpu_pct=3.0, mem_used=None, mem_limit=None,
                       net_read=None, net_write=None, blk_read=None, blk_write=None,
                       pids=None, restarts=0),
    ]
    stats = streams._stats_from_ring(hist)
    # only the sample with a cpu value contributes to the cpu series, etc.
    assert [p.v for p in stats.history.cpu] == [3.0]
    assert [p.v for p in stats.history.mem] == [5.0]
    # current: last sample has cpu but no mem / io → those degrade to None
    assert stats.current.cpuPct == 3.0
    assert stats.current.memUsed is None
    assert stats.current.netIO is None


def test_ring_size_cap():
    poller = streams.StatsPoller(ring_size=3)
    ring = poller._rings["svc"]
    for i in range(10):
        ring.append(streams.Sample(
            t=float(i), cpu_pct=float(i), mem_used=0.0, mem_limit=None,
            net_read=None, net_write=None, blk_read=None, blk_write=None,
            pids=None, restarts=0))
    hist = poller.history("svc")
    assert len(hist) == 3
    assert [s.cpu_pct for s in hist] == [7.0, 8.0, 9.0]


def test_history_unknown_service_empty():
    poller = streams.StatsPoller()
    assert poller.history("nope") == []


# ── poller lifecycle + churn resilience (fake docker, no socket) ──────────────


class _FakeContainer:
    def __init__(self, name, project, stats_payload, restarts=0, raise_on_stats=False):
        self.id = name
        self._name = name
        self._project = project
        self._stats = stats_payload
        self._restarts = restarts
        self._raise = raise_on_stats

    async def show(self):
        return {
            "Config": {"Labels": {"com.docker.compose.project": self._project}},
            "RestartCount": self._restarts,
        }

    async def stats(self, *, stream=False):
        assert stream is False, "poller must use one-shot stats, never streaming"
        if self._raise:
            raise RuntimeError("container vanished")
        return [self._stats]


class _FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    async def list(self, all=False):
        return list(self._containers)


class _FakeDocker:
    """Yields a caller-controlled container list on each construction, so a test
    can simulate churn by swapping the list between polls."""

    _next_batch: list = []

    def __init__(self):
        self.containers = _FakeContainers(list(type(self)._next_batch))

    async def close(self):
        return None


@pytest.fixture()
def fake_docker(monkeypatch):
    import aiodocker
    monkeypatch.setattr(aiodocker, "Docker", _FakeDocker)
    _FakeDocker._next_batch = []
    yield _FakeDocker


async def test_poll_once_populates_ring(fake_docker):
    fake_docker._next_batch = [
        _FakeContainer("multi-server", "multi", _stats_payload(), restarts=1),
        _FakeContainer("multi-worker", "multi", _stats_payload(), restarts=0),
    ]
    poller = streams.StatsPoller()
    await poller._poll_once()
    hist = poller.history("multi")
    assert len(hist) == 1
    # aggregate of the two containers
    assert hist[0].cpu_pct == 80.0
    assert hist[0].restarts == 1


async def test_poll_survives_container_churn(fake_docker):
    poller = streams.StatsPoller()
    # poll 1: two containers
    fake_docker._next_batch = [
        _FakeContainer("multi-server", "multi", _stats_payload()),
        _FakeContainer("multi-worker", "multi", _stats_payload()),
    ]
    await poller._poll_once()
    # poll 2: worker vanished + one raises on stats — must not crash
    fake_docker._next_batch = [
        _FakeContainer("multi-server", "multi", _stats_payload()),
        _FakeContainer("gone", "other", _stats_payload(), raise_on_stats=True),
    ]
    await poller._poll_once()
    assert len(poller.history("multi")) == 2
    # 'other' had its only container fail → no sample recorded
    assert poller.history("other") == []


async def test_poll_ignores_containers_without_project(fake_docker):
    fake_docker._next_batch = [_FakeContainer("loose", None, _stats_payload())]
    poller = streams.StatsPoller()
    await poller._poll_once()
    assert poller.history("loose") == []


async def test_poller_start_stop(fake_docker):
    fake_docker._next_batch = [_FakeContainer("s", "svc", _stats_payload())]
    poller = streams.StatsPoller(interval=0.01)
    await poller.start()
    # let it run a couple of cycles
    for _ in range(50):
        if poller.history("svc"):
            break
        await asyncio.sleep(0.01)
    assert poller.history("svc"), "poller should have populated the ring"
    await poller.stop()
    assert poller._task is None


async def test_poller_run_swallows_exceptions(monkeypatch):
    poller = streams.StatsPoller(interval=0.01)

    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("poll blew up")

    monkeypatch.setattr(poller, "_poll_once", boom)
    await poller.start()
    for _ in range(50):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    # task is still alive despite repeated exceptions
    assert poller._task is not None and not poller._task.done()
    assert calls["n"] >= 2
    await poller.stop()


# ── timestamp splitting for SSE framing ───────────────────────────────────────


def test_split_ts_extracts_rfc3339_prefix():
    ts, msg = streams._split_ts("2026-07-11T18:04:03.123456789Z GET /health 200")
    assert ts == "2026-07-11T18:04:03.123456789Z"
    assert msg == "GET /health 200"


def test_split_ts_no_timestamp():
    ts, msg = streams._split_ts("just a plain line")
    assert ts is None
    assert msg == "just a plain line"


def test_split_ts_empty():
    assert streams._split_ts("") == (None, "")


# ── log mux framing (fake docker container, no socket) ────────────────────────


class _FakeLogContainer:
    """Fake aiodocker container whose .log() yields caller-set chunks."""

    def __init__(self, chunks, tty=False, stop_after=True):
        self._chunks = chunks
        self._tty = tty

    async def show(self):
        return {"Config": {"Tty": self._tty}}

    def log(self, **kwargs):
        chunks = self._chunks

        async def gen():
            for c in chunks:
                yield c

        return gen()


class _FakeLogDocker:
    def __init__(self, by_name):
        self._by_name = by_name
        self.containers = self

    async def get(self, ident):
        return self._by_name[ident]

    async def close(self):
        return None


async def _drain(mux):
    out = []
    while not mux.queue.empty():
        out.append(mux.queue.get_nowait())
    return out


async def test_read_container_logs_reassembles_lines():
    fake = _FakeLogContainer(["line one\nline ", "two\npartial"])
    docker = _FakeLogDocker({"c1": fake})
    mux = streams._LogMux()
    await streams._read_container_logs(docker, "c1", "c1", True, mux)
    items = await _drain(mux)
    lines = [line for (_name, line) in items]
    assert "line one" in lines
    assert "line two" in lines
    assert "partial" in lines  # trailing buffer flushed
    assert streams._STOPPED in lines  # stream ended → stop sentinel


async def test_read_container_logs_error_sentinel():
    class _BoomContainer(_FakeLogContainer):
        def log(self, **kwargs):
            async def gen():
                raise RuntimeError("kaboom")
                yield  # pragma: no cover
            return gen()

    docker = _FakeLogDocker({"c1": _BoomContainer([])})
    mux = streams._LogMux()
    await streams._read_container_logs(docker, "c1", "c1", False, mux)
    items = await _drain(mux)
    assert any(line.startswith(streams._ERROR) for (_n, line) in items)


async def test_log_mux_backpressure_drops_oldest():
    mux = streams._LogMux()
    mux.queue = asyncio.Queue(maxsize=2)
    await mux.emit("c", "a")
    await mux.emit("c", "b")
    await mux.emit("c", "c")  # full → drops oldest ("a")
    items = await _drain(mux)
    lines = [line for (_n, line) in items]
    assert "a" not in lines
    assert "b" in lines and "c" in lines


# ── SSE event stream: framing, prefixing, heartbeats, disconnect ──────────────


class _FakeRequest:
    def __init__(self, disconnect_after=None):
        self._n = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self):
        self._n += 1
        if self._disconnect_after is not None and self._n > self._disconnect_after:
            return True
        return False


async def test_log_event_stream_multi_container_prefixes(monkeypatch):
    import aiodocker
    from alpenglow_dashboard import inventory

    # two containers in the project → prefixed lines
    async def fake_inv():
        return {
            "svc": [
                inventory.DockerContainer("svc-a", "svc", "a", "running", "img", None),
                inventory.DockerContainer("svc-b", "svc", "b", "running", "img", None),
            ]
        }

    monkeypatch.setattr(inventory, "docker_inventory", fake_inv)

    fakes = {
        "svc-a": _FakeLogContainer(["2026-07-11T00:00:01Z hello a\n"]),
        "svc-b": _FakeLogContainer(["2026-07-11T00:00:02Z hello b\n"]),
    }
    monkeypatch.setattr(aiodocker, "Docker", lambda: _FakeLogDocker(fakes))

    req = _FakeRequest(disconnect_after=25)
    events = []
    async for ev in streams._log_event_stream(req, "svc"):
        events.append(ev)
        if len([e for e in events if e.get("event") == "log"]) >= 4:
            break

    log_data = [e["data"] for e in events if e.get("event") == "log"]
    # prefixed with "<container> | <message>"
    assert any(d.startswith("svc-a | ") and "hello a" in d for d in log_data)
    assert any(d.startswith("svc-b | ") and "hello b" in d for d in log_data)
    # timestamp surfaced as the SSE id
    assert any(e.get("id") == "2026-07-11T00:00:01Z" for e in events)


async def test_log_event_stream_single_container_no_prefix(monkeypatch):
    import aiodocker
    from alpenglow_dashboard import inventory

    async def fake_inv():
        return {"svc": [inventory.DockerContainer("solo", "svc", "solo", "running", "img", None)]}

    monkeypatch.setattr(inventory, "docker_inventory", fake_inv)
    fakes = {"solo": _FakeLogContainer(["2026-07-11T00:00:01Z only line\n"])}
    monkeypatch.setattr(aiodocker, "Docker", lambda: _FakeLogDocker(fakes))

    req = _FakeRequest(disconnect_after=25)
    log_data = []
    async for ev in streams._log_event_stream(req, "svc"):
        if ev.get("event") == "log":
            log_data.append(ev["data"])
        if log_data:
            break
    assert log_data == ["only line"]  # no "container | " prefix for single-container


async def test_log_event_stream_stop_note_multi(monkeypatch):
    import aiodocker
    from alpenglow_dashboard import inventory

    async def fake_inv():
        return {
            "svc": [
                inventory.DockerContainer("a", "svc", "a", "running", "img", None),
                inventory.DockerContainer("b", "svc", "b", "running", "img", None),
            ]
        }

    monkeypatch.setattr(inventory, "docker_inventory", fake_inv)
    # empty logs → each reader emits the __stopped__ sentinel immediately
    fakes = {"a": _FakeLogContainer([]), "b": _FakeLogContainer([])}
    monkeypatch.setattr(aiodocker, "Docker", lambda: _FakeLogDocker(fakes))

    req = _FakeRequest(disconnect_after=40)
    notes = []
    async for ev in streams._log_event_stream(req, "svc"):
        if ev.get("event") == "log":
            notes.append(ev["data"])
        if len(notes) >= 2:
            break
    assert all("container stopped" in n for n in notes)
    assert all(n.startswith("[") for n in notes)  # bracketed container tag in multi mode


async def test_log_event_stream_no_containers_note(monkeypatch):
    from alpenglow_dashboard import inventory
    import aiodocker

    async def fake_inv():
        return {}

    monkeypatch.setattr(inventory, "docker_inventory", fake_inv)
    monkeypatch.setattr(aiodocker, "Docker", lambda: _FakeLogDocker({}))

    req = _FakeRequest(disconnect_after=3)
    first = None
    async for ev in streams._log_event_stream(req, "svc"):
        first = ev
        break
    assert first["event"] == "log"
    assert "no running containers" in first["data"]


async def test_log_event_stream_heartbeat_when_idle(monkeypatch):
    from alpenglow_dashboard import inventory
    import aiodocker

    async def fake_inv():
        return {"svc": [inventory.DockerContainer("q", "svc", "q", "running", "img", None)]}

    # a container that never yields any log line (idle stream)
    class _Idle(_FakeLogContainer):
        def log(self, **kwargs):
            async def gen():
                await asyncio.sleep(3600)
                yield  # pragma: no cover
            return gen()

    monkeypatch.setattr(inventory, "docker_inventory", fake_inv)
    monkeypatch.setattr(aiodocker, "Docker", lambda: _FakeLogDocker({"q": _Idle([])}))
    monkeypatch.setattr(streams, "HEARTBEAT_INTERVAL", 0.0)  # force a beat every idle tick

    req = _FakeRequest(disconnect_after=100)
    saw_heartbeat = False
    async for ev in streams._log_event_stream(req, "svc"):
        if "comment" in ev:
            saw_heartbeat = True
            break
    assert saw_heartbeat


# ── route-level behaviour in MOCK mode (A1 regression guard) ──────────────────


def test_mock_stats_route_still_works(client):
    resp = client.get("/api/services/glances/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "history" in body and "current" in body
    assert len(body["history"]["cpu"]) == 60


def test_mock_stats_route_unknown_404(client):
    assert client.get("/api/services/nope/stats").status_code == 404


def test_mock_logs_route_streams(client):
    with client.stream("GET", "/api/services/glances/logs") as resp:
        assert resp.status_code == 200
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"data:" in body:
                break
    assert b"data:" in body


def test_mock_logs_route_unknown_404(client):
    assert client.get("/api/services/nope/logs").status_code == 404


# ── non-mock route 404 for unknown ids (docker-free) ──────────────────────────


def test_stats_route_unknown_id_404_nonmock(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from alpenglow_dashboard.main import create_app

    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    client = TestClient(create_app())
    assert client.get("/api/services/does_not_exist/stats").status_code == 404


def test_logs_route_unknown_id_404_nonmock(monkeypatch):
    from fastapi.testclient import TestClient
    from alpenglow_dashboard.main import create_app

    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    client = TestClient(create_app())
    assert client.get("/api/services/does_not_exist/logs").status_code == 404


def test_service_exists_helper(monkeypatch):
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    assert streams._service_exists("multi") is True
    assert streams._service_exists("does_not_exist") is False
