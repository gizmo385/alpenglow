"""The Vikunja API calls the Upgrades board makes."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import httpx

# Lines in a card's description that tie it to a repo and a version.
REPO_LINE = "Tracked repo:"
TARGET_LINE = "Version to assess:"
_REPO_RE = re.compile(rf"{re.escape(REPO_LINE)}\s*([\w.-]+/[\w.-]+)")
_TARGET_RE = re.compile(rf"{re.escape(TARGET_LINE)}\s*(\S+?)\s*(?:<|$)", re.M)


@dataclass
class Card:
    id: int
    title: str
    description: str
    done: bool
    repo: str | None = None
    target: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, task: dict) -> Card:
        desc = task.get("description") or ""
        repo = _REPO_RE.search(desc)
        target = _TARGET_RE.search(desc)
        return cls(
            id=task["id"],
            title=task.get("title") or "",
            description=desc,
            done=bool(task.get("done")),
            repo=repo.group(1) if repo else None,
            target=html.unescape(target.group(1)) if target else None,
            raw=task,
        )


class Vikunja:
    def __init__(self, base_url: str, token: str, project_id: int, client: httpx.AsyncClient):
        self.base = base_url.rstrip("/")
        self.project_id = project_id
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        resp = await self.client.request(method, f"{self.base}{path}", headers=self.headers, timeout=30, **kw)
        resp.raise_for_status()
        return resp

    async def cards(self) -> list[Card]:
        cards: list[Card] = []
        page = 1
        while True:
            resp = await self._req("GET", f"/projects/{self.project_id}/tasks", params={"page": page, "per_page": 250})
            cards += [Card.from_api(t) for t in resp.json() or []]
            if page >= int(resp.headers.get("x-pagination-total-pages", "1")):
                return cards
            page += 1

    async def create(self, title: str, description: str) -> None:
        await self._req("PUT", f"/projects/{self.project_id}/tasks", json={"title": title, "description": description})

    async def update(self, card: Card, **changes) -> None:
        # Vikunja's update replaces the whole task, so send it back in full.
        await self._req("POST", f"/tasks/{card.id}", json={**card.raw, **changes})

    async def comment(self, card: Card, text: str) -> None:
        await self._req("PUT", f"/tasks/{card.id}/comments", json={"comment": f"<p>{html.escape(text)}</p>"})

    async def comments(self, card_id: int) -> list[dict]:
        return (await self._req("GET", f"/tasks/{card_id}/comments")).json() or []
