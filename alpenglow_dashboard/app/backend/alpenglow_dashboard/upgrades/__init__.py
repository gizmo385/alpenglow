"""Upgrades board: Vikunja cards for pending upgrades, and Claude's verdicts.

The dashboard already reads the update tracker, so it also keeps Vikunja's
Upgrades project in step with it. Every ``UPGRADES_SYNC_INTERVAL`` seconds
(30 minutes by default) it files, retargets and closes cards (see
``cards.py``), and the claude-dispatcher assesses each card and labels it with
a verdict. Those verdicts come back in two places:

- the Updates view (``UpdateEntry.verdict*``, via :func:`annotations_by_repo`);
- ``GET /internal/upgrades/annotations`` for the tracker's Discord bot
  (``ANNOTATIONS_URL``). It sits outside ``/api`` because the bot can't pass
  oauth2-proxy; it is read-only and returns only card titles, verdicts and
  links.

Off unless ``VIKUNJA_API_TOKEN`` and ``VIKUNJA_PROJECT_ID`` are set, and in
mock mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from .. import inventory, mock
from ..integrations._common import AsyncTTLCache
from . import cards, verdicts
from .vikunja import Vikunja

log = logging.getLogger("alpenglow.upgrades")
router = APIRouter(tags=["upgrades"])

_ANNOTATIONS_CACHE = AsyncTTLCache(ttl=60.0)
_sync_lock = asyncio.Lock()


@dataclass(frozen=True)
class Settings:
    vikunja_url: str
    token: str
    project_id: int
    public_url: str | None = None
    versions_path: Path | None = None
    sync_interval: int = 1800

    @classmethod
    def from_env(cls) -> Settings | None:
        token = os.environ.get("VIKUNJA_API_TOKEN")
        project = os.environ.get("VIKUNJA_PROJECT_ID")
        if not (token and project) or mock.mock_enabled():
            return None
        versions = os.environ.get("VERSIONS_PATH")
        path = Path(versions) if versions else inventory.services_root() / "versions.yaml"
        return cls(
            vikunja_url=os.environ.get("VIKUNJA_API_URL", "http://vikunja:3456/api/v1").rstrip("/"),
            token=token,
            project_id=int(project),
            public_url=(os.environ.get("VIKUNJA_PUBLIC_URL") or "").rstrip("/") or None,
            versions_path=path if path.exists() else None,
            sync_interval=int(os.environ.get("UPGRADES_SYNC_INTERVAL") or 1800),
        )


def _api(s: Settings, client: httpx.AsyncClient) -> Vikunja:
    return Vikunja(s.vikunja_url, s.token, s.project_id, client)


async def _load_annotations() -> list[verdicts.Annotation]:
    s = Settings.from_env()
    if s is None:
        return []
    async with httpx.AsyncClient(timeout=30) as client:
        return await verdicts.fetch(_api(s, client), s.public_url)


async def annotations() -> list[verdicts.Annotation]:
    """Open Upgrades cards with their verdicts; ``[]`` when off or unreachable."""
    try:
        return await _ANNOTATIONS_CACHE.get(_load_annotations)
    except (httpx.HTTPError, ValueError) as err:
        log.warning("upgrades: couldn't read the board: %s", err)
        return []


async def annotations_by_repo() -> dict[str, verdicts.Annotation]:
    return {a.repo.lower(): a for a in await annotations()}


async def sync_once() -> cards.Plan | None:
    """Sync the cards with the tracker now. ``None`` when off or the tracker is down."""
    s = Settings.from_env()
    if s is None:
        return None
    from ..integrations import updates  # avoids an import cycle

    payload = await updates._fetch_tracker()
    if payload is None:
        log.warning("upgrades: tracker unavailable; skipping sync")
        return None
    services = [cards.Service.from_api(d) for d in payload.get("services") or [] if isinstance(d, dict)]
    async with _sync_lock, httpx.AsyncClient(timeout=30) as client:
        done = await cards.sync(services, _api(s, client), s.versions_path)
    _ANNOTATIONS_CACHE.invalidate()
    return done


async def _loop(interval: int) -> None:
    await asyncio.sleep(30)  # let the tracker finish its first scan
    while True:
        try:
            todo = await sync_once()
            if todo:
                log.info("upgrades: %d filed, %d retargeted, %d closed",
                         len(todo.create), len(todo.retarget), len(todo.close))
        except Exception:
            log.exception("upgrades: sync failed; trying again next interval")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(app):
    s = Settings.from_env()
    task = asyncio.create_task(_loop(s.sync_interval), name="alpenglow-upgrades-sync") if s else None
    try:
        yield
    finally:
        if task:
            task.cancel()


@router.get("/internal/upgrades/annotations")
async def get_annotations() -> dict:
    """The tracker's ``ANNOTATIONS_URL`` format (see image-updates-tracker)."""
    return verdicts.payload(await annotations())


@router.post("/api/upgrades/sync")
async def post_sync() -> dict:
    try:
        todo = await sync_once()
    except httpx.HTTPError as err:
        raise HTTPException(502, f"sync failed: {err}") from err
    if todo is None:
        raise HTTPException(409, "the upgrades board is off, or the tracker is unavailable")
    return {"filed": len(todo.create), "retargeted": len(todo.retarget), "closed": len(todo.close)}
