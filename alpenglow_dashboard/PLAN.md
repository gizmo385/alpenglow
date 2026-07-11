# Alpenglow Dashboard — Implementation Plan

## Context

`/services/alpenglow_dashboard/design_handoff_alpenglow_dashboard/` contains a high-fidelity design
handoff (README + single-file HTML prototype + 5 screenshots) for a management dashboard for the
Alpenglow home server: at-a-glance health for every compose service, an update queue fed by
image-updates-tracker, and per-service drill-down with real actions (restart / stop / start /
pull & recreate). The task is to build it for real and serve it at the apex **`acbc.house`**
(tailnet-only), replacing Homepage which is being retired.

Decisions already made with the operator:
- **Stack:** FastAPI (Python, `uv`) backend + Vite/React/TypeScript SPA frontend, one container.
- **URL:** apex `acbc.house`. Homepage gives up the apex (retired); cutover is the final step.
  Develop/stage at `dashboard.alpenglow.acbc.house` first.
- **Updates data:** add a JSON endpoint upstream to `gizmo385/image-updates-tracker`
  (local checkout: `~/workspace/image-updates-tracker`), then redeploy that service.
- **Auth:** oauth2-proxy in front (copy the `copyparty` pattern), Keycloak realm `acbc.house`,
  restricted to an admin group. Backend trusts `X-Forwarded-User`/`X-Forwarded-Groups`.
- **Tags:** JSON file store in `./data/` (no Postgres dependency).

## Ground truth (every agent reads this first)

**Required reading before any task:**
1. This file, fully.
2. `/services/alpenglow_dashboard/design_handoff_alpenglow_dashboard/README.md` — the design
   source of truth (layout, tokens, SSO model, tag model, interactions).
3. The prototype `Alpenglow Dashboard.dc.html` (layout/behavior reference; mock data ≠ real data)
   and `screenshots/` for any frontend task.
4. `/services/README.md` — repo conventions (Caddy labels, tiers, networks, secrets).

