"""Compose action runner + busy-state registry (work package B2).

A1 stub: in mock mode actions are accepted and flip the fixture status through
restarting/updating so the frontend busy flow can be exercised; B2 replaces
this with the real `docker compose` subprocess runner, per-service locks,
guardrails (confirm for caddy/postgres/alpenglow_dashboard) and audit logging.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from . import mock, models

router = APIRouter(prefix="/api", tags=["actions"])

# Services whose actions require confirm=true (mirrored by B2's real guardrail).
GUARDED_SERVICES = {"caddy", "postgres", "alpenglow_dashboard"}


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


@router.post("/services/{service_id}/actions")
async def post_action(service_id: str, payload: models.ActionRequest) -> models.ActionResponse:
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
    raise HTTPException(status_code=501, detail="action engine not implemented yet (B2); run with MOCK_DATA=1")
