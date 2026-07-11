"""JSONL audit log (work package B2).

A1 stub: interface only. B2 implements append-only writes to
/data-store/audit.jsonl with {ts, user, service, action, outcome, stderr_tail}.
"""

from __future__ import annotations


async def record(user: str, service: str, action: str, outcome: str, stderr_tail: str = "") -> None:
    raise NotImplementedError("audit log not implemented yet (B2)")
