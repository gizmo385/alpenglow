"""Automatic updates: settings, schedules, the health check and the scheduled pass."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard import actions, autoupdate, repin, upgrades
from alpenglow_dashboard.autoupdate import health, policy, runner
from alpenglow_dashboard.autoupdate.state import State
from alpenglow_dashboard.integrations import update_summary, updates
from alpenglow_dashboard.main import create_app
from alpenglow_dashboard.upgrades.verdicts import Annotation

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
DAILY = policy.Schedule.parse("daily")


# ── schedules ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,interval", [
    ("hourly", timedelta(hours=1)), ("daily", timedelta(days=1)), ("weekly", timedelta(weeks=1)),
    ("6h", timedelta(hours=6)), ("2d", timedelta(days=2)), ("30m", timedelta(minutes=30)),
])
def test_interval_schedules(text, interval):
    s = policy.Schedule.parse(text)
    assert s.next_after(NOW) == NOW + interval


@pytest.mark.parametrize("bad", ["5m", "sometimes", "0 4 * *", "61 * * * *", "0 4 * * 8"])
def test_bad_schedules(bad):
    with pytest.raises(policy.ConfigError):
        policy.Schedule.parse(bad)


@pytest.mark.parametrize("cron,after,expected", [
    ("0 4 * * *", datetime(2026, 11, 20, 3, 59), datetime(2026, 11, 20, 4, 0)),
    ("0 4 * * *", datetime(2026, 11, 20, 4, 0), datetime(2026, 11, 21, 4, 0)),
    ("*/15 * * * *", datetime(2026, 11, 20, 4, 7), datetime(2026, 11, 20, 4, 15)),
    ("30 2 * * 1-5", datetime(2026, 11, 20, 3, 0), datetime(2026, 11, 23, 2, 30)),  # Fri → Mon
    ("0 3 * * 0", datetime(2026, 11, 20, 3, 0), datetime(2026, 11, 22, 3, 0)),  # Sunday
    ("0 3 * * 7", datetime(2026, 11, 20, 3, 0), datetime(2026, 11, 22, 3, 0)),  # Sunday, spelled 7
    ("0 0 1 * *", datetime(2026, 11, 20, 3, 0), datetime(2026, 12, 1, 0, 0)),
    ("0 6,18 * * *", datetime(2026, 11, 20, 7, 0), datetime(2026, 11, 20, 18, 0)),
])
def test_cron_schedules(cron, after, expected):
    assert policy.Schedule.parse(cron).next_after(after) == expected


# ── versions ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("current,latest,size", [
    ("1.30.4", "1.30.5", "patch"),
    ("v1.30.4", "1.31.0", "minor"),
    ("1.30.4", "2.0.0", "major"),
    ("0.21.2", "0.21.3", "patch"),
    ("0.21.2", "0.22.0", "major"),  # 0.x minor releases may break things
    ("v1.0.37.0", "v1.0.37.1", "patch"),
    ("twenty/v2.56.1", "twenty/v2.57.0", "minor"),
    ("2026.11.2", "2026.11.3", "patch"),
    ("1.2", "1.2.1", "patch"),
    ("1.30.5", "1.30.5", None),
    ("1.30.5", "1.30.4", None),
    ("latest", "1.0.0", None),
])
def test_bump_size(current, latest, size):
    assert policy.bump_size(current, latest) == size


# ── does a release qualify? ───────────────────────────────────────────────────


def cfg(**kw):
    return policy.AutoConfig(every=DAILY, **kw)


def pending(**kw):
    base = dict(image="freshrss/freshrss", current="1.30.4", latest="1.30.5",
                published_at=NOW - timedelta(days=3), verdict="Easy", verdict_target="1.30.5")
    return policy.Pending(**(base | kw))


@pytest.mark.parametrize("p,c,reason", [
    (pending(), cfg(), None),
    (pending(latest="1.31.0", verdict_target="1.31.0"), cfg(), "is a minor update; only patch"),
    (pending(latest="1.31.0", verdict_target="1.31.0"), cfg(max_bump="minor"), None),
    (pending(published_at=NOW - timedelta(days=1)), cfg(), "has been out less than 2 days"),
    (pending(published_at=None), cfg(), "no release date"),
    (pending(published_at=None), cfg(soak=timedelta(0)), None),
    (pending(published_at=NOW), cfg(soak=timedelta(0)), None),
    (pending(verdict=None), cfg(), "waiting for Claude's assessment of 1.30.5"),
    (pending(verdict_target="1.30.4"), cfg(), "waiting for Claude's assessment"),
    (pending(verdict="Needs care"), cfg(), "Claude rated 1.30.5 Needs care"),
    (pending(verdict="Needs care"), cfg(verdict="Needs care"), None),
    (pending(verdict="Hold"), cfg(verdict="Needs care"), "Claude rated 1.30.5 Hold"),
    (pending(verdict=None), cfg(verdict="any"), None),
    (pending(latest="2.0.0"), cfg(verdict="any"), "is a major update"),
])
def test_blocker(p, c, reason):
    got = policy.blocker(p, c, NOW)
    assert got is None if reason is None else reason in got


# ── reading versions.yaml ─────────────────────────────────────────────────────


def write_versions(root: Path, text: str) -> Path:
    path = root / "versions.yaml"
    path.write_text(text)
    return path


def test_load_units(tmp_path: Path):
    path = write_versions(tmp_path, """
