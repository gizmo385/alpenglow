"""B2 action-engine tests: compose command construction, the subprocess runner
(with a fake ``create_subprocess_exec`` — no docker socket needed), the busy
registry / concurrency locking, guardrails, service validation, and the audit
log JSONL format.

Route-level tests use the real FastAPI app with ``MOCK_DATA=0`` and
``SERVICES_ROOT`` pointed at on-disk fixtures, so the runner validates against
fixture dirs rather than the live repo.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard import actions, audit
from alpenglow_dashboard.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
ACTION_SERVICES = FIXTURES / "action_services"


# ── shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def real_env(monkeypatch, tmp_path):
    """Non-mock mode, DEV_NO_AUTH admin identity, fixture service dirs, temp
    audit file. Reset the module-global registries so tests don't bleed."""
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(ACTION_SERVICES))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    actions._BUSY.clear()
    actions._SERVICE_LOCKS.clear()
    actions._semaphore = None
    yield tmp_path
    actions._BUSY.clear()
    actions._SERVICE_LOCKS.clear()
    actions._semaphore = None


@pytest.fixture()
def client(real_env) -> TestClient:
    return TestClient(create_app())


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/csrf").json()["token"]
    return {"X-CSRF-Token": token}


class _FakeProc:
    """Stand-in for an asyncio subprocess with configurable returncode/stderr."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"", delay: float = 0.0):
        self.returncode = returncode
        self._stderr = stderr
        self._delay = delay
        self.killed = False

    async def communicate(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return b"", self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _fake_exec_factory(calls: list, returncode: int = 0, stderr: bytes = b"", delay: float = 0.0):
    async def fake_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc(returncode=returncode, stderr=stderr, delay=delay)

    return fake_exec


# ── compose command construction ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "action,expected_tails",
    [
        ("restart", [["restart"]]),
        ("stop", [["stop"]]),
        ("start", [["up", "-d"]]),
        ("pull", [["pull"], ["up", "-d"]]),
    ],
)
def test_compose_commands(action, expected_tails, monkeypatch):
    monkeypatch.setenv("SERVICES_ROOT", str(ACTION_SERVICES))
    cmds = actions._compose_commands("glances", action)
    assert len(cmds) == len(expected_tails)
    for cmd, tail in zip(cmds, expected_tails):
        assert cmd[:2] == ["docker", "compose"]
        assert "--project-directory" in cmd
        pd = cmd[cmd.index("--project-directory") + 1]
        assert pd.endswith("/glances")
        assert cmd[len(cmd) - len(tail):] == tail


def test_compose_commands_rejects_unknown_action():
    with pytest.raises(ValueError):
        actions._compose_commands("glances", "explode")


# ── subprocess runner (fake exec) ─────────────────────────────────────────────


async def test_run_success(monkeypatch):
    calls: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(calls))
    rc, stderr = await actions._run(["docker", "compose", "restart"], timeout=5)
    assert rc == 0 and stderr == ""
    assert calls == [["docker", "compose", "restart"]]


async def test_run_captures_stderr_on_failure(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_exec_factory(calls, returncode=1, stderr=b"boom: no such service\n"),
    )
    rc, stderr = await actions._run(["docker", "compose", "restart"], timeout=5)
    assert rc == 1
    assert "boom: no such service" in stderr


async def test_run_timeout_kills(monkeypatch):
    proc = _FakeProc(returncode=0, delay=10.0)

    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    rc, stderr = await actions._run(["docker", "compose", "pull"], timeout=0.05)
    assert rc == 124
    assert "timed out" in stderr
    assert proc.killed is True


async def test_pull_runs_both_commands(monkeypatch, real_env):
    calls: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(calls))
    await actions._run_job("glances", "pull", "chris", "job-1")
    # pull then up -d
    assert len(calls) == 2
    assert calls[0][-1] == "pull"
    assert calls[1][-2:] == ["up", "-d"]
    # busy cleared afterwards
    assert actions.busy_status("glances") is None


async def test_run_job_stops_at_first_failure(monkeypatch, real_env):
    calls: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_exec_factory(calls, returncode=1, stderr=b"pull failed"),
    )
    await actions._run_job("glances", "pull", "chris", "job-2")
    # second command (up -d) never runs because pull failed
    assert len(calls) == 1
    lines = _read_audit(real_env)
    assert lines[-1]["outcome"] == "failed"
    assert "pull failed" in lines[-1]["stderr"]


# ── busy registry / overlay ───────────────────────────────────────────────────


async def test_busy_registry_overlay_values(monkeypatch, real_env):
    # A slow subprocess keeps the service busy while we observe the overlay.
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowProc:
        returncode = 0

        async def communicate(self):
            started.set()
            await release.wait()
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        return _SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # restart → "restarting"
    actions._BUSY["glances"] = actions._BUSY_STATUS["restart"]
    task = asyncio.create_task(actions._run_job("glances", "restart", "chris", "j"))
    await started.wait()
    assert actions.busy_status("glances") == "restarting"
    assert actions.is_busy("glances") is True
    release.set()
    await task
    assert actions.busy_status("glances") is None


def test_busy_status_unknown_is_none(real_env):
    assert actions.busy_status("nope") is None


# ── route: validation + guardrails ────────────────────────────────────────────


def test_action_unknown_service_404(client):
    resp = client.post(
        "/api/services/does_not_exist/actions", json={"action": "restart"}, headers=_csrf(client)
    )
    assert resp.status_code == 404


def test_action_accepted_returns_job_id(client, monkeypatch, real_env):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory([]))
    resp = client.post(
        "/api/services/glances/actions", json={"action": "restart"}, headers=_csrf(client)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["jobId"].startswith("restart-glances-")


@pytest.mark.parametrize("guarded", ["caddy", "postgres"])
def test_guarded_service_requires_confirm(client, guarded):
    resp = client.post(
        f"/api/services/{guarded}/actions", json={"action": "restart"}, headers=_csrf(client)
    )
    assert resp.status_code == 409
    assert "confirm" in resp.json()["detail"]


def test_guarded_service_with_confirm_accepted(client, monkeypatch, real_env):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory([]))
    resp = client.post(
        "/api/services/caddy/actions",
        json={"action": "restart", "confirm": True},
        headers=_csrf(client),
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


def test_concurrent_action_rejected_while_busy(client):
    # Pre-seed the in-flight registry to simulate an action already running.
    actions._BUSY["glances"] = "restarting"
    resp = client.post(
        "/api/services/glances/actions", json={"action": "restart"}, headers=_csrf(client)
    )
    assert resp.status_code == 409
    assert "in flight" in resp.json()["detail"]


# ── audit log format ──────────────────────────────────────────────────────────


def _read_audit(tmp_path: Path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_audit_accepted_and_final_lines(monkeypatch, real_env):
    calls: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(calls))
    # simulate the route: accepted line, then the job.
    await audit.record("chris", "glances", "restart", "accepted", job_id="job-x")
    await actions._run_job("glances", "restart", "chris", "job-x")
    lines = _read_audit(real_env)
    assert [l["outcome"] for l in lines] == ["accepted", "succeeded"]
    for l in lines:
        assert set(l) == {"ts", "user", "service", "action", "outcome", "jobId", "stderr"}
        assert l["user"] == "chris"
        assert l["service"] == "glances"
        assert l["action"] == "restart"
        assert l["jobId"] == "job-x"
    assert lines[0]["stderr"] == ""


async def test_audit_records_rejected(real_env):
    await audit.record("chris", "glances", "restart", "rejected",
                       stderr_tail="already in flight", job_id="j")
    lines = _read_audit(real_env)
    assert lines[-1]["outcome"] == "rejected"
    assert lines[-1]["stderr"] == "already in flight"


async def test_audit_stderr_tail_truncated(real_env, monkeypatch):
    big = "x" * 5000
    await audit.record("chris", "glances", "pull", "failed", stderr_tail=big)
    lines = _read_audit(real_env)
    assert len(lines[-1]["stderr"]) <= audit._STDERR_TAIL
    assert lines[-1]["stderr"].endswith("x")


async def test_audit_never_raises_on_bad_path(monkeypatch):
    monkeypatch.setenv("AUDIT_PATH", "/proc/nonexistent/cannot/write/audit.jsonl")
    # must not raise even though the dir can't be created / written
    await audit.record("chris", "glances", "restart", "accepted")


def test_audit_ts_is_iso_z(real_env):
    ts = audit._now()
    assert ts.endswith("Z")
    # parseable back to a datetime
    from datetime import datetime

    datetime.fromisoformat(ts.replace("Z", "+00:00"))
