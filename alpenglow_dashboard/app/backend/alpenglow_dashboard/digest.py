"""The daily Discord digest: what's waiting for a person, once a day.

Up to three messages, each left out when it has nothing to say:

* **Security**: the services Claude says need a step from us (Act now, Next
  upgrade), each with its one-line reason. Services whose findings need
  nothing from us (waiting on upstream, not affected, still being assessed)
  are only named, in one line per verdict, and on a day when no service
  needs a step the section is left out.
* **Upgrades**: the open Upgrades cards, grouped by Claude's verdict.
* **Other updates**: pending updates with no card, straight from the tracker.

On a day with none of those, nothing is posted. Everything listed is something
a person can act on: a release the tracker is holding back because its image
isn't published yet is left out until it can be pulled.

This used to be image-updates-tracker's Discord bot, which fetched the first
two from here as "annotations". The dashboard is where the verdicts, the
findings and the decisions live, so it sends the digest itself, and the tracker
only reports what exists upstream.

It goes out through a channel webhook, like the update summary
(:mod:`.integrations.update_summary`), not a bot token: the dashboard is
root-equivalent, and a webhook can only post to its one channel.
``DIGEST_WEBHOOK_URL`` names the channel; unset, the update summary's webhook
is used, and with neither there's no digest. ``DIGEST_AT`` is the local time
it's sent (``HH:MM``, default 09:00); ``DIGEST_AT=off`` turns it off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import mock, security, upgrades
from .integrations import update_summary, updates

log = logging.getLogger("alpenglow.digest")

router = APIRouter(prefix="/api/digest", tags=["digest"])

DEFAULT_AT = "09:00"

_BLUE, _AMBER, _RED = 0x3B82F6, 0xF9A825, 0xC62828
_ACCENTS = {"red": _RED, "amber": _AMBER, "blue": _BLUE}

_DESCRIPTION_LIMIT = 4096  # Discord's limit for an embed
_NOTE_LIMIT = 280


# ── settings ──────────────────────────────────────────────────────────────────


def webhook_url() -> Optional[str]:
    return os.environ.get("DIGEST_WEBHOOK_URL") or os.environ.get("UPDATE_SUMMARY_WEBHOOK_URL") or None


def send_time() -> Optional[tuple[int, int]]:
    """(hour, minute) to send at, local time; ``None`` when the digest is off."""
    raw = (os.environ.get("DIGEST_AT") or DEFAULT_AT).strip()
    if raw.lower() in ("off", "none", "false", "0"):
        return None
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    log.warning("digest: DIGEST_AT=%r isn't HH:MM; using %s", raw, DEFAULT_AT)
    hour, minute = (int(part) for part in DEFAULT_AT.split(":"))
    return hour, minute


def next_send(now: datetime, at: tuple[int, int]) -> datetime:
    """The next time the clock reads ``at``, strictly after ``now``."""
    target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


# ── building the messages ────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit(blocks: list[str], limit: int = _DESCRIPTION_LIMIT) -> str:
    """Join ``blocks`` with blank lines, dropping from the end what doesn't fit."""
    out: list[str] = []
    used = 0
    for i, block in enumerate(blocks):
        more = f"…and {len(blocks) - i} more"
        # Keep room to say how many were left out.
        if used + len(block) + 2 + (len(more) + 2 if i < len(blocks) - 1 else 0) > limit:
            out.append(more)
            break
        out.append(block)
        used += len(block) + 2
    return "\n\n".join(out)


def _item(item: dict, link_label: str) -> str:
    # A security card's title counts its raw findings ("Kopia: 71 open security
    # findings"), which reads as urgency it doesn't carry; the name is enough.
    text = f"**{item.get('name') or item.get('title') or item.get('repo') or '?'}**"
    if item.get("note"):
        text += f"\n{_clip(str(item['note']), _NOTE_LIMIT)}"
    if item.get("url"):
        text += f"\n[{link_label}]({item['url']})"
    return text


def annotations_embed(payload: dict, default_color: int = _BLUE) -> Optional[dict]:
    """One message for a board's cards, grouped by verdict; ``None`` if empty.

    ``payload`` is what ``upgrades.verdicts.payload`` and
    ``security.verdicts.payload`` return: title, groups in display order, items.

    A group marked ``quiet`` holds cards with nothing for a person to do. Its
    cards are named in one line rather than listed, and a board with only
    quiet cards has no message at all.
    """
    items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
    quiet = {g["name"] for g in payload.get("groups") or [] if g.get("quiet")}
    if not any((i.get("group") or "Other") not in quiet for i in items):
        return None
    groups = [(g["name"], g.get("emoji") or "") for g in payload.get("groups") or []]
    known = {name for name, _ in groups}
    groups += [(name, "") for name in dict.fromkeys(i.get("group") or "Other" for i in items) if name not in known]
    label = str(payload.get("link_label") or "Details")

    blocks, counts = [], []
    for name, emoji in groups:
        members = [i for i in items if (i.get("group") or "Other") == name]
        if not members:
            continue
        counts.append(f"{len(members)} {name.lower()}")
        if name in quiet:
            names = ", ".join(str(i.get("name") or i.get("title") or "?") for i in members)
            blocks.append(_clip(f"{emoji} **{name}, nothing to do ({len(members)}):** {names}".strip(), 600))
            continue
        blocks.append(f"### {emoji} {name}".replace("###  ", "### "))
        blocks += [_item(i, label) for i in members]
    return {
        "title": str(payload.get("title") or "Pending"),
        "color": _ACCENTS.get(str(payload.get("accent") or ""), default_color),
        "description": _fit(blocks),
        "footer": {"text": " · ".join(counts)},
    }


