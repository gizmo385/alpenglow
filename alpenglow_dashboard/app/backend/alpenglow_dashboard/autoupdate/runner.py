"""Run automatic updates on each unit's schedule.

Every minute the loop looks for units whose ``every`` has come round. For
each one it reads the tracker and the Upgrades board, and either finds a
release that qualifies (:func:`policy.blocker`) or records why not. Then, by
``AUTO_UPDATE``:

- ``off``: nothing runs;
- ``dry-run`` (the default): a release that would go out is announced on
  Discord once, and nothing changes;
- ``on``: it goes out. Units that share a service directory go together:
  repin (``./pin set``, as the Update button does), then ``docker compose
  pull`` and ``up -d`` for just the compose services using those images, then
  the health check (:mod:`health`).

A failure is never rolled back: the release's database migrations may already
have run, and with a shared Postgres that's messy to undo. The unit is paused
instead, with the reason, until someone presses Resume; the card gets a
comment and the Discord summary says what failed.

With ``AUTO_UPDATE_COMMIT=1``, a successful update commits versions.yaml and
the compose files pin rewrote in the server's checkout (never pushed).
Otherwise they're left uncommitted, like the Update button's.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .. import actions, inventory, mock, repin
from ..upgrades.verdicts import VERDICTS
from . import health, policy
from .state import State, from_iso, iso, now as _now

log = logging.getLogger("alpenglow.autoupdate")

MODES = ("off", "dry-run", "on")
USER = "auto-update"
TITLE = "Docker Services — Automatic Updates"
TICK = 60.0
TRACKER_RETRY = timedelta(minutes=15)
_BLUE = 0x3498DB

_run_lock: Optional[asyncio.Lock] = None


def run_lock() -> asyncio.Lock:
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    return _run_lock


def mode() -> str:
    if mock.mock_enabled():
        return "off"
    value = (os.environ.get("AUTO_UPDATE") or "dry-run").strip().lower()
    if value not in MODES:
        log.error("autoupdate: AUTO_UPDATE=%r isn't one of %s; staying off", value, ", ".join(MODES))
        return "off"
    return value


def default_every() -> policy.Schedule:
    try:
        return policy.Schedule.parse(os.environ.get("AUTO_UPDATE_EVERY") or "daily")
    except policy.ConfigError as err:
        log.error("autoupdate: AUTO_UPDATE_EVERY: %s; using daily", err)
        return policy.Schedule.parse("daily")


def commit_enabled() -> bool:
    return (os.environ.get("AUTO_UPDATE_COMMIT") or "").strip().lower() in ("1", "true", "yes", "on")


def versions_path() -> Path:
    return repin.repo_root() / repin.LOCK_NAME


def load() -> policy.Loaded:
    return policy.load(versions_path(), default_every())


def next_run(unit: policy.Unit, last: datetime) -> datetime:
    """When ``unit`` is next due. Cron lines are read in the server's time zone."""
    if unit.config.every.cron is None:
        return unit.config.every.next_after(last)
    local = last.astimezone().replace(tzinfo=None)
    return unit.config.every.next_after(local).astimezone()


# ── evaluating a unit ─────────────────────────────────────────────────────────


@dataclass
class Plan:
    unit: policy.Unit
    places: list[tuple[str, str]] = field(default_factory=list)  # (service dir, compose service)
    targets: list[repin.Target] = field(default_factory=list)
    pending: list[policy.Pending] = field(default_factory=list)
    card_ids: list[int] = field(default_factory=list)
    hold: Optional[policy.Hold] = None

    @property
    def blocker(self) -> Optional[str]:
        return self.hold.text if self.hold else None

    @property
    def ready(self) -> bool:
        return self.blocker is None and bool(self.unit.follow or self.targets)

    @property
    def version_key(self) -> str:
        return "follow" if self.unit.follow else ",".join(f"{p.image}={p.latest}" for p in self.pending)

    @property
    def versions_text(self) -> str:
        if len(self.pending) == 1:
            return f"{self.pending[0].current} → {self.pending[0].latest}"
        return ", ".join(f"{p.image.rsplit('/', 1)[-1]} {p.current} → {p.latest}" for p in self.pending)


