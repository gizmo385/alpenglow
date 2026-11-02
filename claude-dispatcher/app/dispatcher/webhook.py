"""Turn a Vikunja webhook into a job request, or nothing.

Vikunja signs each delivery with HMAC-SHA256 of the raw body in
``X-Vikunja-Signature``. Deliveries are sent once and never retried, so the
handler answers fast and runs the job in the background.

Two things start a job:

* ``task.created``: a new card in a mapped project.
* ``task.comment.created`` whose text starts with ``@claude``: a follow-up on
  an existing card, with the comment as the new instruction.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
from dataclasses import dataclass

TRIGGER_PREFIX = "@claude"


@dataclass(frozen=True)
class Trigger:
    event: str
    task_id: int
    project_id: int
    title: str
    description: str
    username: str | None
    instruction: str | None = None  # the @claude comment, for follow-ups


def verify(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|div|li|h\d)>|<br\s*/?>", re.IGNORECASE)


def html_to_text(value: str | None) -> str:
    """Vikunja stores descriptions and comments as HTML; jobs get plain text."""
    if not value:
        return ""
    text = _BLOCK_RE.sub("\n", value)
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(payload: dict) -> Trigger | None:
    event = payload.get("event_name") or ""
    data = payload.get("data") or {}
    task = data.get("task") or {}
    doer = data.get("doer") or {}
    if not task.get("id") or not task.get("project_id"):
        return None
    common = dict(
        event=event,
        task_id=int(task["id"]),
        project_id=int(task["project_id"]),
        title=str(task.get("title") or ""),
        description=html_to_text(task.get("description")),
        username=doer.get("username"),
    )
    if event == "task.created":
        return Trigger(**common)
    if event == "task.comment.created":
        text = html_to_text((data.get("comment") or {}).get("comment"))
        if text.lower().startswith(TRIGGER_PREFIX):
            return Trigger(**common, instruction=text[len(TRIGGER_PREFIX):].strip())
    return None
