# pin

Keeps image versions in `versions.yaml` at the repo root and writes them into
compose files and Dockerfiles. YAML anchors can't reach across files, so
instead of referencing the lock file from compose, `pin sync` rewrites the
`image:` and `FROM` lines from it, and `pin check` fails when they disagree.
Compose files stay plain, so `docker compose up -d` needs no extra flags.

Run it from anywhere in the repo with the `./pin` wrapper (needs
[uv](https://docs.astral.sh/uv/)).

## Commands

| Command | What it does |
|---|---|
| `./pin check` | Compare the repo with `versions.yaml`. Exits 1 on any mismatch, unpinned image or unpinned Caddy plugin. CI runs this. |
| `./pin sync [--dry-run]` | Write `versions.yaml` into compose files and Dockerfiles. |
| `./pin set NAME TAG` | Pin an image (and every image in its `group`) to `TAG`. Looks up the digest, which also proves the tag exists. Then syncs. |
| `./pin add IMAGE[:TAG]` | Give a new service's image an entry: a release tag is pinned (with its digest), no tag or `latest` becomes a `float` entry. `create_service.sh --image` runs this. |
| `./pin plugin NAME MODULE VERSION` | Pin an `xcaddy build --with` plugin of a base image (e.g. `caddy`). |
| `./pin digests` | Look up digests for pinned images that don't have one yet. |
| `./pin adopt NAME [--tag T]` | On the server: pin exactly what's running now, so moving off `latest` changes nothing. Reads the image's version label unless `--tag` is given, and refuses if that tag now points at a different digest (`--force` pins the running digest anyway). |
| `./pin list` | Show every entry. |

## versions.yaml

```yaml
exclude:                 # paths never scanned (globs, relative to the repo root)
  - "**/tests/fixtures/**"
extra_files:             # other YAML files with image: lines
  - signoz/casting.yaml
images:
  vabene1111/recipes:    # image name exactly as compose writes it, without tag
    tag: 2.7.1
    digest: sha256:...   # filled by `pin set` / `pin digests`; never typed
    policy: approve      # auto-eligible | approve | manual | float
    group: tandoor       # images that always move together
    compatibility:       # apps and plugins an upgrade assessment must check
      - Kitshn
  caddy:                 # used only in caddy/Dockerfile
    tag: 2.11.7
    plugins:
      github.com/mholt/caddy-l4: v0.1.2
  nextcloud:
    policy: float        # not managed: sync leaves it alone, check accepts any tag
    reason: Not pinned yet; run `./pin adopt nextcloud` on the server
```

- A pinned entry pins **every** use of that image name. If two services need
  different versions of one image, it stays `float` until they agree.
- Compose files get `name:tag@digest`. Dockerfile `FROM` lines get the tag
  only and keep any variant suffix: with `tag: 2.11.7`, `caddy:2.11.7-builder`
  stays a builder image.
- `FROM` lines naming an earlier build stage, `scratch` or an `${ARG}` are
  skipped. Compose tags that read an env var (`${TAG:-latest}`) need a `float`
  entry until they are pinned, at which point `sync` replaces them with the
  literal version.
- `policy` doesn't change what `pin` does. It's for the upgrade pipeline:
  `manual` images are never upgraded automatically.

## Development

```sh
cd tools/pin
uv run pytest
```
