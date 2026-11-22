"""Pending updates for pinned images that follow a moving tag.

An image that publishes only ``latest`` plus per-build ``sha-<commit>`` tags
(youtube-rss-manager) is pinned to a per-build tag with ``auto: {follow:
latest}`` in ``versions.yaml``. It has no GitHub releases, so the tracker never
reports it. This module asks the registry where the followed tag points now
(``pin resolve``, the same lookup the auto-updater uses) and, when that isn't
the pinned tag, reports a pending update for the Updates list and for Update's
repin.

Where each tag points is cached for :data:`_TTL` seconds: every lookup is a
``pin`` run with a few registry requests. Whether a build is *pending* is
decided against ``versions.yaml`` on each call, so a repin clears its row right
away. "Check for updates" drops the cache (:func:`invalidate`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ._common import DEFAULT_TIMEOUT, AsyncTTLCache

log = logging.getLogger("alpenglow.follows")

_TTL = 600.0
_CACHE = AsyncTTLCache(ttl=_TTL)

_MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


@dataclass(frozen=True)
class Resolved:
    """Where one followed tag points."""

    name: str  # versions.yaml key
    follow_tag: str  # the moving tag, e.g. latest
    tag: str  # the per-build tag it points at, e.g. sha-fdfa579
    built_at: Optional[str]  # when that build was made (ISO 8601), if known


@dataclass(frozen=True)
class FollowUpdate:
    """A newer build on a followed tag than the one pinned."""

    name: str
    follow_tag: str
    current: str
    latest: str
    built_at: Optional[str]

    @property
    def compare_url(self) -> Optional[str]:
        """GitHub's commits between the two builds, for a ``ghcr.io/OWNER/REPO``
        image tagged ``sha-<commit>``, whose source repo has the same name."""
        parts = self.name.split("/")
        if len(parts) != 3 or parts[0] != "ghcr.io":
            return None
        old, new = (t.removeprefix("sha-") for t in (self.current, self.latest))
        if not (self.current.startswith("sha-") and self.latest.startswith("sha-")):
            return None
        return f"https://github.com/{parts[1]}/{parts[2]}/compare/{old}...{new}"


# ── registry: when a build was made ──────────────────────────────────────────


def _split_name(name: str) -> tuple[str, str]:
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest
    return "registry-1.docker.io", name if rest else f"library/{name}"


async def _token(client: httpx.AsyncClient, host: str, repo: str) -> Optional[str]:
    resp = await client.get(f"https://{host}/v2/")
    if resp.status_code != 401:
        return None
    challenge = resp.headers.get("WWW-Authenticate", "")
    params = dict(part.split("=", 1) for part in challenge.removeprefix("Bearer ").split(",") if "=" in part)
    params = {k.strip(): v.strip().strip('"') for k, v in params.items()}
    realm = params.pop("realm", None)
    if not realm:
        return None
    query = {"scope": f"repository:{repo}:pull", **({"service": params["service"]} if "service" in params else {})}
    body = (await client.get(realm, params=query)).raise_for_status().json()
    return body.get("token") or body.get("access_token")


async def built_at(name: str, tag: str) -> Optional[str]:
    """The ``created`` time in ``name:tag``'s image config, or ``None``."""
    host, repo = _split_name(name)
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            token = await _token(client, host, repo)
            headers = {"Accept": _MANIFEST_TYPES, **({"Authorization": f"Bearer {token}"} if token else {})}
            base = f"https://{host}/v2/{repo}"
            manifest = (await client.get(f"{base}/manifests/{tag}", headers=headers)).raise_for_status().json()
            if "manifests" in manifest:  # a multi-arch index: any platform's config has the time
                platforms = [m for m in manifest["manifests"]
                             if (m.get("platform") or {}).get("os") not in (None, "unknown")]
                if not platforms:
                    return None
                digest = platforms[0]["digest"]
                manifest = (await client.get(f"{base}/manifests/{digest}", headers=headers)).raise_for_status().json()
            config = (await client.get(f"{base}/blobs/{manifest['config']['digest']}",
                                       headers=headers)).raise_for_status().json()
        created = config.get("created")
        return str(created) if created else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as err:
        log.info("follows: couldn't read when %s:%s was built: %s", name, tag, err)
        return None


# ── where each followed tag points (cached) ──────────────────────────────────


async def _resolve_all() -> list[Resolved]:
    from ..autoupdate import runner  # avoids an import cycle

    loaded = runner.load()
    units = [u for u in loaded.units if u.follow_tag]
    resolved = await runner.resolve_follows(units)
    out = []
    for unit in units:
        tag = resolved.get(unit.id)
        if tag is None or isinstance(tag, Exception):
            log.warning("follows: couldn't tell where %s:%s points: %s", unit.images[0], unit.follow_tag, tag)
            continue
        name = unit.images[0]
        out.append(Resolved(name, str(unit.follow_tag), tag, await built_at(name, tag)))
    return out


async def _resolved() -> list[Resolved]:
    try:
        return await _CACHE.get(_resolve_all)
    except Exception:  # noqa: BLE001 - a broken lookup only hides these rows
        log.exception("follows: lookup failed")
        return []


def invalidate() -> None:
    _CACHE.invalidate()


async def pending() -> list[FollowUpdate]:
    """Followed images whose tag points at a newer build than the pinned one."""
    from .. import repin  # avoids an import cycle

    resolved = await _resolved()
    if not resolved:
        return []
    pins = repin.load_pins(repin.repo_root())
    out = []
    for r in resolved:
        pin = pins.get(repin.image_key(r.name))
        if pin is None or str(pin["tag"]) == r.tag:
            continue
        out.append(FollowUpdate(r.name, r.follow_tag, str(pin["tag"]), r.tag, r.built_at))
    return out
