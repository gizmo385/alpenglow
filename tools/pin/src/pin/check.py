"""Compare the repo with versions.yaml.

Only images listed in versions.yaml are managed. Unlisted images are left
alone, so services can be moved onto the lock one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lock import LOCK_NAME, Lock
from .scan import scan
from .sync import current, desired, plugin_entry


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(lock: Lock) -> Report:
    report = Report(errors=lock.validate())
    entries = lock.entries()
    used: set[str] = set()
    in_compose: set[str] = set()

    for u in scan(lock.root, lock.excludes, lock.extra_files):
        if u.kind == "plugin":
            e = plugin_entry(u, entries)
            if e is None:
                if not any(b in entries for b in u.base_images):
                    continue  # the base image isn't managed yet, so neither are its plugins
                bases = ", ".join(u.base_images) or "its base image"
                report.errors.append(
                    f"{u.where()}: plugin {u.module} isn't pinned; add it under plugins: of {bases} in {LOCK_NAME}"
                )
                continue
        else:
            assert u.ref is not None
            name = u.ref.name
            e = entries.get(name)
            if e is None:
                continue  # not managed yet: only listed images are checked
            used.add(name)
            if u.kind == "compose":
                in_compose.add(name)
            if e.floating:
                continue
        want = desired(u, entries)
        have = current(u)
        if want is not None and want != have:
            report.errors.append(f"{u.where()}: says {have}, {LOCK_NAME} says {want} (run `pin sync`)")

    for name, e in entries.items():
        if name not in used:
            report.warnings.append(f"{LOCK_NAME}: {name} isn't used anywhere")
        elif not e.floating and not e.digest and name in in_compose:
            report.warnings.append(f"{LOCK_NAME}: {name} has no digest (`pin set {name} {e.tag}` resolves one)")
    return report

