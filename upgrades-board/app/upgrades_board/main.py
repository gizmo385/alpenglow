"""HTTP API and the sync loop.

    GET  /annotations  open Upgrades cards with Claude's verdicts (cached 60s)
    POST /sync         sync the cards with the tracker now
    GET  /health
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx
from fastapi import FastAPI, HTTPException

from . import cards, verdicts
from .config import Settings
from .vikunja import Vikunja

logger = logging.getLogger("upgrades_board")
ANNOTATIONS_TTL = 60


class Board:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client
        self.api = Vikunja(settings.vikunja_url, settings.token, settings.project_id, client)
        self._lock = asyncio.Lock()
        self._annotations: tuple[float, dict] | None = None

    async def tracker_services(self) -> list[cards.Service]:
        resp = await self.client.get(f"{self.s.tracker_url}/api/updates", timeout=30)
        resp.raise_for_status()
        return [cards.Service.from_api(d) for d in resp.json().get("services") or []]

    async def sync(self) -> cards.Plan:
        async with self._lock:
            done = await cards.sync(await self.tracker_services(), self.api, self.s.versions_path)
            self._annotations = None
            return done

    async def annotations(self) -> dict:
        cached = self._annotations
        if cached and time.monotonic() - cached[0] < ANNOTATIONS_TTL:
            return cached[1]
        data = verdicts.payload(await verdicts.fetch(self.api, self.s.public_url))
        self._annotations = (time.monotonic(), data)
        return data


async def _loop(board: Board) -> None:
    await asyncio.sleep(30)  # let the tracker finish its first scan
    while True:
        try:
            todo = await board.sync()
            logger.info("synced: %d filed, %d retargeted, %d closed", len(todo.create), len(todo.retarget), len(todo.close))
        except Exception:
            logger.exception("sync failed; trying again next interval")
        await asyncio.sleep(board.s.sync_interval)


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None, run_loop: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or Settings.from_env()
        http = client or httpx.AsyncClient()
        app.state.board = Board(s, http)
        task = asyncio.create_task(_loop(app.state.board)) if run_loop else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            if client is None:
                await http.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/annotations")
    async def annotations() -> dict:
        try:
            return await app.state.board.annotations()
        except httpx.HTTPError as err:
            logger.warning("couldn't read the board: %s", err)
            raise HTTPException(502, "couldn't read the Upgrades board") from err

    @app.post("/sync")
    async def sync_now() -> dict:
        try:
            todo = await app.state.board.sync()
        except httpx.HTTPError as err:
            raise HTTPException(502, f"sync failed: {err}") from err
        return {"filed": len(todo.create), "retargeted": len(todo.retarget), "closed": len(todo.close)}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = create_app()
