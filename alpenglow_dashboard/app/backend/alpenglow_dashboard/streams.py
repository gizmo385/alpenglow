"""SSE log streaming + stats ring buffers (work package B3).

Two responsibilities live here:

- **Log streaming** — ``GET /api/services/{id}/logs`` is a Server-Sent-Events
  stream that multiplexes ``docker logs --tail 100 --follow`` for *every*
  container in the service's compose project. Each line carries the container's
  own timestamp (docker ``timestamps=1``) so the frontend can render its muted
  timestamp column, and is prefixed with the container name when the project has
  more than one container. Periodic SSE comment heartbeats keep idle streams
  alive through proxies; the per-container reader tasks are cancelled cleanly
  when the client disconnects, and a container stopping mid-stream is noted
  in-stream without tearing down the others.

- **Stats history** — a background poller samples ``docker stats`` (one-shot,
  never the streaming variant, to avoid leaking the persistent connection/fd it
  opens) every :data:`POLL_INTERVAL` seconds for RUNNING containers only, and
  keeps a per-service ring buffer of the last :data:`RING_SIZE` samples. It is
  started/stopped with the app lifespan (see :func:`lifespan`) and, as a
  fallback when that wiring is absent, lazily on the first ``/stats`` request.
  The poller tolerates container churn and never propagates an exception.

A1's mock behaviour (``MOCK_DATA=1``) is preserved verbatim: the mock log tail
and synthetic stats history are still served, and neither the poller nor
aiodocker is touched in that mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from . import inventory, mock, models

router = APIRouter(prefix="/api", tags=["streams"])


# ── tunables ──────────────────────────────────────────────────────────────────

# How many historical samples to retain per service (~15 min at 15s cadence).
RING_SIZE = 60
# Seconds between stats polls (matches the frontend's stats-tab poll cadence).
POLL_INTERVAL = 15.0
# Seconds between SSE heartbeat comments on an idle log stream. Kept well under
# the typical 60s proxy idle timeout so caddy/oauth2-proxy never kill the stream.
HEARTBEAT_INTERVAL = 15.0
# Lines of scrollback to prime a new log stream with (docker logs --tail).
LOG_TAIL = 100

# In-band control markers a reader task pushes onto the mux queue to signal a
# container stopping / erroring. The \x00 prefix cannot appear in a docker log
# line (docker strips NULs), so these never collide with real output.
_STOPPED = "\x00__stopped__"
_ERROR = "\x00__error__"


# ── humanization (mirrors mock.py / models.py string conventions) ─────────────


def _humanize_bytes(n: Optional[float]) -> str:
    """Format a byte count like docker stats: '1.2MB', '340kB', '18MB'.

    Uses decimal (1000) units to match ``docker stats`` output, which is what
    the mock fixtures imitate ("1.2 MB / 340 kB").
    """
    if n is None:
        return "0B"
    n = float(n)
    if n < 0:
        n = 0.0
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(n)}{unit}"
            # one decimal place under 10, none above, matching docker's style
            return (f"{n:.1f}{unit}" if n < 10 else f"{round(n)}{unit}")
        n /= 1000.0
    return f"{n:.1f}TB"


def _io_pair(read: Optional[float], write: Optional[float]) -> str:
    """'<in> / <out>' humanized pair, as used for netIO / blockIO."""
    return f"{_humanize_bytes(read)} / {_humanize_bytes(write)}"


# ── stats sample derivation from a docker /stats payload ──────────────────────


def _cpu_percent(stats: dict) -> Optional[float]:
    """Replicate ``docker stats`` CPU% from a one-shot /stats sample.

    A non-streaming sample carries both ``cpu_stats`` and ``precpu_stats`` (the
    prior read), so the delta can be computed from a single response.
    """
    try:
        cpu = stats.get("cpu_stats") or {}
        pre = stats.get("precpu_stats") or {}
        cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage")
        pre_usage = (pre.get("cpu_usage") or {}).get("total_usage")
        system = cpu.get("system_cpu_usage")
        pre_system = pre.get("system_cpu_usage")
        if cpu_usage is None or pre_usage is None or system is None or pre_system is None:
            return None
        cpu_delta = cpu_usage - pre_usage
        system_delta = system - pre_system
        if system_delta <= 0 or cpu_delta < 0:
            return 0.0
        # online_cpus, else fall back to the per-cpu usage array length
        ncpus = cpu.get("online_cpus")
        if not ncpus:
            percpu = (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
            ncpus = len(percpu) or 1
        return round((cpu_delta / system_delta) * ncpus * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _mem_used(stats: dict) -> Optional[float]:
    """Memory in use, docker-style (usage minus the reclaimable page cache)."""
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is None:
        return None
    detail = mem.get("stats") or {}
    # cgroup v1 exposes total_inactive_file; v2 exposes inactive_file.
    cache = detail.get("total_inactive_file")
    if cache is None:
        cache = detail.get("inactive_file", 0)
    used = usage - (cache or 0)
    return float(max(0, used))


def _mem_limit(stats: dict) -> Optional[float]:
    mem = stats.get("memory_stats") or {}
    limit = mem.get("limit")
    return float(limit) if limit else None


def _net_io(stats: dict) -> tuple[Optional[float], Optional[float]]:
    networks = stats.get("networks")
    if not isinstance(networks, dict) or not networks:
        return None, None
    rx = sum((n.get("rx_bytes") or 0) for n in networks.values())
    tx = sum((n.get("tx_bytes") or 0) for n in networks.values())
    return float(rx), float(tx)


def _block_io(stats: dict) -> tuple[Optional[float], Optional[float]]:
    blkio = stats.get("blkio_stats") or {}
    entries = blkio.get("io_service_bytes_recursive")
    if not entries:
        return None, None
    read = write = 0
    for e in entries:
        op = str(e.get("op", "")).lower()
        val = e.get("value") or 0
        if op == "read":
            read += val
        elif op == "write":
            write += val
    return float(read), float(write)


def _pids(stats: dict) -> Optional[int]:
    pids = stats.get("pids_stats") or {}
    current = pids.get("current")
    return int(current) if current is not None else None


@dataclass
class Sample:
    """One poll's worth of derived per-container metrics."""

    t: float
    cpu_pct: Optional[float]
    mem_used: Optional[float]
    mem_limit: Optional[float]
    net_read: Optional[float]
    net_write: Optional[float]
    blk_read: Optional[float]
    blk_write: Optional[float]
    pids: Optional[int]
    restarts: int = 0


