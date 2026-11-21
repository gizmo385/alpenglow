"""Is a service healthy after an automatic update?

For each container the update recreated, over ``AUTO_UPDATE_HEALTH_WAIT``
seconds (3 minutes by default):

- it has to stay running, without Docker restarting it, unless it's a
  one-shot job (compose ``restart: no`` or ``on-failure``, like ollama's
  model pull) that exits cleanly;
- a container with its own healthcheck has to report ``healthy`` by the end
  (``unhealthy`` fails straight away);

and then every Uptime Kuma monitor the unit names has to be up. Anything that
can't be checked (no container found, Kuma unreachable, a monitor name that
doesn't exist) counts as a failure: an automatic update that can't be shown
to work is paused for a person to look at.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

log = logging.getLogger("alpenglow.autoupdate")

_POLL = 10.0
# Kuma checks each monitor on its own interval, so give it a few tries.
_KUMA_TRIES = 6
_KUMA_POLL = 20.0


def health_wait() -> float:
    try:
        return float(os.environ.get("AUTO_UPDATE_HEALTH_WAIT") or 180)
    except ValueError:
        return 180.0


@dataclass(frozen=True)
class ContainerState:
    name: str
    status: str  # running / restarting / exited / ...
    health: Optional[str]  # healthy / unhealthy / starting, or None without a healthcheck
    restarts: int
    image_id: str
    exit_code: Optional[int] = None
    restart_policy: str = ""  # "no"/"" for one-shot jobs, "unless-stopped", ...

    @property
    def finished(self) -> bool:
        """A one-shot job that ran to completion, which is meant to stop
        (the same rule as ``inventory._is_completed_oneoff``)."""
        return (self.status != "running" and self.exit_code == 0
                and self.restart_policy.strip().lower() in ("", "no", "on-failure"))


# (service dir, compose service) → that container's state, or None if it has none.
Inspector = Callable[[str, str], Awaitable[Optional[ContainerState]]]
# monitor name → 1 up, 0 down, 2 pending, 3 maintenance; None if Kuma can't be read;
# KeyError for a monitor that doesn't exist.
MonitorReader = Callable[[], Awaitable[Optional[dict[str, int]]]]


async def inspect(service_dir: str, compose_service: str) -> Optional[ContainerState]:
    """The container compose runs for ``compose_service`` in ``service_dir``."""
    import aiodocker

    from .. import inventory

    root = str(inventory.services_root())
    docker = aiodocker.Docker()
    try:
        found = await docker.containers.list(
            all=True, filters={"label": [f"com.docker.compose.service={compose_service}"]}
        )
        for c in found:
            info = await c.show()
            labels = (info.get("Config") or {}).get("Labels") or {}
            wd = labels.get("com.docker.compose.project.working_dir") or ""
            project = labels.get("com.docker.compose.project") or ""
            if wd.rstrip("/") != f"{root.rstrip('/')}/{service_dir}" and os.path.basename(wd.rstrip("/")) != service_dir \
                    and project != service_dir:
                continue
            state = info.get("State") or {}
            return ContainerState(
                name=str(info.get("Name", "")).lstrip("/"),
                status=str(state.get("Status", "")),
                health=(state.get("Health") or {}).get("Status"),
                restarts=int(info.get("RestartCount") or 0),
                image_id=str(info.get("Image", "")),
                exit_code=state.get("ExitCode"),
                restart_policy=str(((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or ""),
            )
        return None
    finally:
        await docker.close()


async def kuma_status() -> Optional[dict[str, int]]:
    from ..integrations import kuma

    monitors = await kuma._fetch_monitors()
    if monitors is None:
        return None
    return {m.name: m.status for m in monitors}


async def check(
    containers: list[tuple[str, str]],
    monitors: tuple[str, ...],
    *,
    wait: Optional[float] = None,
    poll: float = _POLL,
    inspector: Inspector = inspect,
    read_monitors: MonitorReader = kuma_status,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    kuma_poll: float = _KUMA_POLL,
) -> Optional[str]:
    """``None`` when healthy, else why not."""
    wait = health_wait() if wait is None else wait
    baseline: dict[tuple[str, str], int] = {}
    for key in containers:
        st = await inspector(*key)
        if st is None:
            return f"no container found for {key[1]} in {key[0]}"
        baseline[key] = st.restarts

    elapsed = 0.0
    while True:
        last: dict[tuple[str, str], ContainerState] = {}
        for key in containers:
            st = await inspector(*key)
            if st is None:
                return f"{key[1]} in {key[0]} disappeared"
            if st.finished:
                continue  # a one-shot job that's done
            if st.restarts > baseline[key]:
                return f"{st.name} restarted {st.restarts - baseline[key]} time(s) after the update"
            if st.status != "running":
                code = f" (exit code {st.exit_code})" if st.exit_code else ""
                return f"{st.name} is {st.status or 'not running'}{code}"
            if st.health == "unhealthy":
                return f"{st.name} reports unhealthy"
            last[key] = st
        if elapsed >= wait:
            break
        step = min(poll, wait - elapsed)
        await sleep(step)
        elapsed += step

    starting = [st.name for st in last.values() if st.health not in (None, "healthy")]
    if starting:
        return f"{', '.join(starting)} still not healthy after {wait:.0f}s"

    pending = list(monitors)
    if not pending:
        return None
    for attempt in range(_KUMA_TRIES):
        status = await read_monitors()
        if status is None:
            return "couldn't read Uptime Kuma to check its monitors"
        missing = [m for m in pending if m not in status]
        if missing:
            return f"no Uptime Kuma monitor named {', '.join(missing)}"
        pending = [m for m in pending if status[m] != 1]
        if not pending:
            return None
        if attempt < _KUMA_TRIES - 1:
            await sleep(kuma_poll)
    return f"Uptime Kuma reports {', '.join(pending)} not up"
