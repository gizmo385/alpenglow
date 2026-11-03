"""Repin a pinned image before Update pulls it.

For an image in ``versions.yaml``, ``docker compose pull`` alone does nothing:
the compose file names an exact ``tag@digest``. So for those images Update
first runs the repo's own pin tool, ``./pin set IMAGE TAG``, with the tracker's
new release written the way the image is tagged now (a leading ``v`` and a
suffix such as ``-alpine`` are kept). pin looks up the digest, which also
proves the tag exists, so the next spelling is tried when one doesn't, and it
rewrites the compose files.

Nothing is committed: the edited ``versions.yaml`` and compose files are left
in the server's checkout for a person to commit. Every repin edits the same
``versions.yaml``, so they run one at a time: :func:`repo_lock` orders the
dashboard's own repins, and pin itself holds a ``flock`` on ``versions.yaml``
while it writes, so a ``./pin`` run at a shell at the same moment waits too. A failed
pull or recreate is not rolled back: an older version isn't always safe to go
back to once a new one has migrated its data.

The repo is the host's ``/services`` checkout, so pin runs as the user who
owns it rather than as root. Images not in ``versions.yaml``, or with
``policy: float``, skip all of this and are pulled as before.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from . import inventory

log = logging.getLogger("alpenglow.repin")

LOCK_NAME = "versions.yaml"

# pin looks up digests from the registry, and its first run installs its one
# dependency, so give it a few minutes.
_TIMEOUT_PIN = 180

# pin's exit code for a tag the registry doesn't have (tools/pin/src/pin/cli.py).
_EXIT_NO_SUCH_TAG = 3

_repo_lock: Optional[asyncio.Lock] = None


class RepinError(Exception):
    """The repin couldn't be done; nothing was changed."""


def repo_lock() -> asyncio.Lock:
    """The one lock around every edit of ``versions.yaml``."""
    global _repo_lock
    if _repo_lock is None:
        _repo_lock = asyncio.Lock()
    return _repo_lock


def repo_root() -> Path:
    return inventory.services_root()


# ── what to pin ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    """A pending upgrade of a pinned image."""

    name: str  # versions.yaml key
    current_tag: str
    current_version: str  # the tracker's release names
    latest_version: str
    repo: str = ""  # owner/repo, which ties it to its Upgrades card

    def tags(self) -> list[str]:
        return candidate_tags(self.current_tag, self.current_version, self.latest_version)


def _bare(version: str) -> str:
    return version[1:] if version[:1] in ("v", "V") and version[1:2].isdigit() else version


def candidate_tags(current_tag: str, current_version: str, latest_version: str) -> list[str]:
    """Image tags to try for ``latest_version``, the likeliest first.

    Release names and image tags often differ by a leading ``v``, and some
    images add a suffix (``0.21.1-alpine``). Match the current tag's spelling
    first, then fall back to the plain release name with and without ``v``.
    """
    bare = _bare(latest_version)
    out: list[str] = []
    cur_bare = _bare(current_version)
    if current_tag and cur_bare:
        for spelled in (current_version, cur_bare, f"v{cur_bare}"):
            if spelled and current_tag.startswith(spelled):
                prefix = "v" if spelled.startswith("v") else ""
                out.append(f"{prefix}{bare}{current_tag[len(spelled):]}")
                break
    if current_tag[:1] == "v":
        out += [f"v{bare}", bare]
    else:
        out += [bare, f"v{bare}"]
    out.append(latest_version)
    return list(dict.fromkeys(t for t in out if t))


def image_key(image: str) -> str:
    from .upgrades.cards import image_key as key  # keep one definition

    return key(image)


def load_pins(root: Path) -> dict[str, dict]:
    """``versions.yaml`` entries that are pinned (have a tag and don't float)."""
    path = root / LOCK_NAME
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    pins = {}
    for name, raw in (data.get("images") or {}).items():
        raw = raw or {}
        if raw.get("tag") and raw.get("policy") != "float":
            pins[image_key(str(name))] = {"name": str(name), **raw}
    return pins