def _places(images: tuple[str, ...]) -> list[tuple[str, str]]:
    keys = {repin.image_key(i) for i in images}
    out = []
    for sid, repo in sorted(inventory.scan_repo().items()):
        for svc_name, svc in (repo.services or {}).items():
            if isinstance(svc, dict) and svc.get("image") and repin.image_key(str(svc["image"])) in keys:
                out.append((sid, str(svc_name)))
    return out


def _release_time(entry: dict) -> Optional[datetime]:
    latest = str(entry.get("latest_version") or "")
    releases = [r for r in entry.get("releases") or [] if isinstance(r, dict)]
    match = next((r for r in releases if str(r.get("tag")) == latest), None)
    if match is None and releases:
        match = max(releases, key=lambda r: str(r.get("published_at") or ""))
    return policy.parse_time(match.get("published_at")) if match else None


def evaluate(unit: policy.Unit, payload: dict, annotations: dict, pins: dict, state: State,
             now: datetime) -> Plan:
    """What updating ``unit`` now would do, or why it can't."""
    plan = Plan(unit, places=_places(unit.images))
    paused = state.paused(unit.id)
    if paused:
        plan.hold = policy.Hold(
            "paused", f"paused after a failed update ({paused.get('reason')}); press Resume once it's sorted")
        return plan
    if not plan.places:
        plan.hold = policy.Hold("config", "no compose file uses this image")
        return plan
    if any(sid == actions.SELF_SERVICE and svc == "dashboard" for sid, svc in plan.places):
        plan.hold = policy.Hold("config", "the dashboard can't update itself automatically")
        return plan
    if unit.follow:
        return plan

    keys = {repin.image_key(i): i for i in unit.images}
    for entry in payload.get("services") or []:
        if not isinstance(entry, dict) or not entry.get("has_updates"):
            continue
        name = keys.get(repin.image_key(str(entry.get("image") or "")))
        if name is None:
            continue
        target = repin.target_for(entry, pins)
        if target is None:
            continue  # already pinned to it; waiting for the tracker to rescan
        ann = annotations.get(f"{entry.get('owner')}/{entry.get('repo')}".lower())
        verdict = getattr(ann, "group", None)
        plan.targets.append(target)
        plan.pending.append(policy.Pending(
            image=name,
            current=str(entry.get("current_version") or target.current_tag),
            latest=str(entry["latest_version"]),
            published_at=_release_time(entry),
            verdict=verdict if verdict in VERDICTS else None,  # Hold too, not "Waiting"
            verdict_target=getattr(ann, "target", None),
        ))
        if ann is not None and getattr(ann, "card_id", None):
            plan.card_ids.append(ann.card_id)
    for p in plan.pending:
        plan.hold = policy.hold(p, unit.config, now)
        if plan.hold:
            break
    return plan


def first_run_after(unit: policy.Unit, last: Optional[datetime], when: datetime) -> Optional[datetime]:
    """The first scheduled check at or after ``when``, counting from ``last``."""
    if last is None:
        return None
    t = next_run(unit, last)
    for _ in range(5000):
        if t >= when:
            return t
        t = next_run(unit, t)
    return None


# ── the scheduled pass ────────────────────────────────────────────────────────


async def tick(now: Optional[datetime] = None, *, force: bool = False, only: Optional[str] = None) -> list[Plan]:
    """Check every unit that's due (or every unit, with ``force``)."""
    from .. import upgrades
    from ..integrations import updates

    async with run_lock():
        now = now or _now()
        loaded = load()
        state = State.load()
        due = []
        for unit in loaded.units:
            if only and unit.id != only:
                continue
            entry = state.unit(unit.id)
            last = state.last_run(unit.id)
            retry = entry.get("retry_at")
            if force:
                due.append(unit)
            elif retry:  # the last check couldn't read the tracker
                if now >= (from_iso(retry) or now):
                    due.append(unit)
            elif last is None:
                entry["last_run"] = iso(now)  # first sighting: wait one full interval
            elif now >= next_run(unit, last):
                due.append(unit)
        if not due:
            state.save()
            return []

        payload = await updates._fetch_tracker()
        if payload is None:
            log.warning("autoupdate: tracker unavailable; trying again in %s", TRACKER_RETRY)
            for unit in due:
                state.unit(unit.id).update(waiting="the update tracker is unavailable",
                                           retry_at=iso(now + TRACKER_RETRY))
            state.save()
            return []
        annotations = await upgrades.annotations_by_repo()
        pins = repin.load_pins(repin.repo_root())
        plans = [evaluate(unit, payload, annotations, pins, state, now) for unit in due]
        for p in plans:
            entry = state.unit(p.unit.id)
            entry["last_run"] = iso(now)
            entry.pop("retry_at", None)
            if p.blocker:
                entry["waiting"] = p.blocker
            elif not p.ready:
                entry["waiting"] = "up to date"
            else:
                entry.pop("waiting", None)
        state.save()

        ready = [p for p in plans if p.ready]
        current = mode()
        if ready and current == "dry-run":
            await announce(ready, state)
        elif ready and current == "on":
            await execute(ready, state)
        state.save()
        return plans


