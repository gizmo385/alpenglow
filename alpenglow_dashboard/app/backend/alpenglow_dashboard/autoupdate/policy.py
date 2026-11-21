"""Which images update themselves, how often, and whether a release qualifies.

Pure: no Docker, no network, so every rule here is tested directly.

An image opts in with ``policy: auto-eligible`` in ``versions.yaml``, plus an
optional ``auto:`` block::

    freshrss/freshrss:
      tag: 1.30.4
      digest: sha256:...
      policy: auto-eligible
      auto:
        every: daily        # hourly | daily | weekly | 6h | 2d | "0 4 * * *"
        max_bump: minor     # patch (default) | minor | major
        soak: 3d            # how long a release must be out first (default 2d)
        verdict: Easy       # Claude's verdict needed: Easy (default) | Needs care | any
        monitor: FreshRSS   # Uptime Kuma monitor(s) that must be up afterwards

A ``group`` (images that always move together) is one unit: it updates only
if every member opts in, and its settings are the most cautious of its
members'. A ``policy: float`` entry built from a moving tag can instead set
``auto: {follow: true}``: it is pulled on schedule, and updated when the tag
points somewhere new. An image that publishes only a moving tag plus per-build
tags is pinned to a per-build tag (``sha-5c5381c``) with ``auto: {follow:
latest}``: each update repins it to the per-build tag ``latest`` points at
(``pin resolve``). Neither kind has a version to judge, so verdict, soak and
max_bump don't apply.

``tools/pin`` validates the same block (``pin.lock.auto_problems``), so CI
catches a typo; keep the two in step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

BUMPS = ("patch", "minor", "major")
# Strictest first. "Needs care" accepts Easy too.
VERDICT_LEVELS = ("Easy", "Needs care", "any")
DEFAULT_MAX_BUMP = "patch"
DEFAULT_SOAK = timedelta(days=2)
DEFAULT_VERDICT = "Easy"
MIN_INTERVAL = timedelta(minutes=15)

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_WORDS = {"hourly": "1h", "daily": "1d", "weekly": "1w"}


class ConfigError(ValueError):
    pass


def parse_duration(value: Any) -> timedelta:
    m = _DURATION_RE.match(str(value)) if value is not None else None
    if not m:
        raise ConfigError(f"{value!r} isn't a duration like 30m, 6h, 2d or 1w")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


# ── schedules ─────────────────────────────────────────────────────────────────


def _cron_field(text: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ConfigError(f"bad step in {text!r}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(rng)
            end = hi if step_s else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ConfigError(f"{text!r} is out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Schedule:
    """``every``: a fixed interval, or a 5-field cron line in the server's time zone."""

    text: str
    interval: Optional[timedelta] = None
    cron: Optional[tuple[frozenset[int], ...]] = None  # minute hour dom month dow
    cron_dom_any: bool = True
    cron_dow_any: bool = True

    @classmethod
    def parse(cls, value: Any) -> Schedule:
        text = str(value).strip()
        word = _WORDS.get(text.lower())
        if word or _DURATION_RE.match(text):
            interval = parse_duration(word or text)
            if interval < MIN_INTERVAL:
                raise ConfigError(f"every {text!r} is more often than every 15m")
            return cls(text=text, interval=interval)
        fields = text.split()
        if len(fields) != 5:
            raise ConfigError(f"every {text!r} isn't hourly/daily/weekly, a duration, or a cron line")
        try:
            minute, hour, dom, month, dow = (
                _cron_field(fields[0], 0, 59), _cron_field(fields[1], 0, 23),
                _cron_field(fields[2], 1, 31), _cron_field(fields[3], 1, 12),
                _cron_field(fields[4], 0, 7),
            )
        except ValueError as err:
            raise ConfigError(f"every {text!r}: {err}") from err
        if 7 in dow:  # cron allows both 0 and 7 for Sunday
            dow = dow | {0}
        return cls(text=text, cron=(minute, hour, dom, month, dow),
                   cron_dom_any=fields[2] == "*", cron_dow_any=fields[4] == "*")

    def _cron_day(self, d: datetime) -> bool:
        _, _, dom, month, dow = self.cron  # type: ignore[misc]
        if d.month not in month:
            return False
        dom_ok = d.day in dom
        dow_ok = (d.isoweekday() % 7) in dow
        # Classic cron: with both day fields restricted, either may match.
        if not self.cron_dom_any and not self.cron_dow_any:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, last: datetime) -> datetime:
        """The first run after ``last`` (naive local time for cron lines)."""
        if self.interval is not None:
            return last + self.interval
        minute, hour = self.cron[0], self.cron[1]  # type: ignore[index]
        t = last.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 2):  # at most two years of days
            if self._cron_day(t):
                for h in sorted(hour):
                    if h < t.hour:
                        continue
                    for m in sorted(minute):
                        if h == t.hour and m < t.minute:
                            continue
                        return t.replace(hour=h, minute=m)
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        raise ConfigError(f"every {self.text!r} never matches")