images:
  freshrss/freshrss:
    tag: 1.30.4
    policy: auto-eligible
    auto: {every: 6h, max_bump: minor, soak: 1d, verdict: any, monitor: FreshRSS}
  glances: {tag: "4.0", policy: auto-eligible}
  jellyfin/jellyfin: {tag: "10.10.0", policy: approve}
  henrygd/beszel:
    tag: 0.21.2
    group: beszel
    policy: auto-eligible
    auto: {max_bump: minor, soak: 1d, monitor: Beszel}
  henrygd/beszel-agent:
    tag: 0.21.2-alpine
    group: beszel
    policy: auto-eligible
    auto: {every: weekly, soak: 3d, verdict: Needs care}
  ghcr.io/gizmo385/image-updates-tracker:
    policy: float
    reason: built from main
    auto: {follow: true, every: 6h}
  nextcloud: {policy: float, reason: not yet}
""")
    loaded = policy.load(path, DAILY)
    assert loaded.errors == []
    units = {u.id: u for u in loaded.units}
    assert set(units) == {"image:freshrss/freshrss", "image:glances", "group:beszel",
                          "follow:ghcr.io/gizmo385/image-updates-tracker"}
    fresh = units["image:freshrss/freshrss"].config
    assert (fresh.every.text, fresh.max_bump, fresh.soak, fresh.verdict, fresh.monitors) == \
        ("6h", "minor", timedelta(days=1), "any", ("FreshRSS",))
    assert units["image:glances"].config == policy.AutoConfig(every=DAILY)
    beszel = units["group:beszel"]
    assert beszel.images == ("henrygd/beszel", "henrygd/beszel-agent")
    # The most cautious of each setting; the schedule from the member that sets one.
    assert (beszel.config.every.text, beszel.config.max_bump, beszel.config.soak, beszel.config.verdict) == \
        ("weekly", "patch", timedelta(days=3), "Easy")
    assert beszel.config.monitors == ("Beszel",)
    assert units["follow:ghcr.io/gizmo385/image-updates-tracker"].follow


def test_load_reports_problems(tmp_path: Path):
    path = write_versions(tmp_path, """
images:
  a: {tag: "1", group: g, policy: auto-eligible}
  b: {tag: "1", group: g, policy: approve}
  c: {tag: "1", policy: approve, auto: {every: daily}}
  d: {tag: "1", policy: auto-eligible, auto: {evry: daily}}
  e: {policy: float, reason: x, auto: {every: daily}}
  f: {tag: "1", policy: auto-eligible, auto: {every: 5m}}
