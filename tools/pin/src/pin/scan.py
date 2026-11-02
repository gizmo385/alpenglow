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
import re
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


def find_files(
    root: Path, excludes: list[str], extra: list[str]
) -> tuple[list[Path], list[Path], list[Path]]:
    """``(compose_files, extra_files, dockerfiles)``, relative to ``root``, sorted."""
    compose: set[Path] = set()
    docker: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _excluded(rel.as_posix(), excludes):
            continue
        if any(fnmatch.fnmatch(path.name, g) for g in COMPOSE_GLOBS):
            compose.add(rel)
        elif any(fnmatch.fnmatch(path.name, g) for g in DOCKERFILE_GLOBS):
            docker.add(rel)
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
