"""SSO auto-detection (work package B5).

A1 stub: interface only. B5 implements the three detection signals in authority
order (Keycloak admin API, .env/compose key-name scan, Caddy forward_auth
labels) producing models.SSOInfo per service, cached ~5 minutes.
"""

from __future__ import annotations

from . import models


async def detect(service_id: str) -> models.SSOInfo:
    raise NotImplementedError("SSO detection not implemented yet (B5)")
