"""image-updates-tracker client + routes (work package B4, upstream D1).

A1 stub: routes serve mock fixtures under MOCK_DATA=1. B4 implements the real
client for the tracker's GET /api/updates (added in D1), image-ref matching to
services, the trackerUnavailable degradation, and the POST refresh proxy.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import mock, models

router = APIRouter(prefix="/api", tags=["updates"])


@router.get("/updates")
async def get_updates() -> models.Updates:
    if mock.mock_enabled():
        return mock.mock_updates()
    raise HTTPException(status_code=501, detail="updates integration not implemented yet (B4); run with MOCK_DATA=1")


@router.post("/updates/refresh")
async def refresh_updates() -> models.Updates:
    if mock.mock_enabled():
        result = mock.mock_updates()
        result.refreshing = True
        return result
    raise HTTPException(status_code=501, detail="updates integration not implemented yet (B4); run with MOCK_DATA=1")
