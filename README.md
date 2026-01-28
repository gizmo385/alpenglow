# Alpenglow

A self-hosted home services infrastructure managed as a monorepo. Each service
lives in its own directory and is orchestrated with Docker Compose.

## Repository Structure

Each service is a self-contained directory containing:

- `compose.yaml` -- Docker Compose configuration (required)
- `.gitignore` -- ignores `.env` and secret files
- Service-specific config files and optional maintenance scripts

Services are independently deployable (`docker compose up -d` from any service
directory).

## Networking

### Reverse Proxy (Caddy)

All user-facing services connect to an external Docker network called `caddy`.
A [caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy)
instance reads labels from containers and automatically configures routing and
TLS termination. No ports are published on the host for most services -- Caddy
handles all ingress.

Services declare their routing with labels in `compose.yaml`:

```yaml
labels:
  caddy: "*.acbc.house"
  caddy.@myservice.host: "myservice.acbc.house"
  caddy.reverse_proxy: "@myservice {{upstreams http 8080}}"
```

### Public vs. Tailnet Access

By default, all services are only reachable within the Tailnet. Services that
should also be publicly accessible add a second set of Caddy labels bound to
port `10443`:

```yaml
labels:
  # Tailnet-only (default Caddy listener)
  caddy_0: "*.acbc.house"
  caddy_0.@myservice.host: "myservice.acbc.house"
  caddy_0.reverse_proxy: "@myservice {{upstreams http 8080}}"
  # Publicly exposed via port 10443
  caddy_1: "*.acbc.house:10443"
  caddy_1.@myservice.host: "myservice.acbc.house"
  caddy_1.reverse_proxy: "@myservice {{upstreams http 8080}}"
```

Three domain patterns are used to further organize services:

| Domain pattern | Purpose |
|---------------|---------|
| `*.acbc.house` | Primary services |
| `*.internal.acbc.house` | Local network only (not accessible from Tailnet) |
| `*.alpenglow.acbc.house` | Management dashboards and tools |

### Shared Networks

Two external Docker networks are used across services:

- **`caddy`** -- connects services to the reverse proxy.
- **`postgres`** -- connects services to the shared PostgreSQL instance.

Some services also define internal bridge networks for private communication
between their own containers (e.g. an OAuth2 proxy talking to a backend).

## Database

A centralized PostgreSQL instance (built from a custom Dockerfile) is shared by
services that need a relational database. The custom image adds extensions like
`pgvector` for ML/vector-search workloads. Services connect to it over the
external `postgres` network and resolve it via Docker DNS.

## Secrets Management

Secrets are stored in `.env` files (or named variants like `.hub_env`,
`.proxy_env`, `.backup_env`) that are gitignored across every service directory.
Compose files reference them with `env_file:` directives:

```yaml
env_file:
  - path: .env
    required: true
```

Credentials are never committed to the repository.

## Volumes and Data Persistence

Bind mounts are the preferred approach for persisting data, both for
configuration and bulk storage. This keeps data directly accessible on the host
filesystem and makes backups straightforward. Named volumes are used sparingly,
only for transient data like ML model caches where host-level access is not
needed. Read-only mounts (`:ro`) are used for immutable config files.

Host paths under `/data/` reside on the larger hard disk RAID arrays and are
generally reserved for large files or shared media storage.

## Hardware Access

Some services require passthrough access to host hardware:

- **GPU devices** (`/dev/dri/`) for hardware-accelerated transcoding.
- **Block devices** (`/dev/sda`, `/dev/nvme0`) for S.M.A.R.T. monitoring.
- **Docker socket** (`/var/run/docker.sock`) for container management UIs and
  monitoring agents.

Services that need these declare them under `devices:` or `volumes:` in their
compose files and typically run with explicit `user:` / `group_add:` settings.

## Backup and Monitoring

### Backups

- **PostgreSQL dumps** are performed by a shell script
  (`postgres/run_backups.sh`) that runs `pg_dumpall` via `docker exec`, writes
  the dump to the host filesystem, and cleans up old dumps based on a retention
  policy.
- **Kopia** handles broader file-level backups with a separate verification
  script (`kopia/verify.sh`).

### Health Reporting

Maintenance scripts report success or failure to an Uptime Kuma instance via
curl webhook calls. This provides a unified view of backup health and cron job
execution without requiring a separate push-based monitoring agent.

```bash
curl "$UPTIME_KUMA_WEBHOOK?status=up&msg=Success"
```

## Common Compose Conventions

- **Restart policy**: `restart: unless-stopped` is the default for most
  services; `restart: always` is used for critical infrastructure like
  PostgreSQL.
- **Port exposure**: Services use `expose:` (container-only) rather than
  `ports:` (host-bound) since Caddy handles external access.
- **Image versioning**: A mix of `:latest`, pinned tags, and environment
  variable interpolation (`${IMMICH_VERSION:-release}`).
- **Resource limits**: Applied selectively via `deploy.resources.limits` for
  memory-intensive services.
- **Timezone**: Set explicitly (`TZ: America/Denver`) on services that need
  correct local time.
