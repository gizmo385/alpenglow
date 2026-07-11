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
