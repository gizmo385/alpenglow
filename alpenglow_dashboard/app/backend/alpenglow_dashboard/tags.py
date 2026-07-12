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

from fastapi import APIRouter, HTTPException, Request

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


def _normalize_tag(tag: str) -> str:
    """Collapse internal whitespace runs and strip ends.

    A store-level normalization (G1): guards against case/whitespace
    near-duplicate tags (" Critical" vs "Critical ", "Keycloak  SSO") entering
    the store and surfacing as phantom duplicate filter chips. Case is
    preserved (tags are display strings), only whitespace is normalized.
    """
    return " ".join(str(tag).split())


def _coerce_tags(value: object) -> list[str]:
    if isinstance(value, list):
        # normalize + de-dupe preserving order (case-insensitive de-dupe so
        # "Critical"/"critical" don't both survive a hand-edit).
        out: list[str] = []
        seen: set[str] = set()
        for t in value:
            norm = _normalize_tag(t)
            if not norm:
                continue
            key = norm.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
        return out
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


async def all_settings(
    known_ids: Optional[set[str]] = None,
) -> dict[str, ServiceSettings]:
    """Return the full ``{service_id: {tags, category}}`` map (empty if unset).

    When ``known_ids`` is given, entries for service ids NOT in that set are
    pruned from the returned map (G1 store-level defense): a service directory
    that no longer exists must never contribute phantom tags/categories to the
    inventory, the tag-filter union, or the categories endpoint. The on-disk
    file is left as-is here — :func:`prune_unknown` rewrites it — so a read is
    always side-effect-free.
    """
    async with _lock:
        data = _read_unlocked()
    if known_ids is None:
        return data
    return {sid: entry for sid, entry in data.items() if sid in known_ids}


async def prune_unknown(known_ids: set[str]) -> list[str]:
    """Physically drop store entries whose service id is not in ``known_ids``.

    Returns the pruned ids. Used at startup and by the tag/category management
    surfaces to keep the persisted file honest.
    """
    async with _lock:
        data = _read_unlocked()
        stale = [sid for sid in data if sid not in known_ids]
        if stale:
            for sid in stale:
                del data[sid]
            _write_unlocked(data)
    return stale


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
    # Same normalization/de-dupe path as reads, so what goes in matches what
    # comes back out (no whitespace/case near-duplicates in the store).
    return _coerce_tags(tags)


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


# ── cross-service tag mutations (G1) ──────────────────────────────────────────


async def rename_tag(old: str, new: str) -> list[str]:
    """Rename a tag across every service that carries it. Atomic, single write.

    Returns the ids of services whose tag list changed. If a service already
    carries ``new`` (case-insensitively) the rename de-dupes rather than
    doubling. A no-op (old absent everywhere, or old == new) returns ``[]``.
    """
    old_n = _normalize_tag(old)
    new_n = _normalize_tag(new)
    if not old_n or not new_n:
        return []
    old_key = old_n.casefold()
    changed: list[str] = []
    async with _lock:
        data = _read_unlocked()
        for sid, entry in data.items():
            tags = entry["tags"]
            if not any(t.casefold() == old_key for t in tags):
                continue
            # replace the old tag with the new in place, then re-normalize the
            # whole list (dedupes if the service already had `new`).
            replaced = [new_n if t.casefold() == old_key else t for t in tags]
            entry["tags"] = _coerce_tags(replaced)
            changed.append(sid)
        if changed:
            _write_unlocked(data)
    return changed


async def delete_tag(name: str) -> list[str]:
    """Remove a tag from every service that carries it. Atomic, single write."""
    name_key = _normalize_tag(name).casefold()
    if not name_key:
        return []
    changed: list[str] = []
    async with _lock:
        data = _read_unlocked()
        for sid, entry in data.items():
            tags = entry["tags"]
            kept = [t for t in tags if t.casefold() != name_key]
            if len(kept) != len(tags):
                entry["tags"] = kept
                changed.append(sid)
        if changed:
            _write_unlocked(data)
    return changed


