"""Work out what each usage should say, and rewrite the files that differ."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .lock import Entry, Lock
from .refs import format_ref, split_variant
from .scan import FROM_LINE_RE, IMAGE_LINE_RE, Usage, scan


@dataclass(frozen=True)
class Change:
    usage: Usage
    old: str
    new: str


def plugin_entry(usage: Usage, entries: dict[str, Entry]) -> Entry | None:
    """The lock entry that pins this plugin: a base image of its Dockerfile listing it."""
    for name in usage.base_images:
        e = entries.get(name)
        if e and usage.module in e.plugins:
            return e
    return None


def desired(usage: Usage, entries: dict[str, Entry]) -> str | None:
    """What the usage should read, or None when the lock doesn't manage it."""
    if usage.kind == "plugin":
        e = plugin_entry(usage, entries)
        if e is None:
            return None
        return f"{usage.module}@{e.plugins[usage.module]}"
    assert usage.ref is not None
    e = entries.get(usage.ref.name)
    if e is None or e.floating or not e.tag:
        return None
    if usage.kind == "dockerfile":
        # Keep the variant: caddy:2.11.7-builder follows caddy's tag as 2.11.8-builder.
        _, suffix = split_variant(usage.ref.tag or "")
        return format_ref(usage.ref.name, e.tag + suffix)
    if usage.kind == "extra":
        return format_ref(usage.ref.name, e.tag)
    return format_ref(usage.ref.name, e.tag, e.digest)


def current(usage: Usage) -> str:
    if usage.kind == "plugin":
        return usage.module + (f"@{usage.version}" if usage.version else "")
    return str(usage.ref)


def plan(lock: Lock) -> list[Change]:
    entries = lock.entries()
    changes = []
    for u in scan(lock.root, lock.excludes, lock.extra_files):
        want = desired(u, entries)
        have = current(u)
        if want is not None and want != have:
            changes.append(Change(u, have, want))
    return changes


def _rewrite_line(line: str, change: Change) -> str:
    kind = change.usage.kind
    if kind in ("compose", "extra"):
        m = IMAGE_LINE_RE.match(line)
        assert m, line
        return m.group("prefix") + m.group("q") + change.new + m.group("q") + m.group("rest")
    if kind == "dockerfile":
        m = FROM_LINE_RE.match(line)
        assert m, line
        return m.group("prefix") + change.new + m.group("rest")
    mod = re.escape(change.usage.module or "")
    return re.sub(rf"--with\s+{mod}(?:@[^\s\\]+)?(?=[\s\\]|$)", f"--with {change.new}", line, count=1)


def apply(root: Path, changes: list[Change]) -> list[Path]:
    """Write the changes; return the files touched."""
    by_file: dict[Path, list[Change]] = {}
    for c in changes:
        by_file.setdefault(c.usage.path, []).append(c)
    for rel, file_changes in by_file.items():
        path = root / rel
        text = path.read_text()
        lines = text.split("\n")
        for c in file_changes:
            lines[c.usage.line - 1] = _rewrite_line(lines[c.usage.line - 1], c)
        path.write_text("\n".join(lines))
    return sorted(by_file)