""")
    loaded = policy.load(path, DAILY)
    assert loaded.units == []
    assert [e.split(":")[0] for e in loaded.errors] == ["a", "c", "d", "e", "f"]
    assert "b isn't auto-eligible" in loaded.errors[0]


# ── the health check ──────────────────────────────────────────────────────────


def fake_docker(states: dict):
    """``states[key]`` is a list of ContainerStates handed out in turn (the last repeats)."""
    seen: dict = {}

    async def inspector(sid, svc):
        seq = states.get((sid, svc))
        if seq is None:
            return None
        i = seen.get((sid, svc), 0)
        seen[(sid, svc)] = i + 1
        return seq[min(i, len(seq) - 1)]

    return inspector


def cs(status="running", health_=None, restarts=0, image="sha256:new"):
    return health.ContainerState("app", status, health_, restarts, image)


async def no_sleep(_):
    return None


def run_check(states, monitors=(), kuma=None, wait=30):
    async def read():
        return kuma

    return asyncio.run(health.check(list(states), monitors, wait=wait, poll=10, inspector=fake_docker(states),
                                    read_monitors=read, sleep=no_sleep, kuma_poll=0))


def test_healthy():
    assert run_check({("svc", "app"): [cs()]}) is None


def test_healthcheck_must_end_healthy():
    assert run_check({("svc", "app"): [cs(health_="starting"), cs(health_="healthy")]}) is None
    assert "still not healthy" in run_check({("svc", "app"): [cs(health_="starting")]})
    assert "unhealthy" in run_check({("svc", "app"): [cs(health_="starting"), cs(health_="unhealthy")]})


def test_restarts_and_exits_fail():
    assert "restarted 1 time" in run_check({("svc", "app"): [cs(), cs(), cs(restarts=1)]})
    assert "is exited" in run_check({("svc", "app"): [cs(), cs(status="exited")]})
    assert "no container found" in asyncio.run(health.check(
        [("svc", "gone")], (), wait=0, inspector=fake_docker({}), sleep=no_sleep))


def test_one_shot_jobs_may_finish():
    """ollama-pull shares ollama's image, runs once (restart: no) and exits."""
    def job(status="exited", code=0, policy="no"):
        return health.ContainerState("ollama-pull", status, None, 0, "sha256:new", code, policy)

    states = {("ollama", "ollama"): [cs()], ("ollama", "ollama-pull"): [job("running", None), job()]}
    assert run_check(states) is None
    assert run_check({("ollama", "ollama-pull"): [job(policy="on-failure")]}) is None
    assert "exit code 1" in run_check({("ollama", "ollama-pull"): [job("running", None), job(code=1)]})
    # A long-running service that exits cleanly is still a failure.
    assert "is exited" in run_check({("svc", "app"): [cs(), job(policy="unless-stopped")]})


def test_kuma_monitors():
    states = {("svc", "app"): [cs()]}
    assert run_check(states, ("FreshRSS",), {"FreshRSS": 1}) is None
    assert "not up" in run_check(states, ("FreshRSS",), {"FreshRSS": 0})
    assert "no Uptime Kuma monitor named Nope" in run_check(states, ("Nope",), {"FreshRSS": 1})
    assert "couldn't read Uptime Kuma" in run_check(states, ("FreshRSS",), None)


# ── the scheduled pass ────────────────────────────────────────────────────────


VERSIONS = """\
images:
  freshrss/freshrss:
    tag: 1.30.4
    policy: auto-eligible
    auto: {every: daily}
  henrygd/beszel:
    tag: 0.21.2
    group: beszel
    policy: auto-eligible
  henrygd/beszel-agent:
    tag: 0.21.2-alpine
    group: beszel
    policy: auto-eligible
"""
PINNED_FOLLOW = """\
  ghcr.io/gizmo385/youtube-rss-manager:
    tag: sha-aaa1111
    policy: auto-eligible
    auto: {follow: latest}
"""
FOLLOW = """\
  ghcr.io/gizmo385/image-updates-tracker:
    policy: float
    reason: built from main
    auto: {follow: true, every: 6h}
"""

