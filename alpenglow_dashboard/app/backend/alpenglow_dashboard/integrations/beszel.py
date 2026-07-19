"""Beszel integration (work package B4).

Data source: the Beszel hub's PocketBase REST API at ``BESZEL_URL``
(``beszel:8090`` on the caddy network). Beszel 0.18.x is built on PocketBase
v0.23+, so the surfaces we use are:

- ``POST /api/collections/users/auth-with-password`` → ``{token, record}``.
  Beszel's collection rules gate every read behind an authenticated user who is
  a *member* of the system (``users.id ?= @request.auth.id``); an unauthenticated
  list just returns an empty page. We therefore log in as a dedicated **read-only
  Beszel user** (``BESZEL_USER`` / ``BESZEL_PASSWORD``) that has had the tracked
  system shared with it. The returned token is sent **raw** in the
  ``Authorization`` header (PocketBase wants the bare token, no ``Bearer``).
- ``GET /api/collections/systems/records`` → the live per-host snapshot. Each
  record's ``info`` is a compact JSON blob; we read ``dt`` (CPU temperature, °C)
  and ``u`` (uptime, seconds) for the host tile.
- ``GET /api/collections/system_stats/records`` → time-series samples (``type``
  buckets ``1m``/``10m``/``20m``/``120m``/``480m``). Each ``stats`` blob carries
  ``cpu`` (%), ``mp`` (memory %), ``t`` (a per-sensor temperature map — we chart
  the hottest sensor) and ``b`` (``[sent, received]`` bytes/s — we chart the
  sum) for the host charts.
- ``GET /api/collections/containers/records`` → the live per-container snapshot
  (``cpu`` %, ``memory`` MB, ``net``, ``status``, ``image``), keyed by container
  ``name`` (which matches the Docker container name) for the service detail pages.
- ``GET /api/collections/container_stats/records`` → per-container time-series;
  each ``stats`` blob is an array of ``{n, c, m, b}`` (name, cpu %, memory MB,
  bandwidth) — filtered to one container for its detail chart.

Scope: we track a **single** system, ``BESZEL_SYSTEM`` (default ``Alpenglow``).
That is all the read-only user needs shared with it.

Degradation contract (mirrors the other B4 integrations): no credentials, an
unreachable hub, a 401, or an unparseable payload → every Beszel-derived field
degrades to ``None``/empty, never an exception. Callers substitute nulls rather
than 500.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .. import models
from ._common import DEFAULT_TIMEOUT, AsyncTTLCache, env, env_url

# Live snapshots (host info, containers) refresh fast; the charts are heavier and
# change slowly, so they get a longer TTL.
_INFO_CACHE = AsyncTTLCache(ttl=20.0)
_CHARTS_CACHE = AsyncTTLCache(ttl=30.0)
_CONTAINERS_CACHE = AsyncTTLCache(ttl=20.0)
_CONTAINER_HISTORY_CACHE = AsyncTTLCache(ttl=30.0)

# How many samples to pull for each chart, and from which aggregation bucket.
# ``1m`` retains ~2h of one-minute points — a dense, recent trend line.
_CHART_TYPE = "1m"
_CHART_POINTS = 120


def _base_url() -> str:
    return env_url("BESZEL_URL", "http://beszel:8090")


def _system_name() -> str:
    return env("BESZEL_SYSTEM", "Alpenglow").strip()


def _credentials() -> Optional[tuple[str, str]]:
    user = env("BESZEL_USER", "").strip()
    password = env("BESZEL_PASSWORD", "")
    if not user or not password:
        return None
    return user, password


# ── auth ──────────────────────────────────────────────────────────────────────


class _TokenStore:
    """Holds the current PocketBase auth token, refreshed on demand.

    A single token is shared across concurrent callers; a 401 mid-flight
    invalidates it so the next call re-authenticates. Auth failures raise so the
    caller degrades to ``None`` (and a later call retries with fresh creds).
    """

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._token = None

    async def token(self, client: httpx.AsyncClient) -> Optional[str]:
        if self._token is not None:
            return self._token
        async with self._lock:
            if self._token is not None:
                return self._token
            creds = _credentials()
            if creds is None:
                return None
            user, password = creds
            resp = await client.post(
                f"{_base_url()}/api/collections/users/auth-with-password",
                json={"identity": user, "password": password},
            )
            if resp.status_code != 200:
                return None
            token = resp.json().get("token")
            if not isinstance(token, str) or not token:
                return None
            self._token = token
            return token


_TOKENS = _TokenStore()


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Optional[dict]:
    """Authenticated GET against the hub, re-authing once on a 401.

    Returns the parsed JSON body, or ``None`` on any failure (no creds, auth
    failure, non-200, transport/parse error) — the degradation contract.
    """
    token = await _TOKENS.token(client)
    if token is None:
        return None
    url = f"{_base_url()}{path}"
    try:
        resp = await client.get(url, params=params, headers={"Authorization": token})
        if resp.status_code == 401:
            # Token expired/revoked — drop it and try once more with a fresh one.
            _TOKENS.clear()
            token = await _TOKENS.token(client)
            if token is None:
                return None
            resp = await client.get(url, params=params, headers={"Authorization": token})
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


# ── system id resolution ──────────────────────────────────────────────────────


async def _system_record(client: httpx.AsyncClient) -> Optional[dict]:
    """The single tracked system's record (id + live ``info``), or ``None``.

    Filtered by name so it keeps working even if more systems are later shared
    with the read-only user.
    """
    name = _system_name().replace("'", "\\'")
    body = await _get(
        client,
        "/api/collections/systems/records",
        {"filter": f"name='{name}'", "perPage": 1, "fields": "id,name,status,info"},
    )
    if not body:
        return None
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return None
    return items[0]


def _parse_info(record: dict) -> dict:
    """The record's ``info`` blob as a dict (it is stored as a JSON string)."""
    info = record.get("info")
    if isinstance(info, dict):
        return info
    if isinstance(info, str):
        try:
            parsed = json.loads(info)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ── host live info (CPU temp + uptime) ────────────────────────────────────────


