"""The Security project on Vikunja: one card per service with open findings.

After each sync:

* a service with open findings no card has covered yet gets a card, and the
  claude-dispatcher's ``security`` profile assesses it;
* new findings on a service with an open card update the card and get an
  ``@claude`` comment, so Claude assesses again with the whole list;
* an open card is closed once a clean scan of the service shows none of its
  findings (usually after an upgrade);
* a card closed by hand (say, "Not affected") isn't refiled for the same
  findings, only when new ones appear.

Each card's description carries two marker lines the sync reads back: the
service id, and the ids of the findings it covers.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

from ..upgrades.vikunja import Card, Vikunja
from .findings import OpenFinding, ServiceFindings

log = logging.getLogger("alpenglow.security")

SERVICE_LINE = "Security service:"
FINDINGS_LINE = "Findings covered:"
_SERVICE_RE = re.compile(rf"{re.escape(SERVICE_LINE)}\s*([\w.-]+)")
_FINDINGS_RE = re.compile(rf"{re.escape(FINDINGS_LINE)}\s*([^<\n]*)")

# The comment the sync leaves when new findings arrive on an open card.
NEW_FINDINGS_MARK = "New findings since the last assessment"

TIER_TEXT = {
    "public": "Public (reachable from the internet on :10443)",
    "tailnet": "Tailnet only",
    "management": "Tailnet only (management)",
    "internal": "No ingress (other containers on shared networks can still reach it)",
}


@dataclass
class SecurityCard:
    card: Card
    service: str | None
    covers: set[str]

    @classmethod
    def parse(cls, card: Card) -> SecurityCard:
        desc = html.unescape(card.description or "")
        sid = _SERVICE_RE.search(desc)
        ids = _FINDINGS_RE.search(desc)
        covers = {i.strip() for i in ids.group(1).split(",") if i.strip()} if ids else set()
        return cls(card, sid.group(1) if sid else None, covers)


@dataclass
class Context:
    """What the card shows beyond the findings, per service."""

    name: str
    tier: str = "internal"
    url: str | None = None
    pending: str | None = None  # "to 26.8.3"
    compose_url: str | None = None


@dataclass
class Plan:
    create: list[str] = field(default_factory=list)
    add: list[tuple[SecurityCard, str, list[str]]] = field(default_factory=list)
    close: list[tuple[SecurityCard, str]] = field(default_factory=list)


def plan(services: dict[str, ServiceFindings], cards: list[SecurityCard]) -> Plan:
    """Decide what to do. Pure, so it can be tested without Vikunja."""
    out = Plan()
    by_service: dict[str, list[SecurityCard]] = {}
    for c in cards:
        if c.service:
            by_service.setdefault(c.service, []).append(c)

    for sid, sf in sorted(services.items()):
        existing = by_service.get(sid, [])
        open_cards = [c for c in existing if not c.card.done]
        current = sf.open_ids
        if not current:
            if sf.scanned:
                out.close += [(c, sid) for c in open_cards]
            continue
        covered = set().union(*(c.covers for c in existing)) if existing else set()
        new = sorted(current - covered)
        if not new:
            continue
        if open_cards:
            out.add.append((open_cards[0], sid, new))
        else:
            out.create.append(sid)
    return out


def card_title(ctx: Context, sf: ServiceFindings) -> str:
    n = len(sf.open)
    return f"{ctx.name}: {n} open security finding{'s' if n != 1 else ''}"


def _finding_line(o: OpenFinding) -> str:
    e = html.escape
    f = o.finding
    ident = f'<a href="{e(f.url)}">{e(f.id)}</a>' if f.url else e(f.id)
    aliases = f" ({e(', '.join(f.aliases))})" if f.aliases else ""
    fix = f" → fixed in {e(', '.join(f.fixed_in))}" if f.fixed_in else " (no fixed version yet)"
    bits = [f"{e(f.severity)}"]
    if f.kev:
        bits.append("on CISA's known-exploited list")
    if f.epss is not None:
        bits.append(f"EPSS {f.epss:.1%}")
    head = f"{ident}{aliases}: {', '.join(bits)}. {e(f.package)} {e(f.version)}{fix} [{e(f.type or f.source)}] in {e(o.image)}"
    return head + (f"<br>{e(f.summary)}" if f.summary else "")


def card_description(sid: str, ctx: Context, sf: ServiceFindings) -> str:
    e = html.escape
    lines = [
        f"Service: {e(ctx.name)}",
        f"Exposure: {e(TIER_TEXT.get(ctx.tier, ctx.tier))}" + (f" at {e(ctx.url)}" if ctx.url else ""),
    ]
    for img in sf.images:
        bits = [e(img.ref)]
        if img.digest:
            bits.append(f"digest {e(img.digest[:19])}…")
        if img.local_build:
            bits.append("built on the server from this repo")
        lines.append("Image: " + ", ".join(bits))
    if ctx.pending:
        lines.append(f"Pending upgrade: {e(ctx.pending)}")
    if ctx.compose_url:
        lines.append(f'Compose file: <a href="{e(ctx.compose_url)}">{e(ctx.compose_url)}</a>')
    out = "".join(f"<p>{line}</p>" for line in lines)
    out += f"<p>Open findings ({len(sf.open)}):</p><ul>"
    out += "".join(f"<li>{_finding_line(o)}</li>" for o in sf.open)
    out += "</ul>"
    if sf.ignored:
        out += "<p>Ignored in cve-scanner/ignore.yaml: " + ", ".join(
            f"{e(o.id)} ({e(i.reason or 'no reason given')})" for o, i in sf.ignored) + "</p>"
    out += f"<p>{SERVICE_LINE} {e(sid)}</p><p>{FINDINGS_LINE} {e(', '.join(sorted(sf.open_ids)))}</p>"
    return out


async def sync(services: dict[str, ServiceFindings], contexts: dict[str, Context], api: Vikunja) -> Plan:
    """Bring the Security project in line with the scan. Returns what it did."""
    cards = [SecurityCard.parse(c) for c in await api.cards()]
    todo = plan(services, cards)

    def ctx(sid: str) -> Context:
        return contexts.get(sid) or Context(name=sid)

    for sid in todo.create:
        sf = services[sid]
        await api.create(card_title(ctx(sid), sf), card_description(sid, ctx(sid), sf))
        log.info("security: filed a card for %s (%d findings)", sid, len(sf.open))
    for sc, sid, new in todo.add:
        sf = services[sid]
        await api.update(sc.card, title=card_title(ctx(sid), sf), description=card_description(sid, ctx(sid), sf))
        await api.comment(sc.card, f"@claude {NEW_FINDINGS_MARK}: {', '.join(new)}. "
                                   "The description lists everything open now. Please assess again.")
        log.info("security: %d new findings on card %s (%s)", len(new), sc.card.id, sid)
    for sc, sid in todo.close:
        await api.update(sc.card, done=True)
        await api.comment(sc.card, "Closed by the security scan: a clean scan of the running "
                                   "images no longer shows these findings.")
        log.info("security: closed card %s: %s is clean", sc.card.id, sid)
    return todo
