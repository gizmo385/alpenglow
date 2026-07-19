"""Service inventory: repo scan + docker inventory merge (work package B1).

A1 provides route stubs backed by mock fixtures (MOCK_DATA=1). B1 replaces the
non-mock paths with:

- ``/services/*/compose.yaml`` scanning (PyYAML) → caddy labels ``→`` url/tier,
  restart policy, ports/expose, memory limits;
- docker-socket container state (image, uptime, status) grouped by the
  ``com.docker.compose.project`` label (aiodocker);
- ``metadata.yaml`` merge (category / icon / description / primary_container);
- sidebar meta (hostname + branch parsed from ``/services/.git/HEAD``).

The ``/services`` path and docker socket location both come from env
(``SERVICES_ROOT`` / ``DOCKER_HOST``) with the documented defaults so tests can
point at fixtures instead of the live repo.

Category resolution (F1): the effective category is the per-service settings
override (settings.json via :mod:`tags`) → the ``metadata.yaml`` default →
``"Infrastructure"``. SSO status is no longer auto-detected (F1) — it is a
user-maintained tag, so there is no ``sso`` field on the contract.

Seams for sibling packages (do not implement here):
- ``apply_busy_overlay`` — B2 replaces this to overlay ``restarting``/``updating``
  from its in-flight action registry onto the docker-derived status.
- ``latestVersion`` / ``releasedAt`` / ``changelogUrl`` stay ``None`` here; B4
  fills them from the updates tracker by image ref.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from . import mock, models, tags as tags_store
from .integrations import beszel

router = APIRouter(prefix="/api", tags=["inventory"])


# ── settings ──────────────────────────────────────────────────────────────────


def services_root() -> Path:
    return Path(os.environ.get("SERVICES_ROOT", "/services"))


def metadata_path() -> Path:
    """Locate ``metadata.yaml`` (per-service category/icon/description overrides).

    Order:
      1. ``METADATA_PATH`` env override (tests point this at a fixture);
      2. the service dir inside the read-only ``/services`` mount
         (``<SERVICES_ROOT>/alpenglow_dashboard/metadata.yaml``) — the container
         path, since the file is not copied into the image;
      3. the repo checkout relative to this module (host dev / editable install).
    """
    override = os.environ.get("METADATA_PATH")
    if override:
        return Path(override)
    mounted = services_root() / "alpenglow_dashboard" / "metadata.yaml"
    if mounted.is_file():
        return mounted
    # .../app/backend/alpenglow_dashboard/inventory.py → up 3 → app/, up 1 more → service root
    return Path(__file__).resolve().parents[3] / "metadata.yaml"


# ── docker-status → contract status ───────────────────────────────────────────

_CATEGORY_DEFAULT = "Infrastructure"
_ICON_DEFAULT = "ph-cube"


def _docker_state_to_status(state: str) -> models.Status:
    """Map a docker container ``State`` string to the contract ``up``/``down``.

    Docker only knows up/down; the busy states (restarting/updating) are
    overlaid by B2 via :func:`apply_busy_overlay`.
    """
    return "up" if state == "running" else "down"


_STATUS_RANK = {"down": 0, "restarting": 1, "updating": 2, "up": 3}


def _worst(statuses: list[models.Status]) -> models.Status:
    if not statuses:
        return "down"
    return min(statuses, key=lambda s: _STATUS_RANK.get(s, 0))


# ── caddy label parsing → tier + url ──────────────────────────────────────────


@dataclass
class CaddyRoute:
    site: str  # the caddy site label value, e.g. "*.acbc.house", "acbc.house", "*.internal.acbc.house:10443"
    host: Optional[str] = None  # the @x.host value, e.g. "glances.alpenglow.acbc.house"


def _collect_caddy_routes(labels: dict[str, str]) -> list[CaddyRoute]:
    """Group caddy labels into routes keyed by their caddy prefix.

    caddy-docker-proxy uses either a single ``caddy`` prefix or numbered
    ``caddy_0``/``caddy_1`` prefixes; the site value is the bare prefix label and
    the matcher host lives in ``<prefix>.@name.host``.
    """
    routes: dict[str, CaddyRoute] = {}
    for key, value in labels.items():
        if not isinstance(value, str):
            value = str(value)
        # bare site label: "caddy" or "caddy_0"
        if key == "caddy" or (key.startswith("caddy_") and "." not in key):
            routes.setdefault(key, CaddyRoute(site="")).site = value.strip()
            continue
        # host matcher: "caddy.@foo.host" or "caddy_1.@foo.host"
        if key.endswith(".host") and (key.startswith("caddy.@") or key.startswith("caddy_")):
            prefix = key.split(".", 1)[0]
            routes.setdefault(prefix, CaddyRoute(site="")).host = value.strip()
    return [r for r in routes.values() if r.site]


def _site_is_public(site: str) -> bool:
    return ":10443" in site


def _site_domain(site: str) -> str:
    """Strip a wildcard and the ``:10443`` port suffix from a site label."""
    return site.split(":", 1)[0].lstrip("*").lstrip(".")


def _route_hostname(route: CaddyRoute) -> Optional[str]:
    """The concrete hostname a route resolves to (for building the url)."""
    if route.host:
        return route.host
    domain = _site_domain(route.site)
    # A literal apex label ("caddy: acbc.house") has no wildcard and no @host.
    if domain and "*" not in route.site and not route.site.startswith("*"):
        return domain
    return None


def derive_tier_and_url(labels: dict[str, str]) -> tuple[models.Tier, Optional[str]]:
    """Derive (tier, url) from a service's merged caddy labels.

    Tier precedence (per PLAN.md Ground truth), highest wins:
      1. ``*.internal.acbc.house`` present            → ``internal``  (LAN-only)
      2. ``*.alpenglow.acbc.house`` present           → ``management``
      3. a ``:10443`` label set present               → ``public``
      4. any other ``*.acbc.house`` / apex label      → ``tailnet``
      5. no caddy labels                              → ``internal`` (no ingress)

    Rules 1 & 2 deliberately win over the public ``:10443`` flag — uptime_kuma
    carries both an ``*.alpenglow.acbc.house`` set AND an ``*.alpenglow…:10443``
    set and must classify as ``management``. home_assistant carries both a
    tailnet ``*.acbc.house`` and an ``*.internal.acbc.house`` set; the internal
    pattern is the LAN-only mirror, so it does not force the whole service to
    ``internal`` — see below.
    """
    routes = _collect_caddy_routes(labels)
    if not routes:
        return "internal", None

    domains = [_site_domain(r.site) for r in routes]
    has_internal = any(d.endswith("internal.acbc.house") for d in domains)
    has_management = any(d.endswith("alpenglow.acbc.house") for d in domains)
    has_public = any(_site_is_public(r.site) for r in routes)
    # "tailnet" primary routes: plain *.acbc.house or the literal apex, not
    # internal, not management.
    tailnet_domains = [
        d for d in domains
        if d.endswith("acbc.house")
        and not d.endswith("internal.acbc.house")
        and not d.endswith("alpenglow.acbc.house")
    ]

    # ── tier ──
    if has_management:
        tier: models.Tier = "management"
    elif has_internal and not tailnet_domains:
        # internal is the *only* ingress → LAN-only service (e.g. immichframe)
        tier = "internal"
    elif has_public:
        tier = "public"
    elif tailnet_domains:
        tier = "tailnet"
    elif has_internal:
        # has internal + something odd but no tailnet host → treat as internal
        tier = "internal"
    else:
        tier = "tailnet"

    # ── url: prefer the primary (non-:10443) route matching the chosen tier ──
    url = _pick_url(routes, tier)
    return tier, url


def _pick_url(routes: list[CaddyRoute], tier: models.Tier) -> Optional[str]:
    """Choose the user-facing URL for the service.

    Preference: a non-public (no ``:10443``) route whose domain matches the tier
    we settled on, then any non-public route, then any route. Internal mirror
    routes (``*.internal.acbc.house``) are deprioritised unless the tier itself
    is internal.
    """
    def domain(r: CaddyRoute) -> str:
        return _site_domain(r.site)

    def is_internal(r: CaddyRoute) -> bool:
        return domain(r).endswith("internal.acbc.house")

    non_public = [r for r in routes if not _site_is_public(r.site)]

    ordered: list[CaddyRoute]
    if tier == "management":
        ordered = [r for r in non_public if domain(r).endswith("alpenglow.acbc.house")] or non_public
    elif tier == "internal":
        ordered = [r for r in non_public if is_internal(r)] or non_public
    elif tier == "tailnet":
        ordered = [r for r in non_public if not is_internal(r)] or non_public
    else:  # public
        ordered = [r for r in non_public if not is_internal(r)] or non_public or routes

    for route in ordered:
        host = _route_hostname(route)
        if host:
            return f"https://{host}"
    # last resort: any route with a resolvable host
    for route in routes:
        host = _route_hostname(route)
        if host:
            return f"https://{host}"
    return None


# ── compose file scan ─────────────────────────────────────────────────────────


@dataclass
class RepoService:
    id: str
    compose_path: Path
    services: dict[str, dict]  # compose "services:" block
    # Effective compose project name: the compose file's top-level ``name:`` key
    # when present (it overrides the dir-name default), else the dir name. This
    # is what docker stamps as ``com.docker.compose.project``.
    project_name: str = ""
    labels: dict[str, str] = field(default_factory=dict)  # merged across containers
    tier: models.Tier = "internal"
    url: Optional[str] = None
    restart_policy: str = "—"
    ports: str = "—"
    mem_limit: Optional[str] = None
    # service-name → container_name (for docker matching / primary selection)
    container_names: dict[str, str] = field(default_factory=dict)


def _merged_labels(services_block: dict[str, dict]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for svc in services_block.values():
        if not isinstance(svc, dict):
            continue
        labels = svc.get("labels")
        if isinstance(labels, dict):
            for k, v in labels.items():
                merged[str(k)] = v if isinstance(v, str) else str(v)
        elif isinstance(labels, list):
            for item in labels:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    merged[k.strip()] = v.strip()
    return merged


def _restart_policy(services_block: dict[str, dict]) -> str:
    """The strongest restart policy across containers ('always' > 'unless-stopped')."""
    order = {"no": 0, "on-failure": 1, "unless-stopped": 2, "always": 3}
    best = None
    for svc in services_block.values():
        if not isinstance(svc, dict):
            continue
        policy = svc.get("restart")
        if policy is None:
            continue
        policy = str(policy).strip().strip("'\"")
        if best is None or order.get(policy, -1) > order.get(best, -1):
            best = policy
    return best or "—"


def _ports_summary(services_block: dict[str, dict]) -> str:
    """A compact, de-duplicated port summary from ``ports:``/``expose:``."""
    seen: list[str] = []

    def add(p: str) -> None:
        p = str(p).strip().strip("'\"")
        if not p:
            return
        # ports "8096:8096" → host-facing; keep container side for display
        if ":" in p:
            p = p.split(":")[-1]
        p = p.split("/")[0]  # drop protocol suffix like /tcp
        if p and p not in seen:
            seen.append(p)

    for svc in services_block.values():
        if not isinstance(svc, dict):
            continue
        for key in ("ports", "expose"):
            val = svc.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        add(str(item.get("target") or item.get("published") or ""))
                    else:
                        add(str(item))
    return "/".join(seen) if seen else "—"


def _mem_limit(services_block: dict[str, dict]) -> Optional[str]:
    """Human-readable memory limit from ``deploy.resources.limits.memory`` or
    the legacy ``mem_limit`` key, taken from the largest-limited container."""
    def to_bytes(raw: str) -> Optional[int]:
        raw = str(raw).strip()
        units = {"k": 10**3, "m": 10**6, "g": 10**9, "ki": 2**10, "mi": 2**20, "gi": 2**30,
                 "kb": 10**3, "mb": 10**6, "gb": 10**9}
        num = raw.rstrip("bBkKmMgGiI")
        suffix = raw[len(num):].lower()
        try:
            n = float(num)
        except ValueError:
            return None
        return int(n * units.get(suffix, 1))

    def fmt(raw: str) -> str:
        raw = str(raw).strip()
        # normalise "2G" → "2 GB", "512M" → "512 MB"
        b = to_bytes(raw)
        if b is None:
            return raw
        if b >= 2**30 or b >= 10**9:
            return f"{round(b / 10**9)} GB" if b % 10**9 == 0 else f"{round(b / 2**30)} GB"
        return f"{round(b / 10**6)} MB"

    best_raw: Optional[str] = None
    best_bytes = -1
    for svc in services_block.values():
        if not isinstance(svc, dict):
            continue
        raw = None
        deploy = svc.get("deploy")
        if isinstance(deploy, dict):
            limits = (deploy.get("resources") or {}).get("limits") if isinstance(deploy.get("resources"), dict) else None
            if isinstance(limits, dict) and limits.get("memory"):
                raw = str(limits["memory"])
        if raw is None and svc.get("mem_limit"):
            raw = str(svc["mem_limit"])
        if raw is None:
            continue
        b = to_bytes(raw) or 0
        if b > best_bytes:
            best_bytes, best_raw = b, raw
    return fmt(best_raw) if best_raw is not None else None


def _container_names(services_block: dict[str, dict]) -> dict[str, str]:
    """Map compose service name → explicit container_name (when set)."""
    out: dict[str, str] = {}
    for name, svc in services_block.items():
        if isinstance(svc, dict) and svc.get("container_name"):
            out[name] = str(svc["container_name"])
    return out


def scan_repo(root: Optional[Path] = None) -> dict[str, RepoService]:
    """Scan ``<root>/*/compose.yaml`` and derive per-service repo facts.

    Directories without a compose.yaml (e.g. ``zfs_status_checker``, ``docs``)
    are skipped, per the contract.
    """
    root = root or services_root()
    result: dict[str, RepoService] = {}
    if not root.is_dir():
        return result
    for entry in sorted(root.iterdir()):
        compose = entry / "compose.yaml"
        if not entry.is_dir() or not compose.is_file():
            continue
        try:
            doc = yaml.safe_load(compose.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        services_block = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services_block, dict):
            services_block = {}
        # Honour the compose file's top-level ``name:`` override (compose stamps
        # it as com.docker.compose.project); default to the directory name.
        raw_name = doc.get("name") if isinstance(doc, dict) else None
        project_name = str(raw_name).strip() if isinstance(raw_name, (str, int)) and str(raw_name).strip() else entry.name
        labels = _merged_labels(services_block)
        tier, url = derive_tier_and_url(labels)
        result[entry.name] = RepoService(
            id=entry.name,
            compose_path=compose,
            services=services_block,
            project_name=project_name,
            labels=labels,
            tier=tier,
            url=url,
            restart_policy=_restart_policy(services_block),
            ports=_ports_summary(services_block),
            mem_limit=_mem_limit(services_block),
            container_names=_container_names(services_block),
        )
    return result


# ── metadata.yaml merge ───────────────────────────────────────────────────────


def load_metadata(path: Optional[Path] = None) -> dict[str, dict]:
    path = path or metadata_path()
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    services = doc.get("services") if isinstance(doc, dict) else None
    return services if isinstance(services, dict) else {}


# ── docker socket inventory (aiodocker) ───────────────────────────────────────


@dataclass
class DockerContainer:
    name: str
    project: Optional[str]
    service: Optional[str]  # com.docker.compose.service label
    state: str  # docker "State": running / exited / …
    image: str
    started_at: Optional[str]
    # com.docker.compose.project.working_dir — the absolute service dir on the
    # host; used as the authoritative fallback for attributing a container to a
    # /services directory when the project *name* doesn't equal the dir name.
    working_dir: Optional[str] = None
    exit_code: Optional[int] = None  # State.ExitCode (0 = clean exit)
    # docker HostConfig.RestartPolicy.Name — "no"/"" for one-shot/init jobs,
    # "always"/"unless-stopped"/"on-failure" otherwise.
    restart_policy: Optional[str] = None


def _is_completed_oneoff(c: DockerContainer) -> bool:
    """True for a compose service that ran to completion and is *meant* to exit.

    An init/one-shot container (compose ``restart: no``, e.g. ollama's
    ``ollama-pull`` model-download job) that exited cleanly (code 0) is a
    *success*, not an outage — it must not drag a service's aggregate status to
    ``down``. We require BOTH signals so a crashed/never-restarted service
    (``restart: no`` + non-zero exit) still surfaces as down.
    """
    if c.state == "running":
        return False
    policy = (c.restart_policy or "").strip().lower()
    is_oneoff = policy in ("", "no")
    return is_oneoff and c.exit_code == 0


async def docker_inventory() -> dict[str, list[DockerContainer]]:
    """Return running+stopped containers grouped by compose project.

    Grouping key preference (so containers land on the right ``/services`` dir
    even when the compose project *name* was overridden or the dir renamed):
    the ``com.docker.compose.project.working_dir`` basename when it points into
    the services root, else the ``com.docker.compose.project`` label, else the
    container name. Consumers additionally index by working-dir path (see
    :func:`_containers_for`).

    Returns ``{}`` (never raises) if the docker socket is unavailable, so the
    inventory degrades to repo-only data rather than 500ing.
    """
    import aiodocker

    grouped: dict[str, list[DockerContainer]] = {}
    docker = aiodocker.Docker()  # honours DOCKER_HOST, defaults to the unix socket
    try:
        containers = await docker.containers.list(all=True)
        for c in containers:
            try:
                info = await c.show()
            except Exception:
                continue
            config = info.get("Config", {}) or {}
            labels = config.get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            service = labels.get("com.docker.compose.service")
            working_dir = labels.get("com.docker.compose.project.working_dir")
            names = info.get("Name", "")
            name = names.lstrip("/") if isinstance(names, str) else str(c.id)[:12]
            state_obj = info.get("State", {}) or {}
            exit_raw = state_obj.get("ExitCode")
            host_config = info.get("HostConfig", {}) or {}
            restart = (host_config.get("RestartPolicy") or {}).get("Name")
            dc = DockerContainer(
                name=name,
                project=project,
                service=service,
                state=str(state_obj.get("Status", "")),
                image=str(config.get("Image", "")),
                started_at=state_obj.get("StartedAt"),
                working_dir=working_dir,
                exit_code=int(exit_raw) if isinstance(exit_raw, int) else None,
                restart_policy=str(restart) if restart is not None else None,
            )
            grouped.setdefault(project or name, []).append(dc)
    except Exception:
        return {}
    finally:
        try:
            await docker.close()
        except Exception:
            pass
    return grouped


def _uptime_seconds(started_at: Optional[str]) -> Optional[int]:
    """Parse docker's RFC3339 StartedAt into seconds of uptime."""
    if not started_at or started_at.startswith("0001-01-01"):
        return None
    from datetime import datetime, timezone

    iso = started_at.replace("Z", "+00:00")
    # docker gives nanosecond precision; python only handles microseconds
    if "." in iso:
        head, tail = iso.split(".", 1)
        frac = ""
        for ch in tail:
            if ch.isdigit():
                frac += ch
            else:
                tail = tail[len(frac):]
                break
        iso = f"{head}.{frac[:6]}{tail}"
    try:
        started = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - started
    return max(0, int(delta.total_seconds()))


# ── seams for sibling packages ────────────────────────────────────────────────


def apply_busy_overlay(service_id: str, status: models.Status) -> models.Status:
    """Overlay ``restarting``/``updating`` from the in-flight action registry
    owned by :mod:`.actions` (B2 implements ``busy_status``)."""
    from .actions import busy_status  # function-level: avoids import cycle

    return busy_status(service_id) or status


def resolve_category(meta: dict, override: Optional[str]) -> str:
    """Effective category: settings override → metadata.yaml default →
    ``"Infrastructure"``."""
    if isinstance(override, str) and override.strip():
        return override.strip()
    default = meta.get("category")
    if isinstance(default, str) and default.strip():
        return default.strip()
    return _CATEGORY_DEFAULT


# ── summary/detail assembly ───────────────────────────────────────────────────


def _select_primary(
    sid: str, repo: RepoService, meta: dict, containers: list[DockerContainer]
) -> tuple[Optional[str], Optional[DockerContainer]]:
    """Choose the primary container.

    Order: metadata override (matched against compose service name OR
    container_name), else the container whose compose service name matches the
    dir name, else the first container.
    """
    override = meta.get("primary_container")

    def match(target: str) -> Optional[DockerContainer]:
        for c in containers:
            if c.service == target or c.name == target:
                return c
        return None

    # metadata override
    if override:
        c = match(override)
        if c:
            return override, c
        # override may name a compose service without a running container
        if override in repo.services or override in repo.container_names.values():
            return override, None

    # container whose compose service name == dir name
    if sid in repo.services:
        c = match(sid)
        if c or repo.services:
            return sid, c

    # else first compose service / first container
    if containers:
        return containers[0].service or containers[0].name, containers[0]
    if repo.services:
        first = next(iter(repo.services))
        return first, None
    return None, None


def _compose_image_for(repo: RepoService, service_name: Optional[str]) -> str:
    """Best-effort image string from compose when docker isn't available."""
    if service_name and service_name in repo.services:
        svc = repo.services[service_name]
        if isinstance(svc, dict) and svc.get("image"):
            return str(svc["image"])
    for svc in repo.services.values():
        if isinstance(svc, dict) and svc.get("image"):
            return str(svc["image"])
    return "—"


async def _build_summary(
    sid: str,
    repo: RepoService,
    meta: dict,
    containers: list[DockerContainer],
    svc_tags: list[str],
    category_override: Optional[str] = None,
) -> models.ServiceDetail:
    primary_name, primary = _select_primary(sid, repo, meta, containers)

    # container refs + aggregate status
    #
    # A completed one-shot/init container (compose ``restart: no`` that exited
    # 0 — e.g. ollama's model-pull job) is listed for transparency but excluded
    # from the worst-of aggregate: a *successful* run-to-completion must not
    # report the whole service as down. If EVERY container is such a one-shot we
    # fall back to worst-of-all so the service isn't spuriously "up".
    refs: list[models.ContainerRef] = []
    statuses: list[models.Status] = []
    oneoff_statuses: list[models.Status] = []
    for c in containers:
        st = _docker_state_to_status(c.state)
        refs.append(models.ContainerRef(name=c.name, status=c.state or ("running" if st == "up" else "down")))
        if _is_completed_oneoff(c):
            oneoff_statuses.append(st)
        else:
            statuses.append(st)
    if not statuses and oneoff_statuses:
        # only one-shots (all completed) → don't mask them; use their statuses
        statuses = oneoff_statuses
    if not containers:
        # nothing running/known → present the compose services as down
        for name in repo.services:
            cname = repo.container_names.get(name, name)
            refs.append(models.ContainerRef(name=cname, status="down"))
            statuses.append("down")

    status = apply_busy_overlay(sid, _worst(statuses))

    image = primary.image if primary else _compose_image_for(repo, primary_name)
    current_version = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else "latest"
    uptime = _uptime_seconds(primary.started_at) if primary else None
    if status != "up":
        uptime = None

    from .integrations.updates import service_enrichment  # avoids import cycle

    enrich = (await service_enrichment()).get(sid) or {}

    name = meta.get("name") or sid.replace("_", " ").replace("-", " ").title()

    return models.ServiceDetail(
        id=sid,
        name=name,
        category=resolve_category(meta, category_override),
        icon=meta.get("icon", _ICON_DEFAULT),
        description=meta.get("description", ""),
        image=image,
        currentVersion=current_version,
        latestVersion=enrich.get("latestVersion"),
        releasedAt=enrich.get("releasedAt"),
        changelogUrl=enrich.get("changelogUrl"),
        status=status,
        uptimeSeconds=uptime,
        url=repo.url,
        tier=repo.tier,
        containers=refs,
        tags=list(svc_tags),
        restartPolicy=repo.restart_policy,
        ports=repo.ports,
        memLimit=repo.mem_limit,
        composePath=str(repo.compose_path),
        projectName=sid,
        primaryContainer=primary.name if primary else primary_name,
    )


def _containers_for(
    repo: RepoService,
    docker_all: dict[str, list[DockerContainer]],
    services_dir: Path,
) -> list[DockerContainer]:
    """Attribute running/stopped containers to a service directory.

    Two matching strategies, in order, both anchored to values docker itself
    stamps on the container — so a container can never be mis-attributed to a
    dir it wasn't started from:

    1. **Project name** — the compose project label
       (``com.docker.compose.project``), which equals the compose file's
       top-level ``name:`` when set, else the dir name. This is the normal path.
    2. **Working directory** — the ``com.docker.compose.project.working_dir``
       label equals the absolute service dir path. This rescues services whose
       project *name* was overridden or whose dir was renamed after the
       containers started, where strategy 1 finds nothing. Matching on the
       absolute path is unambiguous: exactly one ``/services/<id>`` dir owns it.

    Strategy 2 only runs as a fallback when strategy 1 is empty, and never
    reassigns a container already grouped under a different project name, so the
    two strategies cannot double-count or steal another dir's containers.
    """
    by_name = docker_all.get(repo.project_name, [])
    if by_name:
        return by_name
    target = str((services_dir / repo.id).resolve())
    matched = [
        c
        for group in docker_all.values()
        for c in group
        if c.working_dir and str(Path(c.working_dir)) == target
    ]
    return matched


async def build_inventory() -> list[models.ServiceDetail]:
    """The full inventory: repo scan × docker × metadata × tags."""
    root = services_root()
    repos = scan_repo(root)
    meta_all = load_metadata()
    docker_all = await docker_inventory()
    # Prune settings for unknown service ids so a stale store entry can never
    # inject a phantom tag/category into the inventory or the tag-filter union.
    settings_all = await tags_store.all_settings(known_ids=set(repos))

    # index docker containers by project, falling back to dir-name match
    out: list[models.ServiceDetail] = []
    for sid, repo in repos.items():
        containers = _containers_for(repo, docker_all, root)
        meta = meta_all.get(sid, {})
        settings = settings_all.get(sid, {"tags": [], "category": None})
        out.append(
            await _build_summary(
                sid, repo, meta, containers, settings["tags"], settings["category"]
            )
        )
    return out


async def build_service(service_id: str) -> Optional[models.ServiceDetail]:
    root = services_root()
    repos = scan_repo(root)
    repo = repos.get(service_id)
    if repo is None:
        return None
    meta = load_metadata().get(service_id, {})
    containers = _containers_for(repo, await docker_inventory(), root)
    settings = (await tags_store.all_settings()).get(
        service_id, {"tags": [], "category": None}
    )
    return await _build_summary(
        service_id, repo, meta, containers, settings["tags"], settings["category"]
    )


# ── sidebar meta (host + branch) ──────────────────────────────────────────────


def sidebar_meta() -> models.OverviewMeta:
    return models.OverviewMeta(host=repo_hostname(), branch=repo_branch())


def repo_hostname() -> str:
    host = socket.gethostname()
    # present the short host label ("alpenglow"), not the FQDN
    return host.split(".", 1)[0] if host else "alpenglow"


def repo_branch() -> str:
    """Parse ``/services/.git/HEAD`` without shelling out to git."""
    head = services_root() / ".git" / "HEAD"
    try:
        text = head.read_text().strip()
    except OSError:
        return "main"
    if text.startswith("ref:"):
        return text.split("/")[-1].strip()
    return text[:7] if text else "main"  # detached HEAD → short sha


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/services")
async def list_services() -> list[models.ServiceSummary]:
    if mock.mock_enabled():
        return mock.mock_services()
    details = await build_inventory()
    # ServiceDetail is a superset of ServiceSummary; FastAPI serialises the
    # declared ServiceSummary shape, dropping the detail-only fields.
    return [models.ServiceSummary(**d.model_dump()) for d in details]


@router.get("/services/{service_id}")
async def get_service(service_id: str) -> models.ServiceDetail:
    if mock.mock_enabled():
        detail = mock.mock_service_detail(service_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return detail
    detail = await build_service(service_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    return detail


@router.get("/services/{service_id}/beszel")
async def get_service_beszel(service_id: str) -> list[models.BeszelContainer]:
    """Live Beszel per-container stats for the service's containers.

    Always 200s with a (possibly empty) list — Beszel being unconfigured/down,
    or none of the service's containers being tracked, just yields ``[]`` and the
    detail tab hides the section. 404 only for an unknown service id.
    """
    if mock.mock_enabled():
        detail = mock.mock_service_detail(service_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return mock.mock_beszel_containers(detail)
    detail = await build_service(service_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    try:
        return await beszel.containers_for([c.name for c in detail.containers])
    except Exception:
        return []


@router.get("/services/{service_id}/compose", response_class=PlainTextResponse)
async def get_compose(service_id: str) -> str:
    if mock.mock_enabled():
        text = mock.mock_compose_text(service_id)
        if text is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return text
    compose = services_root() / service_id / "compose.yaml"
    if not compose.is_file():
        raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
    try:
        return compose.read_text()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="could not read compose.yaml") from exc
