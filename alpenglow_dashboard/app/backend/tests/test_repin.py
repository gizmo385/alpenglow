"""Update on pinned images: repin them all, then pull & recreate each.

The repin runs a stand-in ``pin`` script against a temporary repo, so which
tag gets tried and what lands in the files is real; the compose commands are
faked.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

import pytest
import uvloop

from alpenglow_dashboard import actions, audit, repin
from alpenglow_dashboard.integrations import update_summary, updates
from alpenglow_dashboard.repin import Target, candidate_tags, target_for

# ── which tag ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "current_tag, current, latest, first",
    [
        ("2.7.2", "2.7.2", "2.8.0", "2.8.0"),
        ("2.7.2", "v2.7.2", "v2.8.0", "2.8.0"),
        ("v1.4.0", "v1.4.0", "v1.5.0", "v1.5.0"),
        ("v1.4.0", "1.4.0", "1.5.0", "v1.5.0"),
        ("0.21.1-alpine", "v0.21.1", "v0.21.2", "0.21.2-alpine"),
    ],
)
def test_candidate_tags_follow_the_current_spelling(current_tag, current, latest, first):
    tags = candidate_tags(current_tag, current, latest)
    assert tags[0] == first
    assert len(tags) == len(set(tags))


def test_candidate_tags_drop_a_monorepo_component():
    tags = candidate_tags("v2.54.0", "v2.54", "twenty/v2.56.1")
    assert "v2.56.1" in tags
    assert not any("/" in tag for tag in tags)


def test_candidate_tags_fall_back_to_both_spellings():
    assert candidate_tags("release-7", "7", "v8") == ["8", "v8"]


PINS = {
    "henrygd/beszel": {"name": "henrygd/beszel", "tag": "0.21.1"},
    "jellyfin/jellyfin": {"name": "jellyfin/jellyfin", "tag": "10.10.0"},
}


def entry(image="henrygd/beszel:0.21.1", current="v0.21.1", latest="v0.22.0", **kw):
    return {"image": image, "owner": "henrygd", "repo": "beszel", "has_updates": True,
            "current_version": current, "latest_version": latest, **kw}


def test_target_for_a_pinned_image():
    t = target_for(entry(), PINS)
    assert t == Target("henrygd/beszel", "0.21.1", "v0.21.1", "v0.22.0", "henrygd/beszel")
    assert t.tags()[0] == "0.22.0"


def test_target_for_docker_hub_prefixes():
    assert target_for(entry(image="docker.io/henrygd/beszel:latest"), PINS) is not None


@pytest.mark.parametrize(
    "e",
    [
        entry(image="ghcr.io/other/thing:1"),  # not in versions.yaml
        entry(has_updates=False),
        entry(latest=""),
        entry(latest="v0.21.1"),  # already pinned to it; the tracker is behind
    ],
)
def test_no_target(e):
    assert target_for(e, PINS) is None


def test_load_pins_skips_floating(tmp_path: Path):
    (tmp_path / "versions.yaml").write_text(textwrap.dedent("""\
        images:
          henrygd/beszel: {tag: 0.21.1, policy: approve}
          docker.io/library/redis: {tag: "7.4"}
          ollama/ollama: {policy: float, reason: moves fast}
    """))
    assert set(repin.load_pins(tmp_path)) == {"henrygd/beszel", "redis"}


# ── against a repo ────────────────────────────────────────────────────────────

FAKE_PIN = """\
#!/usr/bin/env python3
# Stand-in for ./pin set NAME TAG: rewrites versions.yaml and compose files.
# Tags starting with "v" don't exist. Logs each call to pin.log; a "slow" file
# makes it pause between reading and writing versions.yaml.
import pathlib, re, sys, time
_, cmd, name, tag = sys.argv
root = pathlib.Path(__file__).parent
with (root / "pin.log").open("a") as log:
    log.write(f"{name} {tag}\\n")
if tag.startswith("v"):
    print("error: manifest unknown", file=sys.stderr)
    sys.exit(3)
