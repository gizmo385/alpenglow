"""Glances integration (work package B4).

Data source: the Glances v4 REST API at ``GLANCES_URL`` (``glances:61208`` on the
caddy network), verified live:
- ``/api/4/load``   → ``{min1,min5,min15,cpucore}``            → host load fields
- ``/api/4/cpu``    → ``{total, ...}``                          → host cpuPct
- ``/api/4/mem``    → ``{total,used}``                          → host mem
- ``/api/4/memswap``→ ``{total,used}``                          → host swap
- ``/api/4/fs``     → ``[{device_name,mnt_point,size,used,percent}]`` → fs bars

Note on ``fs``: the Glances container only sees the filesystems visible *inside
its own mount namespace*, so mount points read as its bind-mount targets rather
than host ``/`` and ``/data``. We therefore key fs bars off the ZFS **device**
pool (``rpool`` → ``/``, ``dpool`` → ``/data``), de-duplicating by pool and
picking the largest sample per pool. The authoritative pool capacity comes from
``zfs.py`` (the D3 cron); these bars are the live-usage complement.

Every field degrades to ``None`` (fs → empty list) on any transport error.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .. import models
from ._common import DEFAULT_TIMEOUT, AsyncTTLCache, env_url

_CACHE = AsyncTTLCache(ttl=15.0)

_ENDPOINTS = ("cpu", "mem", "memswap", "load", "fs")


def _base_url() -> str:
    return env_url("GLANCES_URL", "http://glances:61208")


async def _fetch_all() -> dict[str, Any]:
    """Fetch every needed Glances endpoint concurrently.

    Missing/failed endpoints are simply absent from the returned dict; callers
    treat absence as ``None``. A total failure returns ``{}``.
    """
    base = _base_url()
    out: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            import asyncio

            async def one(name: str) -> tuple[str, Any]:
                try:
                    resp = await client.get(f"{base}/api/4/{name}")
                    if resp.status_code == 200:
                        return name, resp.json()
                except (httpx.HTTPError, ValueError):
                    pass
                return name, None

            results = await asyncio.gather(*(one(n) for n in _ENDPOINTS))
            out = {name: data for name, data in results if data is not None}
    except httpx.HTTPError:
        return {}
    return out


async def _data() -> dict[str, Any]:
    return await _CACHE.get(_fetch_all)


def _num(d: Any, key: str) -> Optional[float]:
    if isinstance(d, dict) and isinstance(d.get(key), (int, float)):
        return float(d[key])
    return None


def _host_from(data: dict[str, Any]) -> models.OverviewHost:
    load = data.get("load")
    cpu = data.get("cpu")
    mem = data.get("mem")
    swap = data.get("memswap")
    return models.OverviewHost(
        load1=_num(load, "min1"),
        load5=_num(load, "min5"),
        load15=_num(load, "min15"),
        cpuPct=_num(cpu, "total"),
        memUsed=_num(mem, "used"),
        memTotal=_num(mem, "total"),
        swapUsed=_num(swap, "used"),
        swapTotal=_num(swap, "total"),
    )


async def host_stats() -> models.OverviewHost:
    try:
        data = await _data()
    except Exception:
        data = {}
    return _host_from(data)


# Device-prefix → friendly fs label. Device names look like "rpool/ROOT/…",
# "dpool/data", etc.; we bucket by the leading pool token.
_POOL_LABELS = {"rpool": "/ (rpool)", "dpool": "/data (dpool)"}


def _fs_from(raw: Any) -> list[models.StorageFs]:
    if not isinstance(raw, list):
        return []
    # Pick the largest sample per pool (a pool can appear under several mounts).
    best: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        device = str(entry.get("device_name", ""))
        pool = device.split("/", 1)[0] if device else ""
        if pool not in _POOL_LABELS:
            continue
        size = entry.get("size")
        if not isinstance(size, (int, float)):
            continue
        cur = best.get(pool)
        if cur is None or size > cur.get("size", 0):
            best[pool] = entry

    out: list[models.StorageFs] = []
    for pool in ("dpool", "rpool"):  # match mock ordering: data first, root second
        entry = best.get(pool)
        if entry is None:
            continue
        used = entry.get("used")
        size = entry.get("size")
        pct = entry.get("percent")
        out.append(
            models.StorageFs(
                label=_POOL_LABELS[pool],
                used=float(used) if isinstance(used, (int, float)) else None,
                size=float(size) if isinstance(size, (int, float)) else None,
                pct=float(pct) if isinstance(pct, (int, float)) else None,
            )
        )
    return out


async def fs_stats() -> list[models.StorageFs]:
    try:
        data = await _data()
    except Exception:
        return []
    return _fs_from(data.get("fs"))
