from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pin.check import check
from pin.cli import main
from pin.lock import Lock, LockError
from pin.ops import adopt, fill_digests, set_plugin, set_tag
from pin.refs import parse_ref, split_variant
from pin.registry import split_name
from pin.sync import apply, plan

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "versions.yaml", f"""
        # top comment survives
        exclude:
          - "**/tests/fixtures/**"
        images:
          vabene1111/recipes:
            tag: 2.7.1
            digest: {D1}
            compatibility: [Kitshn]   # inline comment survives
          ghcr.io/immich-app/immich-server:
            tag: v2.2.0
            group: immich
          ghcr.io/immich-app/immich-machine-learning:
            tag: v2.2.0
            group: immich
          caddy:
            tag: 2.11.7
            policy: manual
            plugins:
              github.com/lucaslorentz/caddy-docker-proxy/v2: v2.13.1
              github.com/caddy-dns/cloudflare: v0.2.4
          nextcloud:
            policy: float
            reason: not pinned yet
    """)
    write(tmp_path, "tandoor/compose.yaml", f"""
        services:
          web_recipes:
            image: vabene1111/recipes:2.7.1@{D1}  # keep me
    """)
    write(tmp_path, "immich/compose.yaml", """
        services:
          server:
            image: "ghcr.io/immich-app/immich-server:v2.2.0"
          ml:
            image: ghcr.io/immich-app/immich-machine-learning:v2.2.0
    """)
    write(tmp_path, "nextcloud/compose.yaml", """
        services:
          app:
            image: nextcloud
    """)
    write(tmp_path, "caddy/Dockerfile", """
        FROM caddy:2.11.7-builder AS builder

        RUN xcaddy build \\
            --with github.com/caddy-dns/cloudflare \\
            --with github.com/lucaslorentz/caddy-docker-proxy/v2@v2.13.1

        FROM caddy:2.11.7

        COPY --from=builder /usr/bin/caddy /usr/bin/caddy
    """)
    write(tmp_path, "x/tests/fixtures/compose.yaml", """
        services:
          a:
            image: ignored:latest
    """)
    return tmp_path


def lock(repo: Path) -> Lock:
    return Lock.load(repo / "versions.yaml")


# ── refs ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref,name,tag,digest",
    [
        ("redis:alpine", "redis", "alpine", None),
        ("nextcloud", "nextcloud", None, None),
        ("host:5000/team/app:1.2", "host:5000/team/app", "1.2", None),
        ("ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}",
         "ghcr.io/immich-app/immich-server", "${IMMICH_VERSION:-release}", None),
        (f"ekofr/pihole-exporter:v1.2.0@{D1}", "ekofr/pihole-exporter", "v1.2.0", D1),
    ],
)
def test_parse_ref(ref, name, tag, digest):
    r = parse_ref(ref)
    assert (r.name, r.tag, r.digest) == (name, tag, digest)
    assert str(r) == ref


def test_interpolated():
    assert parse_ref("twentycrm/twenty:${TAG:-latest}").interpolated
    assert not parse_ref("twentycrm/twenty:1.0").interpolated


@pytest.mark.parametrize(
    "tag,expected",
    [("2.11.7-builder", ("2.11.7", "-builder")), ("3.13-slim", ("3.13", "-slim")),
     ("17.10", ("17.10", "")), ("v1.2", ("v1.2", "")), ("alpine", ("alpine", ""))],
)
def test_split_variant(tag, expected):
    assert split_variant(tag) == expected


@pytest.mark.parametrize(
    "name,expected",
    [("redis", ("registry-1.docker.io", "library/redis")),
     ("ollama/ollama", ("registry-1.docker.io", "ollama/ollama")),
     ("ghcr.io/org/app", ("ghcr.io", "org/app")),
     ("localhost:5000/app", ("localhost:5000", "app"))],
)
def test_split_name(name, expected):
    assert split_name(name) == expected


# ── check ───────────────────────────────────────────────────────────────────


