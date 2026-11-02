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
| Who can start jobs | Webhooks must carry Vikunja's HMAC signature. A card's **project** picks its profile in `profiles.yaml`; a card can't choose. `allowed_users` limits who can start jobs per profile. |
| Runaway use | `JOBS_PER_DAY` and `MAX_CONCURRENT_JOBS` in `.env`; one job per card at a time. |
| Untrusted output | Claude's reply is posted as escaped text, never HTML, and a reply can't start with `@claude`, so it can't trigger another job. |

The job can read its own Claude token. The egress allowlist is what stops a
prompt-injected job from sending it anywhere else.

## Setup

These steps are for Debian/Ubuntu with Docker's apt repo. They haven't been
run on alpenglow yet.

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

### 3. Configure and start the dispatcher

```sh
cp .env.example .env    # JOBS_UID/GID from `id claude-jobs`, a `claude setup-token` token,
                        # a webhook secret, and a Vikunja API token
sudo docker compose up -d --build
```

The `board` network comes from `../vikunja/compose.yaml`, so recreate Vikunja
first (`sudo docker compose up -d` in `../vikunja`).

### 4. Point a Vikunja project at it

1. Create a project, for example **Claude test**, and note its id from the URL.
2. Map it in `profiles.yaml` (`projects: {<id>: test}`) and restart the
   dispatcher.
3. In the project's settings, open **Webhooks** and add:
   - Target URL: `http://claude-dispatcher:8080/webhooks/vikunja`
   - Secret: the same `WEBHOOK_SECRET` as `.env`
   - Events: `task.created`, `task.comment.created`

### 5. Try it

Create a card in that project, such as "What changed in the latest Immich
release?". Within a few seconds it should get a "Working on this" comment,
then Claude's answer. Reply with `@claude ...` to ask a follow-up.

If nothing happens: `sudo docker compose logs dispatcher`, and the proxy's
allow/deny log with
`sudo -u claude-jobs env DOCKER_HOST=$JOBS_DOCKER docker logs claude-egress-test`.

## Profiles

`profiles.yaml` holds the profiles and the project-to-profile map. Adding a
profile, widening `egress` or `allowed_tools`, or mapping a new project are
all changes to what Claude can do, so they go through review in git. See the
comments in that file for each setting.

## Not built yet

- Telemetry to SigNoz (needs a filtering relay in front of Alloy).
- Polling as a fallback for missed webhooks (Vikunja sends each one once).
- The upgrade flow: the update tracker filing cards, assessments, Discord
  approval and the applier.

## Development

```sh
cd app && uv run pytest
```
