"""Post a summary of finished Updates to Discord, beside the update digest.

Goes through a channel webhook (``UPDATE_SUMMARY_WEBHOOK_URL``) rather than the
digest bot's token: the dashboard is root-equivalent, and a webhook can only post
to its one channel. Unset, nothing is posted.

The webhook's secret is in its URL, so the URL mustn't reach a trace or a log.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

from ._common import DEFAULT_TIMEOUT

log = logging.getLogger("alpenglow.update_summary")

_GREEN = 0x2ECC71
_RED = 0xE74C3C
_REASON_LIMIT = 200
_DESCRIPTION_LIMIT = 4096  # Discord's limit for an embed


class _HideWebhook(logging.Filter):
    """Drop httpx's own log line for the post, which quotes the whole URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        url = os.environ.get("UPDATE_SUMMARY_WEBHOOK_URL")
        return not url or url not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideWebhook())


@dataclass(frozen=True)
class Result:
    """How one service's Update ended."""

    service: str
    name: str
    user: str
    ok: bool
    detail: str = ""
    versions: list[str] = field(default_factory=list)


def _reason(detail: str) -> str:
    """The line of a failure worth reading: compose's errors come last."""
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    errors = [line for line in lines if not line.startswith("[update]")]
    reason = (errors[-1] if errors else "Update failed")[:_REASON_LIMIT]
    return f"{reason} (new pin not rolled back)" if len(errors) < len(lines) else reason


def _line(result: Result) -> str:
    versions = ", ".join(result.versions)
    head = f"**{result.name}** {versions}".rstrip()
    return f"✅ {head}" if result.ok else f"❌ {head}: {_reason(result.detail)}"


def build_embed(results: list[Result]) -> dict:
    failed = sum(not r.ok for r in results)
    updated = len(results) - failed
    # The name only: the digest's channel doesn't need people's email addresses.
    users = sorted({r.user.split("@", 1)[0] for r in results})
    counts = [f"{updated} updated"] + ([f"{failed} failed"] if failed else [])
    return {
        "title": "Docker Services — Update Summary",
        "color": _RED if failed else _GREEN,
        "description": "\n".join(_line(r) for r in results)[:_DESCRIPTION_LIMIT],
        "footer": {"text": f"{' · '.join(counts)} · by {', '.join(users)}"},
    }


async def post(results: list[Result]) -> None:
    """Post the summary. A failure is logged and goes no further."""
    url = os.environ.get("UPDATE_SUMMARY_WEBHOOK_URL")
    if not url or not results:
        return
    body = {
        "embeds": [build_embed(results)],
        # A failure's text comes from elsewhere, so it mustn't ping anyone.
        "allowed_mentions": {"parse": []},
    }
    try:
        with suppress_instrumentation():
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(url, json=body)
    except httpx.HTTPError as err:
        log.warning("update summary not posted: %s", type(err).__name__)
        return
    if resp.status_code >= 400:
        log.warning("update summary not posted: Discord returned %s", resp.status_code)
