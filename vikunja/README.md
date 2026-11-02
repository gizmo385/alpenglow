# Vikunja

Kanban board at https://board.acbc.house (Tailnet only), login
through Keycloak. It's the board for the upgrade pipeline: the dispatcher (not
built yet) files a card per pending image upgrade and runs Claude jobs from
it. Each Vikunja project maps to one job profile in the dispatcher's config,
so a project for one-off jobs can get broader permissions than Upgrades.

## Setup

```sh
../postgres/create_service_db.sh vikunja   # role + database in the shared Postgres
cp .env.example .env                        # fill in the password, secret and Keycloak client
mkdir files && sudo chown 1000 files
sudo docker compose up -d
```

In Keycloak, create a confidential client `vikunja` with the redirect URI
`https://board.acbc.house/auth/openid/keycloak`. Log in once with
Keycloak, then set `VIKUNJA_AUTH_LOCAL_ENABLED=false` in `.env` and recreate.

Then create a project named **Upgrades** with these buckets, left to right:
Inbox, Assessing, Needs decision, Approved, Applying, Done, Rolled back,
Skipped. The dispatcher will look buckets up by name.

## Upgrading

Vikunja is part of the upgrade pipeline, so the pipeline never upgrades it
itself. Bump the tag and digest in `compose.yaml` by hand, read the release
notes, then `sudo docker compose up -d`.