def _services(tracker: Optional[dict]) -> list[dict]:
    return [s for s in (tracker or {}).get("services") or [] if isinstance(s, dict)]


def other_updates_embed(tracker: Optional[dict], covered: set[str], title: str) -> Optional[dict]:
    """Pending updates with no card (``covered`` holds the carded repos, lowercased)."""
    pending = sorted(
        (s for s in _services(tracker)
         if s.get("has_updates") and f"{s.get('owner')}/{s.get('repo')}".lower() not in covered),
        key=lambda s: str(s.get("name") or "").casefold(),
    )
    if not pending:
        return None
    blocks = []
    for s in pending:
        line = f"**{s.get('name')}** `{s.get('current_version')}` → `{s.get('latest_tag') or s.get('latest_version')}`"
        if s.get("html_url"):
            line += f" · [Releases]({s['html_url']})"
        blocks.append(line)
    footer = f"{len(pending)} of {len(_services(tracker))} services have updates with no card"
    return {"title": title, "color": _AMBER, "description": _fit(blocks), "footer": {"text": footer}}


def build(upgrades_payload: dict, security_payload: dict, tracker: Optional[dict]) -> list[dict]:
    """Pure: the digest's embeds, one message each, in the order they're sent."""
    out = []
    sec = annotations_embed(security_payload, _AMBER)
    if sec:
        out.append(sec)
    ups = annotations_embed(upgrades_payload)
    if ups:
        out.append(ups)
    covered = {str(i.get("repo") or "").lower() for i in upgrades_payload.get("items") or [] if isinstance(i, dict)}
    other = other_updates_embed(tracker, covered - {""}, "Other updates" if ups else "Docker Services — Update Digest")
    if other:
        if ups:
            ups["footer"]["text"] += f" · {other['footer']['text'].split(' of ')[0]} other below"
        out.append(other)
    return out


# ── gathering and sending ────────────────────────────────────────────────────


async def gather(fresh: bool = True) -> list[dict]:
    """Read the boards and the tracker, and build the digest."""
    if mock.mock_enabled():
        return []
    if fresh:
        await updates.rescan()  # logs and carries on if the tracker is down
    tracker = await updates._tracker_cached()
    ups = upgrades.verdicts.payload(await upgrades.annotations())
    try:
        sec = await security.get_annotations()
    except Exception:  # noqa: BLE001 - the rest of the digest still goes out
        log.exception("digest: couldn't read the security board")
        sec = {}
    return build(ups, sec, tracker)


async def send(fresh: bool = True) -> int:
    """Post today's digest; return how many messages it had."""
    url = webhook_url()
    if not url:
        return 0
    embeds = await gather(fresh)
    for embed in embeds:
        await update_summary.post_embed(embed, url)
    log.info("digest: %s", f"posted {len(embeds)} message(s)" if embeds else "nothing to report")
    return len(embeds)


async def loop() -> None:
    at = send_time()
    assert at is not None
    while True:
        now = datetime.now().astimezone()
        due = next_send(now, at)
        log.info("digest: next at %s", due.isoformat(timespec="minutes"))
        await asyncio.sleep((due - now).total_seconds())
        try:
            await send()
        except Exception:  # noqa: BLE001 - tomorrow's still goes out
            log.exception("digest: failed")


@contextlib.asynccontextmanager
async def lifespan(app):
    on = send_time() is not None and webhook_url() and not mock.mock_enabled()
    task = asyncio.create_task(loop(), name="alpenglow-digest") if on else None
    try:
        yield
    finally:
        if task:
            task.cancel()


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("")
async def get_digest() -> dict:
    """What the digest would say now, without sending it."""
    at = send_time()
    return {
        "enabled": bool(at and webhook_url()),
        "at": f"{at[0]:02d}:{at[1]:02d}" if at else None,
        "messages": await gather(fresh=False),
    }


@router.post("/send")
async def post_send() -> dict:
    """Send the digest now."""
    if not webhook_url():
        raise HTTPException(409, "no webhook: set DIGEST_WEBHOOK_URL or UPDATE_SUMMARY_WEBHOOK_URL")
    return {"sent": await send()}
