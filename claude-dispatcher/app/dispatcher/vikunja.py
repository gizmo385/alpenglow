"""The few Vikunja API calls the dispatcher makes."""

from __future__ import annotations

import html

import httpx


def to_html(text: str) -> str:
    """Render Claude's reply as escaped HTML paragraphs.

    Claude's output is never trusted as markup: it may echo text from web
    pages it read. Escaping keeps it as text in the comment.
    """
    paras = [p for p in text.strip().split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras) or "<p>(empty)</p>"


class Vikunja:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}

    async def comment(self, task_id: int, text: str) -> None:
        resp = await self.client.put(
            f"{self.base}/tasks/{task_id}/comments",
            json={"comment": to_html(text)},
            headers=self.headers,
        )
        resp.raise_for_status()

    async def comments(self, task_id: int) -> list[dict]:
        resp = await self.client.get(f"{self.base}/tasks/{task_id}/comments", headers=self.headers)
        resp.raise_for_status()
        return resp.json() or []