@dataclass
class HostInfo:
    cpu_temp: Optional[float]  # °C
    uptime: Optional[float]  # seconds
    status: Optional[str]  # up | down | paused | pending


async def _fetch_host_info() -> Optional[HostInfo]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        record = await _system_record(client)
    if record is None:
        return None
    info = _parse_info(record)
    return HostInfo(
        cpu_temp=_num(info.get("dt")),
        uptime=_num(info.get("u")),
        status=str(record.get("status")) if record.get("status") else None,
    )


async def host_info() -> Optional[HostInfo]:
    """Live CPU temperature + uptime for the tracked host (``None`` to degrade)."""
    try:
        return await _INFO_CACHE.get(_fetch_host_info)
    except Exception:
        return None


# ── host charts (temp / cpu / memory / bandwidth) ─────────────────────────────


def _epoch(created: Any) -> Optional[float]:
    """PocketBase ``created`` ('2026-07-14 03:50:45.897Z') → epoch seconds."""
    if not isinstance(created, str) or not created:
        return None
    text = created.replace(" ", "T").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _max_temp(stats: dict) -> Optional[float]:
    """Hottest sensor from the ``t`` temperature map (the chart's single value)."""
    temps = stats.get("t")
    if not isinstance(temps, dict):
        return None
    vals = [float(v) for v in temps.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(vals) if vals else None


def _bandwidth_total(stats: dict) -> Optional[float]:
    """Combined throughput (sent + received) in bytes/s from ``b``: [sent, recv]."""
    b = stats.get("b")
    if not isinstance(b, list):
        return None
    vals = [float(v) for v in b if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vals) if vals else None


# Each chart series: (stats-key extractor). Order matches models.HostCharts.
_SERIES = {
    "cpu": lambda s: _num(s.get("cpu")),
    "mem": lambda s: _num(s.get("mp")),
    "temp": _max_temp,
    "bandwidth": _bandwidth_total,
}


def _empty_charts() -> models.HostCharts:
    return models.HostCharts(cpu=[], mem=[], temp=[], bandwidth=[])


async def _fetch_charts() -> models.HostCharts:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        record = await _system_record(client)
        if record is None:
            return _empty_charts()
        system_id = record.get("id")
        if not isinstance(system_id, str) or not system_id:
            return _empty_charts()
        sid = system_id.replace("'", "\\'")
        body = await _get(
            client,
            "/api/collections/system_stats/records",
            {
                "filter": f"system='{sid}' && type='{_CHART_TYPE}'",
                "sort": "created",
                "perPage": _CHART_POINTS,
                "fields": "created,stats",
            },
        )
    if not body:
        return _empty_charts()
    items = body.get("items")
    if not isinstance(items, list):
        return _empty_charts()

    series: dict[str, list[models.StatPoint]] = {k: [] for k in _SERIES}
    for item in items:
        if not isinstance(item, dict):
            continue
        t = _epoch(item.get("created"))
        if t is None:
            continue
        stats = item.get("stats")
        if isinstance(stats, str):
            try:
                stats = json.loads(stats)
            except ValueError:
                continue
        if not isinstance(stats, dict):
            continue
        for key, extract in _SERIES.items():
            v = extract(stats)
            if v is not None:
                series[key].append(models.StatPoint(t=t, v=v))

    return models.HostCharts(
        cpu=series["cpu"], mem=series["mem"], temp=series["temp"], bandwidth=series["bandwidth"]
    )


async def host_charts() -> models.HostCharts:
    """Recent cpu/mem/temp/bandwidth series for the host (empty series to degrade)."""
    try:
        return await _CHARTS_CACHE.get(_fetch_charts)
    except Exception:
        return _empty_charts()


# ── per-container live snapshot ───────────────────────────────────────────────


@dataclass
class ContainerStat:
    name: str
    cpu: Optional[float]  # percent
    memory: Optional[float]  # MB (Beszel reports mebibytes)
    net: Optional[float]
    status: Optional[str]
    image: Optional[str]


async def _fetch_containers() -> dict[str, ContainerStat]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        record = await _system_record(client)
        if record is None:
            return {}
        system_id = record.get("id")
        if not isinstance(system_id, str) or not system_id:
            return {}
        sid = system_id.replace("'", "\\'")
        body = await _get(
            client,
            "/api/collections/containers/records",
            {
                "filter": f"system='{sid}'",
                "perPage": 500,
                "fields": "name,cpu,memory,net,status,image",
            },
        )
    if not body:
        return {}
    items = body.get("items")
    if not isinstance(items, list):
        return {}
    out: dict[str, ContainerStat] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        out[name] = ContainerStat(
            name=name,
            cpu=_num(item.get("cpu")),
            memory=_num(item.get("memory")),
            net=_num(item.get("net")),
            status=str(item.get("status")) if item.get("status") else None,
            image=str(item.get("image")) if item.get("image") else None,
        )
    return out


async def containers() -> dict[str, ContainerStat]:
    """Live per-container snapshot keyed by container name (empty to degrade)."""
    try:
        return await _CONTAINERS_CACHE.get(_fetch_containers)
    except Exception:
        return {}


# Per-container time-series: {name: {"cpu": [StatPoint], "mem": [StatPoint]}}.
# Built from container_stats, whose ``stats`` is a per-sample array of
# ``{n, c, m, b}`` (name, cpu %, memory MiB, bandwidth).
async def _fetch_container_history() -> dict[str, dict[str, list[models.StatPoint]]]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        record = await _system_record(client)
        if record is None:
            return {}
        system_id = record.get("id")
        if not isinstance(system_id, str) or not system_id:
            return {}
        sid = system_id.replace("'", "\\'")
        body = await _get(
            client,
            "/api/collections/container_stats/records",
            {
                "filter": f"system='{sid}' && type='{_CHART_TYPE}'",
                "sort": "created",
                "perPage": _CHART_POINTS,
                "fields": "created,stats",
            },
        )
    if not body:
        return {}
    items = body.get("items")
    if not isinstance(items, list):
        return {}

    hist: dict[str, dict[str, list[models.StatPoint]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        t = _epoch(item.get("created"))
        if t is None:
            continue
        stats = item.get("stats")
        if isinstance(stats, str):
            try:
                stats = json.loads(stats)
            except ValueError:
                continue
        if not isinstance(stats, list):
            continue
        for entry in stats:
            if not isinstance(entry, dict):
                continue
            name = entry.get("n")
            if not isinstance(name, str) or not name:
                continue
            series = hist.setdefault(name, {"cpu": [], "mem": []})
            cpu = _num(entry.get("c"))
            mem = _num(entry.get("m"))
            if cpu is not None:
                series["cpu"].append(models.StatPoint(t=t, v=cpu))
            if mem is not None:
                series["mem"].append(models.StatPoint(t=t, v=mem))
    return hist


async def container_history() -> dict[str, dict[str, list[models.StatPoint]]]:
    """Recent per-container cpu/mem series keyed by name (empty to degrade)."""
    try:
        return await _CONTAINER_HISTORY_CACHE.get(_fetch_container_history)
    except Exception:
        return {}


async def containers_for(names: list[str]) -> list[models.BeszelContainer]:
    """Beszel live stats + recent history for the named containers, in order.

    Names not tracked by Beszel are skipped; an empty list means Beszel is
    unconfigured/unreachable or none of the containers are tracked.
    """
    snap, hist = await asyncio.gather(containers(), container_history())
    out: list[models.BeszelContainer] = []
    for name in names:
        cs = snap.get(name)
        if cs is None:
            continue
        series = hist.get(name, {})
        out.append(
            models.BeszelContainer(
                name=cs.name,
                cpu=cs.cpu,
                memory=cs.memory,
                net=cs.net,
                status=cs.status,
                image=cs.image,
                cpuHistory=series.get("cpu", []),
                memHistory=series.get("mem", []),
            )
        )
    return out
