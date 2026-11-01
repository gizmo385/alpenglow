"""Load, validate and save versions.yaml.

The lock file is edited both by people and by ``pin set``, so it is read and
written with ruamel.yaml's round-trip mode: comments, key order and quoting
survive a ``pin set``.
"""

from __future__ import annotations

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
            )
        return out

    def validate(self) -> list[str]:
        """Problems with the lock file itself, independent of the repo."""
        errors: list[str] = []
        for name, e in self.entries().items():
            where = f"{LOCK_NAME}: {name}"
            if e.policy not in POLICIES:
                errors.append(f"{where}: policy {e.policy!r} is not one of {', '.join(POLICIES)}")
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

    def set_field(self, name: str, key: str, value: Any) -> None:
        raw = self.raw(name)
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value

    def save(self) -> None:
        with self.path.open("w") as fh:
            _yaml().dump(self.doc, fh)


def _opt_str(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)
