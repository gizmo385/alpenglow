"""SSO auto-detection (work package B5).

Three detection signals, cross-referenced in authority order (per the design
handoff §SSO model), folded into one :class:`models.SSOInfo` ``{state, source}``
per service:

1. **Keycloak admin API** — the authoritative "what is actually wired up".
   A client-credentials service account (``SSO_SCAN_CLIENT_*`` in the env, with
   ``view-clients``) fetches ``GET {KEYCLOAK_URL}/admin/realms/{realm}/clients``
   once per TTL; client IDs are matched to service directories by exact dir-name
   match, then by a ``keycloak_client`` override in ``metadata.yaml``.
2. **Env / compose scan** — key *NAMES only* from ``/services/{id}/.env*``
   (``OIDC_*`` / ``OAUTH*`` / ``KEYCLOAK_*``) and an ``oauth2-proxy`` sidecar in
   the service's ``compose.yaml``. Values are never read, logged, or exposed.
3. **Caddy ``forward_auth`` labels** — proxy-level SSO.

Special cases: the ``keycloak`` service itself → ``self``; a service fronted by
an oauth2-proxy sidecar reads as Keycloak-SSO'd (that is the truthful source
even when a realm client also matches); no signal at all → ``none``.

Results are cached per service for ~5 minutes; the realm-clients fetch is cached
once per TTL (not per service). :func:`clear_cache` resets both (used by tests).

**Security:** only env-key *names* ever leave this module. No secret values are
read into detection state, returned in payloads, or written to logs.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from . import models

# ── configuration ─────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = 300  # ~5 minutes

# env-key name prefixes we treat as SSO signals (matched case-insensitively
# against the key NAME only — values are never read).
_KEYCLOAK_PREFIXES = ("KEYCLOAK_", "OIDC_KEYCLOAK")
_OIDC_PREFIXES = ("OIDC_", "OIDC")
_OAUTH_PREFIXES = ("OAUTH2_", "OAUTH_", "OAUTH")

# Keycloak realm clients that are built-in / infrastructure, never a service
# integration — excluded from dir-name matching.
_KEYCLOAK_BUILTIN_CLIENTS = frozenset(
    {
        "account",
        "account-console",
        "admin-cli",
        "broker",
        "realm-management",
        "security-admin-console",
    }
)


def _services_root() -> Path:
    return Path(os.environ.get("SERVICES_ROOT", "/services"))


def _keycloak_url() -> Optional[str]:
    url = os.environ.get("KEYCLOAK_URL", "").strip().rstrip("/")
    return url or None


def _realm() -> str:
    return os.environ.get("KEYCLOAK_REALM", "acbc.house").strip() or "acbc.house"


def _scan_client_id() -> Optional[str]:
    return os.environ.get("SSO_SCAN_CLIENT_ID", "").strip() or None


def _scan_client_secret() -> Optional[str]:
    return os.environ.get("SSO_SCAN_CLIENT_SECRET", "").strip() or None


# ── caches ────────────────────────────────────────────────────────────────────

# folded {state, source} per service id
_result_cache: dict[str, tuple[float, models.SSOInfo]] = {}

# realm client-id set, fetched once per TTL. The sentinel distinguishes
# "not fetched yet" (None) from "fetched, no clients / unreachable" (frozenset()).
_clients_cache: Optional[tuple[float, Optional[frozenset[str]]]] = None


def clear_cache() -> None:
    """Reset both caches (folded results and the realm-clients fetch)."""
    global _clients_cache
    _result_cache.clear()
    _clients_cache = None


# ── signal 1: Keycloak admin API ──────────────────────────────────────────────


async def _fetch_realm_clients() -> Optional[frozenset[str]]:
    """Return the set of clientIds in the realm, or ``None`` if Keycloak is not
    configured / unreachable. Cached once per TTL across all services.

    Never raises: any failure degrades to ``None`` so detection falls back to
    the env/compose/label signals rather than 500ing the inventory.
    """
    global _clients_cache
    now = time.monotonic()
    if _clients_cache is not None and now - _clients_cache[0] < CACHE_TTL_SECONDS:
        return _clients_cache[1]

    clients = await _fetch_realm_clients_uncached()
    _clients_cache = (now, clients)
    return clients


async def _fetch_realm_clients_uncached() -> Optional[frozenset[str]]:
    url = _keycloak_url()
    client_id = _scan_client_id()
    client_secret = _scan_client_secret()
    if not (url and client_id and client_secret):
        return None

    import httpx

    realm = _realm()
    token_url = f"{url}/realms/{realm}/protocol/openid-connect/token"
    clients_url = f"{url}/admin/realms/{realm}/clients"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            token_resp = await http.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json().get("access_token")
            if not token:
                return None
            clients_resp = await http.get(
                clients_url, headers={"Authorization": f"Bearer {token}"}
            )
            clients_resp.raise_for_status()
            payload = clients_resp.json()
    except Exception:
        # Network / auth / parse failure → treat as "no data", never leak secrets.
        return None

    if not isinstance(payload, list):
        return None
    ids = {
        str(c["clientId"])
        for c in payload
        if isinstance(c, dict) and c.get("clientId")
    }
    return frozenset(ids - _KEYCLOAK_BUILTIN_CLIENTS)


def _keycloak_client_for(service_id: str, meta: dict) -> str:
    """The clientId to match for this service: the ``keycloak_client`` metadata
    override, else the directory name."""
    override = meta.get("keycloak_client")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return service_id


# ── signal 2: env-file key-name scan + oauth2-proxy sidecar ───────────────────


def _env_files(service_dir: Path) -> list[Path]:
    """All ``.env`` / ``.env.*`` files in the service dir (real ``.env`` only —
    ``.env.example`` is a committed stub and is skipped so it does not mask a
    missing live config)."""
    if not service_dir.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(service_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name == ".env.example" or name.endswith(".example"):
            continue
        if name == ".env" or name.startswith(".env."):
            out.append(entry)
    return out


def _scan_env_key_names(service_dir: Path) -> set[str]:
    """Collect the *NAMES* of env keys (uppercased) from the service's env
    files. Only the text left of the first ``=`` on each assignment line is
    read; values are never parsed, stored, or returned."""
    names: set[str] = set()
    for path in _env_files(service_dir):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            # strip a leading "export "
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            if key.isidentifier() or all(
                ch.isalnum() or ch == "_" for ch in key
            ):
                names.add(key.upper())
    return names


def _classify_env_keys(names: set[str]) -> Optional[tuple[str, str]]:
    """Map a set of env-key NAMES to an (state, source) SSO signal, or ``None``.

    Keycloak-specific keys outrank generic OIDC/OAUTH keys. The source string
    names the *key* that triggered detection — never a value.
    """
    def first_with_prefix(prefixes: tuple[str, ...]) -> Optional[str]:
        matches = sorted(
            n for n in names if any(n.startswith(p) for p in prefixes)
        )
        return matches[0] if matches else None

    kc = first_with_prefix(_KEYCLOAK_PREFIXES)
    if kc:
        return "keycloak", f"{kc} in .env"
    oidc = first_with_prefix(_OIDC_PREFIXES)
    if oidc:
        return "oidc", f"{oidc} in .env"
    oauth = first_with_prefix(_OAUTH_PREFIXES)
    if oauth:
        return "oidc", f"{oauth} in .env"
    return None


def _has_oauth2_proxy_sidecar(services_block: dict) -> bool:
    """True if any container in the compose ``services:`` block is an
    oauth2-proxy image / named service."""
    if not isinstance(services_block, dict):
        return False
    for name, svc in services_block.items():
        if isinstance(name, str) and "oauth2-proxy" in name.lower():
            return True
        if isinstance(svc, dict):
            image = svc.get("image")
            if isinstance(image, str) and "oauth2-proxy" in image.lower():
                return True
            container = svc.get("container_name")
            if isinstance(container, str) and "oauth2-proxy" in container.lower():
                return True
    return False


# ── signal 3: Caddy forward_auth labels ───────────────────────────────────────


def _has_forward_auth(labels: dict) -> bool:
    if not isinstance(labels, dict):
        return False
    for key, value in labels.items():
        if "forward_auth" in str(key).lower():
            return True
        if isinstance(value, str) and "forward_auth" in value.lower():
            return True
    return False


# ── folding ───────────────────────────────────────────────────────────────────


def _fold(
    *,
    service_id: str,
    is_keycloak_service: bool,
    realm_client: Optional[str],
    has_sidecar: bool,
    has_forward_auth: bool,
    env_signal: Optional[tuple[str, str]],
) -> models.SSOInfo:
    """Combine the signals into one ``{state, source}``.

    Authority / truthfulness order (highest wins):

    1. The service *is* Keycloak            → ``self`` (``realm: acbc.house``).
    2. An ``oauth2-proxy`` sidecar fronts it → ``keycloak`` with the sidecar as
       the source, even if a realm client also matches — the sidecar is the
       actual, most truthful mechanism (the copyparty case).
    3. A realm client matches                → ``keycloak`` (authoritative wiring).
    4. A Caddy ``forward_auth`` label        → ``keycloak`` (proxy-level).
    5. Env-key names                         → ``keycloak`` / ``oidc`` per key.
    6. Nothing                               → ``none``.
    """
    if is_keycloak_service:
        return models.SSOInfo(state="self", source=f"realm: {_realm()}")

    if has_sidecar:
        return models.SSOInfo(
            state="keycloak", source="oauth2-proxy sidecar in compose.yaml"
        )

    if realm_client is not None:
        return models.SSOInfo(
            state="keycloak", source=f"client '{realm_client}' in realm {_realm()}"
        )

    if has_forward_auth:
        return models.SSOInfo(
            state="keycloak", source="forward_auth in compose labels"
        )

    if env_signal is not None:
        state, source = env_signal
        return models.SSOInfo(state=state, source=source)  # type: ignore[arg-type]

    return models.SSOInfo(state="none", source="no OIDC keys found in .env")


# ── public entrypoint ─────────────────────────────────────────────────────────


async def detect(service_id: str, repo: object | None = None) -> models.SSOInfo:
    """Detect SSO for ``service_id``.

    ``repo`` is inventory's ``RepoService`` (parsed compose ``services`` block +
    merged ``labels`` + ``compose_path``); when absent, the compose/label signals
    are read from disk. Result is cached ~5 minutes per service.
    """
    now = time.monotonic()
    cached = _result_cache.get(service_id)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    result = await _detect_uncached(service_id, repo)
    _result_cache[service_id] = (now, result)
    return result


async def _detect_uncached(
    service_id: str, repo: object | None
) -> models.SSOInfo:
    from . import inventory  # function-level: avoids import cycle

    # ── locate the service dir + compose facts ──
    service_dir = _services_root() / service_id
    services_block: dict = {}
    labels: dict = {}
    if repo is not None:
        services_block = getattr(repo, "services", {}) or {}
        labels = getattr(repo, "labels", {}) or {}
        compose_path = getattr(repo, "compose_path", None)
        if isinstance(compose_path, Path):
            service_dir = compose_path.parent
    else:
        # re-parse from disk when called without a repo (e.g. tests)
        compose = service_dir / "compose.yaml"
        if compose.is_file():
            try:
                import yaml

                doc = yaml.safe_load(compose.read_text()) or {}
                if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
                    services_block = doc["services"]
                    labels = inventory._merged_labels(services_block)
            except Exception:
                pass

    # ── metadata (for the keycloak_client override) ──
    meta = inventory.load_metadata().get(service_id, {}) or {}

    # ── special case: the keycloak service itself ──
    is_keycloak_service = service_id == "keycloak"

    # ── signal 1: realm clients ──
    realm_client: Optional[str] = None
    if not is_keycloak_service:
        clients = await _fetch_realm_clients()
        if clients:
            candidate = _keycloak_client_for(service_id, meta)
            if candidate in clients:
                realm_client = candidate

    # ── signal 2: env key names + oauth2-proxy sidecar ──
    has_sidecar = _has_oauth2_proxy_sidecar(services_block)
    env_names = _scan_env_key_names(service_dir)
    env_signal = _classify_env_keys(env_names)

    # ── signal 3: caddy forward_auth ──
    has_forward_auth = _has_forward_auth(labels)

    return _fold(
        service_id=service_id,
        is_keycloak_service=is_keycloak_service,
        realm_client=realm_client,
        has_sidecar=has_sidecar,
        has_forward_auth=has_forward_auth,
        env_signal=env_signal,
    )
