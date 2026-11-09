"""Compose action runner + busy-state registry (work package B2).

Real ``docker compose`` action engine. A POST to ``/api/services/{id}/actions``
validates the service, applies the confirm guardrail, registers an in-flight
busy state, and returns ``{accepted, jobId}`` immediately; the compose command
runs in a background task whose final outcome lands in the audit log and clears
the busy registry.

Design notes
------------
Busy registry
    ``_BUSY`` maps ``service_id -> Status`` (``"restarting"`` for restart,
    ``"updating"`` for pull, and briefly ``"restarting"`` for stop/start so the
    UI shows motion). :func:`busy_status` reads it synchronously — it is called
    from :func:`inventory.apply_busy_overlay` on the (sync) inventory-assembly
    path, so it must not await. Presence of a key == an action is in flight for
    that service; that same presence is what rejects a concurrent action (409).

Locking / concurrency
    ``_service_lock(id)`` — one asyncio.Lock per service, so the same service is
    never actioned twice at once. ``_SEMAPHORE`` (value 2) caps *global*
    concurrency so a burst of pulls can't saturate the host. The in-flight
    registry is the user-visible rejection gate; the per-service lock is a
    belt-and-suspenders guard around the subprocess itself.

Mock mode (unchanged from A1)
    When ``MOCK_DATA=1`` the original fixture-transition behaviour is preserved
    verbatim so the frontend busy flow keeps working without a docker socket.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from . import audit, auth, inventory, mock, models, repin

log = logging.getLogger("alpenglow.actions")

router = APIRouter(prefix="/api", tags=["actions"])

# Services whose actions require confirm=true (self-management hazard: caddy
# drops the user's own connection, postgres/this dashboard are critical/self).
GUARDED_SERVICES = {"caddy", "postgres", "alpenglow_dashboard"}

# Global cap on concurrent compose actions across all services.
_MAX_CONCURRENT_ACTIONS = 2

# Subprocess timeouts (seconds). Pulls fetch images and can take minutes.
_TIMEOUT_DEFAULT = 180
_TIMEOUT_PULL = 1800

# action → busy overlay status while it runs.
_BUSY_STATUS: dict[str, models.Status] = {
    "restart": "restarting",
    "start": "restarting",
    "stop": "restarting",
    "pull": "updating",
}


# ── in-flight registry ────────────────────────────────────────────────────────

# service_id → overlay Status while an action is in flight.
_BUSY: dict[str, models.Status] = {}
_BUSY_LOCK = asyncio.Lock()

# service_id → per-service action lock (created lazily).
_SERVICE_LOCKS: dict[str, asyncio.Lock] = {}

_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily create the global semaphore bound to the running loop.

    Created lazily (not at import) so it binds to whatever event loop is active
    — TestClient spins up a fresh loop per request, and a semaphore captured on
    a dead loop would raise.
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ACTIONS)
    return _semaphore


def _service_lock(service_id: str) -> asyncio.Lock:
    lock = _SERVICE_LOCKS.get(service_id)
    if lock is None:
        lock = asyncio.Lock()
        _SERVICE_LOCKS[service_id] = lock
    return lock


def busy_status(service_id: str) -> models.Status | None:
    """In-flight action overlay consulted by inventory (synchronous).

    Returns ``restarting``/``updating`` while an action is in flight for the
    service, else ``None``. Read without the lock: dict reads are atomic under
    the GIL and this is a best-effort UI overlay, not a correctness invariant.
    """
    return _BUSY.get(service_id)


def is_busy(service_id: str) -> bool:
    return service_id in _BUSY


# ── compose command construction ──────────────────────────────────────────────


def _compose_commands(service_id: str, action: str) -> list[list[str]]:
    """The docker-compose argv(s) for an action.

    ``restart|stop|start`` are single commands; ``pull`` is ``pull`` then
    ``up -d`` to recreate with the freshly pulled image.
    """
    base = [
        "docker",
        "compose",
        "--project-directory",
        str(inventory.services_root() / service_id),
    ]
    if action == "restart":
        return [base + ["restart"]]
    if action == "stop":
        return [base + ["stop"]]
    if action == "start":
        return [base + ["up", "-d"]]
    if action == "pull":
        return [base + ["pull"], base + ["up", "-d"]]
    raise ValueError(f"unknown action {action!r}")


async def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run one subprocess; return (returncode, stderr_text).

    Captures stderr for the audit trail on failure; stdout is discarded (compose
    is noisy and we only surface failure detail). A timeout kills the process
    and reports as a non-zero outcome rather than hanging the background task.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s: {' '.join(cmd)}"
    return proc.returncode or 0, (stderr or b"").decode("utf-8", "replace")


async def _run_commands(service_id: str, action: str) -> tuple[bool, str]:
    """Run an action's compose command(s); return (ok, stderr of the failure)."""
    timeout = _TIMEOUT_PULL if action == "pull" else _TIMEOUT_DEFAULT
    for cmd in _compose_commands(service_id, action):
        rc, stderr = await _run(cmd, timeout)
        if rc != 0:
            return False, stderr
    return True, ""


