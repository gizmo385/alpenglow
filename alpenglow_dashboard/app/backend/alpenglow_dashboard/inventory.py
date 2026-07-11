"""Service inventory: repo scan + docker inventory merge (work package B1).

A1 provides route stubs backed by mock fixtures (MOCK_DATA=1). B1 replaces the
non-mock paths with: /services/*/compose.yaml scanning (caddy labels → url/tier,
restart policy, ports, mem limits), docker-socket container state grouped by
com.docker.compose.project, and metadata.yaml merging.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from . import mock, models

router = APIRouter(prefix="/api", tags=["inventory"])


@router.get("/services")
async def list_services() -> list[models.ServiceSummary]:
    if mock.mock_enabled():
        return mock.mock_services()
    raise HTTPException(status_code=501, detail="inventory not implemented yet (B1); run with MOCK_DATA=1")


@router.get("/services/{service_id}")
async def get_service(service_id: str) -> models.ServiceDetail:
    if mock.mock_enabled():
        detail = mock.mock_service_detail(service_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return detail
    raise HTTPException(status_code=501, detail="inventory not implemented yet (B1); run with MOCK_DATA=1")


@router.get("/services/{service_id}/compose", response_class=PlainTextResponse)
async def get_compose(service_id: str) -> str:
    if mock.mock_enabled():
        text = mock.mock_compose_text(service_id)
        if text is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return text
    raise HTTPException(status_code=501, detail="inventory not implemented yet (B1); run with MOCK_DATA=1")
