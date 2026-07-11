"""App factory: routers, auth middleware, SPA static mount (work package A1)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import actions, auth, inventory, models, streams, tags
from .integrations import overview as overview_routes
from .integrations import updates as updates_routes

APP_VERSION = "0.1.0"

# Baked into the image by the Dockerfile; overridable for dev.
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))


def create_app() -> FastAPI:
    app = FastAPI(title="Alpenglow Dashboard", version=APP_VERSION)

    app.add_middleware(auth.AuthMiddleware)

    @app.get("/api/health")
    async def health() -> models.Health:
        return models.Health(status="ok", version=APP_VERSION)

    app.include_router(auth.router)
    app.include_router(inventory.router)
    app.include_router(tags.router)
    app.include_router(actions.router)
    app.include_router(streams.router)
    app.include_router(overview_routes.router)
    app.include_router(updates_routes.router)

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file() and candidate.resolve().is_relative_to(STATIC_DIR.resolve()):
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
