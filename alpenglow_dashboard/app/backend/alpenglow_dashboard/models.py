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
SSOState = Literal["keycloak", "oidc", "self", "native", "none"]
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


class OverviewMonitors(BaseModel):
    up: Optional[int]
    total: Optional[int]
    note: Optional[str]


class OverviewBackups(BaseModel):
    pgAgo: Optional[str]
    kopiaAgo: Optional[str]
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
    used: Optional[float]
    size: Optional[float]
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


class SSOInfo(BaseModel):
    state: SSOState
    source: str


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
    sso: SSOInfo
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


# ── PUT /api/services/{id}/tags ───────────────────────────────────────────────


class TagsPayload(BaseModel):
    tags: list[str]


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