def test_check_flags_unpinned_plugin_and_missing_digest(repo):
    report = check(lock(repo))
    assert report.errors == [
        "caddy/Dockerfile:4: says github.com/caddy-dns/cloudflare, versions.yaml says "
        "github.com/caddy-dns/cloudflare@v0.2.4 (run `pin sync`)",
    ]
    assert "versions.yaml: ghcr.io/immich-app/immich-server has no digest " \
        "(`pin set ghcr.io/immich-app/immich-server v2.2.0` resolves one)" in report.warnings
    # Dockerfile-only images don't need a digest, and fixtures are excluded.
    assert not any("caddy has no digest" in w for w in report.warnings)
    assert not any("ignored" in e for e in report.errors)


def test_check_errors(repo):
    write(repo, "redis/compose.yaml", "services:\n  r:\n    image: redis:alpine\n")
    write(repo, "tandoor/compose.yaml", "services:\n  w:\n    image: vabene1111/recipes:2.6.0\n")
    write(repo, "caddy/Dockerfile", "FROM caddy:2.11.7\nRUN xcaddy build --with example.com/new\n")
    report = check(lock(repo))
    assert "redis/compose.yaml:3: redis:alpine has no entry in versions.yaml " \
        "(pin it with `pin set`, or add a float entry with a reason)" in report.errors
    assert f"tandoor/compose.yaml:3: says vabene1111/recipes:2.6.0, versions.yaml says " \
        f"vabene1111/recipes:2.7.1@{D1} (run `pin sync`)" in report.errors
    assert "caddy/Dockerfile:2: plugin example.com/new isn't pinned; add it under plugins: of caddy in versions.yaml" \
        in report.errors


def test_validate(repo):
    write(repo, "versions.yaml", """
        images:
          a: {tag: latest}
          b: {policy: float}
          c: {tag: "1", policy: sometimes}
          d: {tag: "1", digest: sha256:abc}
          e: {}
    """)
    errors = lock(repo).validate()
    assert errors == [
        "versions.yaml: a: tag 'latest' is a moving channel, not a version",
        "versions.yaml: b: float entries need a reason",
        "versions.yaml: c: policy 'sometimes' is not one of auto-eligible, approve, manual, float",
        "versions.yaml: d: digest 'sha256:abc' is not sha256:<64 hex>",
        "versions.yaml: e: pinned entries need a tag",
    ]


# ── sync ────────────────────────────────────────────────────────────────────


def test_sync_rewrites_only_references(repo):
    lk = lock(repo)
    lk.set_field("vabene1111/recipes", "tag", "2.8.0")
    lk.set_field("caddy", "tag", "2.11.8")
    lk.save()
    lk = lock(repo)
    apply(repo, plan(lk))
    assert (repo / "tandoor/compose.yaml").read_text() == (
        f"services:\n  web_recipes:\n    image: vabene1111/recipes:2.8.0@{D1}  # keep me\n"
    )
    dockerfile = (repo / "caddy/Dockerfile").read_text()
    assert "FROM caddy:2.11.8-builder AS builder" in dockerfile
    assert "FROM caddy:2.11.8\n" in dockerfile
    assert "--with github.com/caddy-dns/cloudflare@v0.2.4 \\\n" in dockerfile
    assert "caddy-docker-proxy/v2@v2.13.1\n" in dockerfile
    # Quotes kept; floating images untouched.
    assert 'image: "ghcr.io/immich-app/immich-server:v2.2.0"' in (repo / "immich/compose.yaml").read_text()
    assert "image: nextcloud\n" in (repo / "nextcloud/compose.yaml").read_text()
    assert check(lock(repo)).ok
    assert plan(lock(repo)) == []


# ── set / plugin / adopt ───────────────────────────────────────────────────


