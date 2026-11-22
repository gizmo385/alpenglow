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

**Rebuilt tags.** Publishers sometimes push a new image to a tag we already
run (Keycloak re-pushing ``26.6.4`` with a CVE fix; official images rebuilt on
a new base). The tracker only knows releases and our pins name an exact
digest, so nothing would notice. Every pinned image with a digest (except the
followed ones above) is also checked: the registry's digest for its pinned tag
is looked up every :data:`_REBUILD_TTL` seconds (a manifest HEAD, which pulls
nothing and doesn't count against Docker Hub's pull limit), and when it
differs from ``versions.yaml`` the image is reported with ``rebuild=True``.
Update repins the same tag (``pin set NAME TAG`` looks the digest up again).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ._common import DEFAULT_TIMEOUT, AsyncTTLCache

log = logging.getLogger("alpenglow.follows")

_TTL = 600.0
_CACHE = AsyncTTLCache(ttl=_TTL)
# Rebuilt tags: one manifest lookup per pinned image, so less often.
_REBUILD_TTL = 6 * 3600.0
_REBUILD_CACHE = AsyncTTLCache(ttl=_REBUILD_TTL)
_REBUILD_CONCURRENCY = 4

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
    # True: the same tag now points at a different image (current == latest).
    rebuild: bool = False

    @property
    def compare_url(self) -> Optional[str]:
        """GitHub's commits between the two builds, for a ``ghcr.io/OWNER/REPO``
        image tagged ``sha-<commit>``, whose source repo has the same name."""
        parts = self.name.split("/")
        if self.rebuild or len(parts) != 3 or parts[0] != "ghcr.io":
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


# ── rebuilt tags: where each pinned tag points now (cached) ─────────────────


@dataclass(frozen=True)
class TagDigest:
    name: str  # versions.yaml key
    tag: str
    digest: str  # what the registry says the tag points at now
    built_at: Optional[str] = None  # filled in only for tags that moved


async def _head_digest(client: httpx.AsyncClient, name: str, tag: str) -> Optional[str]:
    """The digest ``name:tag`` points at now (a manifest HEAD), or ``None``.

    Asks for the same manifest types as ``pin`` (tools/pin registry.py), so the
    answer is comparable with the digest pin wrote into ``versions.yaml``.
    """
    host, repo = _split_name(name)
    token = await _token(client, host, repo)
    headers = {"Accept": _MANIFEST_TYPES, **({"Authorization": f"Bearer {token}"} if token else {})}
    resp = await client.head(f"https://{host}/v2/{repo}/manifests/{tag}", headers=headers)
    resp.raise_for_status()
    return resp.headers.get("Docker-Content-Digest")


_current_digest = _head_digest  # tests replace this one


def _rebuild_candidates() -> dict[str, tuple[str, str]]:
    """``{name: (tag, digest)}`` for pinned images with a digest, not followed."""
    from .. import repin  # avoids an import cycle

    out = {}
    for pin in repin.load_pins(repin.repo_root()).values():
        auto = pin.get("auto") if isinstance(pin.get("auto"), dict) else {}
        if pin.get("digest") and not auto.get("follow"):
            out[pin["name"]] = (str(pin["tag"]), str(pin["digest"]))
    return out


async def _resolve_tags() -> dict[str, TagDigest]:
    candidates = _rebuild_candidates()
    if not candidates:
        return {}
    sem = asyncio.Semaphore(_REBUILD_CONCURRENCY)
    out: dict[str, TagDigest] = {}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        async def one(name: str, tag: str, pinned: str) -> None:
            async with sem:
                try:
                    digest = await _current_digest(client, name, tag)
                except (httpx.HTTPError, ValueError) as err:
                    log.info("follows: couldn't look up %s:%s: %s", name, tag, err)
                    return
            if not digest:
                return
            when = await built_at(name, digest) if digest != pinned else None
            out[name] = TagDigest(name, tag, digest, when)

        await asyncio.gather(*(one(n, t, d) for n, (t, d) in candidates.items()))
    moved = [n for n, td in out.items() if td.digest != candidates[n][1]]
    log.info("follows: checked %d pinned tags, %d rebuilt since pinned%s", len(out), len(moved),
             f": {', '.join(sorted(moved))}" if moved else "")
    return out


async def _tag_digests() -> dict[str, TagDigest]:
    try:
        return await _REBUILD_CACHE.get(_resolve_tags)
    except Exception:  # noqa: BLE001 - a broken lookup only hides these rows
        log.exception("follows: rebuilt-tag lookup failed")
        return {}


def invalidate() -> None:
    _CACHE.invalidate()
    _REBUILD_CACHE.invalidate()


async def pending() -> list[FollowUpdate]:
    """Followed images whose tag points at a newer build than the pinned one,
    and pinned images whose tag now points at a different image (rebuilds)."""
    from .. import repin  # avoids an import cycle

    resolved, tags = await asyncio.gather(_resolved(), _tag_digests())
    if not resolved and not tags:
        return []
    pins = repin.load_pins(repin.repo_root())
    out = []
    for r in resolved:
        pin = pins.get(repin.image_key(r.name))
        if pin is None or str(pin["tag"]) == r.tag:
            continue
        out.append(FollowUpdate(r.name, r.follow_tag, str(pin["tag"]), r.tag, r.built_at))
    for td in tags.values():
        pin = pins.get(repin.image_key(td.name))
        # Compared with versions.yaml on every call, so a repin (or a new tag)
        # clears the row before the cache expires.
        if pin is None or str(pin["tag"]) != td.tag or str(pin.get("digest") or "") in ("", td.digest):
            continue
        out.append(FollowUpdate(td.name, td.tag, td.tag, td.tag, td.built_at, rebuild=True))
    return out
