"""ZFS status feed (work package B4).

A1 stub: interface only. B4 reads /data-store/zfs_status.json (written by the
zfs_status_checker host cron, package D3), tolerating absence.
"""

from __future__ import annotations

from .. import models


async def pools() -> list[models.StoragePool]:
    raise NotImplementedError("zfs integration not implemented yet (B4)")
