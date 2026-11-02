"""Edits to the lock file: set a tag, pin a plugin, adopt what's running."""

from __future__ import annotations

import json
import os
import sys
import subprocess
from collections.abc import Callable

from .lock import LOCK_NAME, Lock, LockError
from .refs import FLOATING_TAGS, parse_ref
from .registry import resolve_digest
from .scan import scan

Resolver = Callable[[str, str], str]


def _usage_kinds(lock: Lock, name: str) -> set[str]:
    return {
        u.kind
        for u in scan(lock.root, lock.excludes, lock.extra_files)
        if u.ref is not None and u.ref.name == name
    }


def _pin(lock: Lock, name: str, tag: str, digest: str | None) -> None:
    entries = lock.entries()
    lock.set_field(name, "tag", tag)
    lock.set_field(name, "digest", digest)
    if name in entries and entries[name].floating:
        lock.set_field(name, "policy", None)
        lock.set_field(name, "reason", None)


def set_tag(
    lock: Lock, name: str, tag: str, *, resolve: bool = True, resolver: Resolver = resolve_digest
) -> list[str]:
    """Pin ``name`` (and the rest of its group) to ``tag``; return the names changed.

    The digest is resolved from the registry, which also proves the tag exists.
    Only images used in compose files get a digest: Dockerfile FROM lines keep
    a variant suffix (``-builder``) whose digest differs from the plain tag's,
    and extra files (casting.yaml) are read by tools that may not accept one.
    """
    if tag in FLOATING_TAGS:
        raise LockError(f"{tag!r} is a moving channel; pin a release tag")
    if name not in lock.entries():
        lock.raw(name)
    members = lock.group_members(name)
    for member in members:
        digest = None
        if resolve and "compose" in _usage_kinds(lock, member):
            digest = resolver(member, tag)
        _pin(lock, member, tag, digest)
    return members


def fill_digests(lock: Lock, *, resolver: Resolver = resolve_digest) -> tuple[list[str], list[str]]:
    """Look up digests for pinned entries used in compose files that lack one.

    Returns ``(filled, failures)``; a failure (rate limit, unknown tag) leaves
    that entry as it was so the rest still get filled.
    """
    filled, failures = [], []
    for name, e in lock.entries().items():
        if e.floating or not e.tag or e.digest or "compose" not in _usage_kinds(lock, name):
            continue
        try:
            lock.set_field(name, "digest", resolver(name, e.tag))
            filled.append(name)
        except Exception as err:
            failures.append(f"{name}:{e.tag}: {err}")
    return filled, failures


def add_ref(
    lock: Lock, ref: str, *, reason: str = "Not pinned yet", resolver: Resolver = resolve_digest
) -> tuple[str, list[str]]:
    """Give an image reference an entry, for a newly added service.

    A release tag is pinned (with its digest when the registry answers); no tag
    or a channel such as ``latest`` gets a float entry with ``reason``. An image
    that already has an entry is left alone. Returns ``(what_happened, warnings)``.
    """
    r = parse_ref(ref)
    if r.name in lock.entries():
        return f"{r.name} already has an entry", []
    if r.tag and r.tag not in FLOATING_TAGS and not r.interpolated:
        try:
            set_tag(lock, r.name, r.tag, resolver=resolver)
            return f"pinned {r.name} to {r.tag}", []
        except Exception as err:  # registry unreachable or rate limited
            set_tag(lock, r.name, r.tag, resolve=False)
            return f"pinned {r.name} to {r.tag} without a digest", [f"digest lookup failed: {err}"]
    lock.set_field(r.name, "policy", "float")
    lock.set_field(r.name, "reason", reason)
    return f"added {r.name} as float ({reason})", []


def set_plugin(lock: Lock, name: str, module: str, version: str) -> None:
    if name not in lock.entries():
        raise LockError(f"{name} isn't in {LOCK_NAME}")
    raw = lock.raw(name)
    if "plugins" not in raw or raw["plugins"] is None:
        from ruamel.yaml.comments import CommentedMap

        raw["plugins"] = CommentedMap()
    raw["plugins"][module] = version


# ── adopt ───────────────────────────────────────────────────────────────────


