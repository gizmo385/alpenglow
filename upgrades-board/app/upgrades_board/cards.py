"""File a card in the Upgrades project for each pending update.

After each sync there is one card per repo:

* a new release with no card for it gets a card;
* an open card for an older release is retitled and gets an ``@claude``
  comment, so the dispatcher assesses the newer version;
* an open card is marked done once the service runs that version;
* a card closed without upgrading isn't refiled for the same version, only
  when a newer one comes out.

With a versions.yaml, only images listed there get cards, never ones with
``policy: float``, and each card shows the image's policy and its
``compatibility`` list.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .vikunja import REPO_LINE, TARGET_LINE, Card, Vikunja

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Service:
    """One entry of the tracker's ``GET /api/updates``."""

    name: str
    owner: str
    repo: str
    image: str
    current_version: str
    latest_version: str
    has_updates: bool
    html_url: str
    releases: int = 0

    @classmethod
    def from_api(cls, d: dict) -> Service:
        return cls(
            name=str(d.get("name") or d.get("repo") or ""),
            owner=str(d.get("owner") or ""),
            repo=str(d.get("repo") or ""),
            image=str(d.get("image") or ""),
            current_version=str(d.get("current_version") or ""),
            latest_version=str(d.get("latest_version") or ""),
            has_updates=bool(d.get("has_updates")),
            html_url=str(d.get("html_url") or ""),
            releases=len(d.get("releases") or []),
        )

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class LockEntry:
    policy: str = ""
    reason: str = ""
    compatibility: tuple[str, ...] = ()


def strip_tag(image: str) -> str:
    """``ghcr.io/a/b:1.2@sha256:…`` → ``ghcr.io/a/b``, keeping a registry port."""
    image = image.split("@")[0]
    prefix, _, last = image.rpartition("/")
    last = last.split(":")[0]
    return f"{prefix}/{last}" if prefix else last


def image_key(image: str) -> str:
    """The versions.yaml key for an image: no tag, digest or docker.io prefix."""
    name = strip_tag(image)
    for prefix in ("docker.io/", "index.docker.io/"):
        name = name.removeprefix(prefix)
    return name.removeprefix("library/")


def load_lock(path: Path | None) -> dict[str, LockEntry]:
    if not path or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    entries = {}
    for name, raw in (data.get("images") or {}).items():
        raw = raw or {}
        entries[image_key(str(name))] = LockEntry(
            policy=str(raw.get("policy") or ""),
            reason=str(raw.get("reason") or ""),
            compatibility=tuple(str(c) for c in raw.get("compatibility") or ()),
        )
    return entries


def card_title(s: Service) -> str:
    return f"{s.name} {s.current_version} → {s.latest_version}"


def card_description(s: Service, lock: LockEntry | None) -> str:
    e = html.escape
    lines = [
        f"Service: {e(s.name)}",
        f"Image: {e(strip_tag(s.image))}" if s.image else None,
        f"Current version: {e(s.current_version)}",
        f"{TARGET_LINE} {e(s.latest_version)}",
        f'Releases in between: {s.releases} (<a href="{e(s.html_url)}">{e(s.full_repo)} releases</a>)',
    ]
    if lock and lock.policy:
        lines.append(f"Policy: {e(lock.policy)}" + (f" ({e(lock.reason)})" if lock.reason else ""))
    if lock and lock.compatibility:
        lines.append("Check that these still work: " + ", ".join(e(c) for c in lock.compatibility))
    lines.append(f"{REPO_LINE} {e(s.full_repo)}")
    return "".join(f"<p>{line}</p>" for line in lines if line)


@dataclass
class Plan:
    create: list[Service] = field(default_factory=list)
    retarget: list[tuple[Card, Service]] = field(default_factory=list)
    close: list[tuple[Card, Service]] = field(default_factory=list)


def plan(services: list[Service], cards: list[Card], lock: dict[str, LockEntry], listed_only: bool = False) -> Plan:
    """Decide what to do. Pure, so it can be tested without Vikunja."""
    out = Plan()
    by_repo: dict[str, list[Card]] = {}
    for card in cards:
        if card.repo:
            by_repo.setdefault(card.repo, []).append(card)

    for s in services:
        entry = lock.get(image_key(s.image)) if s.image else None
        if entry and entry.policy == "float":
            continue
        if listed_only and entry is None:
            continue
        existing = by_repo.get(s.full_repo, [])
        open_cards = [c for c in existing if not c.done]

        if not s.has_updates:
            out.close += [(c, s) for c in open_cards]
            continue
        if any(c.target == s.latest_version for c in existing):
            continue  # already filed for this version, open or closed
        if open_cards:
            out.retarget.append((open_cards[0], s))
        else:
            out.create.append(s)
    return out


async def sync(services: list[Service], api: Vikunja, versions_path: Path | None) -> Plan:
    """Bring the Upgrades project in line with *services*. Returns what it did."""
    lock = load_lock(versions_path)
    todo = plan(services, await api.cards(), lock, listed_only=versions_path is not None)
    for s in todo.create:
        await api.create(card_title(s), card_description(s, lock.get(image_key(s.image))))
        logger.info("Filed a card for %s %s", s.name, s.latest_version)
    for card, s in todo.retarget:
        await api.update(card, title=card_title(s), description=card_description(s, lock.get(image_key(s.image))))
        await api.comment(card, f"@claude {s.latest_version} is out now. Please assess the upgrade to it instead.")
        logger.info("Moved card %s to %s %s", card.id, s.name, s.latest_version)
    for card, s in todo.close:
        await api.update(card, done=True)
        await api.comment(card, f"Closed by the upgrades board: {s.name} is on {s.current_version} now.")
        logger.info("Closed card %s: %s is up to date", card.id, s.name)
    return todo
