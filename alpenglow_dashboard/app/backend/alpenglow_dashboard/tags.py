"""JSON tag store (work package B1).

A JSON file at ``/data-store/tags.json`` (path from ``TAGS_PATH`` env, that
default) holds ``{service_id: [tag, ...]}``. All reads and read-modify-writes go
through a single module-level ``asyncio.Lock`` so concurrent PUTs cannot clobber
each other. The file is seeded empty on first write.

In MOCK_DATA=1 mode the store is bypassed and tags live in the fixture table so
the UI still round-trips exactly as A1 left it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import mock, models

router = APIRouter(prefix="/api", tags=["tags"])

_lock = asyncio.Lock()


def tags_path() -> Path:
    return Path(os.environ.get("TAGS_PATH", "/data-store/tags.json"))


def _read_unlocked() -> dict[str, list[str]]:
    path = tags_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # coerce to {str: [str]}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(t) for t in v]
    return out


def _write_unlocked(data: dict[str, list[str]]) -> None:
    path = tags_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic within the same filesystem


async def all_tags() -> dict[str, list[str]]:
    """Return the full ``{service_id: [tag, ...]}`` map (empty if unset)."""
    async with _lock:
        return _read_unlocked()


async def get_tags(service_id: str) -> list[str]:
    async with _lock:
        return _read_unlocked().get(service_id, [])


async def set_tags(service_id: str, tags: list[str]) -> list[str]:
    """Atomic read-modify-write of one service's tags. Returns the stored list."""
    cleaned = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
    async with _lock:
        data = _read_unlocked()
        if cleaned:
            data[service_id] = cleaned
        else:
            data.pop(service_id, None)
        _write_unlocked(data)
    return cleaned


@router.put("/services/{service_id}/tags")
async def put_tags(service_id: str, payload: models.TagsPayload) -> models.TagsPayload:
    if mock.mock_enabled():
        svc = mock.MOCK_SERVICES.get(service_id)
        if svc is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        svc["tags"] = list(dict.fromkeys(t.strip() for t in payload.tags if t.strip()))
        return models.TagsPayload(tags=svc["tags"])

    # Non-mock: validate the service exists before persisting tags for it.
    from . import inventory

    if not (inventory.services_root() / service_id / "compose.yaml").is_file():
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    stored = await set_tags(service_id, payload.tags)
    return models.TagsPayload(tags=stored)
