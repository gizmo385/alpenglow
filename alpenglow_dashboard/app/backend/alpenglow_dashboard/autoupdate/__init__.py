"""Automatic updates for the images ``versions.yaml`` opts in.

Settings per image are in :mod:`policy`, the scheduled pass and the update
itself in :mod:`runner`, the post-update check in :mod:`health`, and what's
remembered between runs in :mod:`state`. ``AUTO_UPDATE`` (off | dry-run | on,
default dry-run) decides whether anything actually changes.

Routes (mutations are admin + CSRF, like every other ``/api`` POST):

- ``GET /api/autoupdate``: each unit, its schedule and why it's waiting;
- ``POST /api/autoupdate/run``: check now, ignoring the schedules;
- ``POST /api/autoupdate/resume``: un-pause a unit after a failed update.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import policy, runner
from .state import State, iso

router = APIRouter(prefix="/api/autoupdate", tags=["autoupdate"])


class UnitRequest(BaseModel):
    unit: Optional[str] = None


def _live(plan: Optional[runner.Plan], unit: policy.Unit, last) -> dict:
    """Where the unit's pending release stands right now, for the Updates rows."""
    if plan is None:
        return {"pending": [], "hold": None, "eta": None}
    hold = plan.hold
    eta = None
    if plan.ready:
        eta = runner.next_run(unit, last) if last else None
    elif hold and hold.kind == "soak" and hold.until:
        eta = runner.first_run_after(unit, last, hold.until)
    return {
        "pending": [{"image": p.image, "current": p.current, "latest": p.latest} for p in plan.pending],
        "hold": {"kind": hold.kind, "text": hold.text, "until": iso(hold.until) if hold.until else None}
        if hold else None,
        "eta": iso(eta) if eta else None,
    }


def _unit_view(unit: policy.Unit, state: State, plan: Optional[runner.Plan] = None) -> dict:
    entry = state.get(unit.id)
    last = state.last_run(unit.id)
    cfg = unit.config
    return _live(plan, unit, last) | {
        "id": unit.id,
        "label": unit.label,
        "images": list(unit.images),
        "services": sorted({sid for sid, _ in runner._places(unit.images)}),
        "follow": unit.follow,
        "every": cfg.every.text,
        "maxBump": None if unit.follow else cfg.max_bump,
        "soak": None if unit.follow else policy._fmt(cfg.soak),
        "verdict": None if unit.follow else cfg.verdict,
        "monitors": list(cfg.monitors),
        "lastRun": iso(last) if last else None,
        "nextRun": iso(runner.next_run(unit, last)) if last else None,
        "waiting": entry.get("waiting"),
        "paused": entry.get("paused"),
        "lastResult": entry.get("last_result"),
    }


async def _plans(units: list[policy.Unit], state: State) -> dict[str, runner.Plan]:
    """Each unit's evaluation against the tracker now; empty if it can't be read."""
    from .. import repin, upgrades
    from ..integrations import updates

    if not units:
        return {}
    payload = await updates._tracker_cached()
    if payload is None:
        return {}
    annotations = await upgrades.annotations_by_repo()
    pins = repin.load_pins(repin.repo_root())
    now = runner._now()
    return {u.id: runner.evaluate(u, payload, annotations, pins, state, now) for u in units}


@router.get("")
async def get_status() -> dict:
    loaded = runner.load()
    state = State.load()
    plans = await _plans(loaded.units, state)
    return {
        "mode": runner.mode(),
        "commit": runner.commit_enabled(),
        "running": runner.run_lock().locked(),
        "units": [_unit_view(u, state, plans.get(u.id)) for u in loaded.units],
        "errors": loaded.errors,
    }


@router.post("/run")
async def post_run(payload: Optional[UnitRequest] = None) -> dict:
    if runner.mode() == "off":
        raise HTTPException(409, "automatic updates are off (AUTO_UPDATE=off)")
    only = payload.unit if payload else None
    if only and only not in {u.id for u in runner.load().units}:
        raise HTTPException(404, f"no automatic update unit {only!r}")
    if runner.run_lock().locked():
        raise HTTPException(409, "a check is already running")
    asyncio.get_running_loop().create_task(runner.tick(force=True, only=only))
    return {"started": True, "mode": runner.mode()}


@router.post("/resume")
async def post_resume(payload: UnitRequest) -> dict:
    if not payload.unit:
        raise HTTPException(422, "name the unit to resume")
    async with runner.run_lock():
        state = State.load()
        if not state.resume(payload.unit):
            raise HTTPException(404, f"{payload.unit!r} isn't paused")
        state.save()
    return {"resumed": payload.unit}


@contextlib.asynccontextmanager
async def lifespan(app):
    task = None if runner.mode() == "off" else asyncio.create_task(runner.loop(), name="alpenglow-autoupdate")
    try:
        yield
    finally:
        if task:
            task.cancel()
