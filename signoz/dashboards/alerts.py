#!/usr/bin/env python3
"""SigNoz alert rules as code.

Builds the rules defined at the bottom of this file and creates or updates
them, matched by name, through the SigNoz API. Every query is first run
against live data, so a rule can't silently point at a field or metric that
doesn't exist. Runs the same way as dashboards.py (see its docstring).

    ./alerts.py            # check every query, then create/update
    ./alerts.py --check    # only check the queries

Rules edited in the UI are overwritten on the next run; rules created in the
UI with other names are left alone. Rules named in RETIRED are deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

from dashboards import (
    CADDY_ACCESS_LOG,
    CHECK_WINDOW_MINUTES,
    CROWDSEC_BRUTE_FORCE,
    CROWDSEC_FILE_SERVED,
    CROWDSEC_PARSED,
    PRIVILEGED_DOCKER,
    SSH_LOGIN,
    SUDO,
    SUDO_COMMAND,
    SUDO_REFUSED,
    TRUSTED_SOURCE,
    Query,
    _envelope,
    api,
    counter_total,
)

CHANNEL = "Discord Alerts"


@dataclass
class Rule:
    """Fires when `query` (one builder query) crosses `target` in the way
    `match` describes, evaluated every minute over `window`."""

    name: str
    query: Query
    summary: str
    severity: str = "warning"  # critical | warning | info
    op: str = "above"  # above | below | equal | not_equal
    target: float = 0
    match: str = "at_least_once"  # at_least_once | all_the_times | on_average | in_total | last
    window: str = "5m0s"
    absent_for_minutes: int | None = None  # also fire when the query returns nothing this long
    empty_ok: bool = True  # event rules: no data just means it hasn't happened yet

    def to_json(self) -> dict:
        alert_type = {"logs": "LOGS_BASED_ALERT", "metrics": "METRIC_BASED_ALERT"}[self.query.signal]
        condition = {
            "thresholds": {
                "kind": "basic",
                "spec": [{
                    "name": self.severity,
                    "target": self.target,
                    "matchType": self.match,
                    "op": self.op,
                    "channels": [CHANNEL],
                    "targetUnit": "",
                }],
            },
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "unit": "",
                "queries": [_envelope(self.query)],
            },
            "selectedQueryName": self.query.name,
            "alertOnAbsent": self.absent_for_minutes is not None,
            "requireMinPoints": False,
        }
        if self.absent_for_minutes is not None:
            condition["absentFor"] = self.absent_for_minutes
        return {
            "alert": self.name,
            "alertType": alert_type,
            "ruleType": "threshold_rule",
            "condition": condition,
            "annotations": {"summary": self.summary, "description": self.summary},
            "labels": {},
            "notificationSettings": {
                "groupBy": [],
                "usePolicy": False,
                "renotify": {"enabled": False, "interval": "30m", "alertStates": []},
            },
            "evaluation": {"kind": "rolling", "spec": {"evalWindow": self.window, "frequency": "1m"}},
            "schemaVersion": "v2alpha1",
            "version": "v5",
        }


def check(rule: Rule) -> str | None:
    """Run a rule's query against live data; return a problem or None."""
    end = int(time.time() * 1000)
    body = {
        "schemaVersion": "v1",
        "start": end - CHECK_WINDOW_MINUTES * 60_000,
        "end": end,
        "requestType": "scalar",
        "compositeQuery": {"queries": [_envelope(rule.query)]},
    }
    status, result = api("POST", "/api/v5/query_range", body)
    if status != 200:
        return f"HTTP {status}: {result.get('error', {}).get('message', result)}"
    if rule.empty_ok:
        return None
    for res in result["data"]["data"]["results"]:
        if any(v for row in res.get("data") or [] for v in row):
            return None
    return "no data in the last hour"


def existing_rules() -> dict[str, str]:
    """Rule name -> id for rules already in SigNoz."""
    status, result = api("GET", "/api/v1/rules")
    if status != 200:
        sys.exit(f"listing rules: HTTP {status}: {result}")
    return {r["alert"]: r["id"] for r in result["data"]["rules"]}