async def set_category_for_ids(service_ids: list[str], new: str) -> list[str]:
    """Set an explicit ``category`` override = ``new`` on each id, atomically.

    A single read-modify-write under the store lock, so a category rename moves
    every member service in one atomic file replace (all-or-nothing durability;
    a crash can't leave the members split across two categories). Returns the
    ids whose stored category actually changed.
    """
    new = new.strip()
    if not new:
        return []
    changed: list[str] = []
    async with _lock:
        data = _read_unlocked()
        for sid in service_ids:
            entry = data.get(sid, {"tags": [], "category": None})
            if entry.get("category") != new:
                entry["category"] = new
                data[sid] = entry
                changed.append(sid)
        if changed:
            _write_unlocked(data)
    return changed


def tag_usage(
    settings: dict[str, ServiceSettings],
) -> dict[str, list[str]]:
    """Build ``{tag: [service_id, ...]}`` from a settings map (order-stable)."""
    usage: dict[str, list[str]] = {}
    for sid in sorted(settings):
        for tag in settings[sid]["tags"]:
            usage.setdefault(tag, []).append(sid)
    return usage


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


# ── cross-service tag management surface (G1) ─────────────────────────────────

# Tags that render as the special accent SSO chip on the card (see the frontend
# SSO_TAG_META / seed.py). Renaming/deleting one loses that special rendering —
# the UI surfaces a warning; the backend treats them like any other tag.
SSO_SPECIAL_TAGS = {"Keycloak SSO", "Identity provider"}


def _known_ids() -> set[str]:
    """The set of real service ids (repo dirs with a compose.yaml)."""
    if mock.mock_enabled():
        return set(mock.MOCK_SERVICES.keys())
    from . import inventory

    return set(inventory.scan_repo().keys())


def _mock_tag_usage() -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for sid in sorted(mock.MOCK_SERVICES):
        for tag in mock.MOCK_SERVICES[sid].get("tags", []):
            usage.setdefault(tag, []).append(sid)
    return usage


@router.get("/tags")
async def list_tags() -> list[models.TagInfo]:
    """Every tag in the store with its usage count and member service ids.

    Only tags on *known* services are surfaced (unknown-id entries are pruned),
    so the panel never shows a phantom tag — but the panel can still delete
    residue because :func:`delete_tag` scans the whole file.
    """
    known = _known_ids()
    if mock.mock_enabled():
        usage = _mock_tag_usage()
    else:
        settings = await all_settings(known_ids=known)
        usage = tag_usage(settings)
    return [
        models.TagInfo(
            name=tag,
            count=len(sids),
            services=sids,
            sso=tag in SSO_SPECIAL_TAGS,
        )
        for tag, sids in sorted(usage.items(), key=lambda kv: kv[0].casefold())
    ]


def _request_user(request: Request) -> str:
    identity = getattr(request.state, "identity", None)
    return identity.user if identity else "unknown"


@router.post("/tags/rename")
async def rename_tag_route(
    request: Request, payload: models.TagRenameRequest
) -> models.TagMutationResult:
    if mock.mock_enabled():
        old_key = _normalize_tag(payload.name).casefold()
        new_n = _normalize_tag(payload.to)
        changed: list[str] = []
        for sid, svc in mock.MOCK_SERVICES.items():
            tags = svc.get("tags", [])
            if any(t.casefold() == old_key for t in tags):
                svc["tags"] = _clean_tags(
                    [new_n if t.casefold() == old_key else t for t in tags]
                )
                changed.append(sid)
        return models.TagMutationResult(changed=changed)

    changed = await rename_tag(payload.name, payload.to)
    if changed:
        from . import audit

        await audit.record(
            _request_user(request), "*", "tag-rename", "succeeded",
            stderr_tail=f"{payload.name!r} → {payload.to!r} on {len(changed)} services",
        )
    return models.TagMutationResult(changed=changed)


@router.post("/tags/delete")
async def delete_tag_route(
    request: Request, payload: models.TagDeleteRequest
) -> models.TagMutationResult:
    if mock.mock_enabled():
        name_key = _normalize_tag(payload.name).casefold()
        changed = []
        for sid, svc in mock.MOCK_SERVICES.items():
            tags = svc.get("tags", [])
            kept = [t for t in tags if t.casefold() != name_key]
            if len(kept) != len(tags):
                svc["tags"] = kept
                changed.append(sid)
        return models.TagMutationResult(changed=changed)

    changed = await delete_tag(payload.name)
    if changed:
        from . import audit

        await audit.record(
            _request_user(request), "*", "tag-delete", "succeeded",
            stderr_tail=f"{payload.name!r} removed from {len(changed)} services",
        )
    return models.TagMutationResult(changed=changed)
