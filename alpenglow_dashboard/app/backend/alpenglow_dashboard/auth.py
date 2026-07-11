"""Identity, CSRF and admin-gating (work package A1).

The container sits behind oauth2-proxy, which is the actual trust boundary:
it authenticates against Keycloak and forwards the resulting identity in
``X-Forwarded-User`` / ``X-Forwarded-Groups``. This module:

- reads those headers into a per-request ``Identity`` (401 when absent,
  defense in depth in case the proxy is bypassed on the docker network);
- allows a full bypass with ``DEV_NO_AUTH=1`` (local dev and mock mode),
  which synthesizes an admin identity;
- implements a CSRF double-submit token: ``GET /api/csrf`` issues a token and
  sets it as a cookie; every mutating request (POST/PUT/PATCH/DELETE) must echo
  it in ``X-CSRF-Token`` and it must match the cookie;
- gates mutating routes on membership in ``ADMIN_GROUP`` (default ``admin``).
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import models

CSRF_COOKIE = "alpenglow_csrf"
CSRF_HEADER = "X-CSRF-Token"

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def dev_no_auth() -> bool:
    return os.environ.get("DEV_NO_AUTH", "0") == "1"


def admin_group() -> str:
    return os.environ.get("ADMIN_GROUP", "admin")


@dataclass
class Identity:
    user: str
    groups: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        target = admin_group()
        # oauth2-proxy may forward Keycloak group paths like "/admin".
        return any(g == target or g.lstrip("/") == target for g in self.groups)


def _parse_groups(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def identity_from_request(request: Request) -> Identity | None:
    if dev_no_auth():
        return Identity(user="dev@localhost", groups=[admin_group()])
    user = request.headers.get("X-Forwarded-User")
    if not user:
        return None
    return Identity(user=user, groups=_parse_groups(request.headers.get("X-Forwarded-Groups")))


class AuthMiddleware(BaseHTTPMiddleware):
    """Attaches request.state.identity; enforces auth, CSRF and the admin gate
    on /api routes. Static SPA assets are served unauthenticated (the proxy in
    front covers them; they contain no secrets)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)

        identity = identity_from_request(request)
        request.state.identity = identity

        if identity is None:
            return JSONResponse(
                {"detail": "missing forwarded identity"}, status_code=401
            )

        if request.method in MUTATING_METHODS:
            if not identity.is_admin:
                return JSONResponse(
                    {"detail": f"requires membership in group '{admin_group()}'"},
                    status_code=403,
                )
            cookie = request.cookies.get(CSRF_COOKIE)
            header = request.headers.get(CSRF_HEADER)
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                return JSONResponse(
                    {"detail": "CSRF token missing or mismatched"}, status_code=403
                )

        return await call_next(request)


router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/me")
async def me(request: Request) -> models.Me:
    identity: Identity = request.state.identity
    return models.Me(user=identity.user, groups=identity.groups, isAdmin=identity.is_admin)


@router.get("/csrf")
async def csrf(request: Request) -> JSONResponse:
    token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = JSONResponse(models.Csrf(token=token).model_dump())
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,  # SPA gets the token from the JSON body, not the cookie
        samesite="strict",
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        path="/",
    )
    return response
