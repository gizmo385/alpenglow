# Claude dispatcher

Runs Claude Code on cards from the [Vikunja board](../vikunja/README.md). A new
card, or a comment starting with `@claude`, in a mapped project starts a job.
The job runs in a locked-down container, and its answer comes back as a
comment on the card.

```
Vikunja ──webhook──▶ dispatcher ──job daemon (rootless)──▶ claude-job container
   ▲                     │                                        │ HTTPS only
   └──── comment ◀───────┘                         claude-egress-<profile> proxy ──▶ allowlisted hosts
```

## What keeps a job contained

| Boundary | How |
|---|---|
| No host root | Jobs run on a separate **rootless** Docker daemon owned by the `claude-jobs` user. The dispatcher only has that daemon's socket, never the main one. |
| No secrets | Nothing from the host is mounted. The only secret a job gets is the Claude token. |
| Container | uid 1000, `cap_drop: ALL`, `no-new-privileges`, read-only root, tmpfs for `$HOME` and `/work`, memory/CPU/PID limits, time limit. Optional gVisor (`runtime: runsc` in a profile). |
| Network | Each profile has an internal network with no route out. Its egress proxy allows HTTPS only to `api.anthropic.com` plus the profile's `egress` hosts, and refuses any name that resolves to a LAN, loopback or Tailnet address. |
| Telemetry | With `OTLP_UPSTREAM` set, the egress proxy relays `POST /v1/metrics` (up to 1 MiB) to that one URL and nothing else. Jobs send metrics only, so no prompts, tool details or traces leave the job this way. A job could still forge its own metrics, which at worst skews a dashboard. |
| Who can start jobs | Webhooks must carry Vikunja's HMAC signature. A card's **project** picks its profile in `profiles.yaml`; a card can't choose. `allowed_users` limits who can start jobs per profile. |
| Runaway use | `JOBS_PER_DAY` and `MAX_CONCURRENT_JOBS` in `.env`; one job per card at a time. |
| Untrusted output | Claude's reply is posted as escaped text, never HTML, and a reply can't start with `@claude`, so it can't trigger another job. |

The job can read its own Claude token. The egress allowlist is what stops a
prompt-injected job from sending it anywhere else.

## Setup

These steps are for Debian/Ubuntu with Docker's apt repo. Steps 1 to 3 are
once per server; step 4 is once per Vikunja project.

### 1. A rootless Docker daemon for jobs

```sh
sudo apt-get install -y uidmap docker-ce-rootless-extras systemd-container
sudo useradd --create-home --shell /usr/sbin/nologin claude-jobs
grep claude-jobs /etc/subuid /etc/subgid   # useradd assigns one range each; don't add another
sudo loginctl enable-linger claude-jobs
sudo machinectl shell claude-jobs@ /bin/bash -c 'dockerd-rootless-setuptool.sh install && systemctl --user enable --now docker'
ls -l /run/user/$(id -u claude-jobs)/docker.sock   # should exist
```

Optional gVisor: install `runsc` and register it with the rootless daemon,
then set `runtime: runsc` in `profiles.yaml`.

### 2. Build the job image on that daemon

```sh
JOBS_DOCKER="unix:///run/user/$(id -u claude-jobs)/docker.sock"
tar -C job -c . | sudo -u claude-jobs env DOCKER_HOST=$JOBS_DOCKER docker build -t claude-job:local -
```

Rebuild after changing `job/` (for example to bump `CLAUDE_CODE_VERSION`).

The egress proxy runs from this image too, and it's only recreated when its
allowlist or `OTLP_UPSTREAM` changes. After changing `egress-proxy.js` alone,
remove the `claude-egress-*` containers on the job daemon so the next job
starts a new one.

### 3. Configure and start the dispatcher

```sh
cp .env.example .env
sudo docker compose up -d --build
```

`.env` needs:
- `JOBS_UID` / `JOBS_GID` from `id claude-jobs`.
- `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, run anywhere you're
  signed in to Claude.
- `WEBHOOK_SECRET`: any random string, e.g. `openssl rand -hex 32`.
- `VIKUNJA_API_TOKEN`: in Vikunja, **Settings → API Tokens**. Vikunja
  doesn't let you change a token's permissions later, so tick all of these
  when you create it:
  - **Tasks**: Read One, Update
  - **Tasks Comments**: Read All, Create
  - **Projects Views**: Read All
  - **Projects**: Views Buckets, Views Buckets Tasks (to move cards between
    columns)
  - **Labels**: Read All, Create
  - **Tasks Labels**: Read All, Create, Delete

The `board` network comes from `../vikunja/compose.yaml`, so recreate Vikunja
first (`sudo docker compose up -d` in `../vikunja`).

### 4. Connect a Vikunja project

Each project that should run Claude jobs needs a profile and a webhook.

1. Note the project's id from its URL.
2. Map it to a profile under `projects:` in `profiles.yaml`
   (`<id>: <profile>`), commit, and restart the dispatcher.
3. In the project's settings, open **Webhooks** and add:
   - Target URL: `http://claude-dispatcher:8080/webhooks/vikunja`
   - Secret: the same `WEBHOOK_SECRET` as `.env`
   - Events: `task.created`, `task.comment.created`

A new card in the project starts a job, and a comment starting with
`@claude` starts a follow-up. Each job posts "Working on this", then its
answer.

## Troubleshooting

- **No "Working on this" comment:** `sudo docker compose logs dispatcher`.
  "bad or missing signature" means the webhook secret doesn't match, and
  "has no profile" means the project isn't mapped.
- **The job can't reach a site:** the egress proxy logs each allow and deny.
  `sudo -u claude-jobs env DOCKER_HOST=$JOBS_DOCKER docker logs claude-egress-<profile>`
  (`JOBS_DOCKER` as in step 2).

## Profiles

`profiles.yaml` holds the profiles and the project-to-profile map.

A profile can also move cards and label verdicts:
- `buckets: {working: Assessing, replied: Done}` moves the card to
  the Kanban column with that title when a job starts and when Claude
  replies. A column that doesn't exist is skipped with a warning.
- `verdicts: [Easy, Needs care, Hold]` looks for a reply that opens with
  "Verdict: <one of these>" and gives the card a matching `Verdict: …`
  label (green, amber, red in list order), replacing an older verdict
  label. The Discord digest reads these labels. Adding a
profile, widening `egress` or `allowed_tools`, or mapping a new project are
all changes to what Claude can do, so they go through review in git. See the
comments in that file for each setting.

## Telemetry

Jobs send Claude Code's metrics to SigNoz, where they land alongside the
personal machines' on the "Claude Code Metrics" dashboard. Each one carries
`job.profile=<profile>`, so `job.profile EXISTS` picks out dispatcher jobs.
A job killed at its time limit may not get to flush its last metrics.

## Not built yet

- Polling as a fallback for missed webhooks (Vikunja sends each one once).
- The rest of the upgrade flow: approving from Discord and an applier.
  Cards are filed and their verdicts shown by the dashboard (`alpenglow_dashboard/app/backend/alpenglow_dashboard/upgrades`).

## Development

```sh
cd app && uv run pytest
```