def docker_inspect(ref: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """``docker image inspect`` for one image; honours DOCKER_HOST.

    If the Docker socket needs root, only this one read-only command is retried
    under sudo, so pin itself (and the files it writes) stays yours.
    """
    cmd = ["docker", "image", "inspect", ref]
    try:
        try:
            out = run(cmd, check=True, capture_output=True, text=True).stdout
        except subprocess.CalledProcessError as err:
            if "permission denied" not in (err.stderr or "").lower() or os.geteuid() == 0:
                raise
            print("Docker needs root here; running `sudo docker image inspect`.", file=sys.stderr)
            out = run(["sudo", *cmd], check=True, stdout=subprocess.PIPE, text=True).stdout
    except FileNotFoundError as err:
        raise LockError("docker isn't installed here; run `pin adopt` on the server") from err
    except subprocess.CalledProcessError as err:
        raise LockError(f"docker image inspect {ref} failed: {(err.stderr or '').strip()}") from err
    data = json.loads(out)
    if not data:
        raise LockError(f"docker has no image {ref}")
    return data[0]


def _running_digest(info: dict, name: str) -> str:
    repo_digests = info.get("RepoDigests") or []
    for rd in repo_digests:
        rd_name, _, digest = rd.partition("@")
        if rd_name == name or rd_name.endswith("/" + name):
            return digest
    if repo_digests:
        return repo_digests[0].partition("@")[2]
    raise LockError(f"{name}: the local image has no registry digest (was it built locally?)")


def adopt(
    lock: Lock,
    name: str,
    *,
    tag: str | None = None,
    ref: str | None = None,
    force: bool = False,
    inspect: Callable[[str], dict] = docker_inspect,
    resolver: Resolver = resolve_digest,
) -> tuple[str, str, list[str]]:
    """Pin ``name`` to the exact image the server runs now.

    Returns ``(tag, digest, warnings)``. The tag comes from ``--tag`` or the
    image's ``org.opencontainers.image.version`` label. Unless ``force``, the
    tag must still point at the running digest, so the pin is reproducible.
    """
    warnings: list[str] = []
    if ref is None:
        refs = sorted(
            {str(u.ref) for u in scan(lock.root, lock.excludes, lock.extra_files)
             if u.kind == "compose" and u.ref is not None and u.ref.name == name}
        )
        if not refs:
            raise LockError(f"{name} isn't used in any compose file")
        ref = refs[0]
        if parse_ref(ref).interpolated:
            raise LockError(f"{ref} depends on an env var; pass --ref with what the server runs")
    info = inspect(ref)
    digest = _running_digest(info, name)
    if tag is None:
        labels = (info.get("Config") or {}).get("Labels") or {}
        tag = labels.get("org.opencontainers.image.version")
        if not tag:
            raise LockError(f"{ref} has no version label; pass --tag with the release it runs")
    try:
        tag_digest = resolver(name, tag)
    except Exception as err:  # registry missing the tag, or unreachable
        if not force:
            raise LockError(f"couldn't confirm {name}:{tag} in the registry: {err} (use --force to pin anyway)") from err
        warnings.append(f"couldn't confirm {name}:{tag} in the registry: {err}")
    else:
        if tag_digest != digest:
            msg = f"{name}:{tag} now points at {tag_digest}, but the server runs {digest}"
            if not force:
                raise LockError(msg + " (pass the right --tag, or --force to pin the running digest)")
            warnings.append(msg)
    _pin(lock, name, tag, digest)
    return tag, digest, warnings


def adopt_all(
    lock: Lock,
    *,
    inspect: Callable[[str], dict] = docker_inspect,
    resolver: Resolver = resolve_digest,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Adopt every compose image that has no versions.yaml entry yet.

    Images already listed (pinned or float) are left alone. One image failing
    (not running here, no version label, tag moved, env-var tag) doesn't stop
    the rest. Returns ``(adopted, skipped)`` as ``(name, tag)`` and
    ``(name, reason)`` pairs.
    """
    listed = lock.entries()
    names = sorted(
        {u.ref.name for u in scan(lock.root, lock.excludes, lock.extra_files)
         if u.kind == "compose" and u.ref is not None and u.ref.name not in listed}
    )
    adopted, skipped = [], []
    for name in names:
        try:
            tag, _, _ = adopt(lock, name, inspect=inspect, resolver=resolver)
        except Exception as err:  # each failure is reported, the rest carry on
            skipped.append((name, str(err)))
        else:
            adopted.append((name, tag))
    return adopted, skipped