def test_set_moves_group_and_resolves_digests(repo):
    calls = []

    def resolver(name, tag):
        calls.append((name, tag))
        return D2 if "server" in name else D3

    lk = lock(repo)
    names = set_tag(lk, "ghcr.io/immich-app/immich-server", "v2.3.0", resolver=resolver)
    lk.save()
    assert sorted(names) == ["ghcr.io/immich-app/immich-machine-learning", "ghcr.io/immich-app/immich-server"]
    assert sorted(calls) == [("ghcr.io/immich-app/immich-machine-learning", "v2.3.0"),
                             ("ghcr.io/immich-app/immich-server", "v2.3.0")]
    lk = lock(repo)
    apply(repo, plan(lk))
    text = (repo / "immich/compose.yaml").read_text()
    assert f'"ghcr.io/immich-app/immich-server:v2.3.0@{D2}"' in text
    assert f"ghcr.io/immich-app/immich-machine-learning:v2.3.0@{D3}" in text
    saved = (repo / "versions.yaml").read_text()
    assert "# top comment survives" in saved and "# inline comment survives" in saved


def test_set_dockerfile_only_image_skips_digest(repo):
    lk = lock(repo)
    set_tag(lk, "caddy", "2.11.8", resolver=lambda n, t: pytest.fail("no lookup expected"))
    assert lk.entries()["caddy"].digest is None


def test_set_unpins_float_and_rejects_channels(repo):
    lk = lock(repo)
    with pytest.raises(LockError):
        set_tag(lk, "nextcloud", "latest", resolver=lambda n, t: D1)
    set_tag(lk, "nextcloud", "32.0.1", resolver=lambda n, t: D1)
    e = lk.entries()["nextcloud"]
    assert (e.tag, e.digest, e.policy, e.reason) == ("32.0.1", D1, "approve", None)


def test_set_plugin(repo):
    lk = lock(repo)
    set_plugin(lk, "caddy", "github.com/mholt/caddy-l4", "v0.1.2")
    assert lk.entries()["caddy"].plugins["github.com/mholt/caddy-l4"] == "v0.1.2"


def test_adopt_uses_label_and_running_digest(repo):
    info = {"RepoDigests": [f"nextcloud@{D2}"], "Config": {"Labels": {"org.opencontainers.image.version": "32.0.1"}}}
    lk = lock(repo)
    tag, digest, warnings = adopt(lk, "nextcloud", inspect=lambda ref: info, resolver=lambda n, t: D2)
    assert (tag, digest, warnings) == ("32.0.1", D2, [])
    lk.save()
    apply(repo, plan(lock(repo)))
    assert f"image: nextcloud:32.0.1@{D2}" in (repo / "nextcloud/compose.yaml").read_text()


def test_adopt_refuses_when_tag_moved(repo):
    info = {"RepoDigests": [f"nextcloud@{D2}"], "Config": {"Labels": {}}}
    lk = lock(repo)
    with pytest.raises(LockError, match="no version label"):
        adopt(lk, "nextcloud", inspect=lambda ref: info, resolver=lambda n, t: D2)
    with pytest.raises(LockError, match="now points at"):
        adopt(lk, "nextcloud", tag="32.0.1", inspect=lambda ref: info, resolver=lambda n, t: D3)
    tag, digest, warnings = adopt(
        lk, "nextcloud", tag="32.0.1", force=True, inspect=lambda ref: info, resolver=lambda n, t: D3
    )
    assert digest == D2 and warnings


# ── cli ─────────────────────────────────────────────────────────────────────


def test_cli_check_and_sync(repo, capsys):
    assert main(["--root", str(repo), "check"]) == 1
    assert main(["--root", str(repo), "sync", "--dry-run"]) == 0
    assert "cloudflare -> github.com/caddy-dns/cloudflare@v0.2.4" in capsys.readouterr().out
    assert main(["--root", str(repo), "sync"]) == 0
    assert main(["--root", str(repo), "check", "-q"]) == 0


def test_fill_digests_keeps_going_after_a_failure(repo):
    def resolver(name, tag):
        if "server" in name:
            raise RuntimeError("429")
        return D3

    lk = lock(repo)
    filled, failures = fill_digests(lk, resolver=resolver)
    assert filled == ["ghcr.io/immich-app/immich-machine-learning"]
    assert failures == ["ghcr.io/immich-app/immich-server:v2.2.0: 429"]
    assert lk.entries()["ghcr.io/immich-app/immich-machine-learning"].digest == D3
