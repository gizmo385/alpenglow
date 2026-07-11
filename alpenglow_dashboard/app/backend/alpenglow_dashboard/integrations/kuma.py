"""Uptime Kuma v2 integration (work package B4).

Data source (verified live against ``uptime_kuma:3001``, Uptime Kuma v2):
the Prometheus ``GET /metrics`` endpoint. It is the only surface that exposes
*all* monitors — including the private push monitors (pool health, database
backup, backup verification) that never appear on the public status page. It is
gated behind **HTTP Basic auth** with an Uptime Kuma **API key** (empty
username, the key as the password). Settings → "API Keys" must be enabled and a
key minted; that key goes in ``UPTIME_KUMA_API_KEY``.

What ``/metrics`` gives us (and doesn't):
- ``monitor_status{monitor_id,monitor_name,monitor_type,...} = 1|0|2|3``
  (1 UP, 0 DOWN, 2 PENDING, 3 MAINTENANCE). We count real monitors for the
  Monitors tile (excluding ``monitor_type="group"`` rows, which are just visual
  containers) and read the push monitors' status for the Backups tile ``ok``.
- It does **not** expose a last-heartbeat timestamp, so backup *freshness* ages
  (``pgAgo``/``kopiaAgo``) come instead from the **filesystem** (backup dump /
  log mtimes under the read-only ``/services`` mount + the pg dump dir) — see
  ``backups()``. Kuma still supplies the ``ok`` status from the push monitors.

The Monitors tile also carries a per-monitor drill-down (``monitors`` list) and
a link out to the public Kuma UI (``KUMA_PUBLIC_URL``).

Degradation contract: no API key, unreachable, 401, or unparseable payload →
every Kuma-derived field is ``None``/empty and a truthful ``note`` explains why.
We never fabricate counts (e.g. we do not fall back to the 6-monitor public
status page, which would undercount).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .. import models
from ._common import DEFAULT_TIMEOUT, AsyncTTLCache, ago, env, env_url

_CACHE = AsyncTTLCache(ttl=20.0)

# monitor_status{ ...labels... } <value>
_METRIC_RE = re.compile(r'^monitor_status\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)\s*$')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

# Push monitors that back the Backups tile (matched by monitor name, lowered).
_PG_BACKUP_NAMES = ("database backup", "postgres backup", "pg dump", "pg backup")
_KOPIA_NAMES = ("backup verification", "kopia")


@dataclass
class Monitor:
    id: str
    name: str
    type: str
    status: int  # 1 up, 0 down, 2 pending, 3 maintenance


def _base_url() -> str:
    return env_url("UPTIME_KUMA_URL", "http://uptime_kuma:3001")


def _public_url() -> str:
    """The browser-facing Kuma UI, for the tile's external-link affordance."""
    return env_url("KUMA_PUBLIC_URL", "https://uptime.alpenglow.acbc.house")


def _api_key() -> str:
    return env("UPTIME_KUMA_API_KEY", "").strip()


# Map the raw metric value to the frontend's status vocabulary.
_STATUS_LABEL = {1: "up", 0: "down", 2: "pending", 3: "maintenance"}


def _status_label(value: int) -> str:
    return _STATUS_LABEL.get(value, "unknown")


def _parse_labels(raw: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _LABEL_RE.finditer(raw)}


def parse_metrics(text: str) -> list[Monitor]:
    """Parse the ``monitor_status`` gauge lines out of a Prometheus payload."""
    out: list[Monitor] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        labels = _parse_labels(m.group("labels"))
        try:
            value = int(float(m.group("value")))
        except ValueError:
            continue
        out.append(
            Monitor(
                id=labels.get("monitor_id", ""),
                name=labels.get("monitor_name", ""),
                type=labels.get("monitor_type", ""),
                status=value,
            )
        )
    return out


async def _fetch_monitors() -> Optional[list[Monitor]]:
    """Fetch + parse monitors, or ``None`` when Kuma can't be read.

    ``None`` distinguishes "no data / degrade" from "zero monitors".
    """
    key = _api_key()
    if not key:
        return None
    url = f"{_base_url()}/metrics"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # Uptime Kuma API keys authenticate as Basic auth with an empty
            # username and the key as the password.
            resp = await client.get(url, auth=("", key))
        if resp.status_code != 200:
            return None
        return parse_metrics(resp.text)
    except (httpx.HTTPError, ValueError):
        return None


