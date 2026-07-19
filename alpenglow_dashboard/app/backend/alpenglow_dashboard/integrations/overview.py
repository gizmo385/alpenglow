"""Composed /api/overview route (work package B4).

Assembles the overview tile from every B4 integration plus the B1 inventory
counts. Each integration is awaited independently and *isolated*: any one
failing degrades its own fields to nulls (or an empty list) and never propagates
— ``/api/overview`` must never 500 because a dependency is down. Under
``MOCK_DATA=1`` the A1 mock payload is served unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter

from .. import inventory, mock, models
from . import beszel, glances, kuma, zfs
from . import updates as updates_mod

router = APIRouter(prefix="/api", tags=["overview"])


async def _safe(coro, fallback):
    """Await ``coro``; on any exception return ``fallback`` (degrade, never 500)."""
    try:
        return await coro
    except Exception:
        return fallback


async def _service_counts() -> models.OverviewServices:
    try:
        details = await inventory.build_inventory()
    except Exception:
        return models.OverviewServices(up=0, down=0, total=0)
    total = len(details)
    up = sum(1 for d in details if d.status != "down")
    return models.OverviewServices(up=up, down=total - up, total=total)


async def _updates_count() -> models.OverviewUpdates:
    try:
        enrich = await updates_mod.service_enrichment()
    except Exception:
        enrich = {}
    return models.OverviewUpdates(count=len(enrich))


async def _storage() -> models.OverviewStorage:
    pools = await _safe(zfs.pools(), [])
    fs = await _safe(glances.fs_stats(), [])
    return models.OverviewStorage(pools=pools, fs=fs)


async def _host() -> models.OverviewHost:
    """Glances host stats, enriched with Beszel's CPU temp + uptime.

    Both sources are isolated: Glances failing yields a nulled host, and Beszel
    failing simply leaves ``cpuTemp``/``uptime`` at ``None`` — neither propagates.
    """
    host, info = await asyncio.gather(
        _safe(
            glances.host_stats(),
            models.OverviewHost(
                load1=None, load5=None, load15=None, cpuPct=None,
                memUsed=None, memTotal=None, swapUsed=None, swapTotal=None,
            ),
        ),
        _safe(beszel.host_info(), None),
    )
    if info is not None:
        host.cpuTemp = info.cpu_temp
        host.uptime = info.uptime
    return host


def _meta() -> models.OverviewMeta:
    try:
        return inventory.sidebar_meta()
    except Exception:
        return models.OverviewMeta(host="alpenglow", branch="main")


@router.get("/overview")
async def get_overview() -> models.Overview:
    if mock.mock_enabled():
        return mock.mock_overview()

    services, updates, monitors, backups, host, storage = await asyncio.gather(
        _service_counts(),
        _updates_count(),
        _safe(
            kuma.monitors(),
            models.OverviewMonitors(
                up=None, total=None, note=None, monitors=[], url=None
            ),
        ),
        _safe(
            kuma.backups(),
            models.OverviewBackups(
                pgAgo=None, kopiaAgo=None, pgAt=None, kopiaAt=None, ok=None
            ),
        ),
        _host(),
        _storage(),
    )

    return models.Overview(
        services=services,
        updates=updates,
        monitors=monitors,
        backups=backups,
        host=host,
        storage=storage,
        polledAt=datetime.now(timezone.utc).isoformat(),
        meta=_meta(),
    )


@router.get("/host/charts")
async def get_host_charts() -> models.HostCharts:
    """Recent host time-series (Beszel) for the Overview sparkline card.

    Always 200s: Beszel being down/unconfigured yields empty series, which the
    card renders as a flat "—" rather than erroring.
    """
    if mock.mock_enabled():
        return mock.mock_host_charts()
    return await _safe(
        beszel.host_charts(),
        models.HostCharts(cpu=[], mem=[], temp=[], bandwidth=[]),
    )