**Repo/host facts (verified 2026-07-11, don't rediscover):**
- Host: `alpenglow.acbc.house`; repo checkout is **`/services`** (git remote
  `gizmo385/alpenglow`, branch `main`). The prototype's `~/alpenglow/{id}` copy-command path is
  wrong — use `/services/{id}`.
- Only `sudo docker` is passwordless on this host. All compose commands: `sudo docker compose …`.
- Caddy is caddy-docker-proxy; routing comes from container labels. **The apex needs the literal
  label `caddy: acbc.house` — the `*.acbc.house` wildcard does NOT match the apex.**
- Tier conventions (drive auto-detection): `*.internal.acbc.house` = LAN-only;
  `*.alpenglow.acbc.house` = management; a second `caddy_1: "….:10443"` label set = public;
  plain `*.acbc.house` labels = tailnet; no caddy labels = internal/no web ingress.
- Split-horizon DNS: pihole maps `acbc.house` → tailscale IP `100.113.29.26`, so the apex is
  already tailnet-only via the default Caddy listener. No `:10443` labels for this service, ever.
- Keycloak: `quay.io/keycloak/keycloak` at `sso.acbc.house`, realm **`acbc.house`**
  (see `/services/keycloak/compose.yaml`). oauth2-proxy example: `/services/copyparty/compose.yaml`.
- Monitoring endpoints reachable over the `caddy` docker network by container name:
  `glances:61208` (REST `/api/4/*`), `uptime_kuma:3001` (Uptime Kuma **v2**),
  `beszel:8090`, updates-tracker at container `updates-tracker-release-feeds-1:8585`.
  All base URLs must come from `.env` with these defaults.
- image-updates-tracker today: `GET /` (HTML), `GET /status` → `{refreshing, last_updated}`,
  `POST /refresh`, `GET /digest` (Ollama, slow), `GET /health`, `GET /feeds.opml`. No JSON list —
  that's work package **D1**.
- ZFS pools are `rpool` and `dpool` (not "tank"); `/services/zfs_status_checker/verify_status.sh`
  is an existing cron that pushes pool health to Uptime Kuma push monitors. Backup jobs
  (`postgres/run_backups.sh`, kopia verify) also push to Uptime Kuma.
- Multi-container services exist (immich, twenty, updates-tracker, copyparty, keycloak, beszel,
  caddy). One **service = one repo directory = one compose project**; group containers by the
  `com.docker.compose.project` label (project name defaults to the directory name).
- Host tooling available for dev: node v24, npm, uv, python3.13. Runtime is the container.

**Design tokens:** implement exactly the Nocturne values in the handoff README §Design Tokens as
CSS custom properties in one `tokens.css`. Per prior feedback: no CSS hacks fighting a framework —
we own this UI, so write plain, token-driven CSS. Icons via `@phosphor-icons/react`
(regular weight; fill for status glyphs). Font: Inter 400/500/600 (self-host via `@fontsource/inter`;
no runtime Google Fonts dependency). Primary buttons are accent **outline**, never filled.

## Architecture

```
/services/alpenglow_dashboard/
├── PLAN.md                     ← this document
├── design_handoff_alpenglow_dashboard/   (unchanged)
├── compose.yaml                dashboard + oauth2-proxy
├── metadata.yaml               per-service category/icon/description/primary_container overrides
├── .env / .proxy_env           secrets (gitignored), .env.example / .proxy_env.example committed
├── .gitignore                  data, .env*, node_modules, dist
├── data/                       tags.json, audit.jsonl, zfs_status.json (host cron writes)
└── app/
    ├── Dockerfile              multi-stage: node builds frontend → python runtime (uv) +
    │                           docker CLI + compose plugin; serves SPA + API on :8080
    ├── backend/                FastAPI, pyproject via uv
    │   └── alpenglow_dashboard/
    │       ├── main.py         app factory, static mount, routers
    │       ├── models.py       ALL pydantic response models (the API contract, task A1)
    │       ├── auth.py         forwarded-header identity, admin check, CSRF
    │       ├── inventory.py    repo scan + docker inventory merge          (B1)
    │       ├── actions.py      compose action runner + busy-state registry (B2)
    │       ├── streams.py      SSE logs + stats ring buffers               (B3)
    │       ├── integrations/   glances.py kuma.py updates.py zfs.py        (B4)
    │       ├── sso.py          Keycloak/env/label SSO detection            (B5)
    │       ├── tags.py         JSON tag store                              (B1)
    │       ├── audit.py        JSONL audit log                             (B2)
    │       └── mock.py         fixture data + MOCK_DATA=1 mode             (A1)
    └── frontend/               Vite + React + TS
        └── src/
            ├── theme/tokens.css
            ├── api/            typed client + poll hooks
            ├── components/     Card, Tag, Button, Meter, Sparkline, Toast, StatusDot…
            └── views/          Shell/Sidebar (C1), Overview (C2), Updates (C3), Detail (C4)
```

**Container:** one image, `build: ./app`. Mounts:
- `/var/run/docker.sock:/var/run/docker.sock` (rw — reads *and* actions; the whole container sits
  behind oauth2-proxy admin auth, which is the trust boundary)
- `/services:/services:ro` — **same path as host** so `docker compose` run inside the container
  resolves bind-mount paths identically for the host daemon. Read-only is sufficient
  (compose never writes to project dirs).
- `./data:/data-store` (rw) for tags/audit; zfs_status.json lands there via host cron.
- Networks: `caddy` only. `TZ: America/Denver`.

**Ingress:** oauth2-proxy container (like copyparty's) carries the Caddy labels and proxies to the
app on 8080. Staging label `dashboard.alpenglow.acbc.house` (wildcard `*.alpenglow.acbc.house`
site) during phases A–E1; cutover (E3) switches to literal `caddy: acbc.house`.

**Status model:** `up | down | restarting | updating`. Docker only knows up/down; the action
runner (B2) keeps an in-memory busy registry that overlays `restarting`/`updating` onto inventory
responses while an action is in flight. Frontend polls; no optimistic client state needed beyond
disabling buttons.

## API contract (locked in task A1 as pydantic models; do not drift)

```
GET  /api/health                → {status, version}
GET  /api/me                    → {user, groups, isAdmin}
GET  /api/csrf                  → {token}          (double-submit; all POST/PUT require X-CSRF-Token)
GET  /api/overview              → {services:{up,down,total}, updates:{count},
                                   monitors:{up,total,note}, backups:{pgAgo,kopiaAgo,ok},
                                   host:{load1,load5,load15,cpuPct,memUsed,memTotal,swapUsed,swapTotal},
                                   storage:{pools:[{name,state,used,size,scrubAgo,errors}],
                                            fs:[{label,used,size,pct}]},
                                   polledAt, meta:{host,branch}}
GET  /api/services              → ServiceSummary[]:
     {id, name, category, icon, description, image, currentVersion,
      latestVersion|null, releasedAt|null, changelogUrl|null,
      status, uptimeSeconds|null, url|null, tier, containers:[{name,status}],
      sso:{state: keycloak|oidc|self|native|none, source: str}, tags:[str],
      restartPolicy, ports:str, memLimit:str|null}
GET  /api/services/{id}         → ServiceDetail (summary + facts fields)
GET  /api/services/{id}/stats   → {history:{cpu:[{t,v}], mem:[{t,v}]},
                                   current:{cpuPct,memUsed,memLimit,netIO,blockIO,pids,restarts}}
GET  /api/services/{id}/logs    → SSE stream (docker logs -f --tail 100, all containers, prefixed)
GET  /api/services/{id}/compose → text/plain (the real compose.yaml)
PUT  /api/services/{id}/tags    → {tags:[str]}
POST /api/services/{id}/actions → {action: restart|stop|start|pull, confirm?:bool} → {accepted, jobId}
GET  /api/updates               → {services:[UpdateEntry], lastUpdated, refreshing}
POST /api/updates/refresh       → proxies tracker /refresh
```

`id` = repo directory name. Service identity: enumerate `/services/*/compose.yaml`, match running
containers via `com.docker.compose.project`. Aggregate status = worst container status;
`primary_container` (metadata.yaml, default = container whose service name matches the dir, else
first) supplies image/version/uptime.

## Work packages

Each block below is one Opus agent task: self-contained, with inputs and acceptance criteria.
Dependencies are explicit; anything at the same level with no arrow between them can run in
parallel. **A1 pre-creates every backend module and route stub returning mock data, so B/C agents
fill in bodies without colliding on shared files.** Agents must not `git commit` unless their task
says so.

### Phase A — Foundation (serial; blocks everything)

**A1 — Scaffold, contract & mock mode**
- Create the tree above: FastAPI app with `models.py` implementing the full API contract; every
  route stubbed against `mock.py` fixtures (port the prototype's mock service list, adjusted to
  the *real* ~30 service dirs in `/services`); `MOCK_DATA=1` env switch keeps stubs live after
  real implementations land.
- Frontend scaffold: Vite + React + TS, `tokens.css` with all Nocturne tokens from the handoff
  README, `@phosphor-icons/react`, `@fontsource/inter`, typed API client generated by hand from
  `models.py`, a placeholder shell rendering `/api/services` mock data.
- `Dockerfile` (multi-stage: `node:24-slim` builds `frontend/dist` → `python:3.13-slim` + uv +
  `docker-ce-cli` + `docker-compose-plugin`; FastAPI serves `dist/` statically), `compose.yaml`
  with dashboard + oauth2-proxy (staging labels on `*.alpenglow.acbc.house` /
  `dashboard.alpenglow.acbc.house`), `.env.example`, `.proxy_env.example`, `.gitignore`,
  `metadata.yaml` seeded with category/icon/description for every current service dir
  (categories: Media, Productivity, Home, Infrastructure, Monitoring — mirror the prototype's
  assignments, add the ones it lacks: amniotic, attic, cup, servo_bot, trek, tandoor, twenty,
  homepage, immichframe, ddclient, mosquitto…).
- `auth.py`: middleware reading `X-Forwarded-User`/`X-Forwarded-Groups`; `DEV_NO_AUTH=1` bypass
  for local dev and MOCK mode; CSRF double-submit token; admin-group gate on all mutating routes.
- Dev workflow docs in `app/README.md`: `uv run fastapi dev` + `npm run dev` (Vite proxy to
  :8000), and the containerized path `sudo docker compose up --build`.
- **Accept:** `sudo docker build` succeeds; `MOCK_DATA=1` container serves the SPA shell and all
  API routes with valid mock payloads; contract models match this document exactly.
- **Commit** at end of A1 (foundation snapshot).

### Phase B — Backend (all depend on A1; B2–B5 also depend on B1; B2∥B3∥B4∥B5)

**B1 — Inventory core (+ tags)**
- `inventory.py`: scan `/services/*/compose.yaml` (PyYAML), parse caddy labels → `url` + `tier`
  (rules in Ground truth), restart policy, ports/expose, mem limits; docker socket (`aiodocker`
  or `docker` SDK) for container state, image, uptime, compose-project grouping; merge with
  `metadata.yaml`; sidebar meta (hostname, branch by parsing `/services/.git/HEAD`).
- `tags.py`: `/data-store/tags.json` with asyncio lock; seed empty; PUT endpoint.
- Compose endpoint returns the actual file text.
- **Accept:** against the live socket, `/api/services` returns every service dir with correct
  status/tier/url; stopping a test container flips its status on next poll; tags persist across
  container restart.

**B2 — Action engine + audit**
- `actions.py`: async subprocess runner — `restart|stop|start` →
  `docker compose --project-directory /services/{id} {restart|stop|up -d}`;
  `pull` → `pull` then `up -d`. Per-service lock, global concurrency 2, busy-state registry that
  overlays `restarting`/`updating` onto B1 responses, timeout + error capture.
- Guardrails: acting on `caddy`, `postgres`, or `alpenglow_dashboard` itself requires
  `confirm:true`; every action → `audit.py` JSONL (`ts, user, service, action, outcome, stderr-tail`).
- **Accept:** restart of a low-risk service (`glances`) works end-to-end from curl with CSRF +
  forwarded headers; status transitions visible via `/api/services`; audit line written; action on
  `caddy` without `confirm` → 409.

**B3 — Log streaming + stats history**
- SSE `/logs` (multiplex all project containers, `--tail 100` + follow, prefix container name,
  heartbeat comments to keep proxies happy). Background stats poller (15s, running containers
  only) → ring buffer (last 60 samples) for `/stats` (cpu %, mem, net/block IO, pids, restart count).
- **Accept:** curl SSE shows live lines for a chatty service; `/stats` history grows over time;
  poller survives container churn.

**B4 — External integrations + overview tile**
- `integrations/glances.py`: `/api/4/{cpu,mem,memswap,load,fs}` → host + fs bars.
- `integrations/kuma.py`: Uptime Kuma **v2** — use an API key from `.env`; prefer its metrics/API
  for monitor up/total AND the push monitors for pg-dump / kopia / rpool / dpool freshness. Agent
  must verify against the live instance (`uptime_kuma:3001`) what v2 actually exposes and pick the
  cleanest supported mechanism; degrade tile fields to `null` (frontend renders "—") when data
  isn't available rather than faking it.
- `integrations/zfs.py`: read `/data-store/zfs_status.json` (written by D3; tolerate absence).
- `integrations/updates.py`: client for the tracker's new `/api/updates` (D1), matched to services
  by image ref (strip tag); graceful empty result + a visible `trackerUnavailable` flag if the
  endpoint 404s (D1 not deployed yet). `POST /api/updates/refresh` proxy.
- Compose `/api/overview` from all of the above + B1 counts.
- **Accept:** with real endpoints configured, `/api/overview` fields are live numbers; each
  integration failing (stopped container) degrades to nulls, never 500s.

**B5 — SSO auto-detection**
- `sso.py`, three signals in authority order (per handoff §SSO model):
  1. Keycloak admin API (`sso.acbc.house`, realm `acbc.house`) via a service-account client
     (created in D2) with `view-clients`; match client IDs ↔ service dirs (overrides map in
     `metadata.yaml`).
  2. Env/compose scan: key **names only** from `/services/{id}/.env*` (`OIDC_*`, `OAUTH*`,
     `KEYCLOAK_*`) and oauth2-proxy sidecars in compose files. Never expose values.
  3. Caddy `forward_auth` labels.
- Produce `{state, source}` per the handoff's five states with real source strings
  (e.g. `oauth2-proxy sidecar in compose.yaml`, `OIDC_ISSUER_URL in .env`,
  `no OIDC keys found in .env`); keycloak itself → `self`; cache ~5 min.
- **Accept:** copyparty → oauth2-proxy detection; keycloak → `self`; a known-native service
  (jellyfin; home_assistant has the vendored OIDC plugin and should surface it) and a known-`none`
  service each classify correctly with truthful source strings.

### Phase C — Frontend (C1 depends on A1; C2∥C3∥C4 depend on C1; runs parallel to Phase B against mock mode)

**C1 — Shell, sidebar, primitives, toasts**
- App shell per handoff §Sidebar: 222px sidebar (brand tile, Overview/Updates nav with count
  badge, category filter list with counts, footer host/branch from `/api/overview.meta`), routed
  views (`/`, `/updates`, `/services/:id` — react-router), toast system, shared primitives
  (Card, Tag, Button variants, Input, Meter, StatusDot with softPulse, Sparkline, Table),
  polling hooks (10s services, 30s overview; pause when tab hidden), focus-ring / animation CSS
  (`spin`, `softPulse`, `toastIn`).
- **Accept:** shell pixel-matches `01-overview.png` chrome; nav/category/badge behavior per README.

**C2 — Overview view** — stat tiles (Services, Updates→clickable, Monitors, Backups, Host
resources ×2-span, Storage & ZFS ×2-span with real pool names rpool/dpool), search + tag-filter
row + category grouping + service cards exactly per handoff §View 1 (3px priority border, status
dot, update pill, SSO/tier/tag chips, Open/Manage footer), empty state. Filters compose
(query × category × tag). **Accept:** matches `01-overview.png` with mock data; all filters work.

**C3 — Updates view** — table per §View 2, Copy-all / Update-all header actions, per-row changelog
link + Pull & recreate with spinner/disabled busy state, "tracked by image-updates-tracker"
sub-line, up-to-date empty state, tracker-unavailable notice. Copy command format:
`cd /services/{id} && sudo docker compose pull && sudo docker compose up -d`.
**Accept:** matches `02-updates.png`; busy flow driven by polled status.

**C4 — Service detail** — back button, header (icon tile, status tag, Open), action bar (Pull &
recreate when update, Restart, Stop/Start, Copy update cmd; confirm dialog when API returns the
guardrail 409), four tabs: Overview facts grid + description + tag editor (removable chips, preset
suggestions, custom input), Resources (big numbers + SVG sparklines from `/stats`, Net/Block/PIDs/
Restarts row), Logs (SSE, streaming indicator, autoscroll, error/success line tinting), Compose
(mono block + copy). **Accept:** matches `03/04/05` screenshots; SSE reconnects on drop.

### Phase D — Adjacent systems (independent; can start immediately, parallel to everything)

**D1 — updates-tracker JSON endpoint** *(in `~/workspace/image-updates-tracker`)*
- Add `GET /api/updates` to `server.py`: serialize `update_cache.get()` →
  `{services:[{name, owner, repo, image, current_version, latest_version, has_updates,
  html_url, image_url, releases:[{tag, url, published_at}]}], last_updated, refreshing}`
  (fields already on `ServiceStatus`/`Release`). Include `published_at` of the newest release —
  the dashboard's "Released" column needs it; verify the `Release` dataclass carries a date and
  add it to the feed parse if missing.
- Test locally, commit + push to `main` (its GH Actions builds `ghcr.io/gizmo385/image-updates-tracker:main`),
  then on the host: `cd /services/updates-tracker && sudo docker compose pull && sudo docker compose up -d`,
  verify `curl updates.alpenglow.acbc.house/api/updates`.
- **Accept:** live endpoint returns current pending updates as JSON.

**D2 — Keycloak wiring** *(operator-supervised — touches the live IdP; do this interactively, show
each change before applying)*
- Realm `acbc.house`, via `kcadm.sh` inside the keycloak container (or admin UI steps documented
  for the operator):
  1. Confidential client `alpenglow-dashboard` for oauth2-proxy — redirect URIs for
     `https://dashboard.alpenglow.acbc.house/oauth2/callback` **and**
     `https://acbc.house/oauth2/callback`; groups claim in tokens; note an admin group (reuse the
     existing admin group if one exists — inspect realm first) and restrict via oauth2-proxy
     `allowed_groups`.
  2. Service-account client `alpenglow-dashboard-sso-scan` with realm-management `view-clients`
     role (for B5).
- Fill `.proxy_env` (cookie secret, client secret, issuer `https://sso.acbc.house/realms/acbc.house`,
  `--set-xauthrequest`/pass-user-headers so the backend gets user+groups) and `.env`
  (scan credentials). Update `.example` stubs.
- **Accept:** staging URL forces Keycloak login; non-admin blocked; `/api/me` shows user+groups;
  B5 scan lists realm clients.

**D3 — ZFS status feed**
- Extend `/services/zfs_status_checker/verify_status.sh` (keep its Kuma pushes intact) to also
  write `/services/alpenglow_dashboard/data/zfs_status.json`: per pool (`rpool`, `dpool`) state,
  capacity used/size, last scrub time + errors (parse `zpool status`; use `zpool status -j` if the
  installed version supports it), plus a `generated_at` stamp. Ensure the dashboard container can
  read it (group perms) and document the cron cadence.
- **Accept:** file exists with real pool data; dashboard Storage tile renders it.

### Phase E — Integration, QA, cutover (serial; E1 needs B*, C*, D1, D2)

**E1 — Live integration pass**
- Flip `MOCK_DATA=0`, deploy to staging (`dashboard.alpenglow.acbc.house`), walk every view
  against real data. Fix seams: image↔tracker matching, multi-container aggregation (immich,
  twenty), services with no ingress, host-network containers, tier edge cases (uptime_kuma has a
  public `:10443` label set — it should still read as management, decide precedence: management/
  internal patterns win over public flag). Smoke-test actions end-to-end: restart `glances`, stop/
  start something low-risk, and one real `pull` if an update is pending. Verify audit log + CSRF +
  group gating: the app must reject requests lacking the forwarded identity unless `DEV_NO_AUTH=1`
  (defense in depth behind the proxy).
- **Accept:** a written checklist of every view/action verified live, committed as
  `app/VERIFICATION.md`.

**E2 — Design fidelity QA**
- Compare running app to the 5 screenshots + README measurements (spacing scale, radii, shadows,
  type sizes, chip styles, borders, hover/pulse animations). Fix deviations. Use browser
  screenshots at 1280×860 side-by-side with the handoff PNGs.
- **Accept:** annotated before/after screenshots; no visible token deviations.

**E3 — Apex cutover + retire Homepage**
- Remove `caddy:`/`caddy.reverse_proxy` labels from `/services/homepage/compose.yaml` and
  `sudo docker compose up -d` there (container may keep running unlabeled), then switch
  oauth2-proxy labels to the **literal** `caddy: acbc.house` (keep the staging subdomain as a
  secondary label set). Verify `https://acbc.house` from the tailnet → Keycloak → dashboard.
  Roll back = restore homepage labels.
- Final commit of `/services/alpenglow_dashboard` + homepage change; update repo `/services/README.md`
  only if it references homepage as the apex.
- **Accept:** apex serves the dashboard behind SSO; homepage unreachable at apex; everything in
  `git status` intentional.

## Orchestration guide

Suggested schedule (each box = one Opus agent; D-track can start at t0):

```
t0: A1 ──────────────┐            D1, D3 (parallel, independent)
t1: B1 ── C1         │            D2 (operator-supervised, any time after A1)
t2: B2 B3 B4 B5 ∥ C2 C3 C4
t3: E1 → E2 → E3
```

- Give every agent: this PLAN.md path, the handoff dir path, and its single work-package heading.
- B2–B5 and C2–C4 touch disjoint files by construction (A1 creates the module/file skeleton);
  they can share one worktree. If running truly concurrently, prefer separate worktrees and merge.
- Verification commands available to agents: `sudo docker compose` (passwordless), curl against
  staging, `npm run build`, `uv run pytest` (A1 sets up a minimal test harness; every B task adds
  tests for its module — inventory parsing and SSO classification especially, using fixture
  compose files rather than the live repo).
- Commit points: end of A1, end of Phase B+C (one commit), each D task in its own repo/commit,
  E3 final. Commit messages follow repo style (short imperative, e.g. "Add alpenglow dashboard").

## Risks / notes

- **Self-management hazard:** the dashboard can restart caddy/postgres/itself — mitigated by the
  `confirm` guardrail (B2) and audit log; restarting caddy will drop the user's own connection
  (document in UI copy on the confirm dialog).
- **Uptime Kuma v2 API surface** is the least-certain integration; B4 explicitly allows degrading
  tile fields to "—" rather than blocking.
- `.env` scanning (B5) reads secret files; only key *names* may leave the module — reviewed in E1.
- The `docker compose` CLI inside the container must match host daemon expectations; the
  `/services:/services:ro` same-path mount is what makes bind mounts resolve — do not change the
  mount path.
