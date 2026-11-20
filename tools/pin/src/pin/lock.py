"""Load, validate and save versions.yaml.

The lock file is edited both by people and by ``pin set``, so it is read and
written with ruamel.yaml's round-trip mode: comments, key order and quoting
survive a ``pin set``.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .refs import DIGEST_RE, FLOATING_TAGS

LOCK_NAME = "versions.yaml"

POLICIES = ("auto-eligible", "approve", "manual", "float")
DEFAULT_POLICY = "approve"

# Paths never scanned, whatever the lock file says.
ALWAYS_EXCLUDED = (".git/**", "**/node_modules/**", "**/.venv/**")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


@dataclass
class Entry:
    name: str
    tag: str | None = None
    digest: str | None = None
    group: str | None = None
    policy: str = DEFAULT_POLICY
    reason: str | None = None
    plugins: dict[str, str] = field(default_factory=dict)
    auto: Any = None  # the upgrade pipeline's settings; pin only validates them

    @property
    def floating(self) -> bool:
        return self.policy == "float"


class LockError(Exception):
    pass


class Lock:
    """versions.yaml, plus the raw round-trip document for saving."""

    def __init__(self, path: Path, doc: CommentedMap):
        self.path = path
        self.doc = doc

    # ── reading ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Lock":
        if not path.is_file():
            raise LockError(f"{path} not found")
        doc = _yaml().load(path.read_text()) or CommentedMap()
        if not isinstance(doc, dict):
            raise LockError(f"{path}: top level must be a mapping")
        doc.setdefault("images", CommentedMap())
        return cls(path, doc)

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def excludes(self) -> list[str]:
        return list(ALWAYS_EXCLUDED) + [str(p) for p in self.doc.get("exclude") or []]

    @property
    def extra_files(self) -> list[str]:
        return [str(p) for p in self.doc.get("extra_files") or []]

    def entries(self) -> dict[str, Entry]:
        out: dict[str, Entry] = {}
        for name, raw in (self.doc.get("images") or {}).items():
            raw = raw or {}
            out[str(name)] = Entry(
                name=str(name),
                tag=_opt_str(raw.get("tag")),
                digest=_opt_str(raw.get("digest")),
                group=_opt_str(raw.get("group")),
                policy=str(raw.get("policy") or DEFAULT_POLICY),
                reason=_opt_str(raw.get("reason")),
                plugins={str(k): str(v) for k, v in (raw.get("plugins") or {}).items()},
                auto=raw.get("auto"),
            )
        return out

    def validate(self) -> list[str]:
        """Problems with the lock file itself, independent of the repo."""
        errors: list[str] = []
        for name, e in self.entries().items():
            where = f"{LOCK_NAME}: {name}"
            if e.policy not in POLICIES:
                errors.append(f"{where}: policy {e.policy!r} is not one of {', '.join(POLICIES)}")
            errors += [f"{where}: {problem}" for problem in auto_problems(e)]
            if e.floating:
                if not e.reason:
                    errors.append(f"{where}: float entries need a reason")
                continue
            if not e.tag:
                errors.append(f"{where}: pinned entries need a tag")
            elif e.tag in FLOATING_TAGS:
                errors.append(f"{where}: tag {e.tag!r} is a moving channel, not a version")
            if e.digest and not DIGEST_RE.match(e.digest):
                errors.append(f"{where}: digest {e.digest!r} is not sha256:<64 hex>")
            for mod, ver in e.plugins.items():
                if not ver or ver == "latest":
                    errors.append(f"{where}: plugin {mod} needs a version")
        return errors

    def group_members(self, name: str) -> list[str]:
        entries = self.entries()
        group = entries[name].group
        if not group:
            return [name]
        return [n for n, e in entries.items() if e.group == group]

    # ── writing ──────────────────────────────────────────────────────────

    def raw(self, name: str) -> CommentedMap:
        images = self.doc["images"]
        if name not in images or images[name] is None:
            images[name] = CommentedMap()
        return images[name]

    # New keys go in this order, so a digest lands right after its tag instead of
    # at the end of the entry, where ruamel would put it after any comment that
    # trails the entry (such as the next section's heading).
    KEY_ORDER = ("tag", "digest", "group", "policy", "reason")

    def set_field(self, name: str, key: str, value: Any) -> None:
        raw = self.raw(name)
        if value is None:
            raw.pop(key, None)
        elif key in raw or key not in self.KEY_ORDER:
            raw[key] = value
        else:
            before = self.KEY_ORDER[: self.KEY_ORDER.index(key)]
            pos = max((i + 1 for i, k in enumerate(raw) if k in before), default=0)
            raw.insert(pos, key, value)

    def dumps(self) -> str:
        buf = io.StringIO()
        _yaml().dump(self.doc, buf)
        return buf.getvalue()

    def save(self) -> None:
        self.path.write_text(self.dumps())


# ── auto: the dashboard's automatic updates ─────────────────────────────────
#
# pin doesn't act on these; it checks them so a typo fails `pin check` in CI
# instead of quietly turning automatic updates off. The dashboard reads them
# (alpenglow_dashboard/app/backend/alpenglow_dashboard/autoupdate/policy.py),
# so keep the two in step.

AUTO_KEYS = ("every", "max_bump", "soak", "verdict", "monitor", "follow")
BUMPS = ("patch", "minor", "major")
VERDICTS = ("Easy", "Needs care", "any")
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
_EVERY_WORDS = ("hourly", "daily", "weekly")
_CRON_FIELD_RE = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?(,(\*|\d+(-\d+)?)(/\d+)?)*$")


def _duration_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_DURATION_RE.match(value))


def _every_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value.strip().lower() in _EVERY_WORDS or _duration_ok(value):
        return True
    fields = value.split()
    return len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields)


def auto_problems(e: Entry) -> list[str]:
    """What's wrong with an entry's ``auto`` block, if anything."""
    if e.auto is None:
        return []
    if not isinstance(e.auto, dict):
        return ["auto must be a mapping"]
    out = [f"auto: unknown setting {k!r} (known: {', '.join(AUTO_KEYS)})" for k in e.auto if k not in AUTO_KEYS]
    follow = e.auto.get("follow")
    if follow not in (None, True, False):
        out.append("auto: follow must be true or false")
    if e.floating and follow is not True:
        out.append("auto: a float entry can only have `follow: true` (it has no version to bump)")
    if not e.floating and e.policy != "auto-eligible":
        out.append(f"auto: only applies with policy auto-eligible (this entry is {e.policy})")
    if not e.floating and follow:
        out.append("auto: follow is for float entries that track a moving tag such as main")
    if "every" in e.auto and not _every_ok(e.auto["every"]):
        out.append("auto: every must be hourly, daily, weekly, a duration like 6h or 2d, or a 5-field cron line")
    elif _duration_ok(e.auto.get("every")):
        m = _DURATION_RE.match(e.auto["every"])
        minutes = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440, "w": 10080}[m.group(2)]
        if minutes < 15:
            out.append("auto: every must be at least 15m")
    if "soak" in e.auto and not _duration_ok(e.auto["soak"]):
        out.append("auto: soak must be a duration like 12h or 3d")
    if "max_bump" in e.auto and e.auto["max_bump"] not in BUMPS:
        out.append(f"auto: max_bump must be one of {', '.join(BUMPS)}")
    if "verdict" in e.auto and e.auto["verdict"] not in VERDICTS:
        out.append(f"auto: verdict must be one of {', '.join(VERDICTS)}")
    monitor = e.auto.get("monitor")
    if monitor is not None and not (
        (isinstance(monitor, str) and monitor.strip())
        or (isinstance(monitor, list) and monitor and all(isinstance(m, str) and m.strip() for m in monitor))
    ):
        out.append("auto: monitor must be an Uptime Kuma monitor name or a list of them")
    return out


def _opt_str(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


@contextlib.contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on versions.yaml while a command edits the repo.

    Commands that write (``set``, ``sync``, ``adopt``, ...) read the file, change
    it and rewrite compose files; two at once, say the dashboard's Update
    button and someone at a shell, would lose one of the edits. The lock is an
    advisory ``flock`` on versions.yaml itself, so there's no separate lock
    file whose ownership could get in the way (flock works on a read-only
    descriptor). A second writer waits for the first.
    """
    with path.open("rb") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"waiting for another pin command to finish with {path.name}...", file=sys.stderr)
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