async def _monitors_cached() -> Optional[list[Monitor]]:
    return await _CACHE.get(_fetch_monitors)


def _real_monitors(monitors: list[Monitor]) -> list[Monitor]:
    """Drop ``group`` rows — they are visual containers, not health checks."""
    return [m for m in monitors if m.type != "group"]


async def monitors() -> models.OverviewMonitors:
    mons = await _monitors_cached()
    if mons is None:
        note = (
            "no UPTIME_KUMA_API_KEY configured"
            if not _api_key()
            else "uptime kuma unreachable"
        )
        # Even when we can't read Kuma, still expose the public URL so the tile's
        # "open Kuma" affordance works.
        return models.OverviewMonitors(
            up=None, total=None, note=note, monitors=[], url=_public_url()
        )

    real = _real_monitors(mons)
    total = len(real)
    up = sum(1 for m in real if m.status == 1)
    maintenance = sum(1 for m in real if m.status == 3)
    note = f"{maintenance} in maintenance" if maintenance else None
    # Full per-monitor list (name + status), sorted DOWN-first so the tile can
    # render the down ones prominently.
    refs = [
        models.MonitorRef(name=m.name, status=_status_label(m.status)) for m in real
    ]
    refs.sort(key=lambda r: (r.status != "down", r.name.lower()))
    return models.OverviewMonitors(
        up=up, total=total, note=note, monitors=refs, url=_public_url()
    )


def _find(monitors: list[Monitor], names: tuple[str, ...]) -> Optional[Monitor]:
    for m in monitors:
        low = m.name.lower()
        if any(n in low for n in names):
            return m
    return None


# Backup freshness comes from the filesystem, not Kuma (/metrics has no
# heartbeat timestamps). Both sources sit under the dashboard's read-only
# /services (kopia) or an added :ro mount (the pg dump dir, which lives outside
# /services). Overridable via env for tests / relocation.
def _pg_backup_dir() -> Path:
    return Path(env("PG_BACKUP_DIR", "/pg-backups"))


def _kopia_log() -> Path:
    return Path(env("KOPIA_LOG_PATH", "/services/kopia/backup_run.log"))


def _newest_mtime(path: Path, pattern: Optional[str] = None) -> Optional[float]:
    """mtime of ``path`` (or, if a dir + ``pattern``, its newest match). None if
    nothing readable is found — so the tile degrades truthfully to "—"."""
    try:
        if path.is_dir():
            if pattern is None:
                return None
            candidates = list(path.glob(pattern))
            if not candidates:
                return None
            return max(p.stat().st_mtime for p in candidates)
        return path.stat().st_mtime
    except OSError:
        return None


def _freshness(mtime: Optional[float]) -> tuple[Optional[str], Optional[str]]:
    """(relative "Xh ago", absolute local ISO tooltip) for a backup mtime."""
    if mtime is None:
        return None, None
    rel = ago(time.time() - mtime)
    iso = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(mtime))
    return rel, iso


async def backups() -> models.OverviewBackups:
    mons = await _monitors_cached()

    # `ok` still comes from the Kuma push monitors (unchanged logic): healthy
    # only if every backup monitor we found is UP; None when Kuma is unreadable
    # or neither monitor exists.
    ok: Optional[bool]
    if mons is None:
        ok = None
    else:
        pg = _find(mons, _PG_BACKUP_NAMES)
        kopia = _find(mons, _KOPIA_NAMES)
        found = [m for m in (pg, kopia) if m is not None]
        ok = all(m.status == 1 for m in found) if found else None

    # Freshness ages + absolute tooltips from the filesystem.
    pg_ago, pg_at = _freshness(_newest_mtime(_pg_backup_dir(), "*-daily.sql"))
    kopia_ago, kopia_at = _freshness(_newest_mtime(_kopia_log()))

    return models.OverviewBackups(
        pgAgo=pg_ago,
        kopiaAgo=kopia_ago,
        pgAt=pg_at,
        kopiaAt=kopia_at,
        ok=ok,
    )
