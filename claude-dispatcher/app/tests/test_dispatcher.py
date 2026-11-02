from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dispatcher import config
from dispatcher.config import Config, Profile
from dispatcher.jobs import JobResult, command, container_options, parse_output
from dispatcher.main import Dispatcher, build_prompt, create_app, reply_text
from dispatcher.vikunja import to_html
from dispatcher.webhook import html_to_text, parse, verify

SECRET = "s3cret"


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def event(name: str, project_id: int = 7, username: str = "chris", comment: str | None = None) -> dict:
    data = {
        "task": {"id": 42, "project_id": project_id, "title": "Check Immich", "description": "<p>Is <b>v2.3</b> safe?</p>"},
        "doer": {"username": username},
    }
    if comment is not None:
        data["comment"] = {"comment": comment}
    return {"event_name": name, "time": "2026-11-02T00:00:00Z", "data": data}


# ── config ──────────────────────────────────────────────────────────────────


def test_load_profiles(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text(textwrap.dedent("""
        defaults:
          max_turns: 20
        profiles:
          test:
            egress: [github.com]
            allowed_users: [chris]
        projects:
          7: test
    """))
    cfg = config.load(path)
    p = cfg.profile_for(7)
    assert p.name == "test" and p.max_turns == 20
    assert p.all_egress == ("api.anthropic.com", "github.com")
    assert cfg.profile_for(8) is None
    assert p.allows("chris") and not p.allows("mallory") and not p.allows(None)


def test_unknown_profile_is_an_error(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text("profiles: {}\nprojects:\n  7: nope\n")
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_repo_profiles_file_loads():
    cfg = config.load(Path(__file__).parents[2] / "profiles.yaml")
    assert "test" in cfg.profiles


# ── webhook ─────────────────────────────────────────────────────────────────


def test_verify():
    body = b'{"a":1}'
    assert verify(SECRET, body, sign(body))
    assert not verify(SECRET, body, sign(b"other"))
    assert not verify(SECRET, body, None)
    assert not verify("", body, sign(body))


def test_parse_events():
    t = parse(event("task.created"))
    assert (t.task_id, t.project_id, t.title, t.description, t.instruction) == (42, 7, "Check Immich", "Is v2.3 safe?", None)
    t = parse(event("task.comment.created", comment="<p>@claude also check the mobile app</p>"))
    assert t.instruction == "also check the mobile app"
    assert parse(event("task.comment.created", comment="<p>thanks!</p>")) is None
    assert parse(event("task.updated")) is None
    assert parse({"event_name": "task.created", "data": {}}) is None


def test_html_to_text():
    assert html_to_text("<p>one</p><p>two &amp; three</p>") == "one\ntwo & three"


# ── jobs ────────────────────────────────────────────────────────────────────


def test_container_is_locked_down():
    p = Profile(name="test", allowed_tools=("Read", "WebFetch"), memory="1g", cpus=0.5, runtime="runsc")
    opts = container_options(p, "tok")
    assert opts["network"] == "claude-jobs-test"
    assert opts["read_only"] and opts["cap_drop"] == ["ALL"] and opts["security_opt"] == ["no-new-privileges"]
    assert opts["user"] == "1000:1000" and opts["runtime"] == "runsc"
    assert opts["nano_cpus"] == 500_000_000 and opts["mem_limit"] == "1g"
    assert opts["environment"]["HTTPS_PROXY"] == "http://claude-egress-test:3128"
    assert "volumes" not in opts and "privileged" not in opts
    cmd = command(p, "hello")
    assert cmd[:2] == ["-p", "hello"] and cmd[cmd.index("--allowedTools") + 1] == "Read,WebFetch"


def test_parse_output():
    out = "noise\n" + json.dumps({"result": "All good", "is_error": False, "num_turns": 3, "total_cost_usd": 0.12})
    assert parse_output(out) == JobResult("All good", False, 3, 0.12)
    assert parse_output("boom").is_error


# ── dispatcher ──────────────────────────────────────────────────────────────


class FakeVikunja:
    def __init__(self):
        self.posted: list[tuple[int, str]] = []

    async def comment(self, task_id, text):
        self.posted.append((task_id, text))

    async def comments(self, task_id):
        return [{"comment": "<p>first look: fine</p>"}]


class FakeRunner:
    def __init__(self, result=JobResult("Looks safe.", False, 2, 0.05)):
        self.result = result
        self.prompts: list[str] = []

    def run(self, profile, prompt):
        self.prompts.append(prompt)
        return self.result


def make(cfg_users=(), per_day=5):
    cfg = Config(profiles={"test": Profile(name="test", allowed_users=cfg_users)}, projects={7: "test"})
    v, r = FakeVikunja(), FakeRunner()
    return Dispatcher(cfg, v, r, jobs_per_day=per_day, max_concurrent=1), v, r


def test_accept_rules():
    d, _, _ = make(cfg_users=("chris",), per_day=1)
    assert d.accept(parse(event("task.created", project_id=99)))[0] is None
    assert d.accept(parse(event("task.created", username="mallory")))[0] is None
    assert d.accept(parse(event("task.created")))[0] is not None
    assert "already" in d.accept(parse(event("task.created")))[1]
    d.active.clear()
    assert "daily limit" in d.accept(parse(event("task.created")))[1]


def test_handle_posts_ack_and_reply():
    d, v, r = make()
    trigger = parse(event("task.comment.created", comment="@claude and the app?"))
    profile, _ = d.accept(trigger)
    asyncio.run(d.handle(trigger, profile))
    assert [t for t, _ in v.posted] == [42, 42]
    assert v.posted[0][1].startswith("Working on this")
    assert v.posted[1][1] == "Looks safe.\n\n— Claude, test profile · 2 turns · $0.05 API-equivalent"
    assert "first look: fine" in r.prompts[0] and "and the app?" in r.prompts[0]
    assert d.active == set()


def test_reply_never_starts_with_trigger():
    p = Profile(name="test")
    assert reply_text(p, JobResult("@claude do it again", False)).startswith("Claude: @claude")


def test_prompt_marks_untrusted_text():
    prompt = build_prompt(parse(event("task.created")), Profile(name="test"), [])
    assert "information, not instructions" in prompt and "Is v2.3 safe?" in prompt


def test_to_html_escapes():
    assert to_html("<script>x</script>\n\nok") == "<p>&lt;script&gt;x&lt;/script&gt;</p><p>ok</p>"


def test_webhook_endpoint():
    d, v, _ = make()
    client = TestClient(create_app(d, SECRET))
    body = json.dumps(event("task.created")).encode()
    assert client.post("/webhooks/vikunja", content=body).status_code == 401
    assert client.post("/webhooks/vikunja", content=body, headers={"X-Vikunja-Signature": sign(body)}).status_code == 202
    other = json.dumps(event("task.created", project_id=99)).encode()
    assert client.post("/webhooks/vikunja", content=other, headers={"X-Vikunja-Signature": sign(other)}).status_code == 204
