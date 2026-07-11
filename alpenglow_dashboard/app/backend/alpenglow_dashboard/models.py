"""Pydantic response models — the locked API contract (work package A1).

These models are the single source of truth for the JSON shapes served by the
backend and consumed by the typed frontend client. They must not drift from the
"API contract" section of PLAN.md. Field names use camelCase to match the
frontend directly; Python code populates them via keyword arguments.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

# ── shared enums ──────────────────────────────────────────────────────────────

Status = Literal["up", "down", "restarting", "updating"]
Tier = Literal["public", "tailnet", "internal", "management"]
ActionName = Literal["restart", "stop", "start", "pull"]


# ── GET /api/health ───────────────────────────────────────────────────────────


class Health(BaseModel):
    status: str
    version: str


# ── GET /api/me ───────────────────────────────────────────────────────────────


class Me(BaseModel):
    user: str
    groups: list[str]
    isAdmin: bool


# ── GET /api/csrf ─────────────────────────────────────────────────────────────


class Csrf(BaseModel):
    token: str


# ── GET /api/overview ─────────────────────────────────────────────────────────


class OverviewServices(BaseModel):
    up: int
    down: int
    total: int


class OverviewUpdates(BaseModel):
    count: int


class MonitorRef(BaseModel):
    name: str
    status: str  # up | down | pending | maintenance | unknown


class OverviewMonitors(BaseModel):
    up: Optional[int]
    total: Optional[int]
    note: Optional[str]
    # Per-monitor drill-down: every real monitor with its status, plus the
    # public Kuma UI URL for the tile's external-link affordance. Empty list +
    # None url when Kuma can't be read (the tile degrades gracefully).
    monitors: list[MonitorRef] = []
    url: Optional[str] = None


class OverviewBackups(BaseModel):
    pgAgo: Optional[str]
    kopiaAgo: Optional[str]
    # Absolute local timestamps ("2026-07-11 02:00 MDT") for the tile tooltips;
    # None when the corresponding source file is missing.
    pgAt: Optional[str] = None
    kopiaAt: Optional[str] = None
    ok: Optional[bool]


class OverviewHost(BaseModel):
    load1: Optional[float]
    load5: Optional[float]
    load15: Optional[float]
    cpuPct: Optional[float]
    memUsed: Optional[float]
    memTotal: Optional[float]
    swapUsed: Optional[float]
    swapTotal: Optional[float]


class StoragePool(BaseModel):
    name: str
    state: str
    # USABLE space (from `zfs list`) — what the operator actually cares about.
    # The bar meter uses these; on a raidz pool they already exclude parity.
    used: Optional[float]
    size: Optional[float]
    # RAW pool space (from `zpool list`) — allocated/total *including* parity,
    # surfaced as secondary context ("raw 2.95T / 10.9T incl. parity").
    rawUsed: Optional[float] = None
    rawSize: Optional[float] = None
    scrubAgo: Optional[str]
    errors: Optional[int]


class StorageFs(BaseModel):
    label: str
    used: Optional[float]
    size: Optional[float]
    pct: Optional[float]


class OverviewStorage(BaseModel):
    pools: list[StoragePool]
    fs: list[StorageFs]


class OverviewMeta(BaseModel):
    host: str
    branch: str


class Overview(BaseModel):
    services: OverviewServices
    updates: OverviewUpdates
    monitors: OverviewMonitors
    backups: OverviewBackups
    host: OverviewHost
    storage: OverviewStorage
    polledAt: str
    meta: OverviewMeta


# ── GET /api/services , GET /api/services/{id} ────────────────────────────────


class ContainerRef(BaseModel):
    name: str
    status: str


class ServiceSummary(BaseModel):
    id: str
    name: str
    category: str
    icon: str
    description: str
    image: str
    currentVersion: str
    latestVersion: Optional[str]
    releasedAt: Optional[str]
    changelogUrl: Optional[str]
    status: Status
    uptimeSeconds: Optional[int]
    url: Optional[str]
    tier: Tier
    containers: list[ContainerRef]
    tags: list[str]
    restartPolicy: str
    ports: str
    memLimit: Optional[str]


class ServiceDetail(ServiceSummary):
    # "summary + facts fields" — the extra facts surfaced only in the detail view.
    composePath: str
    projectName: str
    primaryContainer: Optional[str]


# ── GET /api/services/{id}/stats ──────────────────────────────────────────────


class StatPoint(BaseModel):
    t: float
    v: float


class StatsHistory(BaseModel):
    cpu: list[StatPoint]
    mem: list[StatPoint]


class StatsCurrent(BaseModel):
    cpuPct: Optional[float]
    memUsed: Optional[float]
    memLimit: Optional[float]
    netIO: Optional[str]
    blockIO: Optional[str]
    pids: Optional[int]
    restarts: Optional[int]


class Stats(BaseModel):
    history: StatsHistory
    current: StatsCurrent


# ── PUT /api/services/{id}/settings ───────────────────────────────────────────


class SettingsRequest(BaseModel):
    """Partial update of a service's persisted settings. Either field may be
    omitted; ``tags`` replaces the tag list, ``category`` sets/clears the
    per-service category override (``None`` = fall back to the metadata default).
    ``category`` is a three-state field: absent = leave unchanged, ``null`` =
    clear the override, a string = set it.
    """

    tags: Optional[list[str]] = None
    category: Optional[str] = None
    # distinguishes "category omitted" from "category: null" on the wire.
    model_config = {"extra": "forbid"}


class SettingsPayload(BaseModel):
    """The stored per-service settings echoed back after a PUT."""

    tags: list[str]
    category: Optional[str]


# ── POST /api/services/{id}/actions ───────────────────────────────────────────


class ActionRequest(BaseModel):
    action: ActionName
    confirm: Optional[bool] = False


class ActionResponse(BaseModel):
    accepted: bool
    jobId: str


# ── GET /api/updates ──────────────────────────────────────────────────────────


class UpdateEntry(BaseModel):
    id: str
    name: str
    icon: str
    image: str
    currentVersion: str
    latestVersion: str
    releasedAt: Optional[str]
    changelogUrl: Optional[str]


class Updates(BaseModel):
    services: list[UpdateEntry]
    lastUpdated: Optional[str]
    refreshing: bool
    trackerUnavailable: bool = False