async def _finish(service_id: str, action: str, user: str, job_id: str, ok: bool, detail: str) -> None:
    async with _BUSY_LOCK:
        _BUSY.pop(service_id, None)
    if not ok:
        log.error("%s of %s failed (job %s): %s", action, service_id, job_id, detail)
    await audit.record(
        user, service_id, action, "succeeded" if ok else "failed",
        stderr_tail="" if ok else detail, job_id=job_id,
    )


async def _run_job(service_id: str, action: str, user: str, job_id: str) -> None:
    """Background job for restart/stop/start: run, audit the outcome, clear busy."""
    ok, detail = False, ""
    try:
        async with _get_semaphore(), _service_lock(service_id):
            ok, detail = await _run_commands(service_id, action)
    except Exception as exc:  # never let the background task die unaudited
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        await _finish(service_id, action, user, job_id, ok, detail)


# ── update: repin, then pull & recreate ───────────────────────────────────────

# The dashboard's own service. Recreating it ends this process, so it's
# updated last, after everything else in the same request has finished.
SELF_SERVICE = "alpenglow_dashboard"

Repinned = dict[str, list[tuple[repin.Target, str]]]

# What the Updates view shows for a service whose Update has ended. A service
# stays in _UPDATED until the tracker has rescanned and can say for itself; an
# error stays until that service's next Update.
_UPDATED: set[str] = set()
_UPDATE_ERRORS: dict[str, str] = {}
# Results not yet announced in the update summary.
_UPDATE_RESULTS: list = []
# Update requests still deploying. The last one to finish wraps up.
_updates_in_flight = 0


def update_outcome(service_id: str) -> tuple[bool, Optional[str]]:
    """(finished and awaiting the tracker's rescan, why the last Update failed)."""
    return service_id in _UPDATED, _UPDATE_ERRORS.get(service_id)


async def _wrap_up_if_idle() -> None:
    """Once nothing is deploying, post the summary and rescan.

    A rescan reports whatever is running by the time it reads it, so one at the
    end covers every update before it, however many requests they came from. The
    summary likewise covers all of them, and failures need one even with nothing
    to rescan. Neither may stop the dashboard's own update, which comes after this.
    """
    if _updates_in_flight:
        return
    from .integrations import update_summary, updates  # avoids an import cycle

    results = list(_UPDATE_RESULTS)
    _UPDATE_RESULTS.clear()
    try:
        await update_summary.post(results)
    except Exception:  # noqa: BLE001
        log.exception("update summary failed")
    if not _UPDATED:
        return
    done = set(_UPDATED)
    try:
        await updates.rescan()
    except Exception:  # noqa: BLE001
        log.exception("rescan after update failed")
    finally:
        _UPDATED.difference_update(done)


def _pins_note(done: list[tuple[repin.Target, str]]) -> str:
    return "; ".join(f"versions.yaml now pins {t.name} to {tag} (was {t.current_tag})"
                     for t, tag in done)


