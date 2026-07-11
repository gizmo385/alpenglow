# Handoff: Alpenglow Home-Server Management Dashboard

## Overview

A management dashboard for the **Alpenglow** home server — the Docker-Compose monorepo at
`github.com/gizmo385/alpenglow`. It gives an at-a-glance health view of every service, surfaces
pending image updates (from the `image-updates-tracker` service), and lets an operator drill into a
single service to inspect it and **take actions** (restart / stop / start, and `docker compose pull &&
up -d`).

It is intended to live alongside the other management tools on the `*.alpenglow.acbc.house` domain
tier, deployed the same way as every other Alpenglow service (its own directory with a
`compose.yaml`, fronted by the Caddy reverse proxy, authenticated through Keycloak).

---

## About the Design Files

The files in this bundle are **design references created in HTML** — a working prototype that shows the
intended look, layout, and interaction model. **They are not production code to copy directly.** All
data in the prototype is mock data hard-coded in the file; there is no backend.

The task is to **recreate this design in a real application** — reading live data from the Alpenglow
host and executing real container actions — using whatever stack is most appropriate. No stack has been
chosen; pick what fits (see *Suggested Architecture* below for the constraints that matter). Recreate the
visual design faithfully (it is high-fidelity — see *Fidelity*), but build the UI with your chosen
framework's real components rather than porting the prototype's ad-hoc markup.

The prototype is a single file: **`Alpenglow Dashboard.dc.html`**. It is authored as a "Design Component"
and depends on the **Nocturne** design system stylesheet (`_ds/nocturne-…/styles.css`) and the Phosphor
icon web font (loaded from unpkg). You do not need the Design-Component runtime to build the real app —
open the file to read the markup/logic, and use this README as the source of truth. The Nocturne tokens
that matter are reproduced under *Design Tokens*.

---

## Fidelity

**High-fidelity (hifi).** Colors, typography, spacing, radii, and interactions are final and should be
recreated faithfully using the Nocturne token values documented below. Layout measurements in this README
are exact. Recreate the UI pixel-close; substitute your framework's real components for the prototype's
inline-styled markup.

---

## Design language (Nocturne)

Quiet, compact, dark. A near-neutral blue-grey ground, **Inter** at weight 400/500, soft 8px radii, and a
single blurple accent used as a **line, tint, or glow — never a flooded fill**. Contrast comes from tonal
ramps, not saturation. Left-aligned, dense layout (spacing scale is ~0.7×). Primary buttons are an accent
**outline** on transparent, not solid. Icons are **Phosphor** (regular weight inline; fill weight for
status glyphs).

---

## Screens / Views

The app is a single-page shell: a fixed **left sidebar** (222px) + a scrolling **main** area. Three top-level
views swap into main: **Overview**, **Updates**, and **Service Detail**. See `screenshots/`.

### Sidebar (persistent)
- **Width** 222px, full height, `border-right: 1px solid var(--color-divider)`, background
  `linear-gradient(180deg, #1a1c2c, var(--color-bg))`.
- **Brand** row: 30×30 rounded-9px tile with a radial blurple gradient + a `ph-mountains` (fill) glyph in
  `#1a1c2c`; wordmark "Alpenglow" (16px/600) over "HOME SERVER" (10px, uppercase, letter-spacing .08em,
  `--color-neutral-500`).
- **Primary nav** buttons: `Overview` (icon `ph-squares-four`) and `Updates` (icon `ph-arrows-clockwise`).
  Active item = background `color-mix(in srgb, var(--color-accent) 16%, transparent)`, text
  `--color-accent-200`. Updates carries a **count badge** (pill, `--color-accent-700` bg /
  `--color-accent-100` text) = number of services with an update.
- **Categories** list (label 10px uppercase `--color-neutral-600`): Media, Productivity, Home,
  Infrastructure, Monitoring — each with its Phosphor icon, name, and a count. Clicking a category filters
  the Overview to that category (click again to clear); active row bg `--color-neutral-900`.
