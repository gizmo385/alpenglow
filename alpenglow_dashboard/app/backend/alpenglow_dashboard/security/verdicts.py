"""Claude's security verdicts, read back off the Security cards.

The dispatcher's ``security`` profile labels each card ``Verdict: Act now``,
``Verdict: Next upgrade``, ``Verdict: Waiting on upstream`` or ``Verdict: Not
affected``, and Claude's reply opens with the same verdict and one sentence on
why.

The verdicts are sorted by what a person can do. **Act now** and **Next
upgrade** both mean there's a step to take (now, or with an upgrade or rebuild
that exists today). **Waiting on upstream** and **Not affected** mean there
isn't, so they're ``quiet``: the digest only counts them.

One rule is enforced here rather than left to the reviewer. **Next upgrade**
needs something to take. When the card says we run the latest release and the
image isn't built on the server, there is nothing, so that verdict is shown as
**Waiting on upstream**, unless the reply has a ``Mitigation:`` line naming a
step we can take without a new image (a config change, say).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..upgrades.verdicts import VERDICT_PREFIX, html_to_text, reason_from_comments
from .cards import nothing_to_take, GONE_FINDINGS_MARK, IMAGE_CHANGED_MARK, NEW_FINDINGS_MARK, REASSESS_MARK, SecurityCard

ACT_NOW, NEXT_UPGRADE, NOT_AFFECTED = "Act now", "Next upgrade", "Not affected"
UPSTREAM = "Waiting on upstream"
WAITING = "Waiting for assessment"
# Display order: most urgent first. ``quiet`` groups have nothing to do in them.
GROUPS = [
    {"name": ACT_NOW, "emoji": "🔴", "color": "#e08a8a"},
    {"name": NEXT_UPGRADE, "emoji": "🟡", "color": "#f5b942"},
    {"name": UPSTREAM, "emoji": "🔵", "color": "#8fb4d9", "quiet": True},
    {"name": NOT_AFFECTED, "emoji": "🟢", "color": "#6bd39a", "quiet": True},
    {"name": WAITING, "emoji": "⏳", "color": "#9e9e9e", "quiet": True},
]
VERDICTS = (ACT_NOW, NEXT_UPGRADE, UPSTREAM, NOT_AFFECTED)
QUIET = frozenset(g["name"] for g in GROUPS if g.get("quiet"))
COLORS = {g["name"]: g["color"] for g in GROUPS}


@dataclass(frozen=True)
class Verdict:
    service: str
    title: str
    group: str
    note: str = ""
    url: str | None = None
    card_id: int | None = None
    assessed_at: str | None = None  # when Claude gave this verdict

    @property
    def name(self) -> str:
        """The service's name: the card's title without its count of findings."""
        return self.title.split(":", 1)[0].strip() or self.service

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
    """False when Claude was asked to assess again after its last verdict:
    new findings arrived, the running image changed, an "Act now" card lost
    some of its findings, or a person asked for a reassessment."""
    last_verdict = last_new = -1
    for i, comment in enumerate(comments):
        text = html_to_text(comment.get("comment"))
        asked = "@claude" in text and any(mark in text for mark in (GONE_FINDINGS_MARK, IMAGE_CHANGED_MARK, REASSESS_MARK))
        if NEW_FINDINGS_MARK in text or asked:
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
        if verdict == NEXT_UPGRADE and nothing_to_take(sc.card.description) and not has_mitigation(cs):
            verdict = UPSTREAM  # the note stays: it's still why
        at = assessed_at(cs) if verdict != WAITING else None
        out.append(Verdict(sc.service, sc.card.title, verdict, note, card_url(public_url, sc.card.id),
                           sc.card.id, at))
    order = {g["name"]: i for i, g in enumerate(GROUPS)}
    return sorted(out, key=lambda v: (order[v.group], v.title.casefold()))


def _latest_reply(comments: list[dict]) -> tuple[str, str | None]:
    for comment in reversed(comments):
        text = html_to_text(comment.get("comment"))
        if re.match(r"\W*verdict\W*(" + "|".join(map(re.escape, VERDICTS)) + ")", text, re.I):
            return text, str(comment.get("created") or "") or None
    return "", None


_MITIGATION_RE = re.compile(r"^\W*mitigation\s*:\s*\S", re.I | re.M)


def has_mitigation(comments: list[dict]) -> bool:
    """Whether Claude's newest reply names a step that needs no new image."""
    return bool(_MITIGATION_RE.search(_latest_reply(comments)[0]))


def latest_reply(comments: list[dict]) -> str:
    """Claude's newest reply on a card: the last comment that opens with a verdict."""
    return _latest_reply(comments)[0]


def assessed_at(comments: list[dict]) -> str | None:
    """When that reply was written (Vikunja's timestamp on the comment)."""
    return _latest_reply(comments)[1]


def payload(verdicts: list[Verdict], repos: dict[str, str]) -> dict:
    """The tracker's annotations JSON (image-updates-tracker, annotations.py).

    ``repos`` maps a service to the GitHub repo the tracker follows, where
    known. Nothing open means no items, and the digest leaves the section out.
    """
    items = []
    for v in verdicts:
        d = asdict(v)
        d["name"] = v.name
        d["repo"] = repos.get(v.service, "")
        items.append(d)
    accent = "red" if any(v.group == ACT_NOW for v in verdicts) else "amber"
    return {"title": "Security", "link_label": "Open card", "accent": accent,
            "groups": GROUPS, "items": items}
