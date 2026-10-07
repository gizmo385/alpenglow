#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Draws the alpenglow server diagram from the repo itself.

Writes two files into docs/:
  home_server.svg  the static image embedded in the README
  index.html       the same drawing with filters and hover tracing (GitHub Pages)

Run it after adding or changing a service:

    uv run docs/diagram/generate.py

Where each part of the drawing comes from:
  services         every */compose.yaml
  public           a Caddy label set on :10443
  LAN only         Caddy labels only on *.internal.acbc.house
  no web UI        no Caddy labels at all
  columns          `category` in alpenglow_dashboard/metadata.yaml
  display names    `name` in metadata.yaml, else the title-cased directory
  shared services  a directory that defines a network others join (or joins one
                   named after itself, like postgres), or that shares a socket
                   under /run with its users (beszel's docker-proxy socket)
  Keycloak SSO     an oauth2-proxy container, or `sso: true` in metadata.yaml
  /data paths      bind mounts under /data, plus HOST_DATA below
  backup service   whichever service mounts all of /data (Kopia)

Anything that isn't in the repo (disks, household devices, the offsite target)
lives in the HOST section below.
"""
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).parent
REPO = HERE.parents[1]

# ── Host facts the repo can't tell us ─────────────────────────────────────
HOST_DATA = ["/data/immich", "/data/windows", "/data/timemachine"]  # .env / host-only paths
ROOT_POOL = ["/", "/services", "/var/lib/postgresql"]
PERSONAL_DEVICES = ["Windows", "MacOS", "iPhone"]  # on the tailnet
DEVICE_BACKUPS = {"Windows": "/data/windows", "MacOS": "/data/timemachine"}
PHONE_BACKUP = ("iPhone", "MacOS")
OTHER_DEVICES = ["TV", "Photo Frame", "Speakers", "Cameras", "Zigbee / Z-Wave"]  # LAN only
OFFSITE = "Wasabi Offsite"
HOST_LOGIN = "PAM OIDC"  # host accounts provisioned from Keycloak

# ── How the repo is laid out ──────────────────────────────────────────────
INGRESS = "caddy"  # drawn as the Caddy box; its network is ingress, not a shared service
DNS = "pihole"
SSO_PROVIDER = "keycloak"
COLUMNS = [{"Media", "Home"}, {"Productivity"}, {"Infrastructure", "Monitoring"}]
DEFAULT_CATEGORY = "Infrastructure"  # the dashboard's fallback too

# ── Style ─────────────────────────────────────────────────────────────────
C = dict(
    bg="#131116", tail_fill="#1b191e", tail="#6b625c", home="#4caf6a", server="#5b9be0",
    docker_fill="#14232a", docker="#2f4b55", ink="#e9e6ee", muted="#9a94a3", card="#1a1f26",
    card_line="#4a5563", pub="#f27a9b", store="#e08a45", caddy="#cfd6de",
)
PROVIDER_STYLE = {  # line colour and filter label per shared service
    "keycloak": ("#e8923a", "Keycloak SSO"),
    "postgres": ("#a68cf2", "Postgres"),
    "redis": ("#e3c44c", "Redis"),
    "mosquitto": ("#40c4b2", "Mosquitto"),
    "ollama": ("#9fd36a", "Ollama"),
    "docker-proxy": ("#9aa3b5", "Docker API"),
}
SPARE_COLORS = ["#6fb7f0", "#e07ad6", "#d9a066", "#7fd1e8"]  # for shared services added later

CARD_W, CARD_H, PITCH = 150, 30, 39
DX0 = 270  # left edge of the Docker Compose box
Y0 = 362  # top of the first service card
LANE_GAP = 16
JOG_GAP = 12


# ── Reading the repo ──────────────────────────────────────────────────────
@dataclass
class Service:
    id: str
    label: str
    category: str
    sites: list[str]
    joins: set[str]
    defines: set[str]
    mounts: set[str]
    sso: bool
    uses: list[str] = field(default_factory=list)

    @property
    def public(self):
        return any(":10443" in s for s in self.sites)

    @property
    def lan_only(self):
        return bool(self.sites) and all(s.split(":")[0].endswith("internal.acbc.house") for s in self.sites)

    @property
    def headless(self):
        return not self.sites

    @property
    def data(self):
        return {"/data/" + m.split("/")[2] for m in self.mounts if m.startswith("/data/")}

    @property
    def backs_up_data(self):
        return "/data" in self.mounts


def _labels(container):
    labels = container.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) for item in labels if "=" in item)
    return labels


def _bind_source(volume):
    src = volume.split(":", 1)[0] if isinstance(volume, str) else (volume or {}).get("source", "")
    return src.rstrip("/") or "/" if str(src).startswith("/") else None


def _title(name):
    return " ".join(w.capitalize() for w in re.split(r"[_-]", name))


def load_services():
    meta = (yaml.safe_load((REPO / "alpenglow_dashboard/metadata.yaml").read_text()) or {}).get("services") or {}
    services = {}
    for compose in sorted(REPO.glob("*/compose.yaml")):
        sid = compose.parent.name
        doc = yaml.safe_load(compose.read_text()) or {}
        m = meta.get(sid) or {}
        sites, mounts, oauth2_proxy = [], set(), False
        for container in (doc.get("services") or {}).values():
            container = container or {}
            sites += [str(v) for k, v in _labels(container).items() if re.fullmatch(r"caddy(_\d+)?", k)]
            mounts |= {s for s in map(_bind_source, container.get("volumes") or []) if s}
            oauth2_proxy |= "oauth2-proxy" in str(container.get("image", ""))
        joins, defines = set(), set()
        for key, net in (doc.get("networks") or {}).items():
            net = net or {}
            if net.get("external"):
                joins.add(net.get("name", key))
            elif "name" in net:
                defines.add(net["name"])
        services[sid] = Service(sid, m.get("name") or _title(sid), m.get("category") or DEFAULT_CATEGORY,
                                sites, joins, defines, mounts, bool(m.get("sso")) or oauth2_proxy)
    return services


def resolve_shared(services):
    """Works out the shared services and fills in each service's `uses`."""
    provider_of = {net: s.id for s in services.values() for net in s.defines}
    for s in services.values():
        if s.id in s.joins:
            provider_of.setdefault(s.id, s.id)
    provider_of.pop(INGRESS, None)
    providers = set(provider_of.values()) | {SSO_PROVIDER}
    sockets = {p: {m for m in services[p].mounts if m.startswith("/run/")} for p in providers}
    for s in services.values():
        uses = {provider_of[n] for n in s.joins if n in provider_of}
        uses |= {p for p in providers if sockets[p] & s.mounts}
        if s.sso:
            uses.add(SSO_PROVIDER)
        uses.discard(s.id)
        s.uses = sorted(uses)
    return providers


# ── SVG helpers ───────────────────────────────────────────────────────────
out = []
a = out.append


def rect(x, y, w, h, stroke, fill="none", rx=6, sw=1.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    a(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def text(x, y, s, size=14, fill=C["ink"], anchor="middle", weight=400):
    a(f'<text x="{x:g}" y="{y:g}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>')


def path(pts, color, sw=1.6, dash=None, arrow=False):
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:g},{y:g}" for i, (x, y) in enumerate(pts))
    ds = f' stroke-dasharray="{dash}"' if dash else ""
    mk = f' marker-end="url(#ah-{color[1:]})"' if arrow else ""
    a(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{ds}{mk} stroke-linejoin="round"/>')


def dot(x, y, color):
    a(f'<circle cx="{x:g}" cy="{y:g}" r="3" fill="{color}"/>')


def box(x, y, w, h, label, stroke=C["card_line"], size=13, fill=C["card"], dash=None, color=C["ink"], weight=500):
    rect(x, y, w, h, stroke, fill, rx=5, sw=1.4, dash=dash)
    text(x + w / 2, y + h / 2 + size * 0.36, label, size, color, weight=weight)


@contextmanager
def el(*tags, svc=None, deps=(), users=(), label=None, cls=""):
    """Wraps what's drawn inside in a tagged group; the interactive page filters on these."""
    classes = " ".join(["el", cls, *[f"t-{t}" for t in dict.fromkeys(tags)]]).split()
    attrs = f' data-svc="{svc}"' if svc else ""
    attrs += f' data-label="{label}"' if label else ""
    attrs += f' data-deps="{" ".join(deps)}"' if deps else ""
    attrs += f' data-users="{" ".join(users)}"' if users else ""
    a(f'<g class="{" ".join(classes)}"{attrs}>')
    yield
    a("</g>")


# ── Layout ────────────────────────────────────────────────────────────────
def draw(services, providers):
    colour = {}
    spare = iter(SPARE_COLORS)
    for p in sorted(providers):
        colour[p] = PROVIDER_STYLE[p][0] if p in PROVIDER_STYLE else next(spare)

    special = providers | {INGRESS, DNS}
    columns = [[] for _ in COLUMNS]
    for s in services.values():
        if s.id in special:
            continue
        idx = next((i for i, cats in enumerate(COLUMNS) if s.category in cats), len(COLUMNS) - 1)
        columns[idx].append(s)
    col_of = {s.id: i for i, col in enumerate(columns) for s in col}

    # Shared services sit under the average column of the services that use them
    def barycenter(p):
        cols = [col_of[s.id] for s in services.values() if p in s.uses and s.id in col_of]
        return sum(cols) / len(cols) if cols else 1
    order = sorted(providers, key=lambda p: (barycenter(p), p))
    rank = {p: i for i, p in enumerate(order)}
    for col in columns:
        col.sort(key=lambda s: (not s.backs_up_data, s.headless, -len(s.uses),
                                [rank[u] for u in s.uses], s.label.lower()))

    def place(channel_widths):
        """x of each channel's left edge and each column, plus the Docker box's right edge."""
        cur, ch_x, col_x = DX0, [], []
        for k, w in enumerate(channel_widths):
            ch_x.append(cur)
            cur += w
            if k < len(columns):
                col_x.append(cur)
                cur += CARD_W
        return ch_x, col_x, cur

    # Shared services that use each other (Keycloak keeps its realm in Postgres)
    chained = [(p, q) for p in order for q in services[p].uses if q in providers]
    linked = {frozenset(pq) for pq in chained}
    widths_b = [len(services[p].label) * 8.4 + 32 for p in order]
    # Neighbours joined by an arrow need room for it; the rest just need a gap
    gaps_b = [44 if frozenset((p, q)) in linked else 16 for p, q in zip(order, order[1:])]
    row_w = sum(widths_b) + sum(gaps_b)

    def backend_boxes(right):
        spare = max(0, right - DX0 - 20 - row_w)
        extra = min(8, spare / max(1, len(gaps_b)))
        x = DX0 + (right - DX0 - row_w - extra * len(gaps_b)) / 2
        boxes = {}
        for p, w, g in zip(order, widths_b, gaps_b + [0]):
            boxes[p] = (x, w)
            x += w + g + extra
        return boxes

    # A first pass decides which side of its column each connection leaves from
    n_ch = len(columns) + 1
    _, col_x0, right0 = place([40] * n_ch)
    right0 = max(right0, DX0 + 20 + row_w)
    boxes0 = backend_boxes(right0)
    lanes = {}  # (provider, channel) → lane
    stubs = []
    for ci, col in enumerate(columns):
        for i, s in enumerate(col):
            for u in s.uses:
                bx, bw = boxes0[u]
                ch = ci if bx + bw / 2 < col_x0[ci] + CARD_W / 2 else ci + 1
                lanes.setdefault((u, ch), {"provider": u, "channel": ch, "users": []})["users"].append((s, i))
                stubs.append((s, ci, i, u, ch))

    widths = [max(30, (sum(1 for k in lanes if k[1] == ch) + 1) * LANE_GAP) for ch in range(n_ch)]
    ch_x, col_x, docker_r = place(widths)
    docker_r = max(docker_r, DX0 + 20 + row_w)  # the bottom row sets a minimum width
    for ch in range(n_ch):
        here = sorted((k for k in lanes if k[1] == ch), key=lambda k: rank[k[0]])
        for j, k in enumerate(here):
            lanes[k]["x"] = ch_x[ch] + (j + 1) * widths[ch] / (len(here) + 1)
    cx = (DX0 + docker_r) / 2  # Caddy and the ingress line up over the middle

    # Card stub y: connections leaving the same side of a card get their own row
    sides = {}
    for s, ci, i, u, ch in stubs:
        sides.setdefault((s.id, ch), []).append(u)
    stub_y = {}
    for s, ci, i, u, ch in stubs:
        group = sorted(sides[(s.id, ch)], key=rank.get)
        spacing = 10 if len(group) == 2 else 8
        stub_y[(s.id, u)] = Y0 + i * PITCH + CARD_H / 2 + (group.index(u) - (len(group) - 1) / 2) * spacing
    for lane in lanes.values():
        lane["top"] = min(stub_y[(s.id, lane["provider"])] for s, _ in lane["users"])

    # Caddy's own connections (it reads the Docker API) join the nearest lane
    caddy = services.get(INGRESS)
    caddy_y = 300
    caddy_stubs = []
    for u in caddy.uses if caddy else []:
        options = [l for l in lanes.values() if l["provider"] == u]
        if not options:
            continue
        lane = min(options, key=lambda l: abs(l["x"] - cx))
        lane["top"] = caddy_y
        caddy_stubs.append((u, lane))

    cards_bottom = Y0 + max(len(c) for c in columns) * PITCH - (PITCH - CARD_H)
    boxes = backend_boxes(docker_r)
    by_provider = {}
    for lane in sorted(lanes.values(), key=lambda l: l["x"]):
        by_provider.setdefault(lane["provider"], []).append(lane)
    for p, ls in by_provider.items():
        bx, bw = boxes[p]
        for j, lane in enumerate(ls):
            lane["attach"] = bx + bw * (j + 1) / (len(ls) + 1)
    # Lanes that bend sideways get their own height; ordered so same-direction bends don't cross
    rightward = sorted((l for l in lanes.values() if l["attach"] - l["x"] > 1), key=lambda l: -l["x"])
    leftward = sorted((l for l in lanes.values() if l["x"] - l["attach"] > 1), key=lambda l: l["x"])
    for level, lane in enumerate(rightward + leftward):
        lane["jog"] = cards_bottom + 20 + level * JOG_GAP
    BY = cards_bottom + 20 + len(rightward + leftward) * JOG_GAP + 16

    docker_b = BY + 40 + 24 + (14 * sum(1 for p, q in chained if abs(rank[p] - rank[q]) > 1))

    # Storage, sized by what's mounted
    data_users = {}
    for s in services.values():
        for d in s.data:
            data_users.setdefault(d, []).append(s.id)
    data_paths = sorted(set(data_users) | set(HOST_DATA))
    pool_y = 300
    pool_h = 42 + len(data_paths) * 38 + 5
    root_y = pool_y + pool_h + 25
    root_h = 42 + len(ROOT_POOL) * 38 + 8

    sso_box = boxes.get(SSO_PROVIDER)
    pam_y = docker_b + 28
    route_y = max(pam_y + 36 + 36, root_y + root_h + 30)
    server_r = docker_r + 20
    server_b = route_y + 40
    dev_x = server_r + 30
    tail_r = dev_x + 180 + 20
    other_x = tail_r + 50
    W = other_x + 190 + 30
    H = server_b + 40

    # ── Draw ──
    arrow_colors = sorted({C["pub"], C["caddy"], C["muted"], *colour.values()})
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:g} {H:g}" width="{W:g}" height="{H:g}" '
      'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
      'role="img" aria-label="Alpenglow home server: network boundaries, services and their shared dependencies">')
    a("<defs>" + "".join(
        f'<marker id="ah-{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for c in arrow_colors) + "</defs>")
    a(f'<rect width="{W:g}" height="{H:g}" fill="{C["bg"]}"/>')

    # Boundaries
    rect(20, 110, tail_r - 20, server_b + 25 - 110, C["tail"], C["tail_fill"], rx=14)
    text(240, 142, "Tailnet", 26, C["tail"])
    rect(32, 150, W - 44, server_b + 20 - 150, C["home"], rx=14)
    text(W - 34, server_b - 2, "Home Network", 34, C["home"], anchor="end")
    rect(44, 170, server_r - 44, server_b - 170, C["server"], rx=14)
    text(64, 210, "Alpenglow Server", 30, C["server"], anchor="start")
    rect(DX0, 250, docker_r - DX0, docker_b - 250, C["docker"], C["docker_fill"], rx=12)
    text(docker_r - 18, docker_b - 10, "Docker Compose", 15, C["muted"], anchor="end")

    # Outside world
    with el():
        box(cx - 310, 24, 160, 46, "Tailscale", C["ink"], 16, C["bg"])
    with el("public"):
        box(cx - 80, 24, 160, 46, "Internet", C["pub"], 16, C["bg"], color=C["pub"])
    with el("backup"):
        box(W - 220, 24, 200, 60, OFFSITE, C["pub"], 18, C["bg"], color=C["pub"])

    # Storage
    backup_targets = set(DEVICE_BACKUPS.values())
    with el("backup"):
        rect(60, pool_y, 190, pool_h, "#3a3346", "#1c1a26", rx=10)
        text(155, pool_y + 28, "ZFS Data Pool", 17, C["store"])
    data_y = {}
    for i, p in enumerate(data_paths):
        y = pool_y + 42 + i * 38
        data_y[p] = y + 15
        with el(*(["backup"] if p in backup_targets else []), users=data_users.get(p, [])):
            box(76, y, 158, 30, p, C["store"], 12.5, "#1c1a26", color=C["store"], weight=400)
    with el():
        rect(60, root_y, 190, root_h, "#3a3346", "#1c1a26", rx=10)
        text(155, root_y + 28, "ZFS Root Pool", 17, C["store"])
        for i, p in enumerate(ROOT_POOL):
            box(76, root_y + 42 + i * 38, 158, 30, p, C["store"], 12.5, "#1c1a26", color=C["store"], weight=400)

    # Ingress: the public edge and the tailnet both reach Caddy
    with el("public"):
        box(cx - 70, 186, 140, 34, "caddy-edge", C["pub"], 13, C["card"], color=C["pub"])
        path([(cx, 70), (cx, 182)], C["pub"], 2, arrow=True)
        path([(cx, 220), (cx, 278)], C["pub"], 2, arrow=True)
        text(cx + 8, 268, ":10443", 12, C["pub"], anchor="start")
    with el():
        path([(cx - 170, 70), (cx - 170, 258), (cx - 55, 258), (cx - 55, 278)], C["caddy"], 2, arrow=True)
        text(cx - 162, 200, ":443", 12, C["caddy"], anchor="start")
    if DNS in services:
        dns_x = cx - 250
        px = max(DX0 + 12, dns_x - 60)  # sit under the DNS line when there's room
        with el(*services[DNS].uses, svc=DNS, label=services[DNS].label):
            box(px, 282, 120, 36, services[DNS].label, size=14)
        with el():
            if px <= dns_x <= px + 120:
                path([(dns_x, 74), (dns_x, 278)], C["muted"], arrow=True)
            else:
                path([(dns_x, 74), (dns_x, 300), (px + 124, 300)], C["muted"], arrow=True)
            text(dns_x + 8, 200, "DNS", 12, C["muted"], anchor="start")
    with el("public", *(caddy.uses if caddy else []), svc=INGRESS, deps=caddy.uses if caddy else (), label="Caddy"):
        box(cx - 100, 282, 200, 36, "Caddy", C["caddy"], 16, weight=600)
    centers = [x + CARD_W / 2 for x in col_x]
    with el("public"):
        path([(cx, 318), (cx, 334)], C["caddy"])
        path([(centers[0], 334), (centers[-1], 334)], C["caddy"])
        for x in centers:
            path([(x, 334), (x, Y0 - 6)], C["caddy"], arrow=True)

    # Lanes down to the shared services
    for lane in lanes.values():
        p, x = lane["provider"], lane["x"]
        pts = [(x, lane["top"]), (x, BY)]
        if "jog" in lane:
            pts = [(x, lane["top"]), (x, lane["jog"]), (lane["attach"], lane["jog"]), (lane["attach"], BY)]
        with el(p, cls="lane"):
            path(pts, colour[p], arrow=True)

    # Stubs from each card (and Caddy) to its lanes
    for s, ci, i, u, ch in stubs:
        lane, y = lanes[(u, ch)], stub_y[(s.id, u)]
        sx = col_x[ci] if lane["x"] < col_x[ci] else col_x[ci] + CARD_W
        with el(u, svc=s.id):
            path([(sx, y), (lane["x"], y)], colour[u])
            dot(lane["x"], y, colour[u])
    for u, lane in caddy_stubs:
        sx = cx + 100 if lane["x"] > cx else cx - 100
        with el(u, svc=INGRESS):
            path([(sx, caddy_y), (lane["x"], caddy_y)], colour[u])
            dot(lane["x"], caddy_y, colour[u])

    # Service cards
    backup_svc = None
    for ci, col in enumerate(columns):
        for i, s in enumerate(col):
            stroke = C["pub"] if s.public else C["home"] if s.lan_only else C["card_line"]
            extra = (["public"] if s.public else []) + (["backup"] if s.backs_up_data else [])
            with el(*s.uses, *extra, svc=s.id, deps=s.uses, label=s.label, cls="svc"):
                box(col_x[ci], Y0 + i * PITCH, CARD_W, CARD_H, s.label, stroke, 13.5,
                    dash="4 3" if s.headless else None)
            if s.backs_up_data:
                backup_svc = (col_x[ci], Y0 + i * PITCH)

    # Shared services
    for p in order:
        s, (bx, bw) = services[p], boxes[p]
        extra = (["public"] if s.public else []) + [q for q in s.uses if q in providers]
        with el(p, *extra, deps=[p], label=PROVIDER_STYLE.get(p, ("", s.label))[1], cls="backend"):
            box(bx, BY, bw, 40, s.label, C["pub"] if s.public else colour[p], 14.5, color=colour[p], weight=600)
    below = BY + 40 + 14
    for p, q in chained:
        (px, pw), (qx, qw) = boxes[p], boxes[q]
        with el(p, q):
            if abs(rank[p] - rank[q]) == 1:
                x1, x2 = (px + pw, qx) if px < qx else (px, qx + qw)
                path([(x1, BY + 20), (x2 + (-4 if x2 > x1 else 4), BY + 20)], colour[q], arrow=True)
            else:
                path([(px + pw / 2, BY + 40), (px + pw / 2, below), (qx + qw / 2, below), (qx + qw / 2, BY + 44)],
                     colour[q], arrow=True)
                below += 14

    # Keycloak → host logins
    if sso_box:
        bx, bw = sso_box
        with el(SSO_PROVIDER):
            box(bx + bw / 2 - 70, pam_y, 140, 36, HOST_LOGIN, size=14)
            path([(bx + bw / 2, BY + 40), (bx + bw / 2, pam_y - 4)], colour[SSO_PROVIDER], arrow=True)

    # Backups: /data → the backup service → offsite
    if backup_svc:
        kx, ky = backup_svc
        with el("backup"):
            path([(155, pool_y), (155, 234), (kx + CARD_W - 40, 234), (kx + CARD_W - 40, ky - 4)],
                 C["pub"], 1.6, "6 4", arrow=True)
            path([(kx + CARD_W - 15, ky), (kx + CARD_W - 15, 54), (W - 224, 54)], C["pub"], 1.6, "6 4", arrow=True)

    # Personal devices (tailnet) and their backups
    dev_y = {name: 380 + i * 110 for i, name in enumerate(PERSONAL_DEVICES)}
    with el("backup"):
        for name, y in dev_y.items():
            box(dev_x, y, 180, 56, name, C["server"], 17, C["bg"], weight=400)
        src, dst = PHONE_BACKUP
        path([(dev_x + 90, dev_y[src]), (dev_x + 90, dev_y[dst] + 60)], C["pub"], 1.4, "5 4", arrow=True)
        text(dev_x + 98, (dev_y[src] + dev_y[dst] + 56) / 2 + 4, "Backup", 12, C["pub"], anchor="start")
        rx = dev_x + 192
        first = True
        for name, target in DEVICE_BACKUPS.items():
            y = dev_y[name] + 28
            if first:
                path([(dev_x + 180, y), (rx, y), (rx, route_y), (54, route_y), (54, data_y[target]), (72, data_y[target])],
                     C["pub"], 1.6, "6 4", arrow=True)
                dot(54, data_y[target], C["pub"])
                first = False
            else:
                path([(dev_x + 180, y), (rx, y)], C["pub"], 1.6, "6 4")
                path([(54, data_y[target]), (72, data_y[target])], C["pub"], 1.6, "6 4", arrow=True)
        text(dev_x + 84, route_y - 10, "Device Backups", 14, C["pub"])

    # Other devices (home network only) and the key
    for i, name in enumerate(OTHER_DEVICES):
        with el():
            box(other_x, 250 + i * 86, 190, 56, name, C["server"], 17, C["bg"], weight=400)
    LX, LY = other_x - 14, 250 + len(OTHER_DEVICES) * 86 + 30
    text(LX, LY, "Key", 14, C["muted"], anchor="start", weight=600)
    for j, (stroke, dash, label) in enumerate([(C["pub"], None, "Public (:10443)"), (C["home"], None, "LAN only"),
                                               (C["card_line"], "4 3", "No web UI")]):
        rect(LX, LY + 14 + j * 30, 34, 18, stroke, C["card"], rx=4, dash=dash)
        text(LX + 44, LY + 28 + j * 30, label, 13, C["ink"], anchor="start")
    path([(LX, LY + 113), (LX + 34, LY + 113)], C["pub"], 1.6, "6 4")
    text(LX + 44, LY + 118, "Backup", 13, C["ink"], anchor="start")
    a("</svg>")

    chips = [("public", "Public", C["pub"])]
    chips += [(p, PROVIDER_STYLE.get(p, ("", services[p].label))[1], colour[p]) for p in order]
    chips += [("backup", "Backups", C["pub"])]
    return "\n".join(out) + "\n", chips


def main():
    services = load_services()
    providers = resolve_shared(services)
    svg, chips = draw(services, providers)
    (HERE.parent / "home_server.svg").write_text(svg)
    chip_html = "\n".join(
        f'      <button class="chip" data-f="{key}" data-name="{label}" style="--c: {color}" aria-pressed="false">'
        f'{label} <span class="n"></span></button>' for key, label, color in chips)
    page = (HERE / "template.html").read_text().replace("{{CHIPS}}", chip_html).replace("{{SVG}}", svg)
    (HERE.parent / "index.html").write_text(page)


if __name__ == "__main__":
    main()
