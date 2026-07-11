"""JSON per-service settings store (work packages B1 + F1).

A JSON file at ``/data-store/settings.json`` (path from ``SETTINGS_PATH`` env,
that default) holds per-service settings keyed by service id::

    {"immich": {"tags": ["GPU", "User data"], "category": "Media"}, ...}

``category`` is optional (``null`` / absent = no override → fall back to the
metadata.yaml default, then "Infrastructure"). All reads and read-modify-writes
go through a single module-level ``asyncio.Lock`` so concurrent PUTs cannot
clobber each other. The file is seeded empty on first write.

**Migration (F1):** the store was formerly ``tags.json`` holding the flat shape
``{id: [tag, ...]}``. On first read, if ``settings.json`` is absent but a legacy
``tags.json`` exists next to it (or at ``TAGS_PATH``), its contents are migrated
losslessly into the new shape (``{id: {"tags": [...], "category": null}}``). The
legacy file is left untouched; the new file is written on the next mutation.

In MOCK_DATA=1 mode the store is bypassed and settings live in the fixture table
so the UI still round-trips exactly as A1 left it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional, TypedDict

from fastapi import APIRouter, HTTPException

from . import mock, models

router = APIRouter(prefix="/api", tags=["settings"])

_lock = asyncio.Lock()


class ServiceSettings(TypedDict):
    tags: list[str]
    category: Optional[str]


def settings_path() -> Path:
    """The settings store path. ``SETTINGS_PATH`` wins; else the legacy
    ``TAGS_PATH`` dir is reused with the ``settings.json`` name; else the
    documented default under ``/data-store``."""
    override = os.environ.get("SETTINGS_PATH")
    if override:
        return Path(override)
    legacy = os.environ.get("TAGS_PATH")
    if legacy:
        return Path(legacy).with_name("settings.json")
    return Path("/data-store/settings.json")


def _legacy_tags_path() -> Path:
    """Where the pre-F1 flat ``tags.json`` would live (for one-time migration)."""
    legacy = os.environ.get("TAGS_PATH")
    if legacy:
        return Path(legacy)
    return settings_path().with_name("tags.json")


def _coerce_tags(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(t) for t in value]
    return []


def _coerce_entry(value: object) -> ServiceSettings:
    """Normalise one stored value into ``{tags, category}``.

    Accepts both the new dict shape and the legacy bare-list shape so a
    half-migrated file (or a hand-edit) still reads cleanly.
    """
    if isinstance(value, list):  # legacy flat shape: [tag, ...]
        return {"tags": _coerce_tags(value), "category": None}
    if isinstance(value, dict):
        cat = value.get("category")
        return {
            "tags": _coerce_tags(value.get("tags")),
            "category": str(cat) if isinstance(cat, str) and cat.strip() else None,
        }
    return {"tags": [], "category": None}


def _read_unlocked() -> dict[str, ServiceSettings]:
    path = settings_path()
    raw: object = None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # No settings.json yet → attempt a one-time migration from legacy tags.json.
        raw = _read_legacy()
    if not isinstance(raw, dict):
        return {}
    return {str(k): _coerce_entry(v) for k, v in raw.items()}


def _read_legacy() -> Optional[dict]:
    """Read the legacy flat ``tags.json`` for migration, or ``None``."""
    legacy = _legacy_tags_path()
    if legacy == settings_path():
        return None
    try:
        raw = json.loads(legacy.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_unlocked(data: dict[str, ServiceSettings]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Drop empty entries so the file stays tidy (no {tags:[], category:null}).
    pruned = {
        sid: entry
        for sid, entry in data.items()
        if entry.get("tags") or entry.get("category")
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pruned, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic within the same filesystem


# ── read helpers used by inventory ────────────────────────────────────────────


async def all_settings() -> dict[str, ServiceSettings]:
    """Return the full ``{service_id: {tags, category}}`` map (empty if unset)."""
    async with _lock:
        return _read_unlocked()


async def all_tags() -> dict[str, list[str]]:
    """Back-compat: the tag map, for inventory assembly."""
    return {sid: s["tags"] for sid, s in (await all_settings()).items() if s["tags"]}


async def get_tags(service_id: str) -> list[str]:
    async with _lock:
        return _read_unlocked().get(service_id, {"tags": [], "category": None})["tags"]


async def get_category(service_id: str) -> Optional[str]:
    async with _lock:
        entry = _read_unlocked().get(service_id)
        return entry["category"] if entry else None


# ── mutations ─────────────────────────────────────────────────────────────────


def _clean_tags(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in tags if t.strip()))


async def set_tags(service_id: str, tags: list[str]) -> list[str]:
    """Atomic read-modify-write of one service's tags. Returns the stored list."""
    cleaned = _clean_tags(tags)
    async with _lock:
        data = _read_unlocked()
        entry = data.get(service_id, {"tags": [], "category": None})
        entry["tags"] = cleaned
        data[service_id] = entry
        _write_unlocked(data)
    return cleaned


async def set_category(service_id: str, category: Optional[str]) -> Optional[str]:
    """Set (or clear, with ``None``) one service's category override."""
    cat = category.strip() if isinstance(category, str) else None
    cat = cat or None
    async with _lock:
        data = _read_unlocked()
        entry = data.get(service_id, {"tags": [], "category": None})
        entry["category"] = cat
        data[service_id] = entry
        _write_unlocked(data)
    return cat


async def update_settings(
    service_id: str,
    *,
    tags: Optional[list[str]] = None,
    category: Optional[str] = None,
    set_category_field: bool = False,
) -> ServiceSettings:
    """Partial update: apply ``tags`` if given, apply ``category`` only when
    ``set_category_field`` is True (distinguishing "omitted" from "null")."""
    async with _lock:
        data = _read_unlocked()
        entry = data.get(service_id, {"tags": [], "category": None})
        if tags is not None:
            entry["tags"] = _clean_tags(tags)
        if set_category_field:
            cat = category.strip() if isinstance(category, str) else None
            entry["category"] = cat or None
        data[service_id] = entry
        _write_unlocked(data)
        return dict(entry)  # type: ignore[return-value]


# ── route ─────────────────────────────────────────────────────────────────────


@router.put("/services/{service_id}/settings")
async def put_settings(
    service_id: str, payload: models.SettingsRequest
) -> models.SettingsPayload:
    category_set = "category" in payload.model_fields_set

    if mock.mock_enabled():
        svc = mock.MOCK_SERVICES.get(service_id)
        if svc is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        if payload.tags is not None:
            svc["tags"] = _clean_tags(payload.tags)
        if category_set:
            svc["cat"] = (payload.category or "").strip() or svc.get("cat")
            svc["category_override"] = (payload.category or "").strip() or None
        return models.SettingsPayload(
            tags=list(svc["tags"]),
            category=svc.get("category_override"),
        )

    from . import inventory

    if not (inventory.services_root() / service_id / "compose.yaml").is_file():
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    entry = await update_settings(
        service_id,
        tags=payload.tags,
        category=payload.category,
        set_category_field=category_set,
    )
    return models.SettingsPayload(tags=entry["tags"], category=entry["category"])
