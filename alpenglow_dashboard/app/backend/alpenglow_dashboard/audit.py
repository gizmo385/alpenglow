"""JSONL audit log (work package B2).

Append-only audit trail for every compose action the dashboard performs. One
JSON object per line at ``/data-store/audit.jsonl`` (override with ``AUDIT_PATH``),
written under an asyncio lock so concurrent actions never interleave a line.

Every real action writes at least two entries: one when it is *accepted*
(the POST returns ``{accepted, jobId}``) and one for the final *outcome*
(``succeeded`` / ``failed``); a rejected action writes a single ``rejected`` line.

Line shape::

    {"ts": "2026-07-11T18:04:12.511Z", "user": "chris", "service": "glances",
     "action": "restart", "outcome": "succeeded", "jobId": "...", "stderr": ""}

``stderr`` carries a trailing tail of the compose stderr only on failure.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Serialises writes across concurrent actions in the one process.
_lock = asyncio.Lock()

# Max characters of stderr kept per audit line (a generous tail; full output is
# still surfaced to the caller/logs, this just keeps the audit file bounded).
_STDERR_TAIL = 2000


def audit_path() -> Path:
    return Path(os.environ.get("AUDIT_PATH", "/data-store/audit.jsonl"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tail(stderr: str) -> str:
    stderr = (stderr or "").strip()
    if len(stderr) > _STDERR_TAIL:
        return stderr[-_STDERR_TAIL:]
    return stderr


async def record(
    user: str,
    service: str,
    action: str,
    outcome: str,
    stderr_tail: str = "",
    job_id: Optional[str] = None,
) -> None:
    """Append one audit line. Never raises: a failed write must not break the
    action path (best-effort durability, the action itself is the source of truth)."""
    entry = {
        "ts": _now(),
        "user": user,
        "service": service,
        "action": action,
        "outcome": outcome,
        "jobId": job_id,
        "stderr": _tail(stderr_tail),
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    path = audit_path()
    async with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Blocking write is fine: lines are tiny and the lock already
            # serialises us; keeping it simple avoids a thread hop per action.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            # Degrade silently — auditing is best-effort, the action still ran.
            pass
