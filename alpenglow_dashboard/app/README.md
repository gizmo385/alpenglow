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

The verdicts show in the Updates view's Assessment column and lead the
[daily digest](#daily-digest). `POST /api/upgrades/sync` (admin) syncs straight away.

The token needs the **Tasks** preset (tasks, comments and labels). Code is in
`backend/alpenglow_dashboard/upgrades/`.

## Security

The cve-scanner (`/services/cve-scanner`) scans every running image once a
night and writes `cve-scanner/data/findings.json`. The dashboard reads it
(the `/services` mount) and keeps only findings worth a look: on CISA's
known-exploited list, an advisory on the app's own GitHub repo, Critical or
High with a fixed version, or an EPSS of at least `SECURITY_EPSS_THRESHOLD`
(10%). `cve-scanner/ignore.yaml` silences the rest, with a reason.

Every 30 minutes (`SECURITY_SYNC_INTERVAL`), with `VIKUNJA_API_TOKEN` and
`SECURITY_PROJECT_ID` set, it keeps Vikunja's Security project in step:

- a service with open findings gets one card listing them, its exposure
  (public, tailnet, none), the version it runs and whether a newer release
  is out (from the update tracker), and a link to its compose file. The
  claude-dispatcher's `security` profile assesses it and labels it by what
  there is to do:
  - **Act now**: there's evidence it's exploitable as we run it;
  - **Next upgrade**: real, and an upgrade or rebuild that exists today
    fixes it;
  - **Waiting on upstream**: real, but nothing we can pull or rebuild fixes
    it yet (typically we run the latest release and its image hasn't been
    rebuilt with the fixed packages);
  - **Not affected**: not reachable as we run it;
- when the running image changes (an upgrade, a rebuild) and findings are
  still open, the card is rewritten for the new image and Claude assesses
  again, whatever it said before. Until it answers, the service shows as
  waiting for assessment rather than with the old image's verdict. Cards
  filed before this was recorded are reassessed once;
- new findings on an open card update it and leave an `@claude` comment, so
  Claude assesses the whole list again;
- when some of a card's findings go away but others remain (an upgrade, a
  rebuilt image), the card's list is rewritten. An **Act now** card also gets
  an `@claude` comment so Claude assesses again, since the urgent finding may
  be the one that went; other verdicts only get a note, because losing
  findings can't make them worse;
- an open card closes once a clean scan no longer shows its findings, and a
  card you closed isn't refiled for the same findings.

Where it shows up:

- **Security page** (`/security`, in the sidebar): every service with open
  findings, grouped by verdict, with its exposure, the version it runs and
  whether a newer one is out, Claude's one-line reason and when it was
  reviewed. Filters for verdict and exposure, **Reassess** per service, and
  **Reassess all**. This is the place to find a service that has findings but
  no pending update, which the Updates page doesn't list. The sidebar badge
  counts Act now.
- **Overview:** a Security tile leads with how many services need a step
  (Act now and Next upgrade) and opens the Security page, and affected service
  cards get a shield (not for Not affected or Waiting on upstream).
- **Updates:** a Security column after Assessment; flagged services sort
  first.
- **Service → Security tab:** the findings, Claude's latest reply, a box to
  ask Claude a follow-up (an `@claude` comment on the card), and earlier
  cards.