- **Footer** (11px, `--color-neutral-500`): `ph-hard-drives` "nimbus · Denver"; `ph-git-branch`
  "alpenglow @ main". (Host name / git branch — wire to real values.)

### View 1 — Overview  (`screenshots/01-overview.png`)
**Purpose:** answer "is everything up, is anything on fire, what needs updating" in one glance.

- **Header:** `<h2>` "Overview" + a muted summary line
  `"{N} services · {M} linked to Keycloak · {U} updates pending · {D} stopped"`, and a right-aligned
  "polled {ago}" with a green dot.
- **Stat tiles** — a 4-column grid (`repeat(4, 1fr)`, gap 12px) of `.card`s (`--color-surface`,
  radius 8px, `--shadow-sm`):
  1. **Services** — big `{up}` / `{total}` up; legend "● running" (green) / "● stopped" (red, only if any).
  2. **Updates** — big accent `{count}` available; "→ pull & recreate". **Whole tile is clickable → Updates view.**
  3. **Monitors** — `{up}` / `{total}` up; "Uptime Kuma · {note}".
  4. **Backups** — green "OK"; "pg dump {ago} · kopia {ago}".
  5. **Host resources** (spans 2 cols) — kicker + "load {1/5/15}"; three labeled bar meters (CPU, Memory,
     Swap): label + mono value on one row, a 6px track (`--color-neutral-900`) with a filled bar whose width
     is the percentage. CPU bar = `--color-accent-400`, Memory = `#6bd39a`, Swap = `--color-neutral-500`.
  6. **Storage & ZFS** (spans 2 cols) — kicker + a neutral tag "✓ tank ONLINE" (check in `#6bd39a`); two
     bar meters (`/data (RAID-Z2)`, `/ (system SSD)`); footer "last scrub {ago} · 0 errors".
- **Services section:** `<h3>` "Services" + a right-aligned **search input** (230px, magnifier icon inset;
  filters by name / image / category, live). Below, a **tag-filter row**: label "⚑ Tags" + a chip per
  in-use tag; clicking a chip filters to services carrying that tag (toggle). Active chip = accent-800 bg /
  accent border / accent-100 text; inactive = transparent / neutral-700 border / neutral-300 text.
- **Service groups:** for each non-empty category, a header row (category icon in `--color-accent-300`,
  `<h4>` name, count, and a rule that fades to transparent on the right), then a responsive grid of
  **service cards** (`repeat(auto-fill, minmax(268px, 1fr))`, gap 12px).

  **Service card** (`.card`, `--shadow-sm`, padding 13/14px, `cursor:pointer`, hover lifts `translateY(-1px)`
  + `--shadow-md`; **whole card → Service Detail**). A **3px left border** encodes priority: red
  (`#e08a8a`) if stopped, else accent-600 if an update is available, else `--color-neutral-800`.
  Contents, top to bottom:
  - Row: 9px **status dot** (color by status, pulses while busy) · service **name** (15px/500, truncates) ·
    if update available, an accent pill `⬆ {latest}` (`ph-arrow-fat-up`, bg `--color-accent-800`).
  - **Image** ref, mono 11px, `--color-neutral-500`, truncates (e.g. `nextcloud:29.0.4-apache`).
  - Chip row: **SSO chip** (see *SSO model*) + **tier chip** (`.tag-neutral`, tier icon + label).
  - **Tag chips** (only if any): small outlined 10px chips (neutral-700 border, neutral-300 text).
  - Divider, then a footer row: "{statusLabel} · {uptime}" (11px muted) · **Open** icon-button
    (`ph-arrow-square-out`, opens the service URL in a new tab, `stopPropagation` so it doesn't open detail;
    hidden if the service has no web ingress) · **Manage** button (`.btn-secondary`).
- Empty state (no search match): centered magnifier + "No services match "{query}".".

