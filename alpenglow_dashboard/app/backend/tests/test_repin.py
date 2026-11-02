"""Update on pinned images: repin them all, then pull & recreate each.

The repin runs a stand-in ``pin`` script against a temporary repo, so which
tag gets tried and what lands in the files is real; the compose commands are
faked.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from alpenglow_dashboard import actions, audit, repin
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
    sys.exit("error: manifest unknown")
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
    (repo / "pin").write_text("#!/bin/sh\necho 'error: manifest unknown' >&2\nexit 2\n")
    with pytest.raises(repin.RepinError, match="no image tag found.*manifest unknown"):
        asyncio.run(repin.pin(BESZEL))


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

    async def fake_target(service_id):
        return targets.get(service_id)

    monkeypatch.setattr(actions, "_run_commands", fake_commands)
    monkeypatch.setattr(repin, "target", fake_target)
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    actions._BUSY.clear()
    actions._SERVICE_LOCKS.clear()
    actions._semaphore = None

    def audit_lines():
        path = tmp_path / "audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    yield calls, results, audit_lines, targets
    actions._BUSY.clear()
    actions._semaphore = None


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


def test_the_dashboard_updates_itself_last(job, repo: Path):
    calls, *_ = job
    run_update("alpenglow_dashboard", "beszel", "jellyfin")
    assert calls[-1] == ("alpenglow_dashboard", "pull")


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
