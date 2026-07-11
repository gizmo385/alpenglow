"""SSE log streaming + stats ring buffers (work package B3).

A1 stub: mock mode serves a canned log tail as a short SSE stream (with
heartbeats) and synthetic stats history; B3 replaces this with `docker logs -f`
multiplexing and a background stats poller.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from . import mock, models

router = APIRouter(prefix="/api", tags=["streams"])


@router.get("/services/{service_id}/stats")
async def get_stats(service_id: str) -> models.Stats:
    if mock.mock_enabled():
        stats = mock.mock_stats(service_id)
        if stats is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return stats
    raise HTTPException(status_code=501, detail="stats poller not implemented yet (B3); run with MOCK_DATA=1")


async def _mock_log_events(service_id: str) -> AsyncIterator[dict]:
    for line in mock.mock_log_lines(service_id):
        yield {"event": "log", "data": line}
        await asyncio.sleep(0.05)
    # keep the connection open with heartbeats, like the real stream will
    for _ in range(3):
        await asyncio.sleep(5)
        yield {"comment": "heartbeat"}


@router.get("/services/{service_id}/logs")
async def stream_logs(service_id: str) -> EventSourceResponse:
    if mock.mock_enabled():
        if service_id not in mock.MOCK_SERVICES:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return EventSourceResponse(_mock_log_events(service_id))
    raise HTTPException(status_code=501, detail="log streaming not implemented yet (B3); run with MOCK_DATA=1")
