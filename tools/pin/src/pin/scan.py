"""Find every image reference in the repo.

Three kinds of usage are found, each with its file and line so ``sync`` can
rewrite exactly that line and ``check`` can point at it:

* ``compose``: an ``image:`` line in a compose file.
* ``extra``: an ``image:`` line in another YAML file the lock lists under
  ``extra_files``, such as SigNoz's ``casting.yaml``. These get the tag only,
  since the tool that reads them may not accept a digest.
* ``dockerfile``: a ``FROM`` line in a Dockerfile.
* ``plugin``: an ``xcaddy build --with module[@version]`` flag in a Dockerfile.

Lines are matched with regular expressions rather than parsed as YAML, so a
rewrite touches only the reference and keeps comments, quotes and layout.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .refs import ImageRef, parse_ref

COMPOSE_GLOBS = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
DOCKERFILE_GLOBS = ("Dockerfile", "Dockerfile.*", "*.Dockerfile")

IMAGE_LINE_RE = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?image:\s*)(?P<q>["']?)(?P<ref>[^\s"'#]+)(?P=q)(?P<rest>\s*(?:\#.*)?)$"""
)
FROM_LINE_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--platform=\S+\s+)?)(?P<ref>\S+)(?P<rest>.*)$", re.IGNORECASE
)
STAGE_RE = re.compile(r"\bAS\s+(?P<stage>\S+)\s*$", re.IGNORECASE)
PLUGIN_RE = re.compile(r"--with\s+(?P<mod>[^\s@\\]+)(?:@(?P<ver>[^\s\\]+))?")


@dataclass(frozen=True)
class Usage:
    path: Path  # relative to the repo root
    line: int  # 1-based
    kind: str  # compose | extra | dockerfile | plugin
    ref: ImageRef | None = None
    module: str | None = None  # plugin only
    version: str | None = None  # plugin only
    base_images: tuple[str, ...] = ()  # plugin only: FROM images of its Dockerfile

    def where(self) -> str:
        return f"{self.path}:{self.line}"


def _excluded(rel: str, excludes: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat) for pat in excludes)


def _kind(name: str) -> str | None:
    if any(fnmatch.fnmatch(name, g) for g in COMPOSE_GLOBS):
        return "compose"
    if any(fnmatch.fnmatch(name, g) for g in DOCKERFILE_GLOBS):
        return "dockerfile"
    return None


def _git_files(root: Path) -> list[str] | None:
    """Files git tracks under ``root``, or ``None`` when git can't say.

    Reads the index instead of the disk, so service data directories (database
    files, caches, uploads) are never walked. ``safe.directory`` lets it work
    under sudo in a checkout owned by someone else.
    """
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "ls-files", "-z", "--cached"],
            check=True, capture_output=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [f for f in out.decode("utf-8", "replace").split("\0") if f]


def _new_service_files(root: Path) -> list[str]:
    """Files in the repo root and each top-level directory, not recursing.

    Picks up a service that isn't committed yet (``create_service.sh`` runs
    ``pin add`` before the first commit) without walking anything deep.
    """
    out = []
    for d in [root, *(c for c in root.iterdir() if c.is_dir() and not c.name.startswith("."))]:
        try:
            out += [f.relative_to(root).as_posix() for f in d.iterdir() if _kind(f.name)]
        except OSError:  # a data directory we can't read
            continue
    return out


def _walk_files(root: Path, excludes: list[str]) -> list[str]:
    """Every file under ``root``, skipping excluded directories instead of
    walking into them. The fallback when git can't list the files."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not _excluded(f"{rel_dir}{d}/", excludes)]
        out += [f"{rel_dir}{f}" for f in filenames]
    return out


def find_files(
    root: Path, excludes: list[str], extra: list[str]
) -> tuple[list[Path], list[Path], list[Path]]:
    """``(compose_files, extra_files, dockerfiles)``, relative to ``root``, sorted.

    In a git checkout this is the tracked files plus compose files and
    Dockerfiles at the top of each service directory; otherwise a walk of the
    tree that skips excluded directories.
    """
    tracked = _git_files(root)
    candidates = tracked + _new_service_files(root) if tracked is not None else _walk_files(root, excludes)
    compose: set[Path] = set()
    docker: set[Path] = set()
    for rel in candidates:
        kind = _kind(rel.rsplit("/", 1)[-1])
        if kind is None or _excluded(rel, excludes) or not (root / rel).is_file():
            continue
        (compose if kind == "compose" else docker).add(Path(rel))
    extras = {Path(e) for e in extra if (root / e).is_file()}
    return sorted(compose - extras), sorted(extras), sorted(docker)


def scan_compose(root: Path, rel: Path, kind: str = "compose") -> list[Usage]:
    out = []
    for i, line in enumerate((root / rel).read_text().splitlines(), start=1):
        m = IMAGE_LINE_RE.match(line)
        if m:
            out.append(Usage(rel, i, kind, parse_ref(m.group("ref"))))
    return out


def scan_dockerfile(root: Path, rel: Path) -> list[Usage]:
    lines = (root / rel).read_text().splitlines()
    stages: set[str] = set()
    froms: list[Usage] = []
    for i, line in enumerate(lines, start=1):
        m = FROM_LINE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        stage = STAGE_RE.search(m.group("rest"))
        # FROM <earlier stage>, FROM scratch and FROM ${ARG} aren't images we pin.
        if ref.lower() != "scratch" and ref not in stages and "$" not in ref:
            froms.append(Usage(rel, i, "dockerfile", parse_ref(ref)))
        if stage:
            stages.add(stage.group("stage"))
    bases = tuple(sorted({u.ref.name for u in froms if u.ref}))
    plugins = []
    for i, line in enumerate(lines, start=1):
        for m in PLUGIN_RE.finditer(line):
            plugins.append(
                Usage(rel, i, "plugin", module=m.group("mod"), version=m.group("ver"), base_images=bases)
            )
    return froms + plugins


def scan(root: Path, excludes: list[str], extra: list[str]) -> list[Usage]:
    compose, extras, docker = find_files(root, excludes, extra)
    usages: list[Usage] = []
    for rel in compose:
        usages.extend(scan_compose(root, rel))
    for rel in extras:
        usages.extend(scan_compose(root, rel, kind="extra"))
    for rel in docker:
        usages.extend(scan_dockerfile(root, rel))
    return usages
