"""Settings from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    tracker_url: str
    vikunja_url: str
    token: str
    project_id: int
    public_url: str | None = None
    versions_path: Path | None = None
    sync_interval: int = 1800

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [k for k in ("TRACKER_URL", "VIKUNJA_API_URL", "VIKUNJA_API_TOKEN", "VIKUNJA_PROJECT_ID") if not env.get(k)]
        if missing:
            raise SystemExit(f"missing settings: {', '.join(missing)}")
        versions = env.get("VERSIONS_PATH")
        return cls(
            tracker_url=env["TRACKER_URL"].rstrip("/"),
            vikunja_url=env["VIKUNJA_API_URL"].rstrip("/"),
            token=env["VIKUNJA_API_TOKEN"],
            project_id=int(env["VIKUNJA_PROJECT_ID"]),
            public_url=(env.get("VIKUNJA_PUBLIC_URL") or "").rstrip("/") or None,
            versions_path=Path(versions) if versions else None,
            sync_interval=int(env.get("SYNC_INTERVAL") or 1800),
        )