def publish(rule: Rule, existing: dict[str, str]) -> None:
    body = rule.to_json()
    if rule.name in existing:
        status, result = api("PUT", f"/api/v1/rules/{existing[rule.name]}", body)
        verb = "updated"
    else:
        status, result = api("POST", "/api/v1/rules", body)
        verb = "created"
    if status not in (200, 201, 204):
        sys.exit(f"{rule.name}: HTTP {status}: {json.dumps(result)[:500]}")
    print(f"{verb}: {rule.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only run the queries")
    args = parser.parse_args()

    problems = 0
    for rule in RULES:
        if problem := check(rule):
            problems += 1
            print(f"  {rule.name}: {problem}")
    print(f"checked {len(RULES)} rules, {problems} with problems")
    if args.check or problems:
        sys.exit(1 if problems else 0)

    existing = existing_rules()
    for rule in RULES:
        publish(rule, existing)
    for name in RETIRED:
        if name in existing:
            status, result = api("DELETE", f"/api/v1/rules/{existing[name]}")
            if status not in (200, 204):
                sys.exit(f"{name}: HTTP {status}: {json.dumps(result)[:500]}")
            print(f"deleted: {name}")


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

RULES = [
    Rule(
        "Sensitive file served to an outside address",
        Query("A", "logs", "count()", CROWDSEC_FILE_SERVED),
        "Something outside the LAN and tailnet requested a sensitive file (.env, .git/, a "
        "backup...) and got a 2xx back. Check the Caddy access log for what was served.",
        severity="critical",
    ),
    Rule(
        "CrowdSec saw a brute-force attempt",
        Query("A", "logs", "count()", CROWDSEC_BRUTE_FORCE),
        "CrowdSec detected repeated failed logins (watch-only: nothing was blocked). "
        "See the Security dashboard for the IP and scenario.",
    ),
    Rule(
        "CrowdSec isn't reading Caddy's access log",
        Query("A", "metrics", counter_total(CROWDSEC_PARSED), [f"source = '{CADDY_ACCESS_LOG}'"]),
        "CrowdSec has parsed no Caddy access-log lines for an hour. Check that the crowdsec "
        "container is running and that Caddy still writes /var/log/caddy/access.log.",
        op="below", target=1, match="all_the_times", window="1h0m0s", absent_for_minutes=60,
        empty_ok=False,
    ),
    Rule(
        "SSH login from outside the LAN or tailnet",
        Query("A", "logs", "count()", [*SSH_LOGIN, f"body NOT REGEXP '{TRUSTED_SOURCE}'"]),
        "An SSH login succeeded from an address outside the LAN and the tailnet.",
        severity="critical",
    ),
    Rule(
        "sudo by an unexpected user",
        Query("A", "logs", "count()", [
            *SUDO_COMMAND,
            "body NOT LIKE '%chris : %'", "body NOT LIKE '%adam : %'", "body NOT LIKE '%alpenglower : %'",
        ]),
        "An account other than chris, adam or alpenglower ran sudo (for example via the "
        "passwordless docker rule).",
        severity="critical",
    ),
    Rule(
        "sudo refused",
        Query("A", "logs", "count()", [*SUDO, SUDO_REFUSED]),
        "sudo refused a command (wrong password, not allowed, or not in sudoers).",
    ),
    Rule(
        "Privileged docker run via sudo",
        Query("A", "logs", "count()", PRIVILEGED_DOCKER),
        "Someone ran docker via sudo with --privileged, --pid=host or the docker socket mounted. "
        "Expected when an admin does it; unexpected otherwise.",
        severity="info",
    ),
]

# Old rule names to delete. Scans and probes that get nothing back are on the
# Security dashboard only; they're routine on a public address.
RETIRED = ["CrowdSec flagged an attack"]

if __name__ == "__main__":
    main()
