"""Category metadata store + management surface (work package G1).

Sidebar categories become a management surface: rename, pick an icon, and
reorder. This module owns a **sibling** persistent file to the per-service
settings store::

    /data-store/categories.json   (path from CATEGORIES_PATH, that default)

keyed by category name::

    {"Media": {"icon": "play-circle", "order": 0}, "Fun": {"order": 5}, ...}

``icon`` / ``order`` are both optional. A category with metadata but zero
members is still surfaced by ``GET /api/categories`` so its icon/order survive
while empty. The service→category mapping itself lives in the per-service
settings store (:mod:`.tags`); this file only carries presentation metadata.

**Category rename** is the atomic bulk mutation: it rewrites the ``category``
field of every member service in the settings store AND carries this file's
metadata from the old name to the new one, then audit-logs the change. The two
writes each go through their own store's asyncio lock; the settings rewrite is
the operator-visible part (a single atomic file replace moves every member at
once), and the metadata carry-over follows.

Ordering consumed by the sidebar/overview: stored ``order`` first (ascending),
then the known-five defaults, then alphabetical. Icon fallback chain: stored →
known-five map → ``ph-stack``.

MOCK_DATA=1 keeps working: the store is bypassed and the metadata lives in a
process-local dict seeded from the known defaults so the UI round-trips.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional, TypedDict

from fastapi import APIRouter, HTTPException, Request

from . import audit, mock, models, tags as settings_store

router = APIRouter(prefix="/api", tags=["categories"])

_lock = asyncio.Lock()

# The five known categories: fixed default order + icon (mirrors the frontend
# CATEGORY_ORDER / CATEGORY_ICON so sidebar and backend agree on defaults).
KNOWN_ORDER = ["Media", "Productivity", "Home", "Infrastructure", "Monitoring"]
KNOWN_ICON = {
    "Media": "play-circle",
    "Productivity": "briefcase",
    "Home": "house-line",
    "Infrastructure": "stack",
    "Monitoring": "pulse",
}
ICON_FALLBACK = "stack"


class CategoryMeta(TypedDict, total=False):
    icon: str
    order: int


def categories_path() -> Path:
    override = os.environ.get("CATEGORIES_PATH")
    if override:
        return Path(override)
    # sit next to the settings store by default
    return settings_store.settings_path().with_name("categories.json")


# ── process-local mock metadata (bypasses the file in MOCK_DATA=1) ─────────────

_MOCK_META: dict[str, CategoryMeta] = {}


# ── file read/write ───────────────────────────────────────────────────────────


def _read_unlocked() -> dict[str, CategoryMeta]:
    path = categories_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, CategoryMeta] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        entry: CategoryMeta = {}
        icon = value.get("icon")
        if isinstance(icon, str) and icon.strip():
            entry["icon"] = icon.strip()
        order = value.get("order")
        if isinstance(order, int):
            entry["order"] = order
        out[str(name)] = entry
    return out


def _write_unlocked(data: dict[str, CategoryMeta]) -> None:
    path = categories_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pruned = {name: entry for name, entry in data.items() if entry}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pruned, indent=2, sort_keys=True))
    tmp.replace(path)


async def all_meta() -> dict[str, CategoryMeta]:
    if mock.mock_enabled():
        return {k: dict(v) for k, v in _MOCK_META.items()}  # type: ignore[misc]
    async with _lock:
        return _read_unlocked()


# ── ordering / icon resolution ────────────────────────────────────────────────


def order_key(name: str, meta: dict[str, CategoryMeta]) -> tuple[int, int, str]:
    """Sort key implementing: stored order → known-five defaults → alphabetical.

    Buckets: (0, stored_order, name) for categories with an explicit order;
    (1, known_index, name) for the known five without a stored order;
    (2, 0, name) for everything else (alphabetical within the bucket).
    """
    entry = meta.get(name, {})
    if "order" in entry:
        return (0, entry["order"], name.casefold())
    if name in KNOWN_ORDER:
        return (1, KNOWN_ORDER.index(name), name.casefold())
    return (2, 0, name.casefold())


def icon_for(name: str, meta: dict[str, CategoryMeta]) -> str:
    entry = meta.get(name, {})
    if "icon" in entry:
        return entry["icon"]
    return KNOWN_ICON.get(name, ICON_FALLBACK)


def build_category_infos(
    counts: dict[str, int], meta: dict[str, CategoryMeta]
) -> list[models.CategoryInfo]:
    """Merge live member counts with stored metadata into ordered CategoryInfo.

    Every category that has members OR carries metadata is listed (so an empty
    category's icon/order survives). ``order`` in the payload is the resolved
    0-based position after sorting.
    """
    names = set(counts) | set(meta)
    ordered = sorted(names, key=lambda n: order_key(n, meta))
    return [
        models.CategoryInfo(
            name=name,
            icon=icon_for(name, meta),
            order=i,
            count=counts.get(name, 0),
        )
        for i, name in enumerate(ordered)
    ]


# ── live member counts ────────────────────────────────────────────────────────


async def _category_counts() -> dict[str, int]:
    """Effective-category member counts across the live inventory."""
    if mock.mock_enabled():
        counts: dict[str, int] = {}
        for svc in mock.MOCK_SERVICES.values():
            cat = svc.get("category_override") or svc.get("cat") or "Infrastructure"
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    from . import inventory

    repos = inventory.scan_repo()
    known_ids = set(repos)
    meta_all = inventory.load_metadata()
    settings_all = await settings_store.all_settings(known_ids=known_ids)
    counts = {}
    for sid in repos:
        override = settings_all.get(sid, {}).get("category")
        meta = meta_all.get(sid, {})
        cat = inventory.resolve_category(meta, override)
        counts[cat] = counts.get(cat, 0) + 1
    return counts


# ── mutations ─────────────────────────────────────────────────────────────────


async def set_icon(name: str, icon: str) -> None:
    icon = icon.strip()
    if mock.mock_enabled():
        entry = _MOCK_META.setdefault(name, {})
        entry["icon"] = icon
        return
    async with _lock:
        data = _read_unlocked()
        entry = data.get(name, {})
        entry["icon"] = icon
        data[name] = entry
        _write_unlocked(data)


async def set_order(order: list[str]) -> None:
    """Assign explicit ``order`` (0-based) to each named category, in sequence.

    Names not listed keep whatever order they had (or fall back to the default
    buckets). This lets the sidebar send a full top-to-bottom ordering.
    """
    if mock.mock_enabled():
        for i, name in enumerate(order):
            _MOCK_META.setdefault(name, {})["order"] = i
        return
    async with _lock:
        data = _read_unlocked()
        for i, name in enumerate(order):
            entry = data.get(name, {})
            entry["order"] = i
            data[name] = entry
        _write_unlocked(data)


async def rename_category(old: str, new: str) -> list[str]:
    """Rename a category everywhere: rewrite every member service's stored
    category to ``new`` AND carry this file's metadata from ``old`` to ``new``.

    Returns the ids of the member services that were moved. Atomic per store:
    the settings rewrite is a single atomic file replace (all members move at
    once); the metadata carry-over follows under this module's lock.
    """
    old = old.strip()
    new = new.strip()
    if not old or not new or old == new:
        return []

    if mock.mock_enabled():
        moved = []
        for sid, svc in mock.MOCK_SERVICES.items():
            eff = svc.get("category_override") or svc.get("cat")
            if eff == old:
                svc["cat"] = new
                svc["category_override"] = new
                moved.append(sid)
        if old in _MOCK_META:
            _MOCK_META[new] = {**_MOCK_META.pop(old), **_MOCK_META.get(new, {})}
        return moved

    # 1) find EVERY member service (explicit override OR metadata.yaml default
    #    resolving to `old`), then write an explicit override = `new` on all of
    #    them in a single atomic file replace.
    from . import inventory

    repos = inventory.scan_repo()
    known_ids = set(repos)
    meta_all = inventory.load_metadata()
    settings_all = await settings_store.all_settings(known_ids=known_ids)
    members = [
        sid
        for sid in repos
        if inventory.resolve_category(
            meta_all.get(sid, {}), settings_all.get(sid, {}).get("category")
        )
        == old
    ]
    moved = await settings_store.set_category_for_ids(members, new)

    # 2) carry metadata from old → new (preserve new's own icon/order if set).
    async with _lock:
        data = _read_unlocked()
        if old in data:
            carried = data.pop(old)
            data[new] = {**carried, **data.get(new, {})}
            _write_unlocked(data)

    return moved


# ── routes ────────────────────────────────────────────────────────────────────


def _request_user(request: Request) -> str:
    identity = getattr(request.state, "identity", None)
    return identity.user if identity else "unknown"


@router.get("/categories")
async def list_categories() -> list[models.CategoryInfo]:
    counts = await _category_counts()
    meta = await all_meta()
    return build_category_infos(counts, meta)


@router.post("/categories/rename")
async def rename_category_route(
    request: Request, payload: models.CategoryRenameRequest
) -> models.TagMutationResult:
    moved = await rename_category(payload.name, payload.to)
    if not mock.mock_enabled():
        await audit.record(
            _request_user(request), "*", "category-rename", "succeeded",
            stderr_tail=f"{payload.name!r} → {payload.to!r} moved {len(moved)} services",
        )
    return models.TagMutationResult(changed=moved)


@router.post("/categories/icon")
async def set_icon_route(
    request: Request, payload: models.CategoryIconRequest
) -> models.CategoryIconRequest:
    await set_icon(payload.name, payload.icon)
    return payload


@router.post("/categories/order")
async def set_order_route(
    request: Request, payload: models.CategoryOrderRequest
) -> models.CategoryOrderRequest:
    await set_order(payload.order)
    return payload
