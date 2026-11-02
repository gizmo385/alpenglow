"""Read Claude's verdicts back off the board.

The dispatcher labels each assessed card ``Verdict: Easy``, ``Verdict: Needs
care`` or ``Verdict: Hold``, and Claude's reply opens with the same verdict
and a one-sentence reason. This turns the open cards into the annotations the
update tracker's Discord digest and the dashboard show beside each update.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass

from .vikunja import Card, Vikunja

VERDICT_PREFIX = "Verdict: "
WAITING = "Waiting for assessment"
# Display order: "just do it" first, waiting last.
GROUPS = [
    {"name": "Easy", "emoji": "🟢", "color": "#2e7d32"},
    {"name": "Needs care", "emoji": "🟡", "color": "#f9a825"},
    {"name": "Hold", "emoji": "🔴", "color": "#c62828"},
    {"name": WAITING, "emoji": "⏳", "color": "#9e9e9e"},
]
VERDICTS = tuple(g["name"] for g in GROUPS[:-1])

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/li)\s*/?>", re.I)


@dataclass(frozen=True)
class Annotation:
    repo: str
    title: str
    group: str
    note: str = ""
    url: str | None = None
    card_id: int | None = None


def html_to_text(value: str | None) -> str:
    return html.unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", value or ""))).strip()


def verdict_from_labels(labels: list[dict] | None) -> str | None:
    for label in labels or []:
        title = str(label.get("title") or "")
        if title.startswith(VERDICT_PREFIX):
            name = title[len(VERDICT_PREFIX):].strip()
            for v in VERDICTS:
                if v.casefold() == name.casefold():
                    return v
    return None


def reason_from_comments(comments: list[dict], verdict: str) -> str:
    """The sentence after "Verdict: <verdict>" in the newest comment that has one."""
    pattern = re.compile(rf"verdict\W*{re.escape(verdict)}\W*(.*)", re.I)
    for comment in reversed(comments):
        lines = [ln.strip() for ln in html_to_text(comment.get("comment")).splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            m = pattern.search(line.strip("*_"))
            if not m:
                continue
            reason = m.group(1).lstrip(" *_.-–—").rstrip(" *_")
            if not reason and i + 1 < len(lines):
                reason = lines[i + 1].lstrip("-• ").strip()
            return reason
    return ""


def annotations_from(cards: list[Card], comments: dict[int, list[dict]], public_url: str | None) -> list[Annotation]:
    """Pure: one annotation per open card that tracks a repo, best verdict first."""
    out = []
    for card in cards:
        if card.done or not card.repo:
            continue
        verdict = verdict_from_labels(card.raw.get("labels")) or WAITING
        note = reason_from_comments(comments.get(card.id, []), verdict) if verdict != WAITING else ""
        url = f"{public_url.rstrip('/')}/tasks/{card.id}" if public_url else None
        out.append(Annotation(card.repo, card.title, verdict, note, url, card.id))
    order = {g["name"]: i for i, g in enumerate(GROUPS)}
    return sorted(out, key=lambda a: (order[a.group], a.title.casefold()))


def payload(annotations: list[Annotation]) -> dict:
    """The JSON the tracker's ``ANNOTATIONS_URL`` expects."""
    return {"title": "Upgrades", "link_label": "Open card", "groups": GROUPS,
            "items": [asdict(a) for a in annotations]}


async def fetch(api: Vikunja, public_url: str | None) -> list[Annotation]:
    cards = [c for c in await api.cards() if not c.done and c.repo]
    comments = {c.id: await api.comments(c.id) for c in cards if verdict_from_labels(c.raw.get("labels"))}
    return annotations_from(cards, comments, public_url)