# ── versions ──────────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_version(version: str) -> Optional[tuple[int, ...]]:
    """``v1.2.3``, ``twenty/v2.56.1`` or ``1.2.3-alpine`` → ``(1, 2, 3)``."""
    m = _VERSION_RE.match(str(version).rsplit("/", 1)[-1].strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def bump_size(current: str, latest: str) -> Optional[str]:
    """``patch``, ``minor`` or ``major``; ``None`` when they can't be compared
    or ``latest`` isn't newer.

    Before 1.0, semver lets a minor release break things (0.21 → 0.22), so a
    0.x minor bump counts as major. A fourth number (``1.0.37.0``) counts as a
    patch.
    """
    cur, new = parse_version(current), parse_version(latest)
    if cur is None or new is None:
        return None
    width = max(len(cur), len(new), 3)
    cur, new = cur + (0,) * (width - len(cur)), new + (0,) * (width - len(new))
    if new <= cur:
        return None
    if new[0] != cur[0]:
        return "major"
    if new[1] != cur[1]:
        return "major" if cur[0] == 0 else "minor"
    return "patch"


# ── configuration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutoConfig:
    every: Schedule
    max_bump: str = DEFAULT_MAX_BUMP
    soak: timedelta = DEFAULT_SOAK
    verdict: str = DEFAULT_VERDICT
    monitors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unit:
    """What updates as one: an image, a group of images, or a followed image."""

    id: str  # "image:<name>", "group:<group>" or "follow:<name>"
    label: str
    images: tuple[str, ...]  # versions.yaml keys, exactly as written there
    config: AutoConfig
    follow: bool = False  # a float entry pulled on schedule
    follow_tag: Optional[str] = None  # a pinned entry that follows this moving tag


@dataclass
class Loaded:
    units: list[Unit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _monitors(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value):
        return tuple(v.strip() for v in value)
    raise ConfigError("monitor must be an Uptime Kuma monitor name or a list of them")


def parse_auto(raw: Any, default_every: Schedule) -> tuple[AutoConfig, bool | str]:
    """(settings, follow) from an entry's ``auto:`` block; follow is a bool or a tag."""
    raw = raw if raw is not None else {}
    if not isinstance(raw, dict):
        raise ConfigError("auto must be a mapping")
    known = {"every", "max_bump", "soak", "verdict", "monitor", "follow"}
    unknown = sorted(set(map(str, raw)) - known)
    if unknown:
        raise ConfigError(f"unknown auto setting(s): {', '.join(unknown)}")
    max_bump = str(raw.get("max_bump", DEFAULT_MAX_BUMP))
    if max_bump not in BUMPS:
        raise ConfigError(f"max_bump must be one of {', '.join(BUMPS)}")
    verdict = str(raw.get("verdict", DEFAULT_VERDICT))
    if verdict not in VERDICT_LEVELS:
        raise ConfigError(f"verdict must be one of {', '.join(VERDICT_LEVELS)}")
    follow = raw.get("follow", False)
    if isinstance(follow, str) and follow.strip():
        follow = follow.strip()
    elif follow not in (True, False):
        raise ConfigError("follow must be true, false or a tag to follow such as latest")
    cfg = AutoConfig(
        every=Schedule.parse(raw["every"]) if "every" in raw else default_every,
        max_bump=max_bump,
        soak=parse_duration(raw["soak"]) if "soak" in raw else DEFAULT_SOAK,
        verdict=verdict,
        monitors=_monitors(raw.get("monitor")),
    )
    return cfg, follow


def _merge(configs: list[tuple[AutoConfig, bool]]) -> AutoConfig:
    """A group's settings: the most cautious of its members'. Its schedule is
    the first member's that sets one."""
    explicit = [c for c, has_every in configs if has_every]
    cs = [c for c, _ in configs]
    return AutoConfig(
        every=(explicit or cs)[0].every,
        max_bump=min((c.max_bump for c in cs), key=BUMPS.index),
        soak=max(c.soak for c in cs),
        verdict=min((c.verdict for c in cs), key=VERDICT_LEVELS.index),
        monitors=tuple(dict.fromkeys(m for c in cs for m in c.monitors)),
    )


def load(path: Optional[Path], default_every: Schedule) -> Loaded:
    """Every unit that ``versions.yaml`` opts in, and what's wrong with the rest."""
    out = Loaded()
    if path is None or not path.exists():
        return out
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as err:
        out.errors.append(f"couldn't read {path.name}: {err}")
        return out
    images = data.get("images") or {}
    groups: dict[str, list[str]] = {}
    for name, raw in images.items():
        raw = raw or {}
        if raw.get("group"):
            groups.setdefault(str(raw["group"]), []).append(str(name))

    seen_groups: set[str] = set()
    for name, raw in images.items():
        name, raw = str(name), raw or {}
        policy = str(raw.get("policy") or "approve")
        try:
            if policy == "float":
                if raw.get("auto") is None:
                    continue
                cfg, follow = parse_auto(raw.get("auto"), default_every)
                if follow is not True:
                    raise ConfigError("a float entry can only have `follow: true`")
                out.units.append(Unit(f"follow:{name}", name, (name,), cfg, follow=True))
                continue
            if policy != "auto-eligible":
                if raw.get("auto") is not None:
                    raise ConfigError(f"auto only applies with policy auto-eligible (this is {policy})")
                continue
            group = str(raw["group"]) if raw.get("group") else None
            if group is None:
                cfg, follow = parse_auto(raw.get("auto"), default_every)
                if follow is True:
                    raise ConfigError("a pinned entry follows a named tag (`follow: latest`)")
                if follow:
                    out.units.append(Unit(f"follow:{name}", name, (name,), cfg, follow_tag=str(follow)))
                else:
                    out.units.append(Unit(f"image:{name}", name, (name,), cfg))
                continue
            if group in seen_groups:
                continue
            seen_groups.add(group)
            members = groups[group]
            holdouts = [m for m in members if str((images[m] or {}).get("policy") or "approve") != "auto-eligible"]
            if holdouts:
                raise ConfigError(
                    f"group {group} doesn't update automatically: {', '.join(holdouts)} isn't auto-eligible"
                )
            configs = []
            for m in members:
                auto = (images[m] or {}).get("auto")
                cfg, follow = parse_auto(auto, default_every)
                if follow:
                    raise ConfigError(f"{m}: follow can't be used in a group")
                configs.append((cfg, isinstance(auto, dict) and "every" in auto))
            out.units.append(Unit(f"group:{group}", group, tuple(members), _merge(configs)))
        except ConfigError as err:
            out.errors.append(f"{name}: {err}")
    return out


# ── does a release qualify? ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Pending:
    """A pending release of one image, from the tracker and the Upgrades board."""

    image: str  # versions.yaml key
    current: str
    latest: str
    published_at: Optional[datetime]
    verdict: Optional[str] = None  # Claude's verdict, for this exact release
    verdict_target: Optional[str] = None  # the release the card was assessed for


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Hold:
    """Why a release isn't going out automatically yet.

    ``kind`` lets the Updates view say it briefly: ``bump`` (too big),
    ``soak`` (out until ``until``), ``assessment`` (no verdict for it yet),
    ``verdict`` (rated too risky), ``unknown`` (versions or date unreadable),
    ``paused`` and ``config``.
    """

    kind: str
    text: str
    until: Optional[datetime] = None


def hold(p: Pending, cfg: AutoConfig, now: datetime) -> Optional[Hold]:
    """Why this release can't go out automatically yet; ``None`` when it can."""
    size = bump_size(p.current, p.latest)
    if size is None:
        return Hold("unknown", f"can't tell how big {p.current} → {p.latest} is")
    if BUMPS.index(size) > BUMPS.index(cfg.max_bump):
        return Hold("bump", f"{p.current} → {p.latest} is a {size} update; only {cfg.max_bump} updates are automatic")
    if cfg.soak and p.published_at is None:
        return Hold("unknown", f"no release date for {p.latest}, so its soak time can't be checked")
    ready = p.published_at + cfg.soak if cfg.soak else now
    if now < ready:
        return Hold("soak", f"{p.latest} has been out less than {_fmt(cfg.soak)} "
                            f"(ready {ready.strftime('%Y-%m-%d %H:%M UTC')})", ready)
    if cfg.verdict == "any":
        return None
    if p.verdict is None or p.verdict_target != p.latest:
        return Hold("assessment", f"waiting for Claude's assessment of {p.latest}")
    allowed = VERDICT_LEVELS[: VERDICT_LEVELS.index(cfg.verdict) + 1]
    if p.verdict not in allowed:
        return Hold("verdict", f"Claude rated {p.latest} {p.verdict}")
    return None


def blocker(p: Pending, cfg: AutoConfig, now: datetime) -> Optional[str]:
    h = hold(p, cfg, now)
    return h.text if h else None


def _fmt(d: timedelta) -> str:
    hours = int(d.total_seconds() // 3600)
    if hours and hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{int(d.total_seconds() // 60)} minutes"
