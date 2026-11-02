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

    # ── Kanban columns and labels ─────────────────────────────────────────

    async def move(self, project_id: int, task_id: int, bucket_title: str) -> bool:
        """Move a task to the bucket with this title in the project's Kanban view.

        Returns False (and changes nothing) when the project has no Kanban view
        or no bucket by that name.
        """
        buckets = await self._buckets(project_id)
        if buckets is None or bucket_title.casefold() not in buckets:
            return False
        view_id, bucket_id = buckets[bucket_title.casefold()]
        resp = await self.client.post(
            f"{self.base}/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
            json={"task_id": task_id},
            headers=self.headers,
        )
        resp.raise_for_status()
        return True

    async def _buckets(self, project_id: int) -> dict[str, tuple[int, int]] | None:
        resp = await self.client.get(f"{self.base}/projects/{project_id}/views", headers=self.headers)
        resp.raise_for_status()
        kanban = next((v for v in resp.json() or [] if v.get("view_kind") == "kanban"), None)
        if kanban is None:
            return None
        resp = await self.client.get(
            f"{self.base}/projects/{project_id}/views/{kanban['id']}/buckets", headers=self.headers
        )
        resp.raise_for_status()
        return {str(b["title"]).casefold(): (kanban["id"], b["id"]) for b in resp.json() or []}

    async def set_label(self, task_id: int, title: str, replace_prefix: str, color: str = "") -> None:
        """Give the task the label *title*, removing its other labels that start with *replace_prefix*."""
        resp = await self.client.get(f"{self.base}/tasks/{task_id}/labels", headers=self.headers)
        resp.raise_for_status()
        have = resp.json() or []
        for label in have:
            if label["title"].startswith(replace_prefix) and label["title"] != title:
                r = await self.client.delete(f"{self.base}/tasks/{task_id}/labels/{label['id']}", headers=self.headers)
                r.raise_for_status()
        if any(label["title"] == title for label in have):
            return
        label_id = await self._label_id(title, color)
        r = await self.client.put(f"{self.base}/tasks/{task_id}/labels", json={"label_id": label_id}, headers=self.headers)
        r.raise_for_status()

    async def _label_id(self, title: str, color: str) -> int:
        resp = await self.client.get(f"{self.base}/labels", params={"s": title}, headers=self.headers)
        resp.raise_for_status()
        for label in resp.json() or []:
            if label["title"] == title:
                return label["id"]
        resp = await self.client.put(f"{self.base}/labels", json={"title": title, "hex_color": color}, headers=self.headers)
        resp.raise_for_status()
        return resp.json()["id"]