# Stand-in for ./pin set: rewrites the tag in versions.yaml and compose files,
# and prints the "path:line: old -> new" lines the real one does.
FAKE_PIN = """#!/usr/bin/env python3
import pathlib, re, sys
_, cmd, name, tag = sys.argv
root = pathlib.Path(__file__).parent
if cmd == "resolve":  # the tag resolve.txt says `tag` points at
    answer = root / "resolve.txt"
    if not answer.exists():
        sys.exit("error: ghcr.io returned 503")
    print(answer.read_text().strip())
    sys.exit(0)
with (root / "pin.log").open("a") as log:
    log.write(f"{name} {tag}\\n")
lock = root / "versions.yaml"
lock.write_text(re.sub(rf"({re.escape(name)}:\\n    tag: )\\S+", rf"\\g<1>{tag}", lock.read_text()))
for c in sorted(root.glob("*/compose.yaml")):
    text = c.read_text()
    new = re.sub(rf"({re.escape(name)}):[^\\s@]+", rf"\\g<1>:{tag}", text)
    if new != text:
        c.write_text(new)
        print(f"{c.relative_to(root)}:3: {name}:old -> {name}:{tag}")
"""


def release(tag, days_ago):
    return {"tag": tag, "url": "", "published_at": (NOW - timedelta(days=days_ago)).isoformat()}