def _sample_from_stats(raw: dict, restarts: int) -> Sample:
    net_r, net_w = _net_io(raw)
    blk_r, blk_w = _block_io(raw)
    return Sample(
        t=time.time(),
        cpu_pct=_cpu_percent(raw),
        mem_used=_mem_used(raw),
        mem_limit=_mem_limit(raw),
        net_read=net_r,
        net_write=net_w,
        blk_read=blk_r,
        blk_write=blk_w,
        pids=_pids(raw),
        restarts=restarts,
    )


def _aggregate(samples: list[Sample]) -> Sample:
    """Sum the per-container samples of a project into one service sample.

    CPU% and memory add; the mem limit is summed across containers; net/block
    IO totals add; pids add; restarts add. ``None`` metrics are treated as
    absent — the aggregate is ``None`` only when *every* container was ``None``.
    """
    def _sum(getter, is_int=False):
        vals = [getter(s) for s in samples if getter(s) is not None]
        if not vals:
            return None
        total = sum(vals)
        return int(total) if is_int else round(float(total), 2)

    return Sample(
        t=samples[0].t if samples else time.time(),
        cpu_pct=_sum(lambda s: s.cpu_pct),
        mem_used=_sum(lambda s: s.mem_used),
        mem_limit=_sum(lambda s: s.mem_limit),
        net_read=_sum(lambda s: s.net_read),
        net_write=_sum(lambda s: s.net_write),
        blk_read=_sum(lambda s: s.blk_read),
        blk_write=_sum(lambda s: s.blk_write),
        pids=_sum(lambda s: s.pids, is_int=True),
        restarts=sum(s.restarts for s in samples),
    )


def _stats_from_ring(history: list[Sample]) -> models.Stats:
    """Build the contract Stats shape from a service's ring-buffer history."""
    cpu = [
        models.StatPoint(t=s.t, v=s.cpu_pct)
        for s in history
        if s.cpu_pct is not None
    ]
    mem = [
        models.StatPoint(t=s.t, v=s.mem_used)
        for s in history
        if s.mem_used is not None
    ]
    if history:
        last = history[-1]
        current = models.StatsCurrent(
            cpuPct=last.cpu_pct,
            memUsed=last.mem_used,
            memLimit=last.mem_limit,
            netIO=(_io_pair(last.net_read, last.net_write)
                   if last.net_read is not None or last.net_write is not None else None),
            blockIO=(_io_pair(last.blk_read, last.blk_write)
                     if last.blk_read is not None or last.blk_write is not None else None),
            pids=last.pids,
            restarts=last.restarts,
        )
    else:
        current = models.StatsCurrent(
            cpuPct=None, memUsed=None, memLimit=None,
            netIO=None, blockIO=None, pids=None, restarts=None,
        )
    return models.Stats(history=models.StatsHistory(cpu=cpu, mem=mem), current=current)


# ── background stats poller ───────────────────────────────────────────────────


