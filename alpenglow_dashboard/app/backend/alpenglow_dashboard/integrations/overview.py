"""Composed /api/overview route (work package B4).

A1 stub: serves the mock overview under MOCK_DATA=1. B4 composes the real
response from glances + kuma + zfs + updates + B1 inventory counts, degrading
each failing integration to nulls (never 500s).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import mock, models

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
async def get_overview() -> models.Overview:
    if mock.mock_enabled():
        return mock.mock_overview()
    raise HTTPException(status_code=501, detail="overview not implemented yet (B4); run with MOCK_DATA=1")
