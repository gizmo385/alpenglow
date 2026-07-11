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
import os
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from . import audit, auth, inventory, mock, models

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


async def _run_job(service_id: str, action: str, user: str, job_id: str) -> None:
    """Background job: run the compose command(s), audit the outcome, clear busy."""
    timeout = _TIMEOUT_PULL if action == "pull" else _TIMEOUT_DEFAULT
    outcome = "succeeded"
    stderr_tail = ""
    try:
        async with _get_semaphore(), _service_lock(service_id):
            for cmd in _compose_commands(service_id, action):
                rc, stderr = await _run(cmd, timeout)
                if rc != 0:
                    outcome = "failed"
                    stderr_tail = stderr
                    break
    except Exception as exc:  # never let the background task die unaudited
        outcome = "failed"
        stderr_tail = f"{type(exc).__name__}: {exc}"
    finally:
        async with _BUSY_LOCK:
            _BUSY.pop(service_id, None)
        await audit.record(
            user, service_id, action, outcome, stderr_tail=stderr_tail, job_id=job_id
        )


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
    if action == "pull" and svc.get("latest"):
        svc["version"] = svc["latest"]
        svc["latest"] = None
        svc["released"] = None
    svc["status"] = "up"
    svc["uptime"] = 60


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
    known = inventory.scan_repo()
    if service_id not in known:
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")

    # guardrail: high-blast-radius services need explicit confirm.
    if service_id in GUARDED_SERVICES and not payload.confirm:
        raise HTTPException(
            status_code=409,
            detail=f"action on '{service_id}' requires confirm=true",
        )

    job_id = f"{action}-{service_id}-{uuid.uuid4().hex[:12]}"

    # reject if an action is already in flight for this service (register atomically).
    async with _BUSY_LOCK:
        if service_id in _BUSY:
            await audit.record(
                user, service_id, action, "rejected",
                stderr_tail="action already in flight", job_id=job_id,
            )
            raise HTTPException(
                status_code=409,
                detail=f"an action is already in flight for '{service_id}'",
            )
        _BUSY[service_id] = _BUSY_STATUS.get(action, "restarting")

    await audit.record(user, service_id, action, "accepted", job_id=job_id)
    asyncio.get_running_loop().create_task(_run_job(service_id, action, user, job_id))
    return models.ActionResponse(accepted=True, jobId=job_id)
