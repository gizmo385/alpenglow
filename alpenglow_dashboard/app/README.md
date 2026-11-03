# Alpenglow Dashboard — app

FastAPI backend + Vite/React/TS frontend, shipped as one container. See
`../PLAN.md` for the full architecture and work-package breakdown; the API
contract lives in `backend/alpenglow_dashboard/models.py` (mirrored by hand in
`frontend/src/api/types.ts` — keep them in lockstep).

## Local development

Two processes, hot reload on both sides:

```sh
# terminal 1 — backend on :8000, mock data, auth bypassed
cd app/backend
MOCK_DATA=1 DEV_NO_AUTH=1 COOKIE_SECURE=0 uv run fastapi dev alpenglow_dashboard/main.py

# terminal 2 — frontend on :5173, /api proxied to :8000 (vite.config.ts)
cd app/frontend
npm install
npm run dev
```

Open http://localhost:5173.

- `MOCK_DATA=1` serves every route from `mock.py` fixtures (ids mirror the real
  `/services/*` directories).
- `DEV_NO_AUTH=1` synthesizes an admin identity instead of requiring
  `X-Forwarded-User` / `X-Forwarded-Groups` from oauth2-proxy. Never set it in
  production.
- `PROXY_AUTH_SECRET` must match oauth2-proxy's
  `OAUTH2_PROXY_BASIC_AUTH_PASSWORD`. The forwarded headers are only trusted
  on requests carrying it as the Basic auth password, since any container on
  the `caddy` network can reach the backend directly. Unset, every `/api`
  request is rejected.
- `COOKIE_SECURE=0` lets the CSRF cookie work over plain http in dev.

### Tests

```sh
cd app/backend
uv run pytest
```

## Containerized

From the service directory (`/services/alpenglow_dashboard`), with `.env` and
`.proxy_env` populated from the `.example` files:

```sh
sudo docker compose up --build
```

The multi-stage `Dockerfile` builds the frontend with node:24-slim, then runs
the backend on python:3.13-slim (via uv) with `docker-ce-cli` +
`docker-compose-plugin` installed; FastAPI serves the built SPA from
`/app/static` and everything listens on :8080. oauth2-proxy fronts it and
carries the Caddy labels (staging: `dashboard.alpenglow.acbc.house`).

Ad-hoc smoke test without the compose stack:

```sh
sudo docker build -t alpenglow-dashboard ./app
sudo docker run --rm -e MOCK_DATA=1 -e DEV_NO_AUTH=1 \
  -p 127.0.0.1:18080:8080 alpenglow-dashboard
curl http://127.0.0.1:18080/api/health
```

## Upgrades board

With `VIKUNJA_API_TOKEN` and `VIKUNJA_PROJECT_ID` set (see `../compose.yaml`),
the backend keeps Vikunja's Upgrades project in step with the update tracker
every 30 minutes (`UPGRADES_SYNC_INTERVAL`):

- a new release of an image listed in `versions.yaml` (never `policy: float`)
  gets a card, which the claude-dispatcher assesses and labels with a verdict;
- an open card is retargeted, with an `@claude` comment, when a newer release
  comes out, and closed once the server runs the release;
- a card you closed isn't refiled for the same version.

The verdicts show in the Updates view's Assessment column, and
`GET /internal/upgrades/annotations` serves them to the tracker's Discord bot
(`ANNOTATIONS_URL`). That route sits outside `/api` because the bot can't pass
oauth2-proxy: it is read-only and returns only card titles, verdicts and
links. `POST /api/upgrades/sync` (admin) syncs straight away.

The token needs the **Tasks** preset (tasks, comments and labels). Code is in
`backend/alpenglow_dashboard/upgrades/`.

## Updating services

In the Updates view, tick services and press **Update selected**, or press a
row's **Update**. Either posts `POST /api/updates/apply` (admin, CSRF), which:

1. repins every image among them that `versions.yaml` lists (not
   `policy: float`), one at a time (pin locks `versions.yaml` while it
   writes, so a `./pin` at a shell waits its turn too): `/services/pin set IMAGE TAG` with the
   tracker's new release, spelled like the current tag (`v` prefix and a
   suffix such as `-alpine` kept; the next spelling is tried if the registry
   has no such tag, and any other failure stops that service's update with the
   reason, leaving its files as they were);
2. then runs `docker compose pull` and `up -d` for each, two at a time. The
   dashboard's own service goes last.

The repin is **not committed**: `versions.yaml` and the compose files are left
modified in `/services` for you to review and commit. A failed pull or
recreate is **not rolled back** either (going back to an older version isn't
always safe once a release has migrated its data); the audit log records the
error and the pin it left. pin runs as the owner of `/services/versions.yaml`
so it never leaves root-owned files in the checkout, with its virtualenv and
cache under `/tmp/alpenglow-repin`. This is why `/services` is mounted
read-write. Code is in `backend/alpenglow_dashboard/repin.py` and
`actions.py`.

## Layout

```
app/
├── Dockerfile            node build → python runtime (uv + docker CLI)
├── backend/
│   ├── pyproject.toml    uv project; `uv run pytest`
│   ├── alpenglow_dashboard/
│   │   ├── main.py       app factory, routers, SPA static mount
│   │   ├── models.py     ALL pydantic response models (the API contract)
│   │   ├── auth.py       forwarded-header identity, CSRF, admin gate
│   │   ├── mock.py       MOCK_DATA=1 fixtures (real /services dirs)
│   │   ├── inventory.py  (B1) tags.py (B1) actions.py (B2) audit.py (B2)
│   │   ├── streams.py    (B3) sso.py (B5)
│   │   └── integrations/ glances kuma updates zfs overview (B4)
│   └── tests/            contract + auth smoke tests
└── frontend/
    └── src/
        ├── theme/tokens.css   Nocturne design tokens
        ├── api/               types.ts + client.ts (typed, CSRF-aware)
        ├── components/        (C1) shared primitives
        └── views/             (C1–C4) Shell, Overview, Updates, Detail
```