async def _repin_all(service_ids: list[str]) -> tuple[Repinned, dict[str, str]]:
    """Repin each service's pending pinned images, one at a time (see :mod:`repin`).

    Returns ``({service: [(target, new tag)]}, {service: why it can't update})``.
    Services whose images float appear in neither and are just pulled. When one
    of a service's images fails to repin, the service isn't pulled, and any of
    its images already repinned stay so (noted in the reason).
    """
    repinned: Repinned = {}
    failed: dict[str, str] = {}
    async with repin.repo_lock():
        for sid in service_ids:
            done: list[tuple[repin.Target, str]] = []
            try:
                for t in await repin.targets(sid):
                    done.append((t, await repin.pin(t)))
            except repin.RepinError as err:
                failed[sid] = str(err)
            except Exception as exc:  # unreadable versions.yaml or tracker: don't guess
                failed[sid] = f"couldn't work out the new version: {type(exc).__name__}: {exc}"
            if sid in failed and done:
                failed[sid] += f"\n[update] {_pins_note(done)}; not rolled back"
            elif done:
                repinned[sid] = done
    return repinned, failed


async def _deploy(service_id: str, repinned: Repinned) -> tuple[bool, str]:
    """Pull & recreate. A failure is reported, never rolled back.

    Going back to the old version isn't always safe (a new release may already
    have migrated its database), so a failed upgrade leaves the new pin in
    place for a person to look at.
    """
    ok, stderr = await _run_commands(service_id, "pull")
    if ok or service_id not in repinned:
        return ok, stderr
    return False, f"{stderr}\n[update] {_pins_note(repinned[service_id])}; not rolled back"


async def _update_job(jobs: list[tuple[str, str]], user: str) -> None:
    """Background job for Update on one or more services.

    1. Repin every pinned image among them, one at a time, so concurrent edits
       of ``versions.yaml`` never interleave.
    2. Pull & recreate each, in parallel up to the global cap. A failure only
       affects its own service, and is not rolled back (see :func:`_deploy`).

    Each service gets its own audit line and leaves the busy registry when its
    own deploy ends. When the last request in flight has finished, the summary
    goes out and the tracker rescans, so the updated rows clear together.
    """
    from .integrations import update_summary, updates  # avoids an import cycle

    global _updates_in_flight
    _updates_in_flight += 1
    for sid, _ in jobs:
        _UPDATED.discard(sid)
        _UPDATE_ERRORS.pop(sid, None)
    try:
        versions = await updates.pending_versions()  # read before they change
    except Exception:  # noqa: BLE001 - only the summary needs them
        log.exception("couldn't read pending versions")
        versions = {}
    try:
        repinned, failed = await _repin_all([sid for sid, _ in jobs])
    except Exception as exc:  # noqa: BLE001 - audit every job whatever happens
        repinned, failed = {}, {sid: f"{type(exc).__name__}: {exc}" for sid, _ in jobs}

    async def one(sid: str, job_id: str) -> None:
        ok, detail = False, failed.get(sid, "")
        try:
            if sid not in failed:
                async with _get_semaphore(), _service_lock(sid):
                    ok, detail = await _deploy(sid, repinned)
        except Exception as exc:  # never let the background task die unaudited
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            if ok:
                _UPDATED.add(sid)
            else:
                _UPDATE_ERRORS[sid] = detail[-500:] or "Update failed"
            _UPDATE_RESULTS.append(update_summary.Result(
                service=sid, name=updates.display_name(sid), user=user, ok=ok,
                detail=detail, versions=versions.get(sid, []),
            ))
            await _finish(sid, "pull", user, job_id, ok, detail)

    await asyncio.gather(*(one(sid, job) for sid, job in jobs if sid != SELF_SERVICE))
    _updates_in_flight -= 1
    # Before recreating ourselves, which usually ends this process.
    await _wrap_up_if_idle()
    for sid, job in jobs:
        if sid == SELF_SERVICE:
            await one(sid, job)
            await _wrap_up_if_idle()


# ── mock transition (unchanged from A1) ───────────────────────────────────────


async def _mock_transition(service_id: str, action: str) -> None:
    """Briefly show restarting/updating, then settle, like the prototype."""
    svc = mock.MOCK_SERVICES[service_id]
    if action == "stop":
        svc["status"] = "down"
        svc["uptime"] = None
        return
    if action == "start":
        svc["status"] = "up"
        svc["uptime"] = 60
        return
    svc["status"] = "restarting" if action == "restart" else "updating"
    await asyncio.sleep(2.0)
    svc["status"] = "up"
    svc["uptime"] = 60
    if action == "pull" and svc.get("latest"):
        svc["updated"] = True  # as the real tracker's rescan takes a few seconds
        await asyncio.sleep(3.0)
        svc["updated"] = False
        svc["version"] = svc["latest"]
        svc["latest"] = None
        svc["released"] = None


