"""image-updates-tracker client + routes (work package B4, upstream D1).

Talks to the tracker's live ``GET /api/updates`` (added in D1, deployed at
``updates-tracker-release-feeds-1:8585`` / ``updates.alpenglow.acbc.house``).
Real shape (verified live):

    {"services": [{"name","owner","repo","image","current_version",
                   "latest_version","has_updates","html_url","image_url",
                   "releases":[{"tag","url","published_at"}]}],
     "last_updated": <unix float>, "refreshing": bool}

Matching tracker entries → service dirs is by **image ref with the tag
stripped**: e.g. tracker ``ghcr.io/immich-app/immich-machine-learning:release``
and compose ``ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}``
both normalise to ``ghcr.io/immich-app/immich-machine-learning``. A dir can own
several images (immich also carries ``alangrainger/immich-public-proxy``), so
each compose image maps to its dir. Unmatched tracker entries are logged, not
served. Entries with ``has_updates=false`` are ignored.

``GET /api/updates`` returns only *matched* pending updates (contract ``Updates``).
Connection failure / 404 → empty list + ``trackerUnavailable=True``.
``service_enrichment()`` reuses the same cached fetch to hand inventory the
``latestVersion``/``releasedAt``/``changelogUrl`` per dir (inventory calls it on
every request — caching is required).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

from .. import inventory, mock, models
from ._common import DEFAULT_TIMEOUT, AsyncTTLCache, env_url

router = APIRouter(prefix="/api", tags=["updates"])
log = logging.getLogger("alpenglow.updates")

# Cache the raw tracker payload; both the route and service_enrichment() (called
# per inventory request) read through it.
_CACHE = AsyncTTLCache(ttl=30.0)

_ICON_DEFAULT = "ph-cube"


def _base_url() -> str:
    return env_url("UPDATES_TRACKER_URL", "http://updates-tracker-release-feeds-1:8585")


# ── image-ref normalisation ───────────────────────────────────────────────────


def normalize_image(ref: str) -> str:
    """Strip the tag/digest from an image ref, leaving ``registry/path``.

    Handles registry ports (``host:5000/img``) and env-var tags
    (``twentycrm/twenty:${TAG:-latest}``) by only treating a colon in the *last*
    path segment as the tag separator. A ``@sha256:`` digest is also stripped.
    """
    ref = str(ref).strip().strip("'\"")
    if not ref:
        return ref
    ref = ref.split("@", 1)[0]  # drop digest
    # split registry/host from the rest to protect a ":port"
    if "/" in ref:
        prefix, last = ref.rsplit("/", 1)
    else:
        prefix, last = "", ref
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{prefix}/{last}" if prefix else last


# ── tracker fetch (cached) ────────────────────────────────────────────────────


async def _fetch_tracker() -> Optional[dict]:
    """Fetch the raw tracker payload, or ``None`` on unavailability (404/error)."""
    url = f"{_base_url()}/api/updates"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


async def _tracker_cached() -> Optional[dict]:
    return await _CACHE.get(_fetch_tracker)


# ── matching ──────────────────────────────────────────────────────────────────


def _image_to_dir() -> dict[str, str]:
    """``normalized_image → service_dir_id`` from the repo compose scan.

    First writer wins so a shared image (oauth2-proxy) doesn't get clobbered;
    but since matching only cares about images the tracker reports (real app
    images, not sidecars), collisions are harmless in practice.
    """
    mapping: dict[str, str] = {}
    for sid, repo in inventory.scan_repo().items():
        for svc in repo.services.values():
            if isinstance(svc, dict) and svc.get("image"):
                norm = normalize_image(str(svc["image"]))
                if norm:
                    mapping.setdefault(norm, sid)
    return mapping


def _newest_release(entry: dict) -> Optional[dict]:
    releases = entry.get("releases")
    if not isinstance(releases, list) or not releases:
        return None
    # tracker already orders newest-first; be defensive and pick max published_at
    def key(r: dict) -> str:
        return str(r.get("published_at") or "") if isinstance(r, dict) else ""

    dated = [r for r in releases if isinstance(r, dict)]
    if not dated:
        return None
    return max(dated, key=key)


def _match_entries(payload: dict) -> tuple[dict[str, dict], list[str]]:
    """Return ``({dir_id: tracker_entry}, unmatched_names)`` for pending updates.

    Only ``has_updates=true`` entries are considered. If several tracker entries
    map to one dir (immich owns both ``immich-machine-learning`` and
    ``immich-public-proxy``), the entry whose ``repo`` matches the dir id is
    preferred as the "primary" update; otherwise the first-seen wins. The others
    are reported as unmatched-to-a-distinct-dir so nothing is silently dropped.
    """
    services = payload.get("services")
    if not isinstance(services, list):
        return {}, []
    img_map = _image_to_dir()
    matched: dict[str, dict] = {}
    unmatched: list[str] = []
    for entry in services:
        if not isinstance(entry, dict) or not entry.get("has_updates"):
            continue
        norm = normalize_image(str(entry.get("image", "")))
        sid = img_map.get(norm)
        if sid is None:
            unmatched.append(f"{entry.get('name')} ({entry.get('image')})")
            continue
        existing = matched.get(sid)
        if existing is None:
            matched[sid] = entry
            continue
        # collision: prefer the entry whose repo matches the dir id
        if str(entry.get("repo") or "") == sid and str(existing.get("repo") or "") != sid:
            unmatched.append(f"{existing.get('name')} ({existing.get('image')}) [also → {sid}]")
            matched[sid] = entry
        else:
            unmatched.append(f"{entry.get('name')} ({entry.get('image')}) [also → {sid}]")
    return matched, unmatched


def _entry_fields(entry: dict) -> dict[str, Optional[str]]:
    rel = _newest_release(entry)
    return {
        "latestVersion": (str(entry.get("latest_version")) if entry.get("latest_version") else None),
        "releasedAt": (str(rel.get("published_at")) if rel and rel.get("published_at") else None),
        "changelogUrl": (str(entry.get("html_url")) if entry.get("html_url") else None),
    }


def _last_updated_iso(payload: dict) -> Optional[str]:
    from datetime import datetime, timezone

    ts = payload.get("last_updated")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return None


# ── public API for inventory ──────────────────────────────────────────────────


async def service_enrichment() -> dict[str, dict]:
    """``{dir_id: {latestVersion, releasedAt, changelogUrl}}`` for pending updates.

    Cached via the shared tracker fetch; safe to call per inventory request.
    Empty dict when the tracker is unavailable.
    """
    payload = await _tracker_cached()
    if payload is None:
        return {}
    matched, unmatched = _match_entries(payload)
    if unmatched:
        log.info("updates: %d tracker entries unmatched to a service dir: %s",
                 len(unmatched), ", ".join(unmatched))
    return {sid: _entry_fields(entry) for sid, entry in matched.items()}


# ── metadata for update entries ───────────────────────────────────────────────


def _meta_for(sid: str) -> tuple[str, str]:
    """(display name, icon) for a dir, from metadata.yaml with sane defaults."""
    meta = inventory.load_metadata().get(sid, {})
    name = meta.get("name") or sid.replace("_", " ").replace("-", " ").title()
    icon = meta.get("icon") or _ICON_DEFAULT
    return name, icon


def _build_updates(payload: dict) -> models.Updates:
    matched, unmatched = _match_entries(payload)
    if unmatched:
        log.info("updates: %d tracker entries unmatched to a service dir: %s",
                 len(unmatched), ", ".join(unmatched))
    entries: list[models.UpdateEntry] = []
    for sid, entry in sorted(matched.items()):
        fields = _entry_fields(entry)
        latest = fields["latestVersion"]
        if not latest:
            continue  # UpdateEntry.latestVersion is required
        name, icon = _meta_for(sid)
        entries.append(
            models.UpdateEntry(
                id=sid,
                name=name,
                icon=icon,
                image=str(entry.get("image", "")),
                currentVersion=str(entry.get("current_version") or "—"),
                latestVersion=latest,
                releasedAt=fields["releasedAt"],
                changelogUrl=fields["changelogUrl"],
            )
        )
    return models.Updates(
        services=entries,
        lastUpdated=_last_updated_iso(payload),
        refreshing=bool(payload.get("refreshing")),
        trackerUnavailable=False,
    )


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/updates")
async def get_updates() -> models.Updates:
    if mock.mock_enabled():
        return mock.mock_updates()
    payload = await _tracker_cached()
    if payload is None:
        return models.Updates(services=[], lastUpdated=None, refreshing=False,
                              trackerUnavailable=True)
    return _build_updates(payload)


async def _proxy_refresh() -> Optional[dict]:
    """POST the tracker's ``/refresh``; return its ``/api/updates`` snapshot after.

    Returns ``None`` when the tracker is unavailable.
    """
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(f"{base}/refresh")
            if resp.status_code >= 400:
                return None
    except httpx.HTTPError:
        return None
    _CACHE.invalidate()
    return await _fetch_tracker()


@router.post("/updates/refresh")
async def refresh_updates() -> models.Updates:
    if mock.mock_enabled():
        result = mock.mock_updates()
        result.refreshing = True
        return result
    payload = await _proxy_refresh()
    if payload is None:
        return models.Updates(services=[], lastUpdated=None, refreshing=False,
                              trackerUnavailable=True)
    result = _build_updates(payload)
    result.refreshing = True
    return result