TRACKER = {"services": [
    {"name": "FreshRSS", "owner": "FreshRSS", "repo": "FreshRSS", "image": "freshrss/freshrss:1.30.4",
     "current_version": "1.30.4", "latest_version": "1.30.5", "has_updates": True,
     "releases": [release("1.30.5", 3)]},
]}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    root = tmp_path / "services"
    root.mkdir()
    write_versions(root, VERSIONS)
    for sid, svc, image in (("freshrss", "freshrss", "freshrss/freshrss:1.30.4"),
                            ("beszel", "beszel", "henrygd/beszel:0.21.2"),
                            ("updates-tracker", "release-feeds", "ghcr.io/gizmo385/image-updates-tracker:main"),
                            ("youtube_rss_manager", "youtube_rss_manager",
                             "ghcr.io/gizmo385/youtube-rss-manager:sha-aaa1111")):
        (root / sid).mkdir()
        (root / sid / "compose.yaml").write_text(
            f"services:\n  {svc}:\n    image: {image}\n  sidecar:\n    image: redis:latest\n")
    (root / "pin").write_text(FAKE_PIN)
    (root / "pin").chmod(0o755)
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(root))
    monkeypatch.setenv("AUTO_UPDATE_STATE", str(tmp_path / "autoupdate.json"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTO_UPDATE", "on")
    monkeypatch.delenv("AUTO_UPDATE_COMMIT", raising=False)
    repin._repo_lock = None
    runner._run_lock = None
    actions._BUSY.clear()
    actions._SERVICE_LOCKS.clear()
    actions._semaphore = None

    world = {"tracker": json.loads(json.dumps(TRACKER)),
             "annotations": {"freshrss/freshrss": Annotation("FreshRSS/FreshRSS", "t", "Easy", target="1.30.5",
                                                             card_id=9)},
             "compose": [], "compose_fail": {}, "health": None, "health_calls": [], "embeds": [],
             "summaries": [], "rescans": 0, "comments": [], "images": {}}

    async def fetch():
        return world["tracker"]

    async def annotations():
        return world["annotations"]

    async def commands(sid, action, services=()):
        world["compose"].append((sid, action, tuple(services)))
        for key in services:
            if (sid, key) in world["images"]:
                world["images"][(sid, key)] = "sha256:pulled"
        return world["compose_fail"].get(sid, (True, ""))

    async def inspect(sid, svc):
        return cs(image=world["images"].get((sid, svc), "sha256:same"))

    async def check(containers, monitors):
        world["health_calls"].append((sorted(containers), monitors))
        return world["health"]

    async def post_embed(embed):
        world["embeds"].append(embed)

    async def post(results, title=""):
        world["summaries"].append([(r.name, r.ok, r.versions, r.detail) for r in results])

    async def rescan():
        world["rescans"] += 1

    async def comment(card, text):
        world["comments"].append((card, text))
        return True

    monkeypatch.setattr(updates, "_fetch_tracker", fetch)
    monkeypatch.setattr(upgrades, "annotations_by_repo", annotations)
    monkeypatch.setattr(actions, "_run_commands", commands)
    monkeypatch.setattr(health, "inspect", inspect)
    monkeypatch.setattr(health, "check", check)
    monkeypatch.setattr(update_summary, "post_embed", post_embed)
    monkeypatch.setattr(update_summary, "post", post)
    monkeypatch.setattr(updates, "rescan", rescan)
    monkeypatch.setattr(upgrades, "comment", comment)
    world["root"] = root
    yield world
    actions._BUSY.clear()


def tick(at=NOW, **kw):
    return asyncio.run(runner.tick(at, **kw))


def state() -> State:
    return State.load()


def test_first_sighting_waits_a_full_interval(env):
    assert tick() == []
    assert env["compose"] == []
    assert state().last_run("image:freshrss/freshrss") == NOW
    assert tick(NOW + timedelta(hours=23)) == []
    plans = tick(NOW + timedelta(days=1))
    assert {p.unit.id for p in plans} == {"image:freshrss/freshrss", "group:beszel"}


def test_update_goes_out_and_only_touches_its_own_compose_service(env):
    tick()
    tick(NOW + timedelta(days=1))
    root = env["root"]
    assert (root / "pin.log").read_text().splitlines() == ["freshrss/freshrss 1.30.5"]
    assert "freshrss/freshrss:1.30.5" in (root / "freshrss/compose.yaml").read_text()
    # Pulls just the updated image's compose service, not the redis:latest sidecar.
    assert env["compose"] == [("freshrss", "pull", ("freshrss",))]
    assert env["health_calls"] == [([("freshrss", "freshrss")], ())]
    assert env["summaries"] == [[(updates.display_name("freshrss"), True, ["1.30.4 → 1.30.5"], "")]]
    assert env["rescans"] == 1
    s = state().get("image:freshrss/freshrss")
    assert s["last_result"]["ok"] and "paused" not in s
    assert actions._BUSY == {}
    audit = [json.loads(l) for l in (env["root"].parent / "audit.jsonl").read_text().splitlines()]
    assert [(a["user"], a["service"], a["outcome"]) for a in audit] == \
        [("auto-update", "freshrss", "accepted"), ("auto-update", "freshrss", "succeeded")]


def test_failed_health_check_pauses_and_does_not_roll_back(env):
    env["health"] = "freshrss restarted 2 time(s) after the update"
    tick()
    tick(NOW + timedelta(days=1))
    assert "freshrss/freshrss:1.30.5" in (env["root"] / "freshrss/compose.yaml").read_text()
    paused = state().paused("image:freshrss/freshrss")
    assert paused["version"] == "freshrss/freshrss=1.30.5"
    assert "restarted 2 time(s)" in paused["reason"]
    [[(name, ok, _, detail)]] = env["summaries"]
    assert not ok and "not rolled back" in detail
    assert env["comments"] and env["comments"][0][0] == 9
    assert not env["comments"][0][1].startswith("@claude")
    assert env["rescans"] == 0

    # Paused: the next run leaves it alone, even with a newer release.
    env["tracker"]["services"][0].update(latest_version="1.30.6", releases=[release("1.30.6", 5)])
    env["compose"].clear()
    tick(NOW + timedelta(days=2))
    assert env["compose"] == []
    assert "paused" in state().get("image:freshrss/freshrss")["waiting"]


def test_check_now_rescans_the_tracker_first(env):
    tick()
    tick(NOW + timedelta(hours=1), force=True)
    assert env["rescans"] == 2  # the forced check's rescan, then the one after its update
    env["rescans"] = 0
    tick(NOW + timedelta(days=3))  # scheduled checks use the tracker's own scans
    assert env["rescans"] == 0


def test_failed_pull_pauses(env):
    env["compose_fail"]["freshrss"] = (False, "manifest unknown")
    tick()
    tick(NOW + timedelta(days=1))
    assert "docker compose failed in freshrss: manifest unknown" in state().paused("image:freshrss/freshrss")["reason"]
    assert env["health_calls"] == []


@pytest.mark.parametrize("change,why", [
    ({"annotations": {}}, "waiting for Claude's assessment"),
    ({"verdict": "Needs care"}, "Claude rated 1.30.5 Needs care"),
    ({"latest_version": "1.31.0", "releases": [release("1.31.0", 3)]}, "minor update"),
    ({"releases": [release("1.30.5", 0)]}, "has been out less than"),
])
def test_releases_that_dont_qualify_wait(env, change, why):
    if "annotations" in change:
        env["annotations"] = change["annotations"]
    elif "verdict" in change:
        env["annotations"]["freshrss/freshrss"] = Annotation("FreshRSS/FreshRSS", "t", change["verdict"],
                                                             target="1.30.5")
    else:
        env["tracker"]["services"][0].update(change)
    tick()
    tick(NOW + timedelta(days=1))
    assert env["compose"] == []
    assert why in state().get("image:freshrss/freshrss")["waiting"]


def test_dry_run_announces_once_and_changes_nothing(env, monkeypatch):
    monkeypatch.setenv("AUTO_UPDATE", "dry-run")
    tick()
    tick(NOW + timedelta(days=1))
    tick(NOW + timedelta(days=2))
    assert env["compose"] == [] and not (env["root"] / "pin.log").exists()
    assert len(env["embeds"]) == 1
    assert "would update 1.30.4 → 1.30.5" in env["embeds"][0]["description"]
    assert "dry run" in env["embeds"][0]["title"]


def test_off_does_nothing(env, monkeypatch):
    monkeypatch.setenv("AUTO_UPDATE", "off")
    tick()
    tick(NOW + timedelta(days=1))
    assert env["compose"] == [] and env["embeds"] == []


def test_follow_pulls_and_only_reports_a_new_image(env):
    write_versions(env["root"], VERSIONS + FOLLOW)
    tick()
    # Nothing new on main: quiet.
    tick(NOW + timedelta(hours=6))
    assert ("updates-tracker", "pull", ("release-feeds",)) in env["compose"]
    assert ("updates-tracker", "release-feeds") not in [k for call, _ in env["health_calls"] for k in call]
    assert not any("image-updates-tracker" in r[2][0] for s in env["summaries"] for r in s)
    # A new image on main: health-checked and reported.
    env["images"][("updates-tracker", "release-feeds")] = "sha256:old"
    env["compose"].clear()
    env["health_calls"].clear()
    tick(NOW + timedelta(hours=12))
    assert env["health_calls"] == [([("updates-tracker", "release-feeds")], ())]
    assert env["summaries"][-1] == [(updates.display_name("updates-tracker"), True, ["new ghcr.io/gizmo385/image-updates-tracker image"], "")]


def test_pinned_follow_repins_to_the_build_latest_points_at(env):
    write_versions(env["root"], VERSIONS + PINNED_FOLLOW)
    yt = "follow:ghcr.io/gizmo385/youtube-rss-manager"
    (env["root"] / "resolve.txt").write_text("sha-aaa1111")
    tick()
    tick(NOW + timedelta(days=1))  # latest is still the pinned build: nothing to do
    assert "youtube_rss_manager" not in {c[0] for c in env["compose"]}
    assert state().get(yt)["waiting"] == "up to date"

    (env["root"] / "resolve.txt").unlink()  # registry down: wait, don't pull
    tick(NOW + timedelta(days=2))
    assert "couldn't tell where latest points: ghcr.io returned 503" in state().get(yt)["waiting"]
    assert "youtube_rss_manager" not in {c[0] for c in env["compose"]}

    (env["root"] / "resolve.txt").write_text("sha-bbb2222")
    tick(NOW + timedelta(days=3))
    root = env["root"]
    assert "ghcr.io/gizmo385/youtube-rss-manager sha-bbb2222" in (root / "pin.log").read_text()
    assert "youtube-rss-manager:sha-bbb2222" in (root / "youtube_rss_manager/compose.yaml").read_text()
    assert ("youtube_rss_manager", "pull", ("youtube_rss_manager",)) in env["compose"]
    assert ([("youtube_rss_manager", "youtube_rss_manager")], ()) in env["health_calls"]
    assert (updates.display_name("youtube_rss_manager"), True, ["sha-aaa1111 → sha-bbb2222"], "") in \
        env["summaries"][-1]


def test_pinned_follow_is_a_follow_unit(tmp_path: Path):
    path = write_versions(tmp_path, """
images:
  ghcr.io/o/app: {tag: sha-aaa1111, policy: auto-eligible, auto: {follow: latest, every: 6h}}
  ghcr.io/o/grouped: {tag: sha-aaa1111, group: g, policy: auto-eligible, auto: {follow: latest}}
  ghcr.io/o/bare: {tag: "1", policy: auto-eligible, auto: {follow: true}}
""")
    loaded = policy.load(path, DAILY)
    [unit] = loaded.units
    assert (unit.id, unit.follow, unit.follow_tag, unit.config.every.text) == \
        ("follow:ghcr.io/o/app", False, "latest", "6h")
    assert loaded.errors == [
        "ghcr.io/o/grouped: ghcr.io/o/grouped: follow can't be used in a group",
        "ghcr.io/o/bare: a pinned entry follows a named tag (`follow: latest`)",
    ]


def test_busy_service_is_skipped(env):
    tick()
    actions._BUSY["freshrss"] = "updating"
    tick(NOW + timedelta(days=1))
    assert ("freshrss", "pull", ("freshrss",)) not in env["compose"]
    assert "busy" in state().get("image:freshrss/freshrss")["waiting"]
    assert actions._BUSY == {"freshrss": "updating"}


def test_tracker_down_retries_in_15_minutes(env, monkeypatch):
    async def down():
        return None

    tick()
    monkeypatch.setattr(updates, "_fetch_tracker", down)
    tick(NOW + timedelta(days=1))
    assert "unavailable" in state().get("image:freshrss/freshrss")["waiting"]
    monkeypatch.setattr(updates, "_fetch_tracker", lambda: _ret(env["tracker"]))
    tick(NOW + timedelta(days=1, minutes=10))
    assert env["compose"] == []
    tick(NOW + timedelta(days=1, minutes=15))
    assert ("freshrss", "pull", ("freshrss",)) in env["compose"]


async def _ret(value):
    return value


def test_commit_when_enabled(env, monkeypatch):
    root = env["root"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                   check=True)
    (root / "beszel/compose.yaml").write_text((root / "beszel/compose.yaml").read_text() + "# local edit\n")
    monkeypatch.setenv("AUTO_UPDATE_COMMIT", "1")
    tick()
    tick(NOW + timedelta(days=1))
    out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%an|%s", "--name-only"],
                         capture_output=True, text=True).stdout
    head, *files = [line for line in out.splitlines() if line]
    assert head == f"Alpenglow auto-update|auto-update: {updates.display_name('freshrss')} 1.30.4 → 1.30.5"
    assert sorted(files) == ["freshrss/compose.yaml", "versions.yaml"]
    # Someone's unrelated uncommitted edit stays uncommitted.
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "beszel/compose.yaml" in status


def test_no_commit_by_default(env):
    tick()
    tick(NOW + timedelta(days=1))
    assert not (env["root"] / ".git").exists()


def test_batches_join_units_that_share_a_service():
    def plan(uid, *sids):
        u = policy.Unit(uid, uid, (uid,), policy.AutoConfig(every=DAILY))
        return runner.Plan(u, places=[(s, "app") for s in sids])

    a, b, c = plan("a", "x"), plan("b", "y"), plan("c", "x", "z")
    assert [[p.unit.id for p in batch] for batch in runner._batches([a, b, c])] == [["b"], ["a", "c"]]


# ── routes ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("AUTO_UPDATE", "dry-run")
    monkeypatch.setattr(autoupdate, "lifespan", _no_lifespan)
    return TestClient(create_app())


import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def _no_lifespan(app):
    yield


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}


