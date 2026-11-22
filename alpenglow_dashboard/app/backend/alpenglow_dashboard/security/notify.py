"""Immediate Discord pings, for the few findings that can't wait for the digest.

A ping goes out, once, when:

* Claude's verdict on a card is **Act now**; or
* a finding on CISA's known-exploited list turns up in a **public** service,
  without waiting for Claude.

Everything else waits for the 9 AM digest's Security section. Pings go
through a Discord webhook (``DISCORD_SECURITY_WEBHOOK_URL``); which ones were
sent is kept in ``/data-store/security-pings.json`` so a restart doesn't
repeat them.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .findings import ServiceFindings
from .verdicts import ACT_NOW, Verdict

log = logging.getLogger("alpenglow.security")

RED = 0xC62828


@dataclass(frozen=True)
class Ping:
    key: str  # what makes it unique, so it's sent once
    title: str
    text: str
    url: str | None = None


def due(verdicts: list[Verdict], services: dict[str, ServiceFindings], tiers: dict[str, str],
        names: dict[str, str], dashboard_url: str | None, sent: set[str]) -> list[Ping]:
    """Pure: the pings not sent yet."""
    out = []
    for v in verdicts:
        if v.group != ACT_NOW or v.card_id is None:
            continue
        key = f"act-now:{v.card_id}"
        if key not in sent:
            out.append(Ping(key, f"🔴 Act now · {names.get(v.service, v.service)}",
                            v.note or "Claude rates this Act now; see the card.", v.url))
    for sid, sf in sorted(services.items()):
        if tiers.get(sid) != "public":
            continue
        for o in sf.open:
            key = f"kev:{sid}:{o.id}"
            if o.finding.kev and key not in sent:
                fix = f" Fixed in {', '.join(o.finding.fixed_in)}." if o.finding.fixed_in else " No fix released yet."
                url = f"{dashboard_url.rstrip('/')}/services/{sid}" if dashboard_url else None
                out.append(Ping(key, f"🔴 Known-exploited CVE on a public service · {names.get(sid, sid)}",
                                f"{o.id} in {o.finding.package} {o.finding.version} ({o.image}).{fix} "
                                "Claude is assessing it now.", url))
    return out


def load_sent(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def save_sent(path: Path, sent: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(sent)))
        os.replace(tmp, path)
    except OSError as err:
        log.warning("security: can't save %s: %s", path, err)


def message(p: Ping, mention: str | None) -> dict:
    embed: dict = {"title": p.title[:256], "description": p.text[:2000], "color": RED}
    if p.url:
        embed["url"] = p.url
    body: dict = {"embeds": [embed], "allowed_mentions": {"parse": ["users", "roles"]}}
    if mention:
        body["content"] = mention
    return body


async def send(client: httpx.AsyncClient, webhook: str, pings: list[Ping], mention: str | None,
               state: Path) -> int:
    sent = load_sent(state)
    count = 0
    for p in pings:
        try:
            resp = await client.post(webhook, json=message(p, mention), timeout=15)
            resp.raise_for_status()
        except httpx.HTTPError as err:
            log.warning("security: Discord ping failed (%s); trying again next sync", err)
            break
        sent.add(p.key)
        count += 1
    save_sent(state, sent)
    return count