class StatsPoller:
    """Polls docker stats every :data:`POLL_INTERVAL`s into per-service rings.

    One instance is created per app (via lifespan / lazy start). It owns a
    single background task and per-service ``deque`` ring buffers. All docker
    access is one-shot (``stats(stream=False)``) so no long-lived streaming
    connection/fd is held between polls.
    """

    def __init__(self, interval: float = POLL_INTERVAL, ring_size: int = RING_SIZE) -> None:
        self.interval = interval
        self.ring_size = ring_size
        self._rings: dict[str, deque[Sample]] = defaultdict(lambda: deque(maxlen=ring_size))
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # -- lifecycle --

    async def start(self) -> None:
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(), name="alpenglow-stats-poller")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def ensure_started(self) -> None:
        """Lazy-start fallback used by the /stats route when no lifespan ran."""
        if self._task is None or self._task.done():
            await self.start()

    # -- data access --

    def history(self, service_id: str) -> list[Sample]:
        ring = self._rings.get(service_id)
        return list(ring) if ring else []

    # -- polling --

    async def _run(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A poll failure (socket blip, container vanished mid-inspect)
                # must never kill the poller or the app; drop it and retry.
                pass
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        """One polling cycle: for every running container of every service,
        collect a one-shot stats sample and append the per-service aggregate."""
        import aiodocker

        docker = aiodocker.Docker()
        try:
            try:
                containers = await docker.containers.list(all=False)  # running only
            except Exception:
                return
            # group running containers by compose project
            by_project: dict[str, list] = defaultdict(list)
            for c in containers:
                try:
                    info = await c.show()
                except Exception:
                    continue
                labels = (info.get("Config", {}) or {}).get("Labels") or {}
                project = labels.get("com.docker.compose.project")
                if not project:
                    continue
                by_project[project].append((c, info))

            for project, members in by_project.items():
                samples: list[Sample] = []
                for c, info in members:
                    sample = await self._sample_container(c, info)
                    if sample is not None:
                        samples.append(sample)
                if samples:
                    self._rings[project].append(_aggregate(samples))
        finally:
            with contextlib.suppress(Exception):
                await docker.close()

    async def _sample_container(self, container, info: dict) -> Optional[Sample]:
        try:
            restarts = int((info.get("RestartCount") or 0))
            # one-shot stats: returns a list of sample dicts (usually one)
            raw_list = await container.stats(stream=False)
            if not raw_list:
                return None
            raw = raw_list[-1] if isinstance(raw_list, list) else raw_list
            if not isinstance(raw, dict):
                return None
            return _sample_from_stats(raw, restarts)
        except Exception:
            # container vanished between list and stats, or a malformed payload
            return None


# ── poller singleton, wired via lifespan (with lazy fallback) ─────────────────

_poller: Optional[StatsPoller] = None


def get_poller() -> StatsPoller:
    global _poller
    if _poller is None:
        _poller = StatsPoller()
    return _poller


@contextlib.asynccontextmanager
async def lifespan(app):
    """App lifespan hook: run the stats poller for the app's lifetime.

    main.py registers this as an additive ``router.lifespan`` so startup/
    shutdown are owned here. In MOCK mode the poller is not started (mock stats
    are synthetic). Safe to nest with any other lifespan main.py may add.
    """
    poller = get_poller()
    if not mock.mock_enabled():
        # Prune settings-store entries for service ids that no longer exist as
        # repo dirs (G1): stale entries must never contribute phantom
        # tags/categories to the inventory or the management surfaces.
        try:
            from . import tags as settings_store

            known = set(inventory.scan_repo().keys())
            await settings_store.prune_unknown(known)
        except Exception:
            pass  # pruning is best-effort; never block startup
        await poller.start()
    try:
        yield
    finally:
        await poller.stop()


# ── stats route ───────────────────────────────────────────────────────────────


@router.get("/services/{service_id}/stats")
async def get_stats(service_id: str) -> models.Stats:
    if mock.mock_enabled():
        stats = mock.mock_stats(service_id)
        if stats is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return stats

    if not _service_exists(service_id):
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")

    poller = get_poller()
    # Fallback: if the lifespan hook never ran (e.g. an embedding that bypasses
    # it), start the poller lazily on first request so history begins filling.
    await poller.ensure_started()
    return _stats_from_ring(poller.history(service_id))


# ── log streaming ─────────────────────────────────────────────────────────────


def _service_exists(service_id: str) -> bool:
    """A service exists iff its compose project directory exists in the repo."""
    return (inventory.services_root() / service_id / "compose.yaml").is_file()


@dataclass
class _LogMux:
    """Fan-in for multiple per-container docker-log reader tasks.

    Each reader pushes ``(container_name, line)`` tuples onto a shared queue;
    the SSE generator drains the queue and formats events. A sentinel-free
    design: readers just stop; the generator relies on heartbeats + client
    disconnect for liveness.
    """

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2000))
    tasks: list[asyncio.Task] = field(default_factory=list)

    async def emit(self, container: str, line: str) -> None:
        try:
            self.queue.put_nowait((container, line))
        except asyncio.QueueFull:
            # Backpressure: drop the oldest to make room, keeping the stream live
            # rather than blocking the reader (a slow client shouldn't wedge us).
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait((container, line))

    async def close(self) -> None:
        for t in self.tasks:
            if not t.done():
                t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


