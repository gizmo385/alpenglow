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
Inbox, Assessing, Done. The dispatcher will look buckets up by name.

The board tracks which versions have been assessed, not the upgrade itself: a
card lands in Done once Claude has replied, and the dashboard ticks it once the
server runs that version. So an unticked card in Done is assessed but not
applied yet.

Don't make Done the view's done bucket. Vikunja would mark every card done as it
arrives, and the dashboard only reads verdicts from open cards, so they'd vanish
from the Updates view and the Discord digest.

## Upgrading

Vikunja is part of the upgrade pipeline, so the pipeline never upgrades it
itself. Bump the tag and digest in `compose.yaml` by hand, read the release
notes, then `sudo docker compose up -d`.