async def announce(plans: list[Plan], state: State) -> None:
    """Dry run: say once what would have gone out."""
    from ..integrations import update_summary, updates

    lines = []
    for p in plans:
        entry = state.unit(p.unit.id)
        if entry.get("announced") == p.version_key:
            continue
        entry["announced"] = p.version_key
        services = sorted({sid for sid, _ in p.places})
        name = ", ".join(updates.display_name(s) for s in services)
        if p.unit.follow:
            lines.append(f"🔎 **{name}** would pull new `{p.unit.label}` images ({p.unit.config.every.text})")
        else:
            lines.append(f"🔎 **{name}** would update {p.versions_text}")
    if lines:
        await update_summary.post_embed({
            "title": f"{TITLE} (dry run)",
            "color": _BLUE,
            "description": "\n".join(lines)[:4096],
            "footer": {"text": "Nothing was changed · set AUTO_UPDATE=on to let these go out"},
        })


def _batches(plans: list[Plan]) -> list[list[Plan]]:
    """Plans that touch a common service directory go out together."""
    batches: list[tuple[set[str], list[Plan]]] = []
    for p in plans:
        sids = {sid for sid, _ in p.places}
        joined = [b for b in batches if b[0] & sids]
        merged = (sids.union(*(b[0] for b in joined)), [q for b in joined for q in b[1]] + [p])
        batches = [b for b in batches if b not in joined] + [merged]
    return [b[1] for b in batches]


async def execute(plans: list[Plan], state: State) -> None:
    from ..integrations import update_summary, updates

    results: list[update_summary.Result] = []
    for batch in _batches(plans):
        results += await run_batch(batch, state)
        state.save()
    if results:
        await update_summary.post(results, title=TITLE)
    if any(r.ok for r in results):
        await updates.rescan()


class _Failed(Exception):
    pass


async def _release(sid: str, job: str, outcome: str) -> None:
    from .. import audit

    async with actions._BUSY_LOCK:
        actions._BUSY.pop(sid, None)
    await audit.record(USER, sid, "pull", outcome, job_id=job)