def test_status_route(client, env):
    write_versions(env["root"], VERSIONS + FOLLOW)
    tick()
    data = client.get("/api/autoupdate").json()
    assert data["mode"] == "dry-run" and data["errors"] == []
    units = {u["id"]: u for u in data["units"]}
    fresh = units["image:freshrss/freshrss"]
    assert (fresh["every"], fresh["maxBump"], fresh["verdict"], fresh["services"]) == \
        ("daily", "patch", "Easy", ["freshrss"])
    assert fresh["nextRun"].startswith("2026-11-21T12:00")
    assert units["follow:ghcr.io/gizmo385/image-updates-tracker"]["maxBump"] is None


def test_status_route_says_where_each_pending_release_stands(client, env, monkeypatch):
    monkeypatch.setattr(updates, "_tracker_cached", lambda: _ret(env["tracker"]))
    s = state()
    s.unit("image:freshrss/freshrss")["last_run"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    s.save()
    fresh = {u["id"]: u for u in client.get("/api/autoupdate").json()["units"]}["image:freshrss/freshrss"]
    assert fresh["pending"] == [{"image": "freshrss/freshrss", "current": "1.30.4", "latest": "1.30.5"}]
    assert fresh["hold"] is None and fresh["eta"] == fresh["nextRun"]

    # Still soaking: it goes out at the first scheduled check after the soak ends.
    out = datetime.now(timezone.utc) - timedelta(days=1)
    env["tracker"]["services"][0]["releases"] = [{"tag": "1.30.5", "url": "", "published_at": out.isoformat()}]
    fresh = {u["id"]: u for u in client.get("/api/autoupdate").json()["units"]}["image:freshrss/freshrss"]
    assert fresh["hold"]["kind"] == "soak"
    assert datetime.fromisoformat(fresh["eta"]) >= datetime.fromisoformat(fresh["hold"]["until"])
    assert fresh["then"] is None  # Claude already rated it Easy

    # Soaking with no verdict yet: it only goes out if Claude rates it well.
    env["annotations"] = {}
    fresh = {u["id"]: u for u in client.get("/api/autoupdate").json()["units"]}["image:freshrss/freshrss"]
    assert (fresh["hold"]["kind"], fresh["then"]) == ("soak", "assessment")

    env["annotations"] = {"freshrss/freshrss": Annotation("FreshRSS/FreshRSS", "t", "Hold", target="1.30.5")}
    env["tracker"]["services"][0]["releases"] = [release("1.30.5", 30)]
    fresh = {u["id"]: u for u in client.get("/api/autoupdate").json()["units"]}["image:freshrss/freshrss"]
    assert (fresh["hold"]["kind"], fresh["eta"]) == ("verdict", None)


def test_resume_route(client):
    s = state()
    s.pause("image:freshrss/freshrss", "x", "broke", NOW)
    s.result("image:freshrss/freshrss", False, "1.30.4 → 1.30.5: broke", NOW)
    s.save()
    headers = csrf(client)
    assert client.post("/api/autoupdate/resume", json={"unit": "image:freshrss/freshrss"}, headers=headers).status_code == 200
    assert state().paused("image:freshrss/freshrss") is None
    assert "last_result" not in state().get("image:freshrss/freshrss")  # off the recent list
    assert client.post("/api/autoupdate/resume", json={"unit": "image:freshrss/freshrss"}, headers=headers).status_code == 404


def test_run_route(client, monkeypatch):
    calls = []

    async def fake_tick(*a, **kw):
        calls.append(kw)

    monkeypatch.setattr(runner, "tick", fake_tick)
    headers = csrf(client)
    assert client.post("/api/autoupdate/run", json={"unit": "nope"}, headers=headers).status_code == 404
    assert client.post("/api/autoupdate/run", json={}, headers=headers).json()["started"]
    monkeypatch.setenv("AUTO_UPDATE", "off")
    assert client.post("/api/autoupdate/run", json={}, headers=headers).status_code == 409
