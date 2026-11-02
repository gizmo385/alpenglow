# Upgrades board

Keeps Vikunja's **Upgrades** project in step with the
[image updates tracker](https://github.com/gizmo385/image-updates-tracker),
and serves Claude's verdicts on those cards to anything that wants to show
them. The tracker itself knows nothing about Vikunja.

```
tracker /api/updates ──► upgrades-board ──► Upgrades cards ──► claude-dispatcher
                                │                                   │
   dashboard Updates view ◄─────┤◄──── "Verdict: …" labels ◄────────┘
   Discord digest        ◄──────┘  GET /annotations
```

## What it does

Every `SYNC_INTERVAL` seconds (30 minutes by default) it reads the tracker's
`/api/updates` and, for images listed in `versions.yaml` (never
`policy: float` ones):

- files a card for a new release, titled like `Dawarich 1.20.0 → 1.20.1`,
  with the policy and `compatibility` list from `versions.yaml`;
- retitles an open card when a newer release comes out, and comments
  `@claude …` so the dispatcher assesses the new version;
- marks an open card done once the server runs that version;
- doesn't refile a card you closed without upgrading, until a newer version
  comes out.

Cards are matched to repos by the `Tracked repo:` and `Version to assess:`
lines in their description, so leave those in place.

`GET /annotations` returns the open cards with the verdict the dispatcher
labelled them with and the one-sentence reason from Claude's reply:

```json
{"title": "Upgrades", "link_label": "Open card",
 "groups": [{"name": "Easy", "emoji": "🟢", "color": "#2e7d32"}, …],
 "items": [{"repo": "Freika/dawarich", "title": "Dawarich 1.20.0 → 1.20.1",
            "group": "Easy", "note": "1.20.1 is a bug-fix patch…",
            "url": "https://board.acbc.house/tasks/9", "card_id": 9}]}
```

The tracker's Discord bot reads it through `ANNOTATIONS_URL`, and the
dashboard's Updates view through `UPGRADES_BOARD_URL`. `POST /sync` runs a
sync straight away.

## Setup

1. Create a Vikunja API token (**Settings → API Tokens**) with the **Tasks**
   preset. That covers reading and updating tasks, comments and labels.
2. `cp .env.example .env` and set `VIKUNJA_API_TOKEN`.
3. `sudo docker compose up -d --build`

Nothing else holds a Vikunja token for this any more: the tracker's
`VIKUNJA_*` settings can go from `updates-tracker/.env`.

## Development

```sh
cd app && uv run pytest
```