def target_for(entry: dict, pins: dict[str, dict]) -> Target | None:
    """The repin for a tracker entry, or ``None`` when its image isn't pinned."""
    if not entry.get("has_updates") or not entry.get("latest_version"):
        return None
    pin = pins.get(image_key(str(entry.get("image") or "")))
    if pin is None:
        return None
    owner, repo = str(entry.get("owner") or ""), str(entry.get("repo") or "")
    t = Target(
        name=pin["name"],
        current_tag=str(pin["tag"]),
        current_version=str(entry.get("current_version") or ""),
        latest_version=str(entry["latest_version"]),
        repo=f"{owner}/{repo}" if owner and repo else "",
    )
    # Already pinned to it (the tracker hasn't rescanned since): just pull.
    return None if t.current_tag in t.tags() else t


async def target(service_id: str) -> Target | None:
    """The pending upgrade of this service's pinned image, if it has one."""
    from .integrations import updates  # avoids an import cycle

    payload = await updates._tracker_cached()
    if not payload:
        return None
    matched, _ = updates._match_entries(payload)
    entry = matched.get(service_id)
    if entry is None:
        return None
    return target_for(entry, load_pins(repo_root()))


# ── running pin as the repo's owner ───────────────────────────────────


def _owner(root: Path) -> tuple[int, int] | None:
    """(uid, gid) to run as: the repo's owner when we're root and they aren't."""
    if os.geteuid() != 0:
        return None
    st = (root / LOCK_NAME).stat()
    return None if st.st_uid == 0 else (st.st_uid, st.st_gid)


def _secret(key: str) -> bool:
    return any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "KEY"))


def _env(owner: tuple[int, int] | None) -> dict[str, str]:
    home = Path("/tmp/alpenglow-repin") / str(owner[0] if owner else os.geteuid())
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)
        if owner:
            os.chown(home, *owner)
    try:
        name = pwd.getpwuid(owner[0]).pw_name if owner else ""
    except KeyError:
        name = ""
    # Keep the container's own settings (PATH, proxies, CA bundles) but not its
    # secrets: pin needs no token.
    env = {k: v for k, v in os.environ.items() if not _secret(k)}
    return env | {
        "HOME": str(home),
        "USER": name or "alpenglow",
        # Keep pin's environment out of the checkout: the host's tools/pin/.venv
        # points at the host's Python.
        "UV_CACHE_DIR": str(home / "uv-cache"),
        "UV_PROJECT_ENVIRONMENT": str(home / "pin-venv"),
    }


async def _run(argv: list[str], root: Path, timeout: float) -> tuple[int, str]:
    """Run as the repo's owner in ``root``; return (returncode, stdout+stderr)."""
    owner = _owner(root)
    kw = {"user": owner[0], "group": owner[1], "extra_groups": []} if owner else {}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(root),
        env=_env(owner),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kw,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


async def _pin_set(root: Path, name: str, tag: str) -> tuple[int, str]:
    return await _run([str(root / "pin"), "set", name, tag], root, _TIMEOUT_PIN)


# ── the repin ─────────────────────────────────────────────────────────────────


async def pin(t: Target) -> str:
    """Pin ``t`` to its new release; return the tag used. Hold :func:`repo_lock`.

    Tries each of :meth:`Target.tags` until the registry has one. Any other
    failure stops there, since another spelling wouldn't help. pin writes all
    of its files or none, so a failure leaves them as they were.
    """
    root = repo_root()
    tried: list[str] = []
    for candidate in t.tags():
        rc, out = await _pin_set(root, t.name, candidate)
        if rc == 0:
            log.info("repin: %s %s -> %s", t.name, t.current_tag, candidate)
            return candidate
        reason = out.splitlines()[-1] if out else f"exit {rc}"
        if rc != _EXIT_NO_SUCH_TAG:
            raise RepinError(f"couldn't pin {t.name} to {candidate}: {reason}")
        tried.append(f"{candidate}: {reason}")
    raise RepinError(f"no image tag found for {t.name} {t.latest_version} ({'; '.join(tried)})")
