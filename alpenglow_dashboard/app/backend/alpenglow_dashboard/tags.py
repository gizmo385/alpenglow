"""JSON tag store (work package B1).

A1 stub: in mock mode tags are held in-process (mutating the fixture table) so
the UI round-trips; B1 adds the /data-store/tags.json store with an asyncio lock.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import mock, models

router = APIRouter(prefix="/api", tags=["tags"])


@router.put("/services/{service_id}/tags")
async def put_tags(service_id: str, payload: models.TagsPayload) -> models.TagsPayload:
    if mock.mock_enabled():
        svc = mock.MOCK_SERVICES.get(service_id)
        if svc is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        svc["tags"] = list(dict.fromkeys(t.strip() for t in payload.tags if t.strip()))
        return models.TagsPayload(tags=svc["tags"])
    raise HTTPException(status_code=501, detail="tag store not implemented yet (B1); run with MOCK_DATA=1")
