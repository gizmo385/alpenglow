"""Mock fixtures + MOCK_DATA=1 mode (work package A1).

Fixture data mirrors the REAL service directories in /services (ids = repo dir
names, images/hosts/tiers taken from the actual compose files at scaffold time).
Status/version/update flavor is borrowed from the design prototype. Every API
route serves from here while MOCK_DATA=1; real implementations (packages B1-B5)
replace these code paths but must keep this mode working.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import models

SERVICES_ROOT = Path(os.environ.get("SERVICES_ROOT", "/services"))


def mock_enabled() -> bool:
    return os.environ.get("MOCK_DATA", "0") == "1"


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


# ── fixture service table ─────────────────────────────────────────────────────
# One entry per real /services/<dir> that has a compose.yaml.
# Fields: name, cat, icon, desc, image, version, latest, releasedAgo (days),
#         changelog, status, uptime (s), url, tier, containers, tags,
#         restart, ports, memLimit
#
# SSO is no longer auto-detected (F1): it is expressed as user-maintained tags.
# "Keycloak SSO" renders as the accent key chip; "Identity provider" as the
# accent shield-star chip (mirrors the pre-F1 keycloak/self chip styles).

_D = "unless-stopped"

MOCK_SERVICES: dict[str, dict] = {
    # ── Media ──
    "immich": dict(
        name="Immich", cat="Media", icon="ph-images",
        desc="Self-hosted photo & video backup. OIDC login wired to Keycloak; ML jobs run on the shared GPU.",
        image="ghcr.io/immich-app/immich-server:release", version="v2.5.1",
        latest="v2.6.0", released=3, changelog="https://github.com/immich-app/immich/releases",
        status="up", uptime=1051200, url="https://photos.acbc.house", tier="public",
        containers=["immich_server", "immich_machine_learning", "immich_public_proxy"],
        tags=["GPU", "User data"],
        restart=_D, ports="2283", memLimit=None,
    ),
    "jellyfin": dict(
        name="Jellyfin", cat="Media", icon="ph-film-slate",
        desc="Media server with hardware transcoding via /dev/dri. Exposed publicly on :10443.",
        image="jellyfin/jellyfin:10.11.11", version="10.11.11",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://jellyfin.acbc.house", tier="public",
        containers=["jellyfin"], tags=["GPU", "Family"], restart=_D, ports="8096", memLimit=None,
    ),
    "music_assistant": dict(
        name="Music Assistant", cat="Media", icon="ph-music-notes",
        desc="Music library and streaming bridge for Home Assistant.",
        image="ghcr.io/music-assistant/server:latest", version="2.6.0",
        latest="2.7.1", released=6, changelog="https://github.com/music-assistant/server/releases",
        status="up", uptime=694800, url="https://music.acbc.house", tier="tailnet",
        containers=["music-assistant-server"], tags=[], restart=_D, ports="8095", memLimit=None,
    ),
    "immichframe": dict(
        name="ImmichFrame", cat="Media", icon="ph-monitor",
        desc="Digital photo-frame client pulling from Immich. Local network only.",
        image="ghcr.io/immichframe/immichframe:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=694800, url="https://immichframe.internal.acbc.house", tier="internal",
        containers=["immichframe"], tags=[], restart=_D, ports="8080", memLimit=None,
    ),
    # ── Productivity ──
    "nextcloud": dict(
        name="Nextcloud", cat="Productivity", icon="ph-cloud",
        desc="Files, calendar and contacts. Keycloak SSO via the user_oidc app.",
        image="nextcloud:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://nextcloud.acbc.house", tier="tailnet",
        containers=["nextcloud"], tags=["Keycloak SSO", "User data", "Critical"], restart=_D, ports="80", memLimit="2 GB",
    ),
    "linkwarden": dict(
        name="Linkwarden", cat="Productivity", icon="ph-bookmark-simple",
        desc="Bookmark & link archive. Uses Keycloak for authentication.",
        image="ghcr.io/linkwarden/linkwarden:latest", version="v2.13.1",
        latest="v2.14.0", released=4, changelog="https://github.com/linkwarden/linkwarden/releases",
        status="up", uptime=1051200, url="https://linkwarden.acbc.house", tier="tailnet",
        containers=["linkwarden"], tags=["Keycloak SSO"], restart=_D, ports="3000", memLimit=None,
    ),
    "tandoor": dict(
        name="Tandoor", cat="Productivity", icon="ph-cooking-pot",
        desc="Recipe manager and meal planner. Keycloak SSO enabled.",
        image="vabene1111/recipes:2.6.13", version="2.6.13",
        latest="2.7.0", released=2, changelog="https://github.com/TandoorRecipes/recipes/releases",
        status="up", uptime=1051200, url="https://recipes.acbc.house", tier="public",
        containers=["tandoor"], tags=["Keycloak SSO", "Family"], restart=_D, ports="8080", memLimit=None,
    ),
    "twenty": dict(
        name="Twenty", cat="Productivity", icon="ph-address-book",
        desc="Open-source CRM. Container exited after the last deploy — worker cannot reach Redis.",
        image="twentycrm/twenty:latest", version="v1.8.1",
        latest="v1.9.0", released=2, changelog="https://github.com/twentyhq/twenty/releases",
        status="down", uptime=None, url="https://crm.acbc.house", tier="tailnet",
        containers=["twenty-server-1", "twenty-worker-1"], tags=["Beta"], restart=_D, ports="3000", memLimit="2 GB",
    ),
    "freshrss": dict(
        name="FreshRSS", cat="Productivity", icon="ph-rss",
        desc="RSS aggregator serving feeds to the household readers.",
        image="freshrss/freshrss:latest", version="1.26.3",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://rss.acbc.house", tier="tailnet",
        containers=["freshrss"], tags=[], restart=_D, ports="80", memLimit=None,
    ),
    "youtube_rss_manager": dict(
        name="YouTube RSS", cat="Productivity", icon="ph-youtube-logo",
        desc="Turns YouTube subscriptions into clean RSS feeds for FreshRSS.",
        image="ghcr.io/gizmo385/youtube-rss-manager:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=464400, url="https://youtube-rss.acbc.house", tier="tailnet",
        containers=["youtube_rss_manager"], tags=[], restart=_D, ports="8080", memLimit=None,
    ),
    "trek": dict(
        name="Trek", cat="Productivity", icon="ph-airplane-takeoff",
        desc="Trip and travel planner — replaced the intended AdventureLog deployment.",
        image="mauriceboe/trek:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=259200, url="https://travel.acbc.house", tier="tailnet",
        containers=["trek"], tags=["Family"], restart=_D, ports="3000", memLimit=None,
    ),
    "servo_bot": dict(
        name="Servo", cat="Productivity", icon="ph-robot",
        desc="Discord bot (gizmo385/servo). No web ingress — talks outbound to Discord.",
        image="ghcr.io/gizmo385/servo:master", version="master",
        latest=None, released=None, changelog=None,
        status="up", uptime=464400, url=None, tier="internal",
        containers=["servo"], tags=[], restart=_D, ports="—", memLimit=None,
    ),
    # ── Home ──
    "home_assistant": dict(
        name="Home Assistant", cat="Home", icon="ph-house-line",
        desc="Home automation hub. Vendored hass-oidc-auth plugin provides Keycloak login.",
        image="ghcr.io/home-assistant/home-assistant:stable", version="stable",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://home.acbc.house", tier="tailnet",
        containers=["home_assistant"], tags=["Host network"], restart=_D, ports="8123", memLimit=None,
    ),
    "motioneye": dict(
        name="motionEye", cat="Home", icon="ph-video-camera",
        desc="NVR frontend for the property cameras.",
        image="ghcr.io/motioneye-project/motioneye:latest", version="0.44.0",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://cameras.acbc.house", tier="tailnet",
        containers=["motioneye"], tags=[], restart=_D, ports="8765", memLimit=None,
    ),
    "mosquitto": dict(
        name="Mosquitto", cat="Home", icon="ph-broadcast",
        desc="MQTT broker bridging Zigbee/Z-Wave devices to Home Assistant. No web UI.",
        image="eclipse-mosquitto:2", version="2",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url=None, tier="internal",
        containers=["mosquitto"], tags=[], restart=_D, ports="1883", memLimit=None,
    ),
    "amniotic": dict(
        name="Amniotic", cat="Home", icon="ph-speaker-high",
        desc="Multi-channel ambient sound machine controlled through Home Assistant over MQTT.",
        image="fmtr/amniotic:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=694800, url=None, tier="internal",
        containers=["amniotic"], tags=[], restart=_D, ports="8007", memLimit=None,
    ),
    # ── Infrastructure ──
    "caddy": dict(
        name="Caddy", cat="Infrastructure", icon="ph-shield-check",
        desc="Reverse proxy and TLS termination. Reads routing from container labels (caddy-docker-proxy).",
        image="alpenglow/caddy (local build)", version="local",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url=None, tier="internal",
        containers=["caddy", "caddy-edge"], tags=["Critical"], restart=_D, ports="80/443/10443", memLimit=None,
    ),
    "postgres": dict(
        name="PostgreSQL", cat="Infrastructure", icon="ph-database",
        desc="Shared Postgres with pgvector. Backed up nightly via pg_dumpall.",
        image="alpenglow/postgres-pgvector (local build)", version="local",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url=None, tier="internal",
        containers=["postgres-postgres-1"], tags=["Critical", "Stateful"], restart="always", ports="5432", memLimit="4 GB",
    ),
    "redis": dict(
        name="Redis", cat="Infrastructure", icon="ph-lightning",
        desc="Cache and job queue shared across services.",
        image="redis:alpine", version="alpine",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url=None, tier="internal",
        containers=["redis-redis-1"], tags=["Stateful"], restart="always", ports="6379", memLimit="512 MB",
    ),
    "keycloak": dict(
        name="Keycloak", cat="Infrastructure", icon="ph-key",
        desc="The SSO identity provider itself — realm acbc.house backs every OIDC integration.",
        image="quay.io/keycloak/keycloak:26.6.4", version="26.6.4",
        latest="26.7.0", released=3, changelog="https://github.com/keycloak/keycloak/releases",
        status="up", uptime=1051200, url="https://sso.acbc.house", tier="public",
        containers=["keycloak-keycloak-1"], tags=["Identity provider", "Critical"], restart="always", ports="8080", memLimit=None,
    ),
    "pihole": dict(
        name="Pi-hole", cat="Infrastructure", icon="ph-funnel",
        desc="Network-wide DNS and ad-blocking, with dnscrypt-proxy upstream. Split-horizon DNS for acbc.house.",
        image="pihole/pihole:2026.05.0", version="2026.05.0",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://pihole.alpenglow.acbc.house", tier="management",
        containers=["pihole", "dnscrypt-proxy"], tags=["Critical"], restart=_D, ports="53/80", memLimit=None,
    ),
    "ollama": dict(
        name="Ollama", cat="Infrastructure", icon="ph-brain",
        desc="Local LLM runtime on the GPU. Powers the updates-tracker digest summaries.",
        image="ollama/ollama:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=694800, url=None, tier="internal",
        containers=["ollama"], tags=["GPU", "Experimental"], restart=_D, ports="11434", memLimit=None,
    ),
    "ddclient": dict(
        name="ddclient", cat="Infrastructure", icon="ph-globe-hemisphere-west",
        desc="Keeps the dynamic DNS record pointed at the current WAN IP.",
        image="lscr.io/linuxserver/ddclient:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url=None, tier="internal",
        containers=["ddclient"], tags=[], restart=_D, ports="—", memLimit=None,
    ),
    "copyparty": dict(
        name="Copyparty", cat="Infrastructure", icon="ph-folder-open",
        desc="Fast file server and drop-box for quick transfers, behind oauth2-proxy.",
        image="copyparty/ac:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=694800, url="https://files.alpenglow.acbc.house", tier="management",
        containers=["copyparty", "copyparty-oauth2-proxy"],
        tags=["Keycloak SSO"], restart=_D, ports="3923", memLimit=None,
    ),
    "attic": dict(
        name="Attic", cat="Infrastructure", icon="ph-package",
        desc="Self-hosted Nix binary cache server.",
        image="ghcr.io/zhaofengli/attic:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=464400, url="https://attic.alpenglow.acbc.house", tier="management",
        containers=["attic"], tags=["Experimental"], restart=_D, ports="8080", memLimit=None,
    ),
    "alpenglow_dashboard": dict(
        name="Alpenglow Dashboard", cat="Infrastructure", icon="ph-mountains",
        desc="This dashboard — at-a-glance health, updates and actions for every Alpenglow service.",
        image="alpenglow/dashboard (local build)", version="local",
        latest=None, released=None, changelog=None,
        status="up", uptime=3600, url="https://dashboard.alpenglow.acbc.house", tier="management",
        containers=["alpenglow_dashboard", "alpenglow-dashboard-oauth2-proxy"],
        tags=["Keycloak SSO"], restart=_D, ports="8080", memLimit=None,
    ),
    # ── Monitoring ──
    "beszel": dict(
        name="Beszel", cat="Monitoring", icon="ph-gauge",
        desc="Lightweight host & container resource monitoring with historical charts.",
        image="henrygd/beszel:latest", version="0.17.4",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://dash.alpenglow.acbc.house", tier="management",
        containers=["beszel", "beszel-agent"], tags=[], restart=_D, ports="8090", memLimit=None,
    ),
    "glances": dict(
        name="Glances", cat="Monitoring", icon="ph-chart-line",
        desc="Real-time system overview across CPU, disk, network and sensors.",
        image="nicolargo/glances:latest", version="4.4.1",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://glances.alpenglow.acbc.house", tier="management",
        containers=["glances"], tags=[], restart=_D, ports="61208", memLimit=None,
    ),
    "uptime_kuma": dict(
        name="Uptime Kuma", cat="Monitoring", icon="ph-heartbeat",
        desc="Uptime monitoring; receives webhook pings from backup and cron scripts.",
        image="louislam/uptime-kuma:2", version="2",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://uptime.alpenglow.acbc.house", tier="management",
        containers=["uptime_kuma"], tags=[], restart=_D, ports="3001", memLimit=None,
    ),
    "updates-tracker": dict(
        name="Updates Tracker", cat="Monitoring", icon="ph-arrows-clockwise",
        desc="Inspects running containers and checks GitHub for newer releases. Feeds this dashboard.",
        image="ghcr.io/gizmo385/image-updates-tracker:main", version="main",
        latest=None, released=None, changelog=None,
        status="up", uptime=464400, url="https://updates.alpenglow.acbc.house", tier="management",
        containers=["updates-tracker-release-feeds-1", "updates-tracker-bot-1"],
        tags=[], restart=_D, ports="8585", memLimit=None,
    ),
    "kopia": dict(
        name="Kopia", cat="Monitoring", icon="ph-archive",
        desc="File-level encrypted backups with a nightly verification pass.",
        image="kopia/kopia:latest", version="0.21.1",
        latest=None, released=None, changelog=None,
        status="up", uptime=1051200, url="https://kopia.alpenglow.acbc.house", tier="management",
        containers=["kopia"], tags=["Critical"], restart=_D, ports="51515", memLimit=None,
    ),
    "cup": dict(
        name="Cup", cat="Monitoring", icon="ph-coffee",
        desc="Lightweight checker for newer tags of running container images.",
        image="ghcr.io/sergi0g/cup:latest", version="latest",
        latest=None, released=None, changelog=None,
        status="up", uptime=259200, url="https://cup.alpenglow.acbc.house", tier="management",
        containers=["cup"], tags=[], restart=_D, ports="8000", memLimit=None,
    ),
}


# ── builders ──────────────────────────────────────────────────────────────────


def _summary(sid: str, s: dict) -> models.ServiceSummary:
    released = _ago(days=s["released"]) if s.get("released") is not None else None
    return models.ServiceSummary(
        id=sid,
        name=s["name"],
        category=s.get("category_override") or s["cat"],
        icon=s["icon"],
        description=s["desc"],
        image=s["image"],
        currentVersion=s["version"],
        latestVersion=s["latest"],
        releasedAt=released,
        changelogUrl=s["changelog"],
        status=s["status"],
        uptimeSeconds=s["uptime"],
        url=s["url"],
        tier=s["tier"],
        containers=[
            models.ContainerRef(name=c, status="running" if s["status"] != "down" else "exited")
            for c in s["containers"]
        ],
        tags=list(s["tags"]),
        restartPolicy=s["restart"],
        ports=s["ports"],
        memLimit=s["memLimit"],
    )


def mock_services() -> list[models.ServiceSummary]:
    return [_summary(sid, s) for sid, s in MOCK_SERVICES.items()]


def mock_service_detail(sid: str) -> models.ServiceDetail | None:
    s = MOCK_SERVICES.get(sid)
    if s is None:
        return None
    summary = _summary(sid, s)
    return models.ServiceDetail(
        **summary.model_dump(),
        composePath=f"/services/{sid}/compose.yaml",
        projectName=sid,
        primaryContainer=s["containers"][0] if s["containers"] else None,
    )


def mock_stats(sid: str) -> models.Stats | None:
    s = MOCK_SERVICES.get(sid)
    if s is None:
        return None
    seed = len(sid)
    now = time.time()
    down = s["status"] == "down"
    cpu_base = 0.0 if down else 4.0 + (seed % 7) * 2.5
    mem_base = 0.0 if down else (64 + (seed * 37) % 900) * 1024 * 1024
    cpu, mem = [], []
    for i in range(60):
        t = now - (59 - i) * 15
        wobble = 1 + 0.35 * math.sin(i * 0.6 + seed) + 0.12 * math.sin(i * 1.9 + seed * 2)
        cpu.append(models.StatPoint(t=t, v=round(max(0.0, cpu_base * wobble), 2)))
        mem.append(models.StatPoint(t=t, v=round(max(0.0, mem_base * (1 + 0.05 * math.sin(i * 0.4 + seed))))))
    return models.Stats(
        history=models.StatsHistory(cpu=cpu, mem=mem),
        current=models.StatsCurrent(
            cpuPct=None if down else cpu[-1].v,
            memUsed=None if down else mem[-1].v,
            memLimit=None,
            netIO=None if down else "1.2 MB / 340 kB",
            blockIO=None if down else "18 MB / 4 kB",
            pids=None if down else 6 + seed % 20,
            restarts=3 if down else 0,
        ),
    )


def mock_overview() -> models.Overview:
    services = mock_services()
    up = sum(1 for s in services if s.status != "down")
    down = len(services) - up
    updates = sum(1 for s in services if s.latestVersion)
    return models.Overview(
        services=models.OverviewServices(up=up, down=down, total=len(services)),
        updates=models.OverviewUpdates(count=updates),
        monitors=models.OverviewMonitors(
            up=23, total=24, note="1 in maintenance",
            monitors=[
                models.MonitorRef(name="Immich", status="down"),
                models.MonitorRef(name="Jellyfin", status="up"),
                models.MonitorRef(name="Postgres", status="up"),
                models.MonitorRef(name="dpool health", status="up"),
            ],
            url="https://uptime.alpenglow.acbc.house",
        ),
        backups=models.OverviewBackups(
            pgAgo=_ago(hours=3), kopiaAgo=_ago(hours=6),
            pgAt="2026-07-11 02:00 MDT", kopiaAt="2026-07-11 05:10 MDT",
            ok=True,
        ),
        host=models.OverviewHost(
            load1=0.62, load5=0.55, load15=0.48, cpuPct=18.0,
            memUsed=41.2 * 2**30, memTotal=64 * 2**30,
            swapUsed=0.3 * 2**30, swapTotal=8 * 2**30,
            cpuTemp=47.5, uptime=737378,
        ),
        storage=models.OverviewStorage(
            pools=[
                models.StoragePool(name="rpool", state="ONLINE", used=89 * 2**30,
                                   size=240 * 2**30, rawUsed=89 * 2**30,
                                   rawSize=240 * 2**30, scrubAgo=_ago(days=4), errors=0),
                models.StoragePool(name="dpool", state="ONLINE",
                                   used=1.9 * 2**40, size=7.1 * 2**40,
                                   rawUsed=2.9 * 2**40, rawSize=10.9 * 2**40,
                                   scrubAgo=_ago(days=4), errors=0),
            ],
            fs=[
                models.StorageFs(label="/data (dpool)", used=8.4 * 2**40, size=16 * 2**40, pct=52.0),
                models.StorageFs(label="/ (rpool)", used=89 * 2**30, size=240 * 2**30, pct=37.0),
            ],
        ),
        polledAt=datetime.now(timezone.utc).isoformat(),
        meta=models.OverviewMeta(host="alpenglow", branch="main"),
    )


def mock_host_charts() -> models.HostCharts:
    """Synthetic ~2h of one-minute host samples for the Overview chart card."""
    n = 120
    step = 60.0
    now = time.time()

    def series(base: float, amp: float, period: float, phase: float) -> list[models.StatPoint]:
        return [
            models.StatPoint(
                t=now - (n - 1 - i) * step,
                v=round(base + amp * math.sin(i / period + phase), 2),
            )
            for i in range(n)
        ]

    return models.HostCharts(
        cpu=series(18.0, 9.0, 7.0, 0.0),
        mem=series(52.0, 4.0, 11.0, 1.3),
        temp=series(47.0, 3.0, 9.0, 0.6),
        bandwidth=series(160_000, 90_000, 5.0, 2.1),
    )


def mock_beszel_containers(detail: models.ServiceDetail) -> list[models.BeszelContainer]:
    """Synthetic Beszel per-container stats for a service's containers.

    Only the running containers are "tracked" (mirrors Beszel, which reports
    stats for running containers), so stopped ones are omitted.
    """
    n = 60
    step = 60.0
    now = time.time()

    def series(base: float, amp: float, period: float, phase: float) -> list[models.StatPoint]:
        return [
            models.StatPoint(
                t=now - (n - 1 - j) * step,
                v=round(max(0.0, base + amp * math.sin(j / period + phase)), 2),
            )
            for j in range(n)
        ]

    out: list[models.BeszelContainer] = []
    for i, c in enumerate(detail.containers):
        if c.status == "down":
            continue
        cpu = round(0.5 + 2.3 * ((i * 7 + 3) % 9) / 3.0, 2)
        memory = round(80.0 + 90.0 * ((i * 5 + 2) % 7), 2)
        out.append(
            models.BeszelContainer(
                name=c.name,
                cpu=cpu,
                memory=memory,
                net=round(10.0 + i * 3.5, 1),
                status="running",
                image=detail.image,
                cpuHistory=series(cpu, 1.2 + i * 0.1, 6.0 + i, 0.4 * i),
                memHistory=series(memory, 20.0, 9.0 + i, 0.7 * i),
            )
        )
    return out


def mock_updates() -> models.Updates:
    entries = []
    for sid, s in MOCK_SERVICES.items():
        if not s["latest"]:
            continue
        entries.append(models.UpdateEntry(
            id=sid,
            name=s["name"],
            icon=s["icon"],
            image=s["image"],
            currentVersion=s["version"],
            latestVersion=s["latest"],
            releasedAt=_ago(days=s["released"]) if s.get("released") is not None else None,
            changelogUrl=s["changelog"],
        ))
    return models.Updates(
        services=entries,
        lastUpdated=_ago(minutes=12),
        refreshing=False,
        trackerUnavailable=False,
    )


def mock_action(sid: str, action: str) -> models.ActionResponse:
    return models.ActionResponse(accepted=True, jobId=f"mock-{action}-{sid}-{uuid.uuid4().hex[:8]}")


def mock_compose_text(sid: str) -> str | None:
    """Prefer the real compose.yaml from the repo checkout; fall back to a stub."""
    if sid not in MOCK_SERVICES:
        return None
    path = SERVICES_ROOT / sid / "compose.yaml"
    try:
        return path.read_text()
    except OSError:
        s = MOCK_SERVICES[sid]
        return (
            "# mock compose (real file unavailable at {})\n"
            "services:\n  {}:\n    image: {}\n    restart: {}\n".format(path, sid, s["image"], s["restart"])
        )


def mock_log_lines(sid: str) -> list[str]:
    s = MOCK_SERVICES.get(sid)
    if s is None:
        return []
    name = s["containers"][0] if s["containers"] else sid
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if s["status"] == "down":
        return [
            f"{name} | {ts} [worker] connecting to redis at redis:6379",
            f"{name} | {ts} ECONNREFUSED redis:6379 — retry 1/5",
            f"{name} | {ts} ECONNREFUSED redis:6379 — retry 5/5",
            f"{name} | {ts} fatal: could not reach message queue, exiting",
            f"{name} | {ts} container exited with code 1",
        ]
    port = str(s["ports"]).split("/")[0]
    return [
        f"{name} | {ts} starting {sid} {s['version']}",
        f"{name} | {ts} loaded configuration from /config",
        f"{name} | {ts} listening on :{port}",
        f"{name} | {ts} GET /api/health 200 3ms",
        f"{name} | {ts} scheduled task \"housekeeping\" completed",
        f"{name} | {ts} GET /api/health 200 2ms",
    ]
