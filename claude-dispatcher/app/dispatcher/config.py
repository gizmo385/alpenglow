"""profiles.yaml: which Vikunja project runs which job profile.

A profile is everything a job is allowed to do. Cards never choose their
profile: the project they're in does, and only this file (reviewed in git)
maps projects to profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Always reachable: the model API. Everything else is per profile.
BASE_EGRESS = ("api.anthropic.com",)


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    image: str = "claude-job:local"
    allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob", "WebSearch")
    egress: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()  # Vikunja usernames; empty = anyone on the project
    model: str | None = None
    max_turns: int = 30
    timeout_minutes: int = 15
    memory: str = "2g"
    cpus: float = 1.0
    runtime: str | None = None  # e.g. "runsc" for gVisor, when installed on the job daemon
    instructions: str = ""
    # Kanban columns (bucket titles) to move the card to while a job runs and
    # once Claude has replied. Empty leaves the card where it is.
    working_bucket: str = ""
    replied_bucket: str = ""
    # Verdicts Claude's reply may start with ("Verdict: Easy"). Each one found
    # becomes a "Verdict: <name>" label on the card, replacing older ones.
    verdicts: tuple[str, ...] = ()

    @property
    def all_egress(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(BASE_EGRESS + self.egress))

    def allows(self, username: str | None) -> bool:
        return not self.allowed_users or (username or "") in self.allowed_users


@dataclass(frozen=True)
class Config:
    profiles: dict[str, Profile]
    projects: dict[int, str] = field(default_factory=dict)

    def profile_for(self, project_id: int | None) -> Profile | None:
        name = self.projects.get(project_id or -1)
        return self.profiles.get(name) if name else None


class ConfigError(Exception):
    pass


def _tuple(v) -> tuple[str, ...]:
    return tuple(str(x) for x in (v or []))


def load(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    defaults = raw.get("defaults") or {}
    profiles: dict[str, Profile] = {}
    for name, body in (raw.get("profiles") or {}).items():
        merged = {**defaults, **(body or {})}
        try:
            profiles[name] = Profile(
                name=name,
                description=str(merged.get("description", "")),
                image=str(merged.get("image", Profile.image)),
                allowed_tools=_tuple(merged.get("allowed_tools", Profile.allowed_tools)),
                egress=_tuple(merged.get("egress")),
                allowed_users=_tuple(merged.get("allowed_users")),
                model=merged.get("model"),
                max_turns=int(merged.get("max_turns", Profile.max_turns)),
                timeout_minutes=int(merged.get("timeout_minutes", Profile.timeout_minutes)),
                memory=str(merged.get("memory", Profile.memory)),
                cpus=float(merged.get("cpus", Profile.cpus)),
                runtime=merged.get("runtime"),
                instructions=str(merged.get("instructions", "")),
                working_bucket=str((merged.get("buckets") or {}).get("working", "")),
                replied_bucket=str((merged.get("buckets") or {}).get("replied", "")),
                verdicts=_tuple(merged.get("verdicts")),
            )
        except (TypeError, ValueError) as err:
            raise ConfigError(f"profile {name}: {err}") from err
    projects: dict[int, str] = {}
    for project_id, name in (raw.get("projects") or {}).items():
        if name not in profiles:
            raise ConfigError(f"project {project_id} maps to unknown profile {name!r}")
        projects[int(project_id)] = str(name)
    return Config(profiles=profiles, projects=projects)