lock = root / "versions.yaml"
text = lock.read_text()
if (root / "slow").exists():  # widen the read-modify-write window
    time.sleep(0.3)
lock.write_text(re.sub(rf"({re.escape(name)}: {{tag: )[^,}}]+", rf"\\g<1>{tag}", text))
for c in root.glob("*/compose.yaml"):
    c.write_text(re.sub(rf"({re.escape(name)}):[^\\s@]+", rf"\\g<1>:{tag}", c.read_text()))
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "services"
    root.mkdir()
    (root / "versions.yaml").write_text(
        "images:\n  henrygd/beszel: {tag: 0.21.1, policy: approve}\n"
        "  jellyfin/jellyfin: {tag: 10.10.0, policy: approve}\n"
    )
    for sid, image in (("beszel", "henrygd/beszel:0.21.1"), ("jellyfin", "jellyfin/jellyfin:10.10.0")):
        (root / sid).mkdir()
        (root / sid / "compose.yaml").write_text(f"services:\n  app:\n    image: {image}\n")
    (root / "pin").write_text(FAKE_PIN)
    (root / "pin").chmod(0o755)
    monkeypatch.setenv("SERVICES_ROOT", str(root))
    repin._repo_lock = None
    return root


def pin_log(root: Path) -> list[str]:
    path = root / "pin.log"
    return path.read_text().splitlines() if path.exists() else []


BESZEL = Target("henrygd/beszel", "0.21.1", "v0.21.1", "v0.22.0", "henrygd/beszel")
JELLYFIN = Target("jellyfin/jellyfin", "10.10.0", "v10.10.0", "v10.11.0", "jellyfin/jellyfin")


def test_pin_uses_the_first_tag_that_exists(repo: Path):
    t = Target("henrygd/beszel", "v0.21.1", "v0.21.1", "v0.22.0", "")  # tries v0.22.0 first
    assert asyncio.run(repin.pin(t)) == "0.22.0"
    assert pin_log(repo) == ["henrygd/beszel v0.22.0", "henrygd/beszel 0.22.0"]
    assert "henrygd/beszel:0.22.0" in (repo / "beszel/compose.yaml").read_text()
    assert "henrygd/beszel: {tag: 0.22.0" in (repo / "versions.yaml").read_text()


def test_pin_with_no_tag_raises(repo: Path):
    (repo / "pin").write_text("#!/bin/sh\necho 'error: manifest unknown' >&2\nexit 3\n")
    with pytest.raises(repin.RepinError, match="no image tag found.*manifest unknown"):
        asyncio.run(repin.pin(BESZEL))


@pytest.mark.parametrize(
    "error",
    [
        "error: can't write keycloak/compose.yaml; nothing was changed",
        "error: registry-1.docker.io is rate limiting anonymous lookups; try again later",
    ],
)
def test_pin_stops_at_a_failure_that_another_spelling_cannot_fix(repo: Path, error: str):
    (repo / "pin").write_text(f"#!/bin/sh\necho \"$2 $3\" >> {repo}/pin.log\necho \"{error}\" >&2\nexit 2\n")
    with pytest.raises(repin.RepinError) as err:
        asyncio.run(repin.pin(BESZEL))
    assert str(err.value) == f"couldn't pin henrygd/beszel to 0.22.0: {error}"
    assert pin_log(repo) == ["henrygd/beszel 0.22.0"]


def test_pin_stops_when_pin_times_out(repo: Path, monkeypatch):
    calls = []

    async def timed_out(root, name, tag):
        calls.append(tag)
        return 124, "timed out after 180s: ./pin set"

    monkeypatch.setattr(repin, "_pin_set", timed_out)
    with pytest.raises(repin.RepinError, match="couldn't pin .* timed out"):
        asyncio.run(repin.pin(BESZEL))
    assert calls == ["0.22.0"]


# ── running pin, on the event loop the server really uses ─────────────────────