### View 2 — Updates  (`screenshots/02-updates.png`)
**Purpose:** the operator's update queue, fed by `image-updates-tracker`.

- **Header:** `<h2>` "Updates" + line "{U} services have a newer image available". Right side (only when
  updates exist): **"Copy all commands"** (`.btn-secondary`, `ph-copy`) and **"Update all"** (`.btn-primary`,
  `ph-arrows-clockwise`).
- Sub-line (12px muted): "ⓘ Tracked by image-updates-tracker · cross-checked against GitHub releases".
- **Table** (Nocturne `.table`, inside a `.card`): columns **Service** (icon + name), **Image** (mono,
  neutral-400), **Current** (mono), **Latest** (mono, accent-300), **Released** (relative age, muted), and a
  right-aligned **Action** cell: a **changelog** icon-link (`ph-note`, opens the release page) + a
  **"Pull & recreate"** primary button. While that service is updating, the button shows a spinner
  (`ph-spinner`, CSS spin) + "Pulling…" and is disabled.
- **Empty state** (no updates): centered fill check-circle (`#6bd39a`) + "Everything is up to date" +
  "All running images match their latest release."

### View 3 — Service Detail  (`screenshots/03/04/05`)
**Purpose:** inspect and act on one service.

- **Back** ghost button "← Overview".
- **Header:** 46×46 rounded tile (neutral-900 bg, divider border) with the service icon (accent-300) · `<h2>`
  name · a **status tag** (colored dot + label: Running=green tint, Stopped=red tint, Restarting…/Updating…=
  accent-800). Right: **Open** button (`.btn-secondary`, hidden if no URL). Below the name, the mono image ref.
- **Action bar** (surface card, `--shadow-sm`, 12/14px, wraps): if an update is available, a **"Pull &
  recreate → {latest}"** primary button; then **Restart** and **Stop/Start** (`.btn-secondary`); a spacer;
  then **"Copy update cmd"**. Buttons that trigger a transition disable themselves + swap to a spinner while busy.
- **Tabs** (underline style, accent underline on active): **Overview · Resources · Logs · Compose**.
  - **Overview tab:** a `repeat(auto-fit, minmax(240px,1fr))` grid of fact cards, each = uppercase label
    (with icon) + value. Facts: **Status** (colored), **Uptime**, **Image** (mono, wraps), **Version**
    (`current → latest` in accent-300 when an update exists), **Domain** (mono, or "— (no web ingress)"),
    **Access tier**, **Single sign-on** (label **+ a mono sub-line naming how it was detected** — see *SSO
    model*), **Restart policy** (mono), **Ports** (mono), **Mem limit** (mono). Below the grid: a **description**
    paragraph, then a **Tags** editor (see *Tag model*).
  - **Resources tab** (`screenshots` not included for this tab): two cards (CPU %, Memory used/limit) each with a
    big number + an SVG **sparkline** (`polyline`, non-scaling-stroke; CPU accent-400, Memory `#6bd39a`); then a
    row card with **Net I/O**, **Block I/O**, **PIDs**, **Restarts** (all mono). These map directly to
    `docker stats`.
  - **Logs tab** (`screenshots/04-detail-logs.png`): a card with a header bar
    "⌗ docker logs -f --tail 100 {id}" + a "● streaming" indicator (pulsing green dot); body is a mono, dark
    (`#12131f`) scrolling block, one row per line = muted timestamp + colored message (errors in red, success in
    green). Map to a streaming `docker logs` follow.
  - **Compose tab** (`screenshots/05-detail-compose.png`): a card with header "▤ {id}/compose.yaml" + a **Copy**
    ghost button; body is a mono `<pre>` on `#12131f` showing the service's `compose.yaml`. In the real app,
    read the actual file from the repo checkout.

### Toast (global)
Bottom-right surface card with `--shadow-lg`, fill check-circle (`#6bd39a`) + message; auto-dismisses ~2.4s.
Shown on every action (restart/stop/start/pull/copy/tag). Slide-up fade-in (`toastIn`, .18s).