def _split_ts(line: str) -> tuple[Optional[str], str]:
    """Split docker's ``timestamps=1`` RFC3339 prefix off a log line.

    Docker prefixes each line with e.g. ``2026-07-11T18:04:03.123456789Z ``.
    Returns ``(timestamp, message)``; if no timestamp is present the whole line
    is the message. The frontend renders the timestamp in its muted column.
    """
    if not line:
        return None, line
    head, sep, rest = line.partition(" ")
    if sep and len(head) >= 20 and head[4] == "-" and head[7] == "-" and ("T" in head):
        return head, rest
    return None, line


async def _read_container_logs(
    docker, container_id: str, container_name: str, prefix: bool, mux: _LogMux
) -> None:
    """Follow one container's logs (tail + follow) into the mux.

    Notes a mid-stream stop in-band and returns; the mux keeps serving the
    other containers. Never raises out of the task.
    """
    try:
        container = await docker.containers.get(container_id)
        stream = container.log(
            stdout=True,
            stderr=True,
            follow=True,
            tail=LOG_TAIL,
            timestamps=True,
        )
        buffer = ""
        async for chunk in stream:
            # aiodocker yields demultiplexed text chunks that may contain zero,
            # one, or several newlines; reassemble into whole lines.
            buffer += chunk
            while "\n" in buffer:
                raw, buffer = buffer.split("\n", 1)
                if raw:
                    await mux.emit(container_name, raw)
        if buffer.strip():
            await mux.emit(container_name, buffer)
        # Stream ended without cancellation → the container stopped/log closed.
        await mux.emit(container_name, _STOPPED)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await mux.emit(container_name, _ERROR + str(exc))


async def _log_event_stream(
    request: Request, service_id: str
) -> AsyncIterator[dict]:
    """SSE generator: spin up a reader per project container, fan lines out as
    ``log`` events (timestamp + optional container prefix), interleave heartbeat
    comments, and tear everything down on client disconnect."""
    import aiodocker

    docker = aiodocker.Docker()
    mux = _LogMux()
    try:
        grouped = await inventory.docker_inventory()
        containers = grouped.get(service_id, [])
        multi = len(containers) > 1

        if not containers:
            yield {"event": "log", "data": "(no running containers for this service)"}

        for dc in containers:
            # dc.name is the container name; aiodocker.get accepts name or id.
            task = asyncio.create_task(
                _read_container_logs(docker, dc.name, dc.name, multi, mux),
                name=f"logs-{service_id}-{dc.name}",
            )
            mux.tasks.append(task)

        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                container_name, raw = await asyncio.wait_for(
                    mux.queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                # idle tick: heartbeat if due, then loop to re-check disconnect
                now = time.monotonic()
                if now - last_beat >= HEARTBEAT_INTERVAL:
                    last_beat = now
                    yield {"comment": "heartbeat"}
                continue

            # sentinel control lines from the readers
            if raw == _STOPPED:
                note = f"[{container_name}] container stopped" if multi else "container stopped"
                yield {"event": "log", "data": note}
                continue
            if raw.startswith(_ERROR):
                detail = raw[len(_ERROR):]
                note = f"[{container_name}] log stream error: {detail}"
                yield {"event": "log", "data": note}
                continue

            ts, message = _split_ts(raw)
            data = f"{container_name} | {message}" if multi else message
            event = {"event": "log", "data": data}
            if ts:
                event["id"] = ts  # frontend reads the SSE id as the line timestamp
            yield event

            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_INTERVAL:
                last_beat = now
                yield {"comment": "heartbeat"}
    finally:
        await mux.close()
        with contextlib.suppress(Exception):
            await docker.close()


async def _mock_log_events(service_id: str) -> AsyncIterator[dict]:
    for line in mock.mock_log_lines(service_id):
        yield {"event": "log", "data": line}
        await asyncio.sleep(0.05)
    # keep the connection open with heartbeats, like the real stream will
    for _ in range(3):
        await asyncio.sleep(5)
        yield {"comment": "heartbeat"}


@router.get("/services/{service_id}/logs")
async def stream_logs(service_id: str, request: Request) -> EventSourceResponse:
    if mock.mock_enabled():
        if service_id not in mock.MOCK_SERVICES:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return EventSourceResponse(_mock_log_events(service_id))

    if not _service_exists(service_id):
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    return EventSourceResponse(_log_event_stream(request, service_id))