def on_uvloop(coro):
    """Run ``coro`` on uvloop, which is what uvicorn[standard] serves the app on."""
    loop = uvloop.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_run_as_the_repo_owner_on_uvloop(repo: Path, monkeypatch):
    """uvloop's subprocess API refuses user/group/extra_groups; the dashboard runs as root."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"done\n")

    monkeypatch.setattr(repin, "_owner", lambda root: (1002, 1003))
    monkeypatch.setattr(repin, "_env", lambda owner: {})
    monkeypatch.setattr(repin.subprocess, "run", fake_run)
    assert on_uvloop(repin._run(["./pin", "set"], repo, 5)) == (0, "done")
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (1002, 1003, [repo.stat().st_gid])
    assert seen["cwd"] == str(repo)


def test_run_a_real_process_on_uvloop(repo: Path):
    rc, out = on_uvloop(repin._run(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], repo, 5))
    assert rc == 3
    assert out.splitlines() == ["out", "err"]


def test_run_times_out_on_uvloop(repo: Path):
    rc, out = on_uvloop(repin._run(["/bin/sleep", "30"], repo, 0.2))
    assert rc == 124
    assert "timed out" in out


def test_pin_on_uvloop(repo: Path):
    assert on_uvloop(repin.pin(BESZEL)) == "0.22.0"
    assert "henrygd/beszel:0.22.0" in (repo / "beszel/compose.yaml").read_text()


# ── the Update job ────────────────────────────────────────────────────────────


@pytest.fixture()
def job(repo: Path, monkeypatch, tmp_path: Path):
    """Fake compose; returns (compose calls, results by (service, action), audit lines)."""
    calls: list[tuple[str, str]] = []
    results: dict[tuple[str, str], tuple[bool, str]] = {}
    targets = {"beszel": BESZEL, "jellyfin": JELLYFIN}

    async def fake_commands(service_id, action):
        calls.append((service_id, action))
        await asyncio.sleep(0)
        return results.get((service_id, action), (True, ""))

    async def fake_targets(service_id):
        t = targets.get(service_id)
        return t if isinstance(t, list) else [t] if t else []

    monkeypatch.setattr(actions, "_run_commands", fake_commands)
    monkeypatch.setattr(repin, "targets", fake_targets)
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    rescans.clear()

    async def fake_rescan():
        rescans.append(sorted(actions._UPDATED))

    monkeypatch.setattr(updates, "rescan", fake_rescan)
    summaries.clear()

    async def fake_post(results):
        if results:
            summaries.append([(r.service, r.ok) for r in results])

    async def fake_versions():
        return {"beszel": ["0.21.1 → 0.22.0"]}

    monkeypatch.setattr(update_summary, "post", fake_post)
    monkeypatch.setattr(updates, "pending_versions", fake_versions)
    actions._UPDATE_RESULTS.clear()
    actions._UPDATED.clear()
    actions._UPDATE_ERRORS.clear()
    actions._BUSY.clear()
    actions._SERVICE_LOCKS.clear()
    actions._semaphore = None

    def audit_lines():
        path = tmp_path / "audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    yield calls, results, audit_lines, targets
    actions._BUSY.clear()
    actions._UPDATED.clear()
    actions._UPDATE_ERRORS.clear()
    actions._semaphore = None


# What was awaiting the tracker at each rescan.
rescans: list[list[str]] = []
# The (service, ok) results in each posted summary.
summaries: list[list[tuple[str, bool]]] = []


def run_update(*sids: str):
    for sid in sids:
        actions._BUSY[sid] = "updating"
    asyncio.run(actions._update_job([(sid, f"job-{sid}") for sid in sids], "chris"))


def test_update_repins_everything_before_any_pull(job, repo: Path):
    calls, _, audit_lines, _ = job
    run_update("beszel", "jellyfin", "ollama")  # ollama floats: no repin
    assert pin_log(repo) == ["henrygd/beszel 0.22.0", "jellyfin/jellyfin 10.11.0"]
    assert sorted(calls) == [("beszel", "pull"), ("jellyfin", "pull"), ("ollama", "pull")]
    assert {(a["service"], a["outcome"]) for a in audit_lines()} == {
        ("beszel", "succeeded"), ("jellyfin", "succeeded"), ("ollama", "succeeded"),
    }
    assert actions._BUSY == {}


def test_failed_pull_is_reported_not_rolled_back(job, repo: Path):
    calls, results, audit_lines, _ = job
    results[("beszel", "pull")] = (False, "manifest for henrygd/beszel:0.22.0 not found")
    run_update("beszel", "jellyfin")
    assert pin_log(repo) == ["henrygd/beszel 0.22.0", "jellyfin/jellyfin 10.11.0"]
    assert "henrygd/beszel:0.22.0" in (repo / "beszel/compose.yaml").read_text()
    assert sorted(calls) == [("beszel", "pull"), ("jellyfin", "pull")]
    by = {a["service"]: a for a in audit_lines() if a["outcome"] != "accepted"}
    assert by["jellyfin"]["outcome"] == "succeeded"
    assert by["beszel"]["outcome"] == "failed"
    assert "pins henrygd/beszel to 0.22.0 (was 0.21.1); not rolled back" in by["beszel"]["stderr"]


def test_failed_repin_skips_that_pull(job, repo: Path):
    calls, _, audit_lines, targets = job
    targets["beszel"] = Target("henrygd/beszel", "0.21.1", "v0.21.1", "vNOPE", "")
    run_update("beszel", "jellyfin")
    assert calls == [("jellyfin", "pull")]
    by = {a["service"]: a for a in audit_lines()}
    assert by["beszel"]["outcome"] == "failed" and "no image tag found" in by["beszel"]["stderr"]


def test_update_repins_every_image_of_a_service(job, repo: Path):
    """signoz has two pinned images (signoz/signoz and its collector)."""
    calls, _, audit_lines, targets = job
    targets["beszel"] = [BESZEL, JELLYFIN]
    run_update("beszel")
    assert pin_log(repo) == ["henrygd/beszel 0.22.0", "jellyfin/jellyfin 10.11.0"]
    assert calls == [("beszel", "pull")]
    assert [a["outcome"] for a in audit_lines() if a["outcome"] != "accepted"] == ["succeeded"]


def test_one_failed_repin_of_a_service_skips_its_pull(job, repo: Path):
    calls, _, audit_lines, targets = job
    targets["beszel"] = [BESZEL, Target("jellyfin/jellyfin", "10.10.0", "v10.10.0", "vNOPE", "")]
    run_update("beszel")
    assert calls == []
    by = {a["service"]: a for a in audit_lines()}
    assert by["beszel"]["outcome"] == "failed"
    assert "no image tag found for jellyfin/jellyfin" in by["beszel"]["stderr"]
    assert "pins henrygd/beszel to 0.22.0 (was 0.21.1); not rolled back" in by["beszel"]["stderr"]


def test_the_dashboard_updates_itself_last(job, repo: Path):
    calls, *_ = job
    run_update("alpenglow_dashboard", "beszel", "jellyfin")
    assert calls[-1] == ("alpenglow_dashboard", "pull")


def test_one_rescan_after_every_service_in_a_batch(job):
    run_update("beszel", "jellyfin", "ollama")
    assert rescans == [["beszel", "jellyfin", "ollama"]]
    assert actions._UPDATED == set()


def test_two_requests_in_flight_share_one_rescan(job):
    for sid in ("beszel", "jellyfin"):
        actions._BUSY[sid] = "updating"

    async def two_clicks():
        await asyncio.gather(
            actions._update_job([("beszel", "job-a")], "chris"),
            actions._update_job([("jellyfin", "job-b")], "chris"),
        )

    asyncio.run(two_clicks())
    assert rescans == [["beszel", "jellyfin"]]


def test_one_summary_covers_a_batch_and_its_failures(job):
    _, results, _, _ = job
    results[("jellyfin", "pull")] = (False, "pull failed")
    run_update("beszel", "jellyfin")
    assert len(summaries) == 1
    assert sorted(summaries[0]) == [("beszel", True), ("jellyfin", False)]


def test_two_requests_in_flight_share_one_summary(job):
    for sid in ("beszel", "jellyfin"):
        actions._BUSY[sid] = "updating"

    async def two_clicks():
        await asyncio.gather(
            actions._update_job([("beszel", "job-a")], "chris"),
            actions._update_job([("jellyfin", "job-b")], "chris"),
        )

    asyncio.run(two_clicks())
    assert len(summaries) == 1 and len(summaries[0]) == 2


def test_a_batch_that_all_failed_still_gets_a_summary(job):
    _, results, _, _ = job
    results[("ollama", "pull")] = (False, "pull failed")
    run_update("ollama")
    assert summaries == [[("ollama", False)]]


def test_a_failed_summary_doesnt_stop_the_rescan(job, monkeypatch):
    async def broken_post(results):
        raise RuntimeError("discord exploded")

    monkeypatch.setattr(update_summary, "post", broken_post)
    run_update("beszel")
    assert rescans == [["beszel"]]


def test_the_summary_goes_out_before_the_dashboard_recreates_itself(job):
    calls, _, _, _ = job
    run_update("alpenglow_dashboard", "ollama")
    assert summaries[0] == [("ollama", True)]
    assert calls[-1] == ("alpenglow_dashboard", "pull")


def test_no_rescan_when_nothing_updated_and_the_error_is_kept(job):
    _, results, _, _ = job
    results[("ollama", "pull")] = (False, "pull failed")
    run_update("ollama")
    assert rescans == []
    assert actions.update_outcome("ollama") == (False, "pull failed")


def test_a_failure_doesnt_stop_the_rescan_for_the_rest(job):
    _, results, _, _ = job
    results[("jellyfin", "pull")] = (False, "pull failed")
    run_update("beszel", "jellyfin")
    assert rescans == [["beszel"]]
    updated, error = actions.update_outcome("jellyfin")
    assert updated is False and "pull failed" in error and "not rolled back" in error


def test_a_new_update_clears_the_last_error(job):
    _, results, _, _ = job
    results[("ollama", "pull")] = (False, "pull failed")
    run_update("ollama")
    results.clear()
    run_update("ollama")
    assert actions.update_outcome("ollama") == (False, None)


def test_rescan_comes_before_the_dashboard_recreates_itself(job, monkeypatch):
    calls, _, _, _ = job
    seen_at_rescan: list = []

    async def fake_rescan():
        seen_at_rescan.append(list(calls))

    monkeypatch.setattr(updates, "rescan", fake_rescan)
    run_update("alpenglow_dashboard", "ollama")
    assert ("alpenglow_dashboard", "pull") not in seen_at_rescan[0]
    assert ("ollama", "pull") in seen_at_rescan[0]


def test_a_failed_rescan_doesnt_stop_the_dashboards_update(job, monkeypatch):
    calls, _, _, _ = job

    async def broken_rescan():
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(updates, "rescan", broken_rescan)
    run_update("alpenglow_dashboard", "ollama")
    assert calls[-1] == ("alpenglow_dashboard", "pull")
    assert actions._UPDATED == set()


def test_back_to_back_clicks_queue_on_the_lock(job, repo: Path):
    """Two separate Update requests in flight at once both land in versions.yaml."""
    (repo / "slow").write_text("")
    for sid in ("beszel", "jellyfin"):
        actions._BUSY[sid] = "updating"

    async def two_clicks():
        await asyncio.gather(
            actions._update_job([("beszel", "job-a")], "chris"),
            actions._update_job([("jellyfin", "job-b")], "chris"),
        )

    asyncio.run(two_clicks())
    lock = (repo / "versions.yaml").read_text()
    assert "henrygd/beszel: {tag: 0.22.0" in lock
    assert "jellyfin/jellyfin: {tag: 10.11.0" in lock
