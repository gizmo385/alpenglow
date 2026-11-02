"""Run one Claude job in a locked-down container on the job daemon.

The job daemon is a separate rootless Docker daemon (DOCKER_HOST), so even
full control of it isn't root on the host. Each profile gets:

* an internal network ``claude-jobs-<profile>`` with no route out, and
* an egress proxy ``claude-egress-<profile>`` on that network and on
  ``claude-egress-out``, allowing HTTPS only to the profile's hosts.

A job container joins only its profile's internal network, so the proxy is
its one way out. It runs as uid 1000 with no capabilities, a read-only root
filesystem, tmpfs for $HOME and /work, no host mounts and no secrets other
than the Claude token.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

import docker
from docker.errors import NotFound
from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout

from .config import Profile

log = logging.getLogger("dispatcher.jobs")

LABEL = "claude-dispatcher"
OUT_NETWORK = "claude-egress-out"
PROXY_PORT = 3128


@dataclass(frozen=True)
class JobResult:
    text: str
    is_error: bool
    turns: int | None = None
    cost_usd: float | None = None


def job_network(profile: Profile) -> str:
    return f"claude-jobs-{profile.name}"


def proxy_name(profile: Profile) -> str:
    return f"claude-egress-{profile.name}"


def command(profile: Profile, prompt: str) -> list[str]:
    cmd = [
        "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(profile.max_turns),
        "--allowedTools", ",".join(profile.allowed_tools),
    ]
    if profile.model:
        cmd += ["--model", profile.model]
    if profile.instructions:
        cmd += ["--append-system-prompt", profile.instructions]
    return cmd


def container_options(profile: Profile, token: str) -> dict:
    proxy = f"http://{proxy_name(profile)}:{PROXY_PORT}"
    return dict(
        image=profile.image,
        user="1000:1000",
        environment={
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "NO_PROXY": "",
        },
        network=job_network(profile),
        read_only=True,
        tmpfs={
            "/tmp": "rw,size=256m",
            "/home/node": "rw,size=256m,uid=1000,gid=1000",
            "/work": "rw,size=512m,uid=1000,gid=1000",
        },
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        pids_limit=256,
        mem_limit=profile.memory,
        nano_cpus=int(profile.cpus * 1e9),
        runtime=profile.runtime,
        labels={LABEL: "job", f"{LABEL}.profile": profile.name},
        detach=True,
    )


def parse_output(stdout: str) -> JobResult:
    """Claude Code's ``--output-format json`` result, from the last JSON line."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "result" in data:
            return JobResult(
                text=str(data.get("result") or ""),
                is_error=bool(data.get("is_error")),
                turns=data.get("num_turns"),
                cost_usd=data.get("total_cost_usd"),
            )
    tail = "\n".join(stdout.strip().splitlines()[-20:])
    return JobResult(text=f"Claude didn't return a result. Last output:\n{tail}", is_error=True)


class Runner:
    def __init__(self, token: str, client: docker.DockerClient | None = None):
        self.token = token
        self.client = client or docker.from_env()

    # ── egress ───────────────────────────────────────────────────────────

    def _network(self, name: str, internal: bool):
        try:
            return self.client.networks.get(name)
        except NotFound:
            log.info("creating network %s (internal=%s)", name, internal)
            return self.client.networks.create(name, driver="bridge", internal=internal, labels={LABEL: "network"})

    def ensure_egress(self, profile: Profile) -> None:
        """Make sure the profile's network and proxy exist with its current allowlist."""
        allow = ",".join(profile.all_egress)
        digest = hashlib.sha256(allow.encode()).hexdigest()[:12]
        jobs_net = self._network(job_network(profile), internal=True)
        self._network(OUT_NETWORK, internal=False)
        name = proxy_name(profile)
        try:
            existing = self.client.containers.get(name)
            if existing.labels.get(f"{LABEL}.allow") == digest and existing.status == "running":
                return
            existing.remove(force=True)
        except NotFound:
            pass
        log.info("starting %s allowing %s", name, allow)
        proxy = self.client.containers.create(
            image=profile.image,
            name=name,
            entrypoint=["node", "/opt/egress-proxy.js"],
            environment={"EGRESS_ALLOW": allow, "EGRESS_PORT": str(PROXY_PORT)},
            user="1000:1000",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit="128m",
            pids_limit=64,
            network=OUT_NETWORK,
            restart_policy={"Name": "unless-stopped"},
            labels={LABEL: "egress", f"{LABEL}.allow": digest},
        )
        jobs_net.connect(proxy, aliases=[name])
        proxy.start()

    # ── jobs ─────────────────────────────────────────────────────────────

    def run(self, profile: Profile, prompt: str) -> JobResult:
        self.ensure_egress(profile)
        container = self.client.containers.run(command=command(profile, prompt), **container_options(profile, self.token))
        try:
            try:
                container.wait(timeout=profile.timeout_minutes * 60)
            except (ReadTimeout, RequestsConnectionError):
                container.kill()
                return JobResult(text=f"Stopped after {profile.timeout_minutes} minutes.", is_error=True)
            stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            return parse_output(stdout)
        finally:
            try:
                container.remove(force=True)
            except NotFound:
                pass
