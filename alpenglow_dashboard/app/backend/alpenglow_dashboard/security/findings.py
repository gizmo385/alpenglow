"""The cve-scanner's findings, attributed to services and filtered.

The scanner writes everything Grype and GitHub report
(``cve-scanner/data/findings.json``). Most of it is noise: base-OS packages
with a fix nobody has shipped yet, low-severity bugs. A finding is *open*
when it clears the bar below and isn't ignored in ``cve-scanner/ignore.yaml``:

- it's on CISA's known-exploited list, whatever its severity;
- it's an advisory on the app's own GitHub repo (the scanner checks those
  separately, since a package scan often misses the app itself);
- it's Critical or High and a fixed version exists;
- its EPSS (chance of exploitation in the next 30 days) is at least
  ``SECURITY_EPSS_THRESHOLD`` (10% by default).

Pure apart from reading the two files, so it's easy to test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("alpenglow.security")

SEVERITIES = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")


def severity_rank(sev: str) -> int:
    try:
        return SEVERITIES.index(sev.capitalize())
    except ValueError:
        return len(SEVERITIES)


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    package: str
    version: str
    type: str
    aliases: tuple[str, ...] = ()
    fixed_in: tuple[str, ...] = ()
    fix_state: str = "unknown"
    epss: float | None = None
    epss_percentile: float | None = None
    kev: bool = False
    url: str | None = None
    summary: str = ""
    source: str = "grype"

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        def num(v):
            return float(v) if isinstance(v, (int, float)) else None

        return cls(
            id=str(d.get("id") or ""),
            severity=str(d.get("severity") or "Unknown").capitalize(),
            package=str(d.get("package") or ""),
            version=str(d.get("version") or ""),
            type=str(d.get("type") or ""),
            aliases=tuple(str(a) for a in d.get("aliases") or ()),
            fixed_in=tuple(str(v) for v in d.get("fixed_in") or ()),
            fix_state=str(d.get("fix_state") or "unknown"),
            epss=num(d.get("epss")),
            epss_percentile=num(d.get("epss_percentile")),
            kev=bool(d.get("kev")),
            url=str(d["url"]) if d.get("url") else None,
            summary=str(d.get("summary") or ""),
            source=str(d.get("source") or "grype"),
        )

    @property
    def ids(self) -> set[str]:
        return {self.id, *self.aliases}

    @property
    def fixed(self) -> bool:
        return bool(self.fixed_in) and self.fix_state in ("fixed", "unknown")


def why_open(f: Finding, epss_threshold: float) -> str | None:
    """Why a finding clears the bar, or ``None`` when it doesn't."""
    if f.kev:
        return "known exploited"
    if f.source == "ghsa":
        return "advisory on the app's repo"
    if f.severity in ("Critical", "High") and f.fixed:
        return f"{f.severity.lower()}, fix available"
    if f.epss is not None and f.epss >= epss_threshold:
        return f"EPSS {f.epss:.0%}"
    return None


@dataclass(frozen=True)
class Ignore:
    id: str
    image: str = "*"  # an image name without tag, or * for every image
    reason: str = ""

    def matches(self, image_names: set[str], f: Finding) -> bool:
        return self.id in f.ids and (self.image == "*" or self.image in image_names)


def load_ignores(path: Path | None) -> list[Ignore]:
    if not path or not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as err:
        log.warning("security: can't read %s: %s", path, err)
        return []
    out = []
    for raw in data.get("ignore") or []:
        if isinstance(raw, dict) and raw.get("id"):
            out.append(Ignore(str(raw["id"]), str(raw.get("image") or "*"), str(raw.get("reason") or "")))
    return out


def strip_tag(ref: str) -> str:
    ref = ref.split("@")[0]
    prefix, _, last = ref.rpartition("/")
    last = last.split(":")[0]
    name = f"{prefix}/{last}" if prefix else last
    for p in ("docker.io/", "index.docker.io/"):
        name = name.removeprefix(p)
    return name.removeprefix("library/")


@dataclass
class ImageScan:
    image_id: str
    ref: str
    local_build: bool = False
    repo_digests: tuple[str, ...] = ()
    packages: int | None = None
    error: str | None = None
    repo: str | None = None
    containers: tuple[str, ...] = ()
    findings: list[Finding] = field(default_factory=list)

    @property
    def names(self) -> set[str]:
        return {strip_tag(self.ref)}

    @property
    def digest(self) -> str | None:
        for d in self.repo_digests:
            if "@" in d:
                return d.split("@", 1)[1]
        return None