---

## SSO model (Keycloak / OIDC) — **important**

The dashboard treats SSO status as an **auto-detected** signal, and this is a first-class requirement.
Each service shows an SSO chip (card + detail) and, in the detail Overview tab, a **detection source**
sub-line. The prototype's five states and the source strings they display:

| State (`sso`) | Chip label | Chip style | Detection source sub-line (mock) |
|---|---|---|---|
| `keycloak` | "Keycloak" | accent (accent-800 bg) | `OIDC_ISSUER_URL + OIDC_CLIENT_ID in .env` |
| `oidc` | "OIDC" | accent | `OAUTH_ISSUER_URL in .env` |
| `self` | "Identity provider" | accent (`ph-shield-star`) | `realm: acbc` |
| `native` | "Native auth" | neutral | `app-managed login · no OIDC keys` |
| `none` | "No SSO" | outline/muted (`ph-warning`) | `no OIDC keys found in .env` |

**How to make detection real** (no single signal covers everything — cross-reference all three, in order
of authority):
1. **Keycloak clients API** (authoritative "what is actually wired up"): query
   `GET /admin/realms/{realm}/clients` and match client IDs back to compose services (via a naming
   convention or an overrides map, mirroring how `image-updates-tracker` maps image→repo).
2. **`.env` / compose scan** (per-app integrations): look for `OIDC_*` / `OAUTH2_*` / `KEYCLOAK_*` env keys
   or an `oauth2-proxy` sidecar in the service's `compose.yaml`. Yields the positive detection strings above
   **and the negative one** ("no OIDC keys found") that flags a gap.
3. **Caddy labels** (proxy-level SSO): services behind `forward_auth` / an oauth2-proxy expose it in labels.

The `none` state is the actionable one — it's the "not yet linked to Keycloak" gap the operator wants to see.

### Authenticating the dashboard itself
This is a management tool that executes privileged actions, so it must sit behind Keycloak like the rest of
Alpenglow. Recommended: front it with an **oauth2-proxy** (or Caddy `forward_auth`) bound to the Keycloak
realm, restricted to an admin group/role. The app should read the authenticated identity from the proxy's
forwarded headers (e.g. `X-Forwarded-User` / `X-Forwarded-Groups`) and **authorize every action** against an
admin role — never expose the action endpoints unauthenticated on the LAN. Actions should be POST endpoints
with CSRF protection and audit logging.

---

## Tag model

A general, **user-maintained** labelling system, separate from the auto-detected SSO status.
- Each service has a list of string tags. Shown as chips on the service card and edited in the detail
  Overview tab.
- **Editor:** applied tags render as removable accent chips (each with an `×` / `ph-x` remove button); a row
  of **preset suggestion** chips (outlined, `ph-plus`) that aren't already applied adds them on click; a
  **custom** text input adds an arbitrary tag on Enter.
- **Preset vocabulary:** `Critical`, `Stateful`, `GPU`, `User data`, `Beta`, `Experimental`, `Family`,
  `Host network`, `Exposed`.
- **Overview tag-filter** row is built from the union of tags currently in use.
- Persist tags server-side (a small store keyed by service id — a JSON/SQLite table, or an overrides file in
  the repo). Seed examples from the prototype: postgres→[Critical, Stateful], immich→[GPU, User data],
  redis→[Stateful], keycloak→[Critical], home_assistant→[Host network], ollama→[GPU, Experimental].

---

## Interactions & Behavior

- **Navigation:** sidebar Overview/Updates switch views; category rows filter Overview (toggle); Updates
  stat tile → Updates view; any service card / "Manage" → Service Detail; "← Overview" returns; detail tabs
  swap tab content.
- **Search:** live substring filter over name/image/category. **Tag filter:** single active tag, toggle.
  Both compose with the category filter.
