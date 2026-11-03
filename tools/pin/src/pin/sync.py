"""Work out what each usage should say, and rewrite the files that differ."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .lock import LOCK_NAME, Entry, Lock, LockError
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


def _render(root: Path, changes: list[Change]) -> dict[Path, str]:
    """The new text of each file the changes touch, by path relative to ``root``."""
    by_file: dict[Path, list[Change]] = {}
    for c in changes:
        by_file.setdefault(c.usage.path, []).append(c)
    out = {}
    for rel, file_changes in by_file.items():
        lines = (root / rel).read_text().split("\n")
        for c in file_changes:
            lines[c.usage.line - 1] = _rewrite_line(lines[c.usage.line - 1], c)
        out[rel] = "\n".join(lines)
    return out


def _write_together(root: Path, contents: dict[Path, str]) -> None:
    """Write every file or, as far as the filesystem allows, none of them.

    Anything that can't be written is found before the first write, and a write
    that fails anyway (a full disk) puts back the files already touched. Files
    are rewritten in place: a rename would lose their owner and group, and the
    flock held on versions.yaml.
    """
    blocked = [str(rel) for rel in contents if not os.access(root / rel, os.W_OK)]
    if blocked:
        raise LockError(f"can't write {', '.join(blocked)}; nothing was changed")
    originals = {rel: (root / rel).read_text() for rel in contents}
    written: list[Path] = []
    try:
        for rel, text in contents.items():
            written.append(rel)
            (root / rel).write_text(text)
    except OSError as err:
        unrestored = []
        for rel in written:
            try:
                (root / rel).write_text(originals[rel])
            except OSError:
                unrestored.append(str(rel))
        outcome = (
            f"couldn't restore {', '.join(unrestored)}; check them with `git diff`"
            if unrestored
            else "nothing was changed"
        )
        raise LockError(f"writing {written[-1]} failed: {err}; {outcome}") from err


def apply(root: Path, changes: list[Change], lock: Lock | None = None) -> list[Path]:
    """Write the changes, and ``lock`` when given, together; return the files changed.

    versions.yaml goes first: if the process is killed partway, it's still the
    truth and ``pin sync`` finishes the job.
    """
    rendered = _render(root, changes)
    contents = {Path(LOCK_NAME): lock.dumps()} if lock else {}
    _write_together(root, contents | rendered)
    return sorted(rendered)