- **Discord:** `DISCORD_SECURITY_WEBHOOK_URL` gets an immediate ping, once,
  for an Act now verdict or for a known-exploited CVE on a public service
  (the latter without waiting for Claude). Sent pings are kept in
  `data/security-pings.json`. Everything else goes in the 9 AM digest's
  Security section (see [Daily digest](#daily-digest)). Nothing
  open means no section and no ping.

Setup:

1. Create a **Security** project in Vikunja, map its id to `security` in
   `claude-dispatcher/profiles.yaml`, and add the dispatcher webhook to it
   (claude-dispatcher/README.md, step 4).
2. Set `SECURITY_PROJECT_ID` (and optionally the Discord settings) in `.env`;
   see `.env.example`.

To get a fresh verdict without waiting for something to change, use
**Reassess** on a service's Security tab (`POST
/api/services/{id}/security/reassess`). It first asks the cve-scanner to look
at the service's running images again (a file in `cve-scanner/data/requests/`,
which the scanner removes when it's done), so an upgrade since last night's
scan is what gets assessed: seconds when the image is the same, minutes when
it's new. Then the card is rewritten with today's version and findings and
Claude is asked. If the scanner doesn't answer within
`SECURITY_RESCAN_TIMEOUT` (900 s), Claude is asked about the findings we have.

`POST /api/security/reassess` asks again on every open card at once, e.g.
after changing the reviewer's instructions in
`claude-dispatcher/profiles.yaml`. It doesn't rescan.

`POST /api/security/sync` (admin) syncs straight away. Code is in
`backend/alpenglow_dashboard/security/`.

## Daily digest

Once a day (`DIGEST_AT`, local time, default `09:00`) the dashboard posts what's
waiting for a person to Discord, in up to three messages:

1. **Security**: the services with an **Act now** or **Next upgrade** verdict,
   each with Claude's one-line reason. Services with nothing to do (waiting on
   upstream, not affected, being assessed) are only named, one line per
   verdict, and when no service needs a step the message is left out.
2. **Upgrades**: the open Upgrades cards, grouped by verdict.
3. **Other updates**: pending updates with no card, from the tracker.

Everything in it is something you can act on. A release the tracker is holding
back because its image isn't published yet is left out until it can be pulled.

A message with nothing in it is left out, and on a day with none nothing is
posted. The tracker rescans first, so the digest matches what's running.

It goes to `DIGEST_WEBHOOK_URL`, or to `UPDATE_SUMMARY_WEBHOOK_URL` when that's
unset; with neither, or with `DIGEST_AT=off`, there is no digest.
`GET /api/digest` shows what it would say now, and `POST /api/digest/send`
sends it now.

The tracker's Discord bot used to send this, fetching the verdicts from the
dashboard. It isn't run any more: the tracker reports what exists upstream, and
the dashboard decides what to say about it. `GET /internal/upgrades/annotations`
and `GET /internal/security/annotations` are what that bot read, and nothing
uses them now.

## Updating services

The Updates view lists one row per pending image. A service with several
(`signoz`: `signoz/signoz` and `signoz/signoz-otel-collector`) gets a row for
each, and its rows select and update together: Update works on the whole
service.

Rows come from image-updates-tracker, which only knows GitHub releases. An
image pinned with `auto: {follow: latest}` (see "Your own images" below) has
none, so the dashboard asks the registry where `latest` points itself, using the same
`./pin resolve` as the auto-updater, cached for 10 minutes and redone on
**Check for updates**. When that's a newer build than the pin, it gets a row
too (`sha-5c5381c` → `sha-fdfa579`, the build's age, a GitHub compare link for
`ghcr.io/OWNER/REPO` images), whether or not automatic updates are on. Its
Update repins to exactly that tag.

Publishers sometimes push a new image to a tag we already run (Keycloak
re-pushing `26.6.4` with a CVE fix, official images rebuilt on a new base).
Every 6 hours the dashboard asks each pinned image's registry where its tag
points (a manifest lookup: nothing is pulled, and Docker Hub doesn't count it
against the pull limit). When that's not the digest in `versions.yaml`, the
image gets a **rebuilt** row (`2.1.2-alpine` → `2.1.2-alpine`, rebuilt) whose
Update repins the same tag to the new digest. A new release of the same image
replaces its rebuilt row. **Check for updates** looks again straight away.

Tick services and press **Update selected**, or press a row's **Update**.
Either posts `POST /api/updates/apply` (admin, CSRF), which:

1. repins every image among them that `versions.yaml` lists (not
   `policy: float`), one at a time (pin locks `versions.yaml` while it
   writes, so a `./pin` at a shell waits its turn too): `/services/pin set IMAGE TAG` with the
   tracker's new release, spelled like the current tag (`v` prefix and a
   suffix such as `-alpine` kept; the next spelling is tried if the registry
   has no such tag, and any other failure stops that service's update with the
   reason, leaving that image's files as they were; any of the service's
   other images already repinned stay so, and the reason says which);
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

While a row waits it shows **Updated**, or why its update failed. When the last
update in flight has finished, the tracker rescans once, which clears the updated
rows, and a summary of the results goes to Discord if
`UPDATE_SUMMARY_WEBHOOK_URL` is set in `.env` (a channel webhook, which can only post to its one
channel: the dashboard is root-equivalent and shouldn't hold a bot token). The dashboard's own successful
update ends the process first, so it isn't in a summary, and its row clears at the
tracker's next scan. Failure reasons are posted, so mind who can read the channel.

## Automatic updates

Images can update themselves, one by one, on rules set beside their pin in
`versions.yaml`. An image opts in with `policy: auto-eligible`, and an
optional `auto:` block changes the defaults:

```yaml
freshrss/freshrss:
  tag: 1.30.4
  digest: sha256:...
  policy: auto-eligible
  auto:
    every: daily        # hourly | daily | weekly | 6h | 2d | a cron line ("0 4 * * *", in TZ)
    max_bump: minor     # patch (default) | minor | major
    soak: 3d            # how long a release must be out first (default 2d)
    verdict: Easy       # Claude's verdict needed (default Easy) | Needs care | any
    monitor: FreshRSS   # Uptime Kuma monitor(s) that must be up afterwards (optional)
```

`./pin check` (and so CI) rejects an `auto:` block it doesn't understand.

**What qualifies.** When an image's `every` comes round, the dashboard reads
the tracker and the Upgrades board. The newest release goes out only if:

- it is no bigger than `max_bump`. Before 1.0 a minor release counts as major
  (0.21 → 0.22), since semver lets those break things;
- it has been out for `soak`;
- Claude's verdict on its card, for that exact release, is good enough.
  `verdict: any` skips this, for your own images or anything you'd update
  regardless. A card moved to a newer release counts as unassessed until
  Claude answers again;
- nobody is updating that service already, and it isn't paused.

In the Updates view, a pending row that will go out by itself says when
("Auto · in 19h"), or just "Auto" while it waits for Claude's assessment;
hover it for the reason. Rows that stay manual get no note. Below the table, **Automatic updates** lists only what needs a look:
services paused after a failed update, with **Resume**, and what updated
automatically in the last week (Resume clears the failure from it). **Check
now** rescans the tracker first, so a rollback or repin made by hand counts.
**Show rules** lists every opted-in service and its settings. A service's first check is one full
interval after it opts in (or after the dashboard first sees it).

**Several images.** Each image is its own unit, so in a service with several
images only the ones that opt in update. Images that share a `group` are one
unit: it updates only if every member is auto-eligible, with the most cautious
of their settings (and the first member's `every` that sets one). Units in the
same service directory that are due together go out together.

**Your own images.** A `policy: float` entry on a moving tag such as `main`
can set `auto: {follow: true, every: 6h}`. It's pulled on schedule and, when
the tag points at a new image, health-checked and reported like any other.
An image that publishes only `latest` plus per-build tags (`sha-<commit>`) is
pinned to a per-build tag with `auto: {follow: latest}` instead. Each check asks
the registry which `sha-*` tag `latest` points at (`./pin resolve`) and, when
it has moved, repins to it like any other update, so the build that runs is
recorded in versions.yaml and `./pin set` puts an older one back. Neither kind
has a version to judge, so `max_bump`, `soak` and `verdict` don't apply.

**What an update does.** The same repin as the Update button (`./pin set`),
then `docker compose pull` and `up -d` for just the compose services that use
the updated images, so a sidecar on `latest` isn't swept along. Then the
health check: over `AUTO_UPDATE_HEALTH_WAIT` seconds (180 by default) each
recreated container has to keep running without a restart, a container with
its own healthcheck has to end up `healthy`, and every `monitor` has to be up
in Uptime Kuma. The Discord summary (`UPDATE_SUMMARY_WEBHOOK_URL`) lists what
went out.

**When it fails, nothing is rolled back.** A release may already have
migrated its data in the shared Postgres. The unit is paused with the reason,
its Upgrades card gets a comment, the summary says what failed, and nothing
more is tried for it until you press **Resume** under Automatic updates. The new
pin stays, as with the Update button, so to try it again after rolling the
container back, roll the pin back too (`./pin set NAME OLD_TAG`); otherwise
the service already counts as on the new release.

**Settings** (`../compose.yaml`):

| Variable | Default | |
|---|---|---|
| `AUTO_UPDATE` | `dry-run` | `off`, `dry-run` (announce on Discord once what would go out, change nothing) or `on` |
| `AUTO_UPDATE_EVERY` | `daily` | Schedule for entries that don't set `every` |
| `AUTO_UPDATE_COMMIT` | off | `1` commits `versions.yaml` and the compose files pin rewrote, in `/services`, after each successful update. Never pushed. Off, they're left uncommitted like the Update button's |
| `AUTO_UPDATE_GIT_AUTHOR` | `Alpenglow auto-update <auto-update@alpenglow.local>` | Author of those commits |
| `AUTO_UPDATE_HEALTH_WAIT` | `180` | Seconds a recreated container must stay healthy |
| `AUTO_UPDATE_STATE` | `/data-store/autoupdate.json` | Schedules, pauses and last results |

`GET /api/autoupdate` lists each unit, where its pending release stands
(`hold`, `eta`) and why it last waited, `POST
/api/autoupdate/run` checks every unit now regardless of schedule (**Check
now**), and `POST /api/autoupdate/resume` un-pauses one. Code is in
`backend/alpenglow_dashboard/autoupdate/`.

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
