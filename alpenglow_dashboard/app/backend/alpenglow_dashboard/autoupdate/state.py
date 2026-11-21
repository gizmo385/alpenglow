"""What automatic updates remember between runs and restarts.

One JSON file, ``/data-store/autoupdate.json`` (``AUTO_UPDATE_STATE``), keyed
by unit id::

    {"units": {"image:freshrss/freshrss": {
        "last_run": "2026-11-20T04:00:00+00:00",   # last scheduled check
        "waiting": "Claude rated 1.31.0 Needs care", # why nothing went out
        "paused": {"version": "1.30.5", "reason": "...", "at": "..."},
        "last_result": {"at": "...", "ok": true, "text": "1.30.4 → 1.30.5"},
        "announced": "1.30.5"                       # dry run: said so once
    }}}

Written whole to a temporary file and renamed, so a crash never leaves half a
file. Unreadable or missing means a fresh start: every unit waits one full
interval before its first check.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("alpenglow.autoupdate")


def state_path() -> Path:
    return Path(os.environ.get("AUTO_UPDATE_STATE", "/data-store/autoupdate.json"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: Any) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class State:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self.data.setdefault("units", {})

    @classmethod
    def load(cls, path: Optional[Path] = None) -> State:
        path = path or state_path()
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as err:
            log.warning("autoupdate: couldn't read %s (%s); starting afresh", path, err)
            data = {}
        return cls(path, data)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            tmp.replace(self.path)
        except OSError as err:
            log.error("autoupdate: couldn't save %s: %s", self.path, err)

    def unit(self, unit_id: str) -> dict:
        return self.data["units"].setdefault(unit_id, {})

    def get(self, unit_id: str) -> dict:
        return self.data["units"].get(unit_id) or {}

    # convenience accessors

    def last_run(self, unit_id: str) -> Optional[datetime]:
        return from_iso(self.get(unit_id).get("last_run"))

    def paused(self, unit_id: str) -> Optional[dict]:
        return self.get(unit_id).get("paused")

    def pause(self, unit_id: str, version: str, reason: str, at: datetime) -> None:
        self.unit(unit_id)["paused"] = {"version": version, "reason": reason, "at": iso(at)}

    def resume(self, unit_id: str) -> bool:
        return self.unit(unit_id).pop("paused", None) is not None

    def result(self, unit_id: str, ok: bool, text: str, at: datetime) -> None:
        self.unit(unit_id)["last_result"] = {"at": iso(at), "ok": ok, "text": text}
