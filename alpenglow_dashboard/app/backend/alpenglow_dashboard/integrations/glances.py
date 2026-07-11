"""Glances integration (work package B4).

A1 stub: interface only. B4 implements GET {GLANCES_URL}/api/4/{cpu,mem,
memswap,load,fs} → OverviewHost + OverviewStorage.fs, degrading to None fields
on failure (never 500s).
"""

from __future__ import annotations

from .. import models


async def host_stats() -> models.OverviewHost:
    raise NotImplementedError("glances integration not implemented yet (B4)")


async def fs_stats() -> list[models.StorageFs]:
    raise NotImplementedError("glances integration not implemented yet (B4)")