- **Actions** (each shows a toast; the target's status pulses / buttons disable while in flight):
  - **Restart** — status → "Restarting…" then back to "Running" (prototype fakes ~1.7s; real = `docker
    compose restart` / container restart).
  - **Stop / Start** — toggles Running/Stopped (real = `compose stop` / `up -d`).
  - **Pull & recreate** — status → "Updating…", then version bumps to latest and the update badge clears
    (prototype ~2.1s; real = `docker compose pull && docker compose up -d` in the service dir, streaming
    progress). **Update all** runs this for every service with an update.
  - **Copy update cmd / Copy all / Copy compose** — writes to clipboard. The per-service command format is
    `cd ~/alpenglow/{id} && docker compose pull && docker compose up -d`.
- **Animations:** `spin` (0.9s linear, spinners), `softPulse` (1.6s, status dots & streaming indicator),
  `toastIn` (.18s), card hover transform/shadow (.12s). Focus ring: `2px solid var(--color-accent)`,
  offset 2px (never the browser default).

---

## State Management

Prototype state (recreate as app/server state as appropriate):
- `view` (`overview` | `updates` | `detail`), `selId` (selected service id), `tab`
  (`overview`|`resources`|`logs`|`compose`).
- `query` (search), `cat` (category filter | "All"), `tag` (active tag filter | ""), `newTag` (custom-tag input).
- `services[]` — per service: `id, name, cat, icon, sub (subdomain), image, version, latest|null, released,
  status (up|down|restarting|updating), sso, tier (public|tailnet|internal|management), cpu, mem, memLimit,
  uptime, restart, ports, desc`.
- `tags` — map of service id → string[].
- `toast` — transient message.

**Real data sources** (all read-only except the action endpoints):
- **Docker socket** (`/var/run/docker.sock`, mounted `:ro` for reads) — container list, status, image,
  restart policy, ports, uptime; `docker stats` for the Resources tab; `docker logs` for the Logs tab.
- **image-updates-tracker API** (`:8585`, already in the repo) — pending updates: current vs latest tag,
  release date, changelog/repo link. This is the Updates view's backing data.
- **Beszel / Glances** — host CPU/RAM/swap, load average (Overview host tile) and richer per-container
  history.
- **Uptime Kuma** — monitor up/total for the Monitors tile.
- **Keycloak admin API** — client list for SSO detection (see *SSO model*).
- **ZFS / backup scripts** — pool status + last-scrub, and the pg-dump / kopia webhook timestamps Uptime Kuma
  already receives (Storage & Backups tiles).

---

## Design Tokens

**Colors (Nocturne — from `_ds/nocturne-…/styles.css`):**
- Ground `--color-bg` `#161826`; surface `--color-surface` `#232532`; text `--color-text` `#e9e9ed`.
- Accent `--color-accent` `#9184d9`. Accent ramp: 100 `#f5f4ff`, 200 `#e7e5fe`, 300 `#d2cefd`,
  400 `#b5abfc`, 500 `#968ae0`, 600 `#796cbf`, 700 `#5d5294`, 800 `#423a6a`, 900 `#2b2741`.
- Neutral ramp: 100 `#f3f5fe`, 200 `#e4e7f5`, 300 `#cfd3e5`, 400 `#b2b6ca`, 500 `#9397ab`, 600 `#75798c`,
  700 `#595d6c`, 800 `#3f424d`, 900 `#292b31`.
- Divider `color-mix(in srgb, #e9e9ed 16%, transparent)`.
- **Status colors introduced for this dashboard** (harmonious, low-chroma): OK/running `#6bd39a`,
  down/error `#e08a8a`, busy (restart/update) = the accent `#9184d9`. Log panel bg `#12131f`. Sidebar
  gradient top `#1a1c2c`.

**Typography:** Inter (400/500/600). h1 42 / h2 32 / h3 25 / h4 20 / h5 16 / h6 13 (uppercase, tracking
.08em); body 15px/1.55. Headings weight 500, letter-spacing -0.015em — **do not bold past 500**. Mono UI
values use `ui-monospace, monospace`.

**Spacing (0.7× scale):** 2.8 / 5.6 / 8.4 / 11.2 / 16.8 / 22.4 px (`--space-1..8`).
**Radius:** sm 4 / md 8 / lg 14 px. **Shadows:** sm `0 0 0 1px #3f424d`; md `0 0 0 1px #595d6c, 0 6px 18px
rgba(0,0,0,.55)`; lg `0 0 0 1px #9397ab, 0 16px 40px rgba(0,0,0,.65)`. On this dark ground, **elevation is
an edge + ambient darkness — don't stack heavy shadows.**

**Nocturne component classes used** (see the stylesheet): `.btn` + `.btn-primary/-secondary/-ghost/-icon`,
`.tag` + `.tag-accent/-neutral/-outline`, `.card` + `.card-kicker/-title/-body/-meta` + `.elev-sm/md/lg`,
`.input`, `.table`. Primary buttons are **outlined, never filled**; accent is never used as a large flooded fill.

---

## Assets

- **Icons:** Phosphor (`@phosphor-icons/web`), regular + fill weights. In the real app, install the
  Phosphor package for your framework rather than the unpkg CDN link the prototype uses. Icons referenced:
  mountains, squares-four, arrows-clockwise, play-circle, briefcase, house-line, stack, pulse, hard-drives,
  git-branch, magnifying-glass, tag, arrow-fat-up, arrow-square-out, key, shield-star, user, warning, globe,
  shield, house, wrench, images, film-slate, music-notes, monitor, cloud, bookmark-simple, cooking-pot,
  address-book, rss, youtube-logo, video-camera, broadcast, shield-check, database, lightning, funnel,
  brain, globe-hemisphere-west, folder-open, gauge, chart-line, heartbeat, archive, arrow-left, arrow-right,
  arrow-clockwise, play, stop, terminal-window, file-text, note, copy, plus, x, info, check-circle, circle,
  clock, cube, link, plugs, memory.
- **Fonts:** Inter (Google Fonts, weights 400/500/600/700).
- **No bitmap/photo assets** are used. No custom logo file — the brand mark is a gradient tile + Phosphor glyph.
- The Nocturne stylesheet (`_ds/nocturne-…/styles.css`) is the token source; copy its `:root` variables into
  your app's theme layer.

---

## Files

- **`Alpenglow Dashboard.dc.html`** — the full prototype (markup + logic + mock data). Open it to read the
  exact layout and the mock data set of ~26 Alpenglow services. Depends on the Nocturne stylesheet and the
  Phosphor web font.
- **`screenshots/`** — `01-overview.png`, `02-updates.png`, `03-detail-overview.png`, `04-detail-logs.png`,
  `05-detail-compose.png`.

---

## Suggested Architecture (no stack was specified)

Constraints that should drive the choice, not a prescription:
- **Deploys like the rest of Alpenglow:** its own dir with a `compose.yaml`, `expose:` (no host ports),
  Caddy labels on `*.alpenglow.acbc.house`, joined to the `caddy` network (and `postgres` only if you want a
  DB for tags/audit).
- **Reads** the Docker socket (`:ro`), the image-updates-tracker API, Beszel/Glances, Uptime Kuma, and the
  Keycloak admin API.
- **Executes** container actions — so the service (or a small privileged agent it calls) needs write access
  to the Docker socket and the repo checkout. Keep this behind Keycloak (oauth2-proxy / forward_auth),
  authorize by admin role, CSRF-protect, and audit-log every action.
- A single small backend (Python/FastAPI or Node both fit the repo's existing languages) serving a JSON API
  + the UI is plenty; the UI can be server-rendered or a small SPA. Whatever you choose, recreate the
  Nocturne look via the tokens above.