async def run_batch(batch: list[Plan], state: State) -> list:
    """Update one batch; return a summary result per plan that changed or failed."""
    from .. import upgrades
    from ..integrations import update_summary, updates

    sids = sorted({sid for p in batch for sid, _ in p.places})
    jobs: dict[str, str] = {}
    for sid in sids:
        job = await actions._register(sid, "pull", USER)
        if job is None:
            for s, j in jobs.items():
                await _release(s, j, "skipped")
            for p in batch:
                state.unit(p.unit.id)["waiting"] = f"{sid} was busy; trying again next run"
            return []
        jobs[sid] = job

    follow_places = [key for p in batch if p.unit.follow for key in p.places]
    pinned: list[tuple[repin.Target, str]] = []
    files: list[str] = []
    changed: list[tuple[str, str]] = []
    ok, detail = False, ""
    try:
        targets = [t for p in batch for t in p.targets]
        if targets:
            async with repin.repo_lock():
                for t in targets:
                    tag, rewritten = await repin.pin_with_files(t)
                    pinned.append((t, tag))
                    files += rewritten
        before = {key: await health.inspect(*key) for key in follow_places}
        for sid in sids:
            services = sorted({svc for p in batch for s, svc in p.places if s == sid})
            async with actions._get_semaphore(), actions._service_lock(sid):
                good, err = await actions._run_commands(sid, "pull", services)
            if not good:
                raise _Failed(f"docker compose failed in {sid}: {err.strip()[-300:] or 'no output'}")
        for p in batch:
            if not p.unit.follow:
                changed += p.places
                continue
            for key in p.places:
                after = await health.inspect(*key)
                if before.get(key) is None or after is None or after.image_id != before[key].image_id:
                    changed.append(key)
        monitors = tuple(dict.fromkeys(
            m for p in batch if any(k in changed for k in p.places) for m in p.unit.config.monitors
        ))
        if changed:
            reason = await health.check(changed, monitors)
            if reason:
                raise _Failed(reason)
        ok = True
    except repin.RepinError as err:
        detail = str(err)
    except _Failed as err:
        detail = str(err)
    except Exception as exc:  # noqa: BLE001 - every failure pauses and is reported
        log.exception("autoupdate: batch %s failed", sids)
        detail = f"{type(exc).__name__}: {exc}"
    if not ok and pinned:
        detail += "\n[update] " + actions._pins_note(pinned) + "; not rolled back"
    for sid, job in jobs.items():
        await actions._finish(sid, "pull", USER, job, ok, detail)
    note = ""
    if ok and pinned and commit_enabled():
        try:
            note = await commit(batch, files) or ""
        except Exception as exc:  # noqa: BLE001 - the update itself worked
            note = f"[update] couldn't commit the new pins: {type(exc).__name__}: {exc}"
        if note:
            log.warning("autoupdate: %s", note)
    first_line = (detail.splitlines() or ["the update failed"])[0]

    when = _now()
    results = []
    for p in batch:
        touched = any(k in changed for k in p.places)
        if p.unit.follow and ok and not touched:
            continue  # nothing new on the tag
        text = p.versions_text if not p.unit.follow else f"new {p.unit.label} image"
        name = ", ".join(updates.display_name(s) for s in sorted({s for s, _ in p.places}))
        state.result(p.unit.id, ok, text if ok else f"{text}: {first_line}", when)
        if ok:
            state.unit(p.unit.id).pop("waiting", None)
        else:
            state.pause(p.unit.id, p.version_key, first_line, when)
            for card in p.card_ids:
                await upgrades.comment(card, f"Automatic update to {text} failed and was not rolled back: "
                                             f"{first_line}. Automatic updates for this are paused.")
        results.append(update_summary.Result(service=p.places[0][0], name=name, user=USER, ok=ok,
                                             detail=(detail or note), versions=[text]))
    return results


# ── committing ────────────────────────────────────────────────────────────────


def _author() -> tuple[str, str]:
    raw = os.environ.get("AUTO_UPDATE_GIT_AUTHOR") or "Alpenglow auto-update <auto-update@alpenglow.local>"
    name, _, email = raw.partition("<")
    return name.strip() or "Alpenglow auto-update", email.rstrip(">").strip() or "auto-update@alpenglow.local"


def commit_message(batch: list[Plan]) -> str:
    from ..integrations import updates

    lines = []
    for p in batch:
        if p.unit.follow:
            continue
        name = ", ".join(updates.display_name(s) for s in sorted({s for s, _ in p.places}))
        lines.append(f"{name} {p.versions_text}")
    subject = "auto-update: " + "; ".join(lines)
    if len(subject) <= 72:
        return subject
    return "auto-update: " + f"{len(lines)} services\n\n" + "\n".join(f"- {line}" for line in lines)


async def commit(batch: list[Plan], files: list[str]) -> Optional[str]:
    """Commit versions.yaml and ``files`` in the server's checkout; the problem, if any."""
    name, email = _author()
    root = repin.repo_root()
    # safe.directory: .git may belong to someone other than versions.yaml's owner.
    argv = ["git", "-c", f"safe.directory={root}", "-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "--quiet", "-m", commit_message(batch), "--", repin.LOCK_NAME, *dict.fromkeys(files)]
    async with repin.repo_lock():
        rc, out = await repin._run(argv, root, 60)
    if rc != 0:
        return f"[update] couldn't commit the new pins: {(out.splitlines() or ['git failed'])[-1]}"
    return None


# ── the loop ──────────────────────────────────────────────────────────────────


async def loop() -> None:
    await asyncio.sleep(45)  # after the tracker's first scan and the board's first sync
    errors: list[str] = []
    while True:
        try:
            if mode() != "off":
                loaded = load()
                if loaded.errors != errors:
                    errors = loaded.errors
                    for e in errors:
                        log.error("autoupdate: versions.yaml: %s", e)
                await tick()
        except Exception:  # noqa: BLE001
            log.exception("autoupdate: pass failed; trying again next minute")
        await asyncio.sleep(TICK)
