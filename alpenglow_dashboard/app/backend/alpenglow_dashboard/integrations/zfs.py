"""ZFS status feed (work package B4).

Reads ``/data-store/zfs_status.json`` (override ``ZFS_STATUS_PATH``), the file
the ``zfs_status_checker`` host cron (package D3) writes. Real schema (verified):

    {
      "generated_at": <unix seconds>,
      "generated_at_iso": "...",
      "pools": {
        "<name>": {
          "name", "state", "healthy", "size", "used", "free",
          "capacity_pct", "last_scrub" (unix secs), "scrub_errors",
          "read_errors", "write_errors", "checksum_errors", ...
        }, ...
      }
    }

Maps each pool to ``models.StoragePool`` (``used``/``size`` in bytes, ``scrubAgo``
humanised from ``last_scrub``, ``errors`` = sum of read/write/checksum errors).

Degradation: a missing/unreadable/malformed file, or one older than
``STALE_SECONDS`` (~2h — the cron writes more often, so an old stamp means it
stopped), yields **no pools** rather than stale numbers.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .. import models
from ._common import ago

STALE_SECONDS = 2 * 60 * 60  # 2h


def status_path() -> Path:
    return Path(os.environ.get("ZFS_STATUS_PATH", "/data-store/zfs_status.json"))


def _read(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _pool_errors(pool: dict) -> Optional[int]:
    keys = ("read_errors", "write_errors", "checksum_errors")
    vals = [pool.get(k) for k in keys]
    if all(isinstance(v, (int, float)) for v in vals):
        return int(sum(vals))  # type: ignore[arg-type]
    # fall back to scrub_errors alone if the per-vdev counters are absent
    se = pool.get("scrub_errors")
    return int(se) if isinstance(se, (int, float)) else None


def _to_pool(name: str, pool: dict) -> models.StoragePool:
    last_scrub = pool.get("last_scrub")
    scrub_ago = (
        ago(time.time() - last_scrub)
        if isinstance(last_scrub, (int, float)) and last_scrub > 0
        else None
    )
    size = pool.get("size")
    used = pool.get("used")
    return models.StoragePool(
        name=str(pool.get("name") or name),
        state=str(pool.get("state") or "UNKNOWN"),
        used=float(used) if isinstance(used, (int, float)) else None,
        size=float(size) if isinstance(size, (int, float)) else None,
        scrubAgo=scrub_ago,
        errors=_pool_errors(pool),
    )


def _parse(doc: dict) -> list[models.StoragePool]:
    generated_at = doc.get("generated_at")
    if isinstance(generated_at, (int, float)) and generated_at > 0:
        if time.time() - generated_at > STALE_SECONDS:
            return []  # stale → don't surface old numbers
    pools = doc.get("pools")
    if not isinstance(pools, dict):
        return []
    out: list[models.StoragePool] = []
    for name, pool in pools.items():
        if isinstance(pool, dict):
            out.append(_to_pool(str(name), pool))
    return out


async def pools() -> list[models.StoragePool]:
    doc = _read(status_path())
    if not isinstance(doc, dict):
        return []
    return _parse(doc)
