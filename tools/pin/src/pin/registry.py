"""Resolve a tag to its manifest digest with the OCI distribution API.

Anonymous pulls only: the registry answers ``GET /v2/`` with a
``WWW-Authenticate: Bearer realm=...`` challenge, we fetch a pull token from
that realm, then ``HEAD`` the manifest and read ``Docker-Content-Digest``.
That digest is the multi-arch index digest when the tag has one, which is what
``docker compose pull`` records, so ``name:tag@digest`` pulls the same image.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .refs import parse_ref

DOCKER_HUB = "registry-1.docker.io"

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    pass


class TagNotFound(RegistryError):
    """The registry answered, and has no such tag."""


def split_name(name: str) -> tuple[str, str]:
    """``(registry_host, repository)`` for an image name.

    ``redis`` -> (registry-1.docker.io, library/redis); ``ollama/ollama`` ->
    (registry-1.docker.io, ollama/ollama); ``ghcr.io/org/app`` -> (ghcr.io, org/app).
    """
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest
    if not rest:
        return DOCKER_HUB, f"library/{name}"
    return DOCKER_HUB, name


def _token(host: str, repo: str, timeout: float) -> str | None:
    try:
        urllib.request.urlopen(f"https://{host}/v2/", timeout=timeout)
        return None  # no auth needed
    except urllib.error.HTTPError as err:
        if err.code != 401:
            raise RegistryError(f"{host}: GET /v2/ returned {err.code}") from err
        challenge = err.headers.get("WWW-Authenticate", "")
    if not challenge.lower().startswith("bearer"):
        raise RegistryError(f"{host}: unsupported auth challenge {challenge!r}")
    params = dict(_CHALLENGE_RE.findall(challenge))
    realm = params.pop("realm", None)
    if not realm:
        raise RegistryError(f"{host}: auth challenge has no realm")
    query = {"scope": f"repository:{repo}:pull"}
    if "service" in params:
        query["service"] = params["service"]
    url = f"{realm}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = json.load(resp)
    return body.get("token") or body.get("access_token")


def resolve_digest(name: str, tag: str, timeout: float = 20.0) -> str:
    """The digest ``name:tag`` points at right now."""
    host, repo = split_name(parse_ref(name).name)
    token = _token(host, repo, timeout)
    req = urllib.request.Request(
        f"https://{host}/v2/{repo}/manifests/{tag}", method="HEAD", headers={"Accept": MANIFEST_TYPES}
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            digest = resp.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise TagNotFound(f"{name}:{tag} doesn't exist in {host}") from err
        if err.code == 429:
            raise RegistryError(f"{host} is rate limiting anonymous lookups; try again later") from err
        raise RegistryError(f"{name}:{tag}: {host} returned {err.code}") from err
    if not digest:
        raise RegistryError(f"{name}:{tag}: {host} sent no Docker-Content-Digest header")
    return digest
