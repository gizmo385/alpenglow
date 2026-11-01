"""``pin``: keep image versions in versions.yaml and write them into the repo.

    pin check                       compare the repo with versions.yaml
    pin sync [--dry-run]            write versions.yaml into compose files and Dockerfiles
    pin set NAME TAG                pin an image (and its group), resolving the digest
    pin plugin NAME MODULE VERSION  pin an xcaddy plugin of a base image
    pin digests                     look up missing digests for pinned images
    pin adopt NAME [--tag T]        pin what the server runs now (run on the server)
    pin list                        show every entry
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .check import check
from .lock import LOCK_NAME, Lock, LockError
from .ops import adopt, fill_digests, set_plugin, set_tag
from .registry import RegistryError
from .sync import apply, plan


def find_root(start: Path) -> Path:
    for d in [start, *start.parents]:
        if (d / LOCK_NAME).is_file():
            return d
    raise LockError(f"no {LOCK_NAME} in {start} or any parent; pass --root")


def _sync(lock: Lock, dry_run: bool = False) -> int:
    changes = plan(lock)
    for c in changes:
        print(f"{c.usage.where()}: {c.old} -> {c.new}")
    if not dry_run and changes:
        files = apply(lock.root, changes)
        print(f"updated {len(files)} file(s)")
    elif not changes:
        print("everything matches versions.yaml")
    return 0


def cmd_check(lock: Lock, args: argparse.Namespace) -> int:
    report = check(lock)
    if not args.quiet:
        for w in report.warnings:
            print(f"warning: {w}")
    for e in report.errors:
        print(f"error: {e}")
    if report.ok:
        print("versions.yaml and the repo agree")
        return 0
    print(f"{len(report.errors)} problem(s)")
    return 1


def cmd_sync(lock: Lock, args: argparse.Namespace) -> int:
    return _sync(lock, args.dry_run)


def cmd_set(lock: Lock, args: argparse.Namespace) -> int:
    names = set_tag(lock, args.name, args.tag, resolve=not args.no_digest)
    lock.save()
    print(f"pinned {', '.join(names)} to {args.tag}")
    return _sync(lock)


def cmd_plugin(lock: Lock, args: argparse.Namespace) -> int:
    set_plugin(lock, args.name, args.module, args.version)
    lock.save()
    print(f"pinned {args.module} to {args.version}")
    return _sync(lock)


def cmd_digests(lock: Lock, args: argparse.Namespace) -> int:
    filled, failures = fill_digests(lock)
    for f in failures:
        print(f"warning: {f}")
    if filled:
        lock.save()
        print(f"filled {len(filled)} digest(s)")
        _sync(lock)
    return 1 if failures else 0


def cmd_adopt(lock: Lock, args: argparse.Namespace) -> int:
    tag, digest, warnings = adopt(lock, args.name, tag=args.tag, ref=args.ref, force=args.force)
    for w in warnings:
        print(f"warning: {w}")
    lock.save()
    print(f"pinned {args.name} to {tag}@{digest}")
    return _sync(lock)


def cmd_list(lock: Lock, args: argparse.Namespace) -> int:
    for name, e in sorted(lock.entries().items()):
        if e.floating:
            print(f"{name:55} float      {e.reason}")
        else:
            digest = (e.digest or "")[:19]
            print(f"{name:55} {e.policy:10} {e.tag:16} {digest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pin", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, help=f"repo root holding {LOCK_NAME} (default: search upward)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("check", help="compare the repo with versions.yaml")
    s.add_argument("-q", "--quiet", action="store_true", help="hide warnings")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("sync", help="write versions.yaml into compose files and Dockerfiles")
    s.add_argument("-n", "--dry-run", action="store_true", help="show changes without writing")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("set", help="pin an image (and its group) to a tag")
    s.add_argument("name")
    s.add_argument("tag")
    s.add_argument("--no-digest", action="store_true", help="don't look up the digest")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("plugin", help="pin an xcaddy plugin of a base image")
    s.add_argument("name")
    s.add_argument("module")
    s.add_argument("version")
    s.set_defaults(func=cmd_plugin)

    s = sub.add_parser("digests", help="look up missing digests for pinned images")
    s.set_defaults(func=cmd_digests)

    s = sub.add_parser("adopt", help="pin the image the server runs now")
    s.add_argument("name")
    s.add_argument("--tag", help="release tag (default: the image's version label)")
    s.add_argument("--ref", help="image reference to inspect (for env-var tags)")
    s.add_argument("--force", action="store_true", help="pin the running digest even if the tag moved")
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("list", help="show every entry")
    s.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve() if args.root else find_root(Path.cwd())
        lock = Lock.load(root / LOCK_NAME)
        return args.func(lock, args)
    except (LockError, RegistryError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