@dataclass(frozen=True)
class OpenFinding:
    image: str  # the image's ref
    finding: Finding
    why: str

    @property
    def id(self) -> str:
        return self.finding.id


@dataclass
class ServiceFindings:
    id: str
    images: list[ImageScan] = field(default_factory=list)
    open: list[OpenFinding] = field(default_factory=list)
    ignored: list[tuple[OpenFinding, Ignore]] = field(default_factory=list)
    below_bar: int = 0  # findings that didn't clear the bar

    @property
    def scanned(self) -> bool:
        """Every image scanned cleanly, so "no open findings" can be trusted."""
        return bool(self.images) and all(i.error is None for i in self.images)

    @property
    def open_ids(self) -> set[str]:
        return {o.id for o in self.open}

    @property
    def worst(self) -> str | None:
        if not self.open:
            return None
        return min((o.finding.severity for o in self.open), key=severity_rank)


@dataclass
class Scan:
    scanned_at: str | None
    errors: list[str]
    db_built: str | None
    images: list[ImageScan]
    services: dict[str, ServiceFindings]


def service_id(container: dict) -> str | None:
    """The /services directory a container came from: its compose working dir, else its project."""
    wd = container.get("working_dir")
    if wd:
        return Path(str(wd)).name
    return container.get("project") or None


def _db_built(db: object) -> str | None:
    if isinstance(db, dict):
        for k in ("built", "Built"):
            if db.get(k):
                return str(db[k])
        for v in db.values():
            got = _db_built(v)
            if got:
                return got
    return None


def build(doc: dict, ignores: list[Ignore], epss_threshold: float) -> Scan:
    images: list[ImageScan] = []
    services: dict[str, ServiceFindings] = {}
    for raw in doc.get("images") or []:
        if not isinstance(raw, dict):
            continue
        img = ImageScan(
            image_id=str(raw.get("image_id") or ""),
            ref=str(raw.get("ref") or raw.get("image_id") or ""),
            local_build=bool(raw.get("local_build")),
            repo_digests=tuple(str(d) for d in raw.get("repo_digests") or ()),
            packages=raw.get("packages") if isinstance(raw.get("packages"), int) else None,
            error=str(raw["error"]) if raw.get("error") else None,
            repo=str(raw["repo"]) if raw.get("repo") else None,
            containers=tuple(str(c.get("name")) for c in raw.get("containers") or [] if isinstance(c, dict)),
            findings=[Finding.from_dict(f) for f in raw.get("findings") or [] if isinstance(f, dict) and f.get("id")],
        )
        images.append(img)
        sids = {service_id(c) for c in raw.get("containers") or [] if isinstance(c, dict)} - {None}
        for sid in sorted(sids):
            sf = services.setdefault(sid, ServiceFindings(sid))
            sf.images.append(img)
            seen = sf.open_ids | {o.id for o, _ in sf.ignored}
            for f in img.findings:
                why = why_open(f, epss_threshold)
                if why is None:
                    sf.below_bar += 1
                    continue
                if f.id in seen:
                    continue  # the same advisory in two packages or two images: one finding
                seen.add(f.id)
                of = OpenFinding(img.ref, f, why)
                ign = next((i for i in ignores if i.matches(img.names, f)), None)
                if ign:
                    sf.ignored.append((of, ign))
                else:
                    sf.open.append(of)
    for sf in services.values():
        sf.open.sort(key=lambda o: (not o.finding.kev, severity_rank(o.finding.severity), o.id))
    return Scan(
        scanned_at=str(doc["scanned_at"]) if doc.get("scanned_at") else None,
        errors=[str(e) for e in doc.get("errors") or []],
        db_built=_db_built(doc.get("db")),
        images=images,
        services=services,
    )


def load(path: Path, ignore_path: Path | None, epss_threshold: float) -> Scan | None:
    """``None`` when the scanner hasn't written anything yet (or wrote garbage)."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        log.warning("security: can't read %s: %s", path, err)
        return None
    if not isinstance(doc, dict):
        return None
    return build(doc, load_ignores(ignore_path), epss_threshold)
