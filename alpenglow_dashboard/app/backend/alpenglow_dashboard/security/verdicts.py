"""Claude's security verdicts, read back off the Security cards.

The dispatcher's ``security`` profile labels each card ``Verdict: Act now``,
``Verdict: Next upgrade`` or ``Verdict: Not affected``, and Claude's reply
opens with the same verdict and one sentence on why.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..upgrades.verdicts import VERDICT_PREFIX, html_to_text, reason_from_comments
from .cards import NEW_FINDINGS_MARK, SecurityCard

ACT_NOW, NEXT_UPGRADE, NOT_AFFECTED = "Act now", "Next upgrade", "Not affected"
WAITING = "Waiting for assessment"
# Display order: most urgent first.
GROUPS = [
    {"name": ACT_NOW, "emoji": "🔴", "color": "#e08a8a"},
    {"name": NEXT_UPGRADE, "emoji": "🟡", "color": "#f5b942"},
    {"name": NOT_AFFECTED, "emoji": "🟢", "color": "#6bd39a"},
    {"name": WAITING, "emoji": "⏳", "color": "#9e9e9e"},
]
VERDICTS = (ACT_NOW, NEXT_UPGRADE, NOT_AFFECTED)
COLORS = {g["name"]: g["color"] for g in GROUPS}


@dataclass(frozen=True)
class Verdict:
    service: str
    title: str
    group: str
    note: str = ""
    url: str | None = None
    card_id: int | None = None

    @property
    def color(self) -> str:
        return COLORS[self.group]


def verdict_from_labels(labels: list[dict] | None) -> str | None:
    for label in labels or []:
        title = str(label.get("title") or "")
        if title.startswith(VERDICT_PREFIX):
            name = title[len(VERDICT_PREFIX):].strip()
            for v in VERDICTS:
                if v.casefold() == name.casefold():
                    return v
    return None


def is_current(comments: list[dict]) -> bool:
    """False when new findings arrived after Claude's last verdict."""
    last_verdict = last_new = -1
    for i, comment in enumerate(comments):
        text = html_to_text(comment.get("comment"))
        if NEW_FINDINGS_MARK in text:
            last_new = i
        elif re.search(r"verdict\W*(" + "|".join(map(re.escape, VERDICTS)) + ")", text, re.I):
            last_verdict = i
    return last_new < last_verdict or last_new == -1


def card_url(public_url: str | None, card_id: int) -> str | None:
    return f"{public_url.rstrip('/')}/tasks/{card_id}" if public_url else None


def from_cards(cards: list[SecurityCard], comments: dict[int, list[dict]], public_url: str | None) -> list[Verdict]:
    """Pure: one verdict per open card, most urgent first."""
    out = []
    for sc in cards:
        if sc.card.done or not sc.service:
            continue
        verdict = verdict_from_labels(sc.card.raw.get("labels")) or WAITING
        cs = comments.get(sc.card.id, [])
        if verdict != WAITING and not is_current(cs):
            verdict = WAITING
        note = reason_from_comments(cs, verdict) if verdict != WAITING else ""
        out.append(Verdict(sc.service, sc.card.title, verdict, note, card_url(public_url, sc.card.id), sc.card.id))
    order = {g["name"]: i for i, g in enumerate(GROUPS)}
    return sorted(out, key=lambda v: (order[v.group], v.title.casefold()))


def latest_reply(comments: list[dict]) -> str:
    """Claude's newest reply on a card: the last comment that opens with a verdict."""
    for comment in reversed(comments):
        text = html_to_text(comment.get("comment"))
        if re.match(r"\W*verdict\W*(" + "|".join(map(re.escape, VERDICTS)) + ")", text, re.I):
            return text
    return ""


def payload(verdicts: list[Verdict], repos: dict[str, str]) -> dict:
    """The tracker's annotations JSON (image-updates-tracker, annotations.py).

    ``repos`` maps a service to the GitHub repo the tracker follows, where
    known. Nothing open means no items, and the digest leaves the section out.
    """
    items = []
    for v in verdicts:
        d = asdict(v)
        d["repo"] = repos.get(v.service, "")
        items.append(d)
    accent = "red" if any(v.group == ACT_NOW for v in verdicts) else "amber"
    return {"title": "Security", "link_label": "Open card", "accent": accent,
            "groups": GROUPS, "items": items}