# ── route ─────────────────────────────────────────────────────────────────────


def _request_user(request: Request) -> str:
    identity: auth.Identity | None = getattr(request.state, "identity", None)
    return identity.user if identity else "unknown"


@router.post("/services/{service_id}/actions")
async def post_action(
    request: Request, service_id: str, payload: models.ActionRequest
) -> models.ActionResponse:
    if mock.mock_enabled():
        if service_id not in mock.MOCK_SERVICES:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        if service_id in GUARDED_SERVICES and not payload.confirm:
            raise HTTPException(
                status_code=409,
                detail=f"action on '{service_id}' requires confirm=true",
            )
        asyncio.get_running_loop().create_task(_mock_transition(service_id, payload.action))
        return mock.mock_action(service_id, payload.action)

    action = payload.action
    user = _request_user(request)

    # validate the service against the real repo scan (reuse inventory's helper).
    if service_id not in inventory.scan_repo():
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")

    # guardrail: high-blast-radius services need explicit confirm.
    if service_id in GUARDED_SERVICES and not payload.confirm:
        raise HTTPException(
            status_code=409,
            detail=f"action on '{service_id}' requires confirm=true",
        )

    job_id = await _register(service_id, action, user)
    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"an action is already in flight for '{service_id}'",
        )
    loop = asyncio.get_running_loop()
    if action == "pull":
        loop.create_task(_update_job([(service_id, job_id)], user))
    else:
        loop.create_task(_run_job(service_id, action, user, job_id))
    return models.ActionResponse(accepted=True, jobId=job_id)


async def _register(service_id: str, action: str, user: str) -> str | None:
    """Mark the service busy and audit the acceptance; ``None`` if it's already busy."""
    job_id = f"{action}-{service_id}-{uuid.uuid4().hex[:12]}"
    async with _BUSY_LOCK:
        if service_id in _BUSY:
            busy = True
        else:
            busy = False
            _BUSY[service_id] = _BUSY_STATUS.get(action, "restarting")
    if busy:
        await audit.record(
            user, service_id, action, "rejected",
            stderr_tail="action already in flight", job_id=job_id,
        )
        return None
    await audit.record(user, service_id, action, "accepted", job_id=job_id)
    return job_id


@router.post("/updates/apply")
async def post_update_batch(
    request: Request, payload: models.UpdateBatchRequest
) -> models.UpdateBatchResponse:
    """Update several services at once: repin them all, then pull & recreate.

    Services that are unknown or already busy are listed under ``rejected``;
    the rest start. A guarded service without ``confirm`` refuses the whole
    request (409), like the single-service action.
    """
    ids = list(dict.fromkeys(payload.services))
    guarded = sorted(s for s in ids if s in GUARDED_SERVICES)
    if guarded and not payload.confirm:
        raise HTTPException(
            status_code=409,
            detail=f"updating {', '.join(guarded)} requires confirm=true",
        )

    jobs: list[models.UpdateJob] = []
    rejected: list[models.UpdateRejected] = []

    if mock.mock_enabled():
        for sid in ids:
            if sid not in mock.MOCK_SERVICES:
                rejected.append(models.UpdateRejected(service=sid, reason="unknown service"))
                continue
            asyncio.get_running_loop().create_task(_mock_transition(sid, "pull"))
            jobs.append(models.UpdateJob(service=sid, jobId=mock.mock_action(sid, "pull").jobId))
        return models.UpdateBatchResponse(jobs=jobs, rejected=rejected)

    user = _request_user(request)
    known = inventory.scan_repo()
    started: list[tuple[str, str]] = []
    for sid in ids:
        if sid not in known:
            rejected.append(models.UpdateRejected(service=sid, reason="unknown service"))
            continue
        job_id = await _register(sid, "pull", user)
        if job_id is None:
            rejected.append(models.UpdateRejected(service=sid, reason="an action is already in flight"))
            continue
        started.append((sid, job_id))
        jobs.append(models.UpdateJob(service=sid, jobId=job_id))
    if started:
        asyncio.get_running_loop().create_task(_update_job(started, user))
    return models.UpdateBatchResponse(jobs=jobs, rejected=rejected)
