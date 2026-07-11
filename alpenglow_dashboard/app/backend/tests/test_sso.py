"""B5 SSO auto-detection tests.

Signals are exercised against on-disk fixture service dirs with *synthetic* .env
files (synthetic key NAMES only — no real credentials, no values) under
``tests/fixtures/sso``, and against fake Keycloak realm payloads driven through
``httpx.MockTransport``. No live Keycloak is contacted.

Security invariant under test: only env-key *names* ever surface in a source
string — never a value.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from alpenglow_dashboard import sso

FIXTURES = Path(__file__).parent / "fixtures" / "sso"


@pytest.fixture(autouse=True)
def _sso_env(monkeypatch):
    """Point the scanner at the SSO fixtures, disable the real Keycloak by
    default (no scan creds), and clear caches around every test."""
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURES))
    monkeypatch.setenv("METADATA_PATH", str(FIXTURES / "metadata.yaml"))
    monkeypatch.setenv("KEYCLOAK_REALM", "acbc.house")
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("SSO_SCAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("SSO_SCAN_CLIENT_SECRET", raising=False)
    sso.clear_cache()
    yield
    sso.clear_cache()


def _fake_clients(monkeypatch, client_ids: set[str] | None):
    """Force the realm-clients signal to a fixed set (or None = Keycloak down)."""
    async def _fake():
        return None if client_ids is None else frozenset(client_ids)

    monkeypatch.setattr(sso, "_fetch_realm_clients", _fake)


# ── env / compose scan (no Keycloak) ──────────────────────────────────────────


async def test_env_keycloak_keys(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("env_keycloak")
    assert info.state == "keycloak"
    assert info.source == "KEYCLOAK_FIXTURE_CLIENT_ID in .env"


async def test_env_oidc_keys(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("env_oidc")
    assert info.state == "oidc"
    assert info.source == "OIDC_FIXTURE_CLIENT_ID in .env"


async def test_env_oauth_keys(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("env_oauth")
    assert info.state == "oidc"
    assert info.source.endswith("in .env")
    assert info.source.startswith("OAUTH")


async def test_env_none(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("env_none")
    assert info.state == "none"
    assert info.source == "no OIDC keys found in .env"


async def test_env_example_is_ignored(monkeypatch):
    """A committed .env.example with OIDC keys must NOT mask a real gap: the
    env_none fixture has SSO keys only in .env.example → still 'none'."""
    _fake_clients(monkeypatch, None)
    info = await sso.detect("env_none")
    assert info.state == "none"


# ── oauth2-proxy sidecar ──────────────────────────────────────────────────────


async def test_oauth2_proxy_sidecar(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("sidecar")
    assert info.state == "keycloak"
    assert info.source == "oauth2-proxy sidecar in compose.yaml"


async def test_sidecar_beats_realm_client(monkeypatch):
    """copyparty case: a service with BOTH a sidecar and a realm client reads as
    the sidecar (the truthful mechanism)."""
    _fake_clients(monkeypatch, {"sidecar"})
    info = await sso.detect("sidecar")
    assert info.state == "keycloak"
    assert info.source == "oauth2-proxy sidecar in compose.yaml"


# ── caddy forward_auth ─────────────────────────────────────────────────────────


async def test_forward_auth_label(monkeypatch):
    _fake_clients(monkeypatch, None)
    info = await sso.detect("forwardauth")
    assert info.state == "keycloak"
    assert info.source == "forward_auth in compose labels"


# ── keycloak realm-client matching ────────────────────────────────────────────


async def test_realm_client_dir_name_match(monkeypatch):
    _fake_clients(monkeypatch, {"dirmatch", "other"})
    info = await sso.detect("dirmatch")
    assert info.state == "keycloak"
    assert info.source == "client 'dirmatch' in realm acbc.house"


async def test_realm_client_override_match(monkeypatch):
    """dir 'override_mismatch' matches realm clientId via keycloak_client."""
    _fake_clients(monkeypatch, {"renamed-client-in-realm"})
    info = await sso.detect("override_mismatch")
    assert info.state == "keycloak"
    assert info.source == "client 'renamed-client-in-realm' in realm acbc.house"


async def test_realm_client_no_match_falls_through(monkeypatch):
    """No matching client + no local keys → none."""
    _fake_clients(monkeypatch, {"someone-else"})
    info = await sso.detect("dirmatch")
    assert info.state == "none"


async def test_realm_client_beats_env_oidc(monkeypatch):
    """Authoritative realm match outranks a generic OIDC env key."""
    _fake_clients(monkeypatch, {"env_oidc"})
    info = await sso.detect("env_oidc")
    assert info.state == "keycloak"
    assert info.source == "client 'env_oidc' in realm acbc.house"


# ── keycloak service itself ───────────────────────────────────────────────────


async def test_keycloak_service_is_self(monkeypatch):
    _fake_clients(monkeypatch, {"anything"})
    info = await sso.detect("keycloak")
    assert info.state == "self"
    assert info.source == "realm: acbc.house"


# ── builtin clients are never matched ─────────────────────────────────────────


def test_builtin_clients_stripped_from_fetch():
    assert "realm-management" in sso._KEYCLOAK_BUILTIN_CLIENTS
    assert "account" in sso._KEYCLOAK_BUILTIN_CLIENTS


# ── security: no secret VALUES in the source string ───────────────────────────


async def test_source_never_contains_values(monkeypatch):
    """Source strings name env KEYS only; a value present in the .env must never
    appear in the returned source."""
    _fake_clients(monkeypatch, None)
    for sid in ("env_keycloak", "env_oidc", "env_oauth", "env_none"):
        info = await sso.detect(sid)
        assert "xxx" not in info.source  # the synthetic value placeholder
        assert "=" not in info.source


# ── caching ───────────────────────────────────────────────────────────────────


async def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    async def _counting():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sso, "_fetch_realm_clients", _counting)
    await sso.detect("env_oidc")
    await sso.detect("env_oidc")
    assert calls["n"] == 1  # second call served from the result cache


async def test_clear_cache_forces_recompute(monkeypatch):
    calls = {"n": 0}

    async def _counting():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sso, "_fetch_realm_clients", _counting)
    await sso.detect("env_oidc")
    sso.clear_cache()
    await sso.detect("env_oidc")
    assert calls["n"] == 2


async def test_realm_clients_fetched_once_per_ttl(monkeypatch):
    """The realm-clients HTTP fetch is cached once across services, not per
    service."""
    monkeypatch.setenv("KEYCLOAK_URL", "https://sso.example")
    monkeypatch.setenv("SSO_SCAN_CLIENT_ID", "scanner")
    monkeypatch.setenv("SSO_SCAN_CLIENT_SECRET", "synthetic-secret")

    fetches = {"n": 0}

    async def _fetch_uncached():
        fetches["n"] += 1
        return frozenset({"dirmatch"})

    monkeypatch.setattr(sso, "_fetch_realm_clients_uncached", _fetch_uncached)
    sso.clear_cache()

    await sso.detect("dirmatch")
    await sso.detect("env_none")
    await sso.detect("sidecar")
    assert fetches["n"] == 1


# ── httpx.MockTransport: exercise the real fetch/parse path ────────────────────


async def test_fetch_realm_clients_via_mock_transport(monkeypatch):
    """Drive the real token-grant + clients fetch through httpx.MockTransport,
    verifying builtins are stripped and clientIds parsed."""
    monkeypatch.setenv("KEYCLOAK_URL", "https://sso.example")
    monkeypatch.setenv("SSO_SCAN_CLIENT_ID", "scanner")
    monkeypatch.setenv("SSO_SCAN_CLIENT_SECRET", "synthetic-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fake-token"})
        if request.url.path.endswith("/clients"):
            assert request.headers["Authorization"] == "Bearer fake-token"
            return httpx.Response(
                200,
                json=[
                    {"clientId": "immich"},
                    {"clientId": "jellyfin"},
                    {"clientId": "account"},  # builtin → stripped
                    {"clientId": "realm-management"},  # builtin → stripped
                ],
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)
    sso.clear_cache()

    clients = await sso._fetch_realm_clients_uncached()
    assert clients == frozenset({"immich", "jellyfin"})


async def test_fetch_realm_clients_unreachable_returns_none(monkeypatch):
    """A transport error degrades to None (fall back to local signals)."""
    monkeypatch.setenv("KEYCLOAK_URL", "https://sso.example")
    monkeypatch.setenv("SSO_SCAN_CLIENT_ID", "scanner")
    monkeypatch.setenv("SSO_SCAN_CLIENT_SECRET", "synthetic-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)
    sso.clear_cache()

    assert await sso._fetch_realm_clients_uncached() is None


async def test_no_scan_creds_returns_none():
    """Without scan credentials configured, the realm fetch is a no-op None."""
    assert await sso._fetch_realm_clients_uncached() is None


# ── detect() integrates via inventory's RepoService seam ──────────────────────


async def test_detect_with_repo_object(monkeypatch):
    """When called with inventory's RepoService, compose facts come from it."""
    from alpenglow_dashboard import inventory

    _fake_clients(monkeypatch, None)
    repos = inventory.scan_repo(FIXTURES)
    repo = repos["sidecar"]
    info = await sso.detect("sidecar", repo)
    assert info.state == "keycloak"
    assert info.source == "oauth2-proxy sidecar in compose.yaml"
