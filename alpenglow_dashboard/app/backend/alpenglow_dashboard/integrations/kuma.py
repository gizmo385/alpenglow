"""Uptime Kuma v2 integration (work package B4).

A1 stub: interface only. B4 implements monitor up/total plus the push-monitor
freshness reads (pg dump, kopia, rpool, dpool) against uptime_kuma:3001,
degrading fields to None when unavailable.
"""

from __future__ import annotations

from .. import models


async def monitors() -> models.OverviewMonitors:
    raise NotImplementedError("uptime kuma integration not implemented yet (B4)")


async def backups() -> models.OverviewBackups:
    raise NotImplementedError("uptime kuma integration not implemented yet (B4)")
