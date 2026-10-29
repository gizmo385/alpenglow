#!/usr/bin/env python3
"""SigNoz dashboards as code.

Builds the dashboards defined at the bottom of this file in SigNoz's v2
(Perses) format and creates or updates them, matched by display name, through
the SigNoz API. Every query is first run against live data, so a panel can't
silently point at a field or metric that doesn't exist.

    ./dashboards.py            # check every query, then create/update
    ./dashboards.py --check    # only check the queries

SigNoz sits behind a Keycloak login and has no API keys (impersonation, see
../casting.yaml), so run this from a container on signoz-network, pointed
straight at SigNoz (see ../README.md). With SigNoz's own login back on, it
runs from anywhere with a service-account key (Editor role) in ../.api_env
(SIGNOZ_API_KEY=...). Dashboards edited in the UI are overwritten on the next
run, so make lasting changes here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = os.environ.get("SIGNOZ_URL", "https://monitoring.acbc.house")
KEY_FILE = Path(__file__).resolve().parent.parent / ".api_env"
# Unmodified dashboards from https://github.com/SigNoz/dashboards, published
# as-is alongside the ones defined here.
LIBRARY_DIR = Path(__file__).resolve().parent / "library"
CHECK_WINDOW_MINUTES = 60
NO_DATA = "no data in the last hour"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _api_key() -> str | None:
    """The key from ../.api_env, or None when there's no key file (run
    in-network against impersonation, where SigNoz ignores keys anyway)."""
    if not KEY_FILE.exists():
        return None
    for line in KEY_FILE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "SIGNOZ_API_KEY":
            return value.strip()
    sys.exit(f"SIGNOZ_API_KEY not found in {KEY_FILE}")


def api(method: str, path: str, body: object = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if key := _api_key():
        headers["SIGNOZ-API-KEY"] = key
    request = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def key(name: str, context: str = "", data_type: str = "string") -> dict:
    return {"name": name, "signal": "", "fieldContext": context, "fieldDataType": data_type}


def metric(name: str, time_agg: str = "rate", space_agg: str = "sum", reduce: str = "avg") -> dict:
    """A metrics aggregation: time_agg per series, then space_agg across them.
    `reduce` turns the series into one number for tables and number panels."""
    return {
        "metricName": name,
        "temporality": "",
        "timeAggregation": time_agg,
        "spaceAggregation": space_agg,
        "reduceTo": reduce,
    }


def counter_total(name: str) -> dict:
    """Increase of a counter: per step in charts, summed over the range in tables."""
    return metric(name, "increase", "sum", "sum")


def gauge_now(name: str) -> dict:
    """Latest value of a gauge, summed across series."""
    return metric(name, "latest", "sum", "last")


@dataclass
class Query:
    """A builder query. `where` clauses are ANDed; clauses that reference a
    dashboard variable ($...) are dropped when checking against live data."""

    name: str
    signal: str
    agg: str | dict | None = None  # expression for traces/logs, metric() for metrics
    where: list[str] = field(default_factory=list)
    by: list[str | dict] = field(default_factory=list)
    order_by: str | None = None  # field or aggregation expression, descending
    limit: int | None = None
    legend: str = ""
    hidden: bool = False
    functions: list[str] = field(default_factory=list)  # e.g. fillZero, cumulativeSum, applied in order
    step: int = 60  # seconds; SigNoz raises it for long ranges

    def spec(self, drop_variables: bool = False) -> dict:
        clauses = [c for c in self.where if not (drop_variables and "$" in c)]
        spec = {
            "name": self.name,
            "stepInterval": self.step,
            "signal": self.signal,
            "source": "",
            "disabled": self.hidden,
            "filter": {"expression": " AND ".join(f"({c})" for c in clauses)},
            "groupBy": [k if isinstance(k, dict) else key(k) for k in self.by],
            "order": [],
            "having": {"expression": ""},
            "functions": [{"name": f} for f in self.functions],
            "legend": self.legend,
        }
        if self.agg is not None:
            spec["aggregations"] = [self.agg if isinstance(self.agg, dict) else {"expression": self.agg}]
        if self.order_by:
            spec["order"] = [{"key": key(self.order_by), "direction": "desc"}]
        if self.limit:
            spec["limit"] = self.limit
        return spec


@dataclass
class Formula:
    name: str
    expression: str
    legend: str = ""

    def spec(self, drop_variables: bool = False) -> dict:
        return {
            "name": self.name,
            "expression": self.expression,
            "disabled": False,
            "having": {"expression": ""},
            "legend": self.legend,
        }


def _envelope(item: Query | Formula, drop_variables: bool = False) -> dict:
    kind = "builder_query" if isinstance(item, Query) else "builder_formula"
    return {"type": kind, "spec": item.spec(drop_variables)}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

REQUEST_TYPES = {
    "timeseries": "time_series",
    "bar": "time_series",
    "number": "scalar",
    "pie": "scalar",
    "table": "scalar",
    "list": "raw",
}

LEGEND = {"position": "bottom", "mode": "list", "customColors": None}
AXES = {"softMin": None, "softMax": None, "isLogScale": False}


@dataclass
class Panel:
    title: str
    kind: str  # one of REQUEST_TYPES
    queries: list[Query | Formula]
    description: str = ""
    unit: str = ""
    column_units: dict[str, str] = field(default_factory=dict)
    fields: list[dict] = field(default_factory=list)  # list panels: columns
    width: int = 6
    height: int = 6
    empty_ok: bool = False  # event panels: no data just means it hasn't happened yet
    colors: dict[str, str] = field(default_factory=dict)  # series legend label -> color
    stacked: bool = True  # bar panels: stack the series (False when one is a subset of another)
    # Timeseries panels: leave gaps longer than a step empty instead of drawing
    # a line across them, with points so lone values still show. For values
    # that aren't zero when nothing happens (ratios); counts use fillZero.
    break_gaps: bool = False

    def plugin(self) -> dict:
        formatting = {"unit": self.unit, "decimalPrecision": "2"}
        visualization = {"timePreference": "global_time"}
        legend = {**LEGEND, "customColors": self.colors or None}
        if self.kind == "timeseries":
            return {
                "kind": "signoz/TimeSeriesPanel",
                "spec": {
                    "visualization": {**visualization, "fillSpans": False},
                    "formatting": formatting,
                    "chartAppearance": {
                        "lineInterpolation": "spline",
                        "showPoints": self.break_gaps,
                        "lineStyle": "solid",
                        "fillMode": "none",
                        # SigNoz joins gaps no longer than max(threshold, step).
                        "spanGaps": {"fillOnlyBelow": True, "fillLessThan": "1m"} if self.break_gaps
                        else {"fillOnlyBelow": False, "fillLessThan": ""},
                    },
                    "axes": AXES,
                    "legend": legend,
                    "thresholds": None,
                },
            }
        if self.kind == "bar":
            return {
                "kind": "signoz/BarChartPanel",
                "spec": {
                    "visualization": {**visualization, "fillSpans": False, "stackedBarChart": self.stacked},
                    "formatting": formatting,
                    "axes": AXES,
                    "legend": legend,
                    "thresholds": None,
                },
            }
        if self.kind == "number":
            return {
                "kind": "signoz/NumberPanel",
                "spec": {"visualization": visualization, "formatting": formatting, "thresholds": None},
            }
        if self.kind == "pie":
            return {
                "kind": "signoz/PieChartPanel",
                "spec": {"visualization": visualization, "formatting": formatting, "legend": legend},
            }
        if self.kind == "table":
            return {
                "kind": "signoz/TablePanel",
                "spec": {
                    "visualization": visualization,
                    "formatting": {"columnUnits": self.column_units, "decimalPrecision": "2"},
                    "thresholds": None,
                },
            }
        if self.kind == "list":
            return {"kind": "signoz/ListPanel", "spec": {"selectFields": self.fields}}
        raise ValueError(f"unknown panel kind {self.kind!r}")

    def query_block(self, drop_variables: bool = False) -> dict:
        request_type = REQUEST_TYPES[self.kind]
        if len(self.queries) == 1 and isinstance(self.queries[0], Query):
            spec = self.queries[0].spec(drop_variables)
            return {
                "kind": request_type,
                "spec": {"name": spec["name"], "plugin": {"kind": "signoz/BuilderQuery", "spec": spec}},
            }
        return {
            "kind": request_type,
            "spec": {
                "plugin": {
                    "kind": "signoz/CompositeQuery",
                    "spec": {"queries": [_envelope(q, drop_variables) for q in self.queries]},
                }
            },
        }

    def to_json(self) -> dict:
        return {
            "kind": "Panel",
            "spec": {
                "display": {"name": self.title, "description": self.description},
                "plugin": self.plugin(),
                "queries": [self.query_block()],
                "links": [],
            },
        }


def variable(name: str, signal: str, multiple: bool = True) -> dict:
    """A dropdown populated from the live values of a field."""
    return {
        "kind": "ListVariable",
        "spec": {
            "display": {"name": name, "description": ""},
            "allowAllValue": multiple,
            "allowMultiple": multiple,
            "customAllValue": "",
            "capturingRegexp": "",
            "sort": "alphabetical-asc",
            "plugin": {"kind": "signoz/DynamicVariable", "spec": {"name": name, "signal": signal}},
            "name": name,
        },
    }


@dataclass
class Section:
    """A titled, collapsible group of rows."""

    title: str
    rows: list[list[Panel]]
    open: bool = True


@dataclass
class Dashboard:
    name: str
    description: str
    rows: list[list[Panel]] = field(default_factory=list)  # untitled, above any sections
    variables: list[dict] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def __post_init__(self) -> None:
        titles = [p.title for p in self.panels()]
        # Panel ids derive from the title, so titles must be unique.
        if duplicates := {t for t in titles if titles.count(t) > 1}:
            raise ValueError(f"{self.name}: duplicate panel titles {sorted(duplicates)}")

    def panels(self) -> list[Panel]:
        return [p for section in self._grids() for row in section.rows for p in row]

    def _grids(self) -> list[Section]:
        return ([Section("", self.rows)] if self.rows else []) + self.sections

    def to_json(self) -> dict:
        panels, layouts = {}, []
        for section in self._grids():
            items, y = [], 0
            for row in section.rows:
                x = 0
                for panel in row:
                    panel_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.name}/{panel.title}"))
                    panels[panel_id] = panel.to_json()
                    items.append({
                        "x": x, "y": y, "width": panel.width, "height": panel.height,
                        "content": {"$ref": f"#/spec/panels/{panel_id}"},
                    })
                    x += panel.width
                y += max(p.height for p in row)
            grid = {"items": items}
            if section.title:
                grid["display"] = {"title": section.title, "collapse": {"open": section.open}}
            layouts.append({"kind": "Grid", "spec": grid})
        return {
            "schemaVersion": "v6",
            "image": "",
            "generateName": True,
            # Shown in the UI, as a hint that edits made there get overwritten.
            "tags": [{"key": "managed-by", "value": "dashboards.py"}],
            "spec": {
                "display": {"name": self.name, "description": self.description},
                "variables": self.variables,
                "panels": panels,
                "layouts": layouts,
                "duration": "6h",
                "refreshInterval": "",
                "links": [],
            },
        }


# ---------------------------------------------------------------------------
# Check / publish
# ---------------------------------------------------------------------------


def check(panel: Panel) -> str | None:
    """Run a panel's queries against live data; return a problem or None."""
    end = int(time.time() * 1000)
    body = {
        "schemaVersion": "v1",
        "start": end - CHECK_WINDOW_MINUTES * 60_000,
        "end": end,
        "requestType": REQUEST_TYPES[panel.kind],
        "compositeQuery": {"queries": [_envelope(q, drop_variables=True) for q in panel.queries]},
    }
    status, result = api("POST", "/api/v5/query_range", body)
    if status != 200:
        return f"HTTP {status}: {result.get('error', {}).get('message', result)}"
    for res in result["data"]["data"]["results"]:
        if res.get("aggregations") or res.get("data") or res.get("rows") or res.get("series"):
            return None
    return NO_DATA


def publish(body: dict, existing: dict[str, dict]) -> None:
    name = body["spec"]["display"]["name"]
    if name in existing:
        # Updates name the dashboard by its internal name, not generateName.
        current = existing[name]
        update = {k: v for k, v in body.items() if k != "generateName"} | {"name": current["name"]}
        status, result = api("PUT", f"/api/v2/dashboards/{current['id']}", update)
        verb = "updated"
    else:
        status, result = api("POST", "/api/v2/dashboards", body)
        verb = "created"
    if status not in (200, 201):
        sys.exit(f"{name}: HTTP {status}: {json.dumps(result)[:500]}")
    print(f"{verb}: {name}")


def existing_dashboards() -> dict[str, dict]:
    """Display name -> {"id", "name"} for dashboards already in SigNoz."""
    status, result = api("GET", "/api/v2/dashboards?limit=200")
    if status != 200:
        sys.exit(f"listing dashboards: HTTP {status}: {result}")
    return {d["spec"]["display"]["name"]: d for d in result["data"]["dashboards"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only run the queries")
    args = parser.parse_args()

    problems = 0
    for dashboard in DASHBOARDS:
        for panel in dashboard.panels():
            problem = check(panel)
            if problem == NO_DATA and panel.empty_ok:
                print(f"  {dashboard.name} / {panel.title}: no data yet (allowed: shows only when it happens)")
            elif problem:
                problems += 1
                print(f"  {dashboard.name} / {panel.title}: {problem}")
    print(f"checked {sum(len(d.panels()) for d in DASHBOARDS)} panels, {problems} with problems")
    if args.check or problems:
        sys.exit(1 if problems else 0)

    existing = existing_dashboards()
    for dashboard in DASHBOARDS:
        publish(dashboard.to_json(), existing)
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        publish(json.loads(path.read_text()), existing)
    for name in RETIRED:
        if name in existing:
            status, result = api("DELETE", f"/api/v2/dashboards/{existing[name]['id']}")
            if status not in (200, 204):
                sys.exit(f"{name}: HTTP {status}: {json.dumps(result)[:500]}")
            print(f"deleted: {name}")


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

SERVICE = "serviceName IN $service.name"
HTTP_SERVER = "spanKind = 'Server' AND httpMethod EXISTS"

service_health = Dashboard(
    name="Service Health",
    description=(
        "HTTP request rate, errors and latency for every traced service (OBI "
        "eBPF traces plus the apps' own OpenTelemetry instrumentation). "
        "Database servers are on the Infrastructure dashboard."
    ),
    variables=[variable("service.name", "traces")],
    rows=[
        [
            Panel("HTTP requests / s", "number", [Query("A", "traces", "rate()", [HTTP_SERVER, SERVICE])],
                  unit="reqps", width=3, height=3),
            Panel("Error rate", "number", [
                Query("A", "traces", "count()", [HTTP_SERVER, "hasError = true", SERVICE], hidden=True),
                Query("B", "traces", "count()", [HTTP_SERVER, SERVICE], hidden=True),
                Formula("F1", "A / B * 100"),
            ], unit="percent", width=3, height=3),
            Panel("p95 latency", "number", [Query("A", "traces", "p95(durationNano)", [HTTP_SERVER, SERVICE])],
                  unit="ns", width=3, height=3),
            Panel("Services reporting", "number",
                  [Query("A", "traces", "count_distinct(serviceName)", [SERVICE])], width=3, height=3),
        ],
        [
            Panel("HTTP requests / s by service", "timeseries",
                  [Query("A", "traces", "rate()", [HTTP_SERVER, SERVICE], by=["serviceName"],
                         legend="{{serviceName}}")], unit="reqps"),
            Panel("p95 latency by service", "timeseries",
                  [Query("A", "traces", "p95(durationNano)", [HTTP_SERVER, SERVICE], by=["serviceName"],
                         legend="{{serviceName}}")], unit="ns"),
        ],
        [
            Panel("Failing spans / min by service", "bar",
                  [Query("A", "traces", "count()", ["hasError = true", SERVICE], by=["serviceName"],
                         legend="{{serviceName}}")],
                  description="Any span marked as an error, including failed outgoing calls and DB queries."),
            Panel("HTTP 5xx / min by service", "bar",
                  [Query("A", "traces", "count()", [HTTP_SERVER, "responseStatusCode LIKE '5%'", SERVICE],
                         by=["serviceName"], legend="{{serviceName}}")], empty_ok=True),
        ],
        [
            Panel("Service summary", "table", [
                Query("A", "traces", "rate()", [HTTP_SERVER, SERVICE], by=["serviceName"], legend="req/s"),
                Query("B", "traces", "count()", [HTTP_SERVER, "hasError = true", SERVICE], by=["serviceName"],
                      hidden=True),
                Query("C", "traces", "count()", [HTTP_SERVER, SERVICE], by=["serviceName"], hidden=True),
                Formula("F1", "B / C * 100", "error %"),
                Query("D", "traces", "p50(durationNano)", [HTTP_SERVER, SERVICE], by=["serviceName"],
                      legend="p50"),
                Query("E", "traces", "p95(durationNano)", [HTTP_SERVER, SERVICE], by=["serviceName"],
                      legend="p95"),
            ], column_units={"A": "reqps", "F1": "percent", "D": "ns", "E": "ns"}, width=12, height=8),
        ],
        [
            Panel("Slowest endpoints (p95)", "table",
                  [Query("A", "traces", "p95(durationNano)", [HTTP_SERVER, SERVICE],
                         by=["serviceName", "name"], order_by="p95(durationNano)", limit=20, legend="p95")],
                  column_units={"A": "ns"}, height=8),
            Panel("Most frequent errors", "table",
                  [Query("A", "traces", "count()", ["hasError = true", SERVICE], by=["serviceName", "name"],
                         order_by="count()", limit=20, legend="errors")], height=8),
        ],
    ],
)

# --- Infrastructure -------------------------------------------------------------
# Host: hostmetrics from signoz-host-metrics (system.*) plus node-exporter's ZFS
# and temperature metrics (node_*). DNS: pihole-exporter. Ingress: Caddy's
# per-host metrics. Databases: Alloy's Postgres/Redis exporters plus OBI spans.

CPU = "system.cpu.time"
MEMORY = "system.memory.usage"
ARC = "node_zfs_arc_size"
TEMP = "node_hwmon_temp_celsius"
FS = "system.filesystem.usage"
# The pools' root datasets; every dataset in a pool reports the pool's free space.
POOL_ROOTS = "mountpoint IN ('/', '/dpool')"
# Values are totals over Pi-hole's rolling 24 hours, not counters.
PIHOLE_UPSTREAMS = "destination NOT IN ('cache', 'blocklist')"
REQUESTS = "caddy_http_request_duration_seconds.count"
LATENCY = "caddy_http_request_duration_seconds.bucket"
DB_CLIENT = "spanKind = 'Client' AND dbOperation EXISTS"
PG_SERVER = ["serviceName = 'postgres'", "spanKind = 'Server'"]
ERROR_LOGS = "severity_text IN ('ERROR', 'FATAL')"


def gauge_max(name: str) -> dict:
    """Latest value of a gauge, the highest across series."""
    return metric(name, "latest", "max", "last")


host_section = Section("Host", [
    [
        Panel("CPU busy", "number", [
            Query("A", "metrics", metric(CPU), ["state != 'idle'"], hidden=True),
            Query("B", "metrics", metric(CPU), hidden=True),
            Formula("F1", "A / B * 100"),
        ], unit="percent", width=2, height=3),
        Panel("Memory used (excl. ZFS ARC)", "number", [
            Query("A", "metrics", gauge_now(MEMORY), ["state = 'used'"], hidden=True),
            Query("B", "metrics", gauge_now(ARC), hidden=True),
            Query("C", "metrics", gauge_now("system.memory.limit"), hidden=True),
            Formula("F1", "(A - B) / C * 100"),
        ], unit="percent", width=2, height=3,
            description="Linux counts the ZFS ARC as used, but ZFS gives it back under memory pressure."),
        Panel("Load (15m)", "number", [Query("A", "metrics", gauge_now("system.cpu.load_average.15m"))],
              width=2, height=3, description="8 logical CPUs."),
        Panel("CPU temperature", "number",
              [Query("A", "metrics", gauge_max(TEMP), ["chip = 'platform_coretemp_0'"])],
              unit="celsius", width=2, height=3),
        Panel("NVMe pool free", "number",
              [Query("A", "metrics", gauge_now(FS), ["state = 'free'", "mountpoint = '/'"])],
              unit="bytes", width=2, height=3),
        Panel("HDD pool free", "number",
              [Query("A", "metrics", gauge_now(FS), ["state = 'free'", "mountpoint = '/dpool'"])],
              unit="bytes", width=2, height=3),
    ],
    [
        Panel("CPU cores busy by state", "timeseries",
              [Query("A", "metrics", metric(CPU), ["state != 'idle'"], by=["state"], legend="{{state}}")],
              description="CPU seconds per second, so 1 = one core fully busy."),
        Panel("Memory", "timeseries", [
            Query("A", "metrics", metric(MEMORY, "avg", "sum"), ["state IN ('used', 'cached', 'free')"],
                  by=["state"], legend="{{state}}"),
            Query("B", "metrics", metric(ARC, "avg", "sum"), legend="ZFS ARC (part of used)"),
        ], unit="bytes"),
    ],
    [
        Panel("Disk busy by device", "timeseries", [
            Query("A", "metrics", metric("system.disk.io_time"), by=["device"], hidden=True),
            Formula("F1", "A * 100", "{{device}}"),
        ], unit="percent",
            description="Share of time each disk had I/O in flight. sda-sdc are the HDD pool (dpool); "
                        "flat at 0 means idle, and they may have spun down."),
        Panel("Disk throughput by device", "timeseries",
              [Query("A", "metrics", metric("system.disk.io"), by=["device", "direction"],
                     legend="{{device}} {{direction}}")], unit="Bps"),
    ],
    [
        Panel("Network by interface", "timeseries",
              [Query("A", "metrics", metric("system.network.io"), by=["device", "direction"],
                     legend="{{device}} {{direction}}")], unit="Bps"),
        Panel("Temperatures", "timeseries",
              [Query("A", "metrics", metric(TEMP, "max", "max"), by=["chip"], legend="{{chip}}")],
              unit="celsius", description="Hottest sensor per chip (coretemp = CPU package and cores)."),
    ],
    [
        Panel("Largest datasets", "table",
              [Query("A", "metrics", gauge_now(FS), ["state = 'used'"], by=["device"], order_by="__result",
                     limit=15, legend="used")],
              column_units={"A": "bytes"}, height=7),
        Panel("Load average", "timeseries", [
            Query("A", "metrics", metric("system.cpu.load_average.1m", "avg", "sum"), legend="1m"),
            Query("B", "metrics", metric("system.cpu.load_average.5m", "avg", "sum"), legend="5m"),
            Query("C", "metrics", metric("system.cpu.load_average.15m", "avg", "sum"), legend="15m"),
        ], height=7),
    ],
])

dns_section = Section("DNS (Pi-hole)", [
    [
        Panel("Queries (24h)", "number", [Query("A", "metrics", gauge_now("pihole_dns_queries_today"))],
              width=2, height=3),
        Panel("Blocked (24h)", "number", [Query("A", "metrics", gauge_max("pihole_ads_percentage_today"))],
              unit="percent", width=2, height=3),
        Panel("Answered from cache (24h)", "number", [
            Query("A", "metrics", gauge_now("pihole_queries_cached"), hidden=True),
            Query("B", "metrics", gauge_now("pihole_dns_queries_today"), hidden=True),
            Formula("F1", "A / B * 100"),
        ], unit="percent", width=2, height=3),
        Panel("Active clients", "number", [Query("A", "metrics", gauge_now("pihole_unique_clients"))],
              width=2, height=3),
        Panel("Domains on blocklists", "number",
              [Query("A", "metrics", gauge_now("pihole_domains_being_blocked"))], width=2, height=3),
        Panel("Blocking enabled", "number", [Query("A", "metrics", gauge_max("pihole_status"))],
              width=2, height=3, description="1 = on, 0 = blocking is disabled."),
    ],
    [
        Panel("Upstream response time", "timeseries",
              [Query("A", "metrics", metric("pihole_forward_destinations_responsetime", "avg", "max"),
                     [PIHOLE_UPSTREAMS], by=["destination"], legend="{{destination}}")],
              unit="s",
              description="10.0.4.20 / fd52:...::20 = dnscrypt-proxy, 100.100.100.100 = Tailscale "
                          "MagicDNS, 192.168.68.1 = the router."),
        Panel("Where queries were answered (24h)", "pie",
              [Query("A", "metrics", gauge_now("pihole_forward_destinations"), by=["destination"],
                     legend="{{destination}}")]),
    ],
    [
        Panel("Reply types (24h)", "pie",
              [Query("A", "metrics", gauge_now("pihole_reply"), ["type NOT IN ('none', 'unknown')"],
                     by=["type"], legend="{{type}}")], width=4),
        Panel("Query types (24h)", "pie",
              [Query("A", "metrics", gauge_now("pihole_querytypes"), by=["type"], legend="{{type}}")], width=4),
        Panel("Top clients (24h)", "table",
              [Query("A", "metrics", gauge_now("pihole_top_sources"), by=["source", "source_name"],
                     order_by="__result", limit=10, legend="queries")], width=4),
    ],
])

ingress_section = Section("Ingress (Caddy)", [
    [
        Panel("Caddy requests / s", "number", [Query("A", "metrics", metric(REQUESTS))],
              unit="reqps", width=4, height=3),
        Panel("Caddy 5xx / s", "number", [Query("A", "metrics", metric(REQUESTS), ["code LIKE '5%'"])],
              unit="reqps", width=4, height=3, empty_ok=True),
        Panel("Caddy p95 latency", "number", [Query("A", "metrics", metric(LATENCY, "", "p95"))],
              unit="s", width=4, height=3),
    ],
    [
        Panel("Requests / s by site", "timeseries",
              [Query("A", "metrics", metric(REQUESTS), by=["host"], legend="{{host}}")], unit="reqps"),
        Panel("p95 latency by site", "timeseries",
              [Query("A", "metrics", metric(LATENCY, "", "p95"), by=["host"], legend="{{host}}")], unit="s"),
    ],
    [
        Panel("5xx / s by site", "timeseries",
              [Query("A", "metrics", metric(REQUESTS), ["code LIKE '5%'"], by=["host"], legend="{{host}}")],
              unit="reqps", empty_ok=True),
        Panel("4xx / s by site", "timeseries",
              [Query("A", "metrics", metric(REQUESTS), ["code LIKE '4%'"], by=["host"], legend="{{host}}")],
              unit="reqps"),
    ],
    [
        Panel("Responses by status code", "bar",
              [Query("A", "metrics", metric(REQUESTS), by=["code"], legend="{{code}}")], unit="reqps"),
        Panel("Upstream health", "table",
              [Query("A", "metrics", metric("caddy_reverse_proxy_upstreams_healthy", "min", "min"),
                     by=["upstream"], legend="healthy")],
              description="1 = healthy, 0 = Caddy considers the upstream down."),
    ],
])

databases_section = Section("Databases", [
    [
        Panel("Postgres connections", "number", [Query("A", "metrics", gauge_now("pg_stat_activity_count"))],
              width=2, height=3),
        Panel("Postgres cache hit rate", "number", [
            Query("A", "metrics", metric("pg_stat_database_blks_hit"), hidden=True),
            Query("B", "metrics", metric("pg_stat_database_blks_read"), hidden=True),
            Formula("F1", "A / (A + B) * 100"),
        ], unit="percent", width=2, height=3,
            description="Block reads served from shared buffers rather than disk (or the OS/ZFS cache)."),
        Panel("Postgres size", "number", [Query("A", "metrics", gauge_now("pg_database_size_bytes"))],
              unit="bytes", width=2, height=3),
        Panel("Redis memory", "number", [Query("A", "metrics", gauge_now("redis_memory_used_bytes"))],
              unit="bytes", width=2, height=3),
        Panel("Redis commands / s", "number", [Query("A", "metrics", metric("redis_commands_processed_total"))],
              unit="reqps", width=2, height=3),
        Panel("Redis hit rate", "number", [
            Query("A", "metrics", metric("redis_keyspace_hits_total"), hidden=True),
            Query("B", "metrics", metric("redis_keyspace_misses_total"), hidden=True),
            Formula("F1", "A / (A + B) * 100"),
        ], unit="percent", width=2, height=3,
            description="Lookups that found their key. Job queues poll for keys that often don't exist "
                        "yet, so a low rate isn't necessarily a problem."),
    ],
    [
        Panel("Postgres connections by database", "timeseries",
              [Query("A", "metrics", metric("pg_stat_activity_count", "avg", "sum"), ["datname != ''"],
                     by=["datname"], legend="{{datname}}")]),
        Panel("Postgres transactions / s by database", "timeseries",
              [Query("A", "metrics", metric("pg_stat_database_xact_commit"), ["datname != ''"],
                     by=["datname"], legend="{{datname}}")], unit="reqps"),
    ],
    [
        Panel("Database sizes", "table",
              [Query("A", "metrics", gauge_now("pg_database_size_bytes"),
                     ["datname NOT IN ('template0', 'template1')"], by=["datname"], order_by="__result",
                     legend="size")],
              column_units={"A": "bytes"}, width=4),
        Panel("Redis memory used", "timeseries",
              [Query("A", "metrics", metric("redis_memory_used_bytes", "avg", "sum"), legend="used")],
              unit="bytes", width=4),
        Panel("Redis keys by database", "timeseries",
              [Query("A", "metrics", metric("redis_db_keys", "avg", "sum"), by=["db"], legend="{{db}}")],
              width=4),
    ],
    [
        Panel("Postgres queries / s by operation", "timeseries",
              [Query("A", "traces", "rate()", PG_SERVER, by=["dbOperation"], legend="{{dbOperation}}")],
              unit="reqps"),
        Panel("Postgres p95 query time by operation", "timeseries",
              [Query("A", "traces", "p95(durationNano)", PG_SERVER, by=["dbOperation"],
                     legend="{{dbOperation}}")], unit="ns"),
    ],
    [
        Panel("DB calls / s by calling service", "timeseries",
              [Query("A", "traces", "rate()", [DB_CLIENT], by=["serviceName"], legend="{{serviceName}}")],
              unit="reqps"),
        Panel("p95 DB call time by calling service", "timeseries",
              [Query("A", "traces", "p95(durationNano)", [DB_CLIENT], by=["serviceName"],
                     legend="{{serviceName}}")], unit="ns"),
    ],
    [
        Panel("Redis commands / s by command", "timeseries",
              [Query("A", "traces", "rate()", ["serviceName = 'redis'", "spanKind = 'Server'"],
                     by=["dbOperation"], legend="{{dbOperation}}")], unit="reqps"),
        Panel("Busiest tables (Postgres)", "table",
              [Query("A", "traces", "count()", PG_SERVER, by=["name"], order_by="count()", limit=15,
                     legend="queries"),
               Query("B", "traces", "p95(durationNano)", PG_SERVER, by=["name"], legend="p95")],
              column_units={"B": "ns"}),
    ],
    [
        Panel("Slowest DB calls", "list",
              [Query("A", "traces", None, [DB_CLIENT], order_by="durationNano", limit=50)],
              fields=[key("serviceName"), key("name"), key("durationNano", data_type="number"),
                      key("db.statement", "attribute")],
              width=12, height=8),
    ],
])

logs_section = Section("Logs", [
    [
        Panel("Log lines / min by service", "bar",
              [Query("A", "logs", "count()", by=[key("service.name", "resource")], legend="{{service.name}}")]),
        Panel("Errors / min by service", "bar",
              [Query("A", "logs", "count()", [ERROR_LOGS], by=[key("service.name", "resource")],
                     legend="{{service.name}}")]),
    ],
    [
        Panel("Errors by service", "table", [
            Query("A", "logs", "count()", [ERROR_LOGS], by=[key("service.name", "resource")],
                  order_by="count()", limit=25, legend="errors"),
            Query("B", "logs", "count()", by=[key("service.name", "resource")], legend="all lines"),
        ], width=4, height=9),
        Panel("Recent errors", "list",
              [Query("A", "logs", None, [ERROR_LOGS], order_by="timestamp", limit=100)],
              fields=[key("service.name", "resource"), key("severity_text", "log"), key("body", "log")],
              width=8, height=9),
    ],
])

infrastructure = Dashboard(
    name="Infrastructure",
    description=(
        "The host (CPU, memory, disks, network, ZFS, temperatures), DNS (Pi-hole), ingress (Caddy), "
        "the shared Postgres and Redis, and logs from every container and the host journal. Error "
        "logs use the severity Alloy infers from each line."
    ),
    sections=[host_section, dns_section, ingress_section, databases_section, logs_section],
)

# Root spans the apps' own instrumentation creates around background work (see
# each repo's tracing code: scheduled jobs, the downloader loop, bot commands).
JOB = (
    "name IN ('download', 'maintenance', 'refresh update cache') "
    "OR name LIKE 'job %' OR name LIKE 'command %'"
)

jobs = Dashboard(
    name="Background Jobs",
    description=(
        "Scheduled jobs, the YouTube downloader loop and Discord bot commands in "
        "youtube_rss_manager, updates-tracker and servo."
    ),
    rows=[
        [
            Panel("Runs / min by job", "bar",
                  [Query("A", "traces", "count()", [JOB], by=["serviceName", "name"],
                         legend="{{serviceName}}: {{name}}")]),
            Panel("Duration by job (max)", "timeseries",
                  [Query("A", "traces", "max(durationNano)", [JOB], by=["serviceName", "name"],
                         legend="{{serviceName}}: {{name}}")], unit="ns"),
        ],
        [
            Panel("Job summary", "table", [
                Query("A", "traces", "count()", [JOB], by=["serviceName", "name"], legend="runs"),
                Query("B", "traces", "countIf(hasError = true)", [JOB], by=["serviceName", "name"],
                      legend="failed"),
                Query("C", "traces", "avg(durationNano)", [JOB], by=["serviceName", "name"], legend="avg"),
                Query("D", "traces", "max(durationNano)", [JOB], by=["serviceName", "name"], legend="max"),
            ], column_units={"C": "ns", "D": "ns"}, width=12, height=7),
        ],
        [
            Panel("Failed runs", "list",
                  [Query("A", "traces", None, [JOB, "hasError = true"], order_by="timestamp", limit=50)],
                  fields=[key("serviceName"), key("name"), key("durationNano", data_type="number"),
                          key("statusMessage")],
                  width=12, height=6, empty_ok=True),
        ],
    ],
)

# --- YouTube RSS Manager -------------------------------------------------------
# Metrics come from the app itself (youtube_subs_opml/metrics.py for events,
# web/services/library_metrics.py for library gauges refreshed every 5 minutes).

YT_ATTEMPTS = "yt_rss_feed_fetch_attempts"
YT_POLLS = "yt_rss_feed_polls"
YT_FALLBACKS = "yt_rss_feed_api_fallbacks"
FALLBACK_OK = "outcome = 'ok'"
YOUTUBE = "platform = 'youtube'"
FAILED_POLL = "outcome != 'ok'"
BY_CHANNEL = {"by": ["channel"], "order_by": "__result", "limit": 20}

# Series colors are looked up by exact legend label, so each status class gets
# every code in its range.
HTTP_STATUS_COLORS = (
    {str(code): "#2BB673" for code in range(200, 300)}
    | {str(code): "#F5A623" for code in range(400, 500)}
    | {str(code): "#E5484D" for code in range(500, 600)}
    | {"network_error": "#8B8FA3"}
)

youtube_rss = Dashboard(
    name="YouTube RSS Manager",
    description=(
        "Whether channel feeds are arriving (from RSS or, when RSS fails, the Data "
        "API fallback), then RSS-level diagnostics (YouTube's intermittent 404s, "
        "counted on every attempt including retries), the library by channel and "
        "category, and the downloader. Video lengths come from the downloader's "
        "probe, so length figures cover probed videos only."
    ),
    rows=[
        [
            Panel("Feeds refreshed", "number", [
                Query("A", "metrics", counter_total(YT_POLLS), ["outcome = 'ok'"], hidden=True),
                Query("B", "metrics", counter_total(YT_POLLS), hidden=True),
                Query("C", "metrics", counter_total(YT_FALLBACKS), [FALLBACK_OK], hidden=True),
                Formula("F1", "(A + C) / B * 100"),
            ], unit="percent", width=3, height=3,
                description=(
                    "Channel polls that ended in a usable feed: RSS after retries, or failing that the "
                    "Data API fallback. The fallback runs at most hourly per channel, so a long RSS "
                    "outage tops out around a third of 20-minute sweeps."
                )),
            Panel("New videos found", "number",
                  [Query("A", "metrics", counter_total("yt_rss_feed_new_videos"))],
                  width=3, height=3, empty_ok=True),
            Panel("Channels followed", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_channels"), ["ignored = 'false'"])],
                  width=2, height=3),
            Panel("Videos recorded", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_channel_videos"))], width=2, height=3),
            Panel("Archive size", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_archive_bytes"))], unit="bytes", width=2, height=3),
        ],
        [
            Panel("How YouTube polls ended", "bar", [
                Query("A", "metrics", counter_total(YT_POLLS), [YOUTUBE, "outcome = 'ok'"], legend="RSS"),
                Query("B", "metrics", counter_total(YT_POLLS), [YOUTUBE, FAILED_POLL], hidden=True),
                Query("C", "metrics", counter_total(YT_FALLBACKS), [FALLBACK_OK], legend="Data API"),
                Formula("F1", "B - C", "no fresh feed"),
            ], colors={"RSS": "#2BB673", "Data API": "#4E8EF7", "no fresh feed": "#E5484D"},
                description=(
                    "Each channel poll by where its feed came from. 'No fresh feed' polls failed on both "
                    "(or skipped the API because the channel used it within the hour); readers keep "
                    "getting that channel's last cached feed."
                )),
            Panel("Data API fallbacks by outcome", "bar",
                  [Query("A", "metrics", counter_total(YT_FALLBACKS), by=["outcome"], legend="{{outcome}}")],
                  colors={"ok": "#2BB673", "skipped": "#8B8FA3", "rate_limited": "#8B8FA3", "error": "#E5484D"},
                  width=4, empty_ok=True,
                  description=(
                      "Fallback attempts after an RSS failure. 'skipped' means not sent, because the "
                      "channel fell back within YOUTUBE_API_FALLBACK_INTERVAL_MINUTES (named "
                      "'rate_limited' before 2026-09-27); 'error' covers everything Google rejected, "
                      "including its own rate limits and spent quota."
                  )),
            Panel("Data API quota used", "number",
                  [Query("A", "metrics", counter_total(YT_FALLBACKS), ["outcome IN ('ok', 'error')"])],
                  width=2, empty_ok=True,
                  description=(
                      "Units spent over the selected range, 1 per call, against a default 10,000/day "
                      "(resets at midnight Pacific)."
                  )),
        ],
        [
            Panel("Channels with failed RSS polls", "table", [
                Query("A", "metrics", counter_total(YT_POLLS), [FAILED_POLL], legend="failed", **BY_CHANNEL),
                Query("B", "metrics", counter_total(YT_POLLS), ["outcome = 'ok'"], by=["channel"], legend="ok"),
                Query("C", "metrics", counter_total(YT_FALLBACKS), [FALLBACK_OK], by=["channel"],
                      legend="recovered via API"),
            ], width=8, height=8, empty_ok=True,
                description=(
                    "RSS polls that still failed after retries, per channel, next to its successful RSS "
                    "polls and the failures the Data API fallback covered. A channel with failures and "
                    "neither RSS nor API successes over a long range is persistently broken."
                )),
            Panel("Failed RSS polls by final status", "pie",
                  [Query("A", "metrics", counter_total(YT_POLLS), [FAILED_POLL], by=["status"],
                         legend="{{status}}")], colors=HTTP_STATUS_COLORS, width=4, height=8, empty_ok=True,
                  description="Before any Data API fallback; see 'How YouTube polls ended' for what readers got."),
        ],
        # RSS diagnostics. Failures here don't mean feeds went stale: the fallback
        # can cover them (see the rows above).
        [
            Panel("RSS feed requests by status", "bar",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), by=["status"], legend="{{status}}")],
                  colors=HTTP_STATUS_COLORS, width=5,
                  description=(
                      "Every RSS feed request, retries included, by HTTP status (or network_error). "
                      "Failures here may still have been covered by the Data API fallback."
                  )),
            Panel("YouTube RSS 404 rate", "timeseries", [
                Query("A", "metrics", counter_total(YT_ATTEMPTS), [YOUTUBE, "status = '404'"], hidden=True),
                Query("B", "metrics", counter_total(YT_ATTEMPTS), [YOUTUBE], hidden=True),
                Formula("F1", "A / B * 100", "404 %"),
            ], unit="percent", width=5, empty_ok=True,
                description=(
                    "Share of YouTube RSS requests answered with 404, per minute of sweeping. "
                    "Doesn't include the Data API fallback."
                )),
            Panel("RSS 404 responses", "number",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), ["status = '404'"])],
                  width=2, empty_ok=True,
                  description="Every 404 from an RSS feed request, including ones a retry recovered from."),
        ],
        [
            Panel("RSS requests by attempt number", "bar",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), by=["attempt"], legend="attempt {{attempt}}")],
                  width=5,
                  description="Attempt 2+ means the first request was throttled or failed and was retried."),
            Panel("YouTube RSS retries turned off / on", "bar",
                  [Query("A", "metrics", counter_total("yt_rss_feed_retry_breaker"), by=["state"],
                         legend="{{state}}")],
                  colors={"tripped": "#E5484D", "reset": "#2BB673"}, width=3, empty_ok=True,
                  description=(
                      "'tripped': 5 YouTube channels in a row failed their first RSS request, so retries "
                      "were turned off and each channel gets one request before the Data API fallback. "
                      "'reset': 5 in a row worked again, so retries are back on. Marks when an outage "
                      "started and ended."
                  )),
            Panel("Poll time p95 by platform", "timeseries",
                  [Query("A", "metrics", metric("yt_rss_feed_poll_duration.bucket", "", "p95"), by=["platform"],
                         legend="{{platform}}")], unit="s", width=4,
                  description="Per channel RSS fetch, including retries and backoff (not the Data API fallback)."),
        ],
        [
            Panel("Categories", "table", [
                Query("A", "metrics", gauge_now("yt_rss_category_channels"), by=["category"], legend="channels",
                      order_by="__result"),
                Query("B", "metrics", gauge_now("yt_rss_category_videos"), by=["category"], legend="videos"),
                Query("C", "metrics", gauge_now("yt_rss_category_avg_video_duration"), by=["category"],
                      legend="avg length"),
                Query("D", "metrics", gauge_now("yt_rss_category_total_duration"), by=["category"],
                      legend="total runtime"),
            ], column_units={"C": "s", "D": "s"}, width=8, height=8,
                description="Lengths cover probed videos only."),
            Panel("Video lengths", "pie",
                  [Query("A", "metrics", gauge_now("yt_rss_videos_by_length"), by=["length"], legend="{{length}}")],
                  width=4, height=8, description="Probed videos only."),
        ],
        [
            Panel("Most active channels (last 30 days)", "table", [
                Query("A", "metrics", gauge_now("yt_rss_channel_recent_uploads"), legend="uploads (30d)",
                      **BY_CHANNEL),
                Query("B", "metrics", gauge_now("yt_rss_channel_videos"), by=["channel"], legend="videos recorded"),
            ], height=8),
            Panel("Quietest channels", "table",
                  [Query("A", "metrics", gauge_now("yt_rss_channel_days_since_upload"), legend="days since upload",
                         **BY_CHANNEL)],
                  height=8, description="Days since each channel's newest recorded video."),
        ],
        [
            Panel("Largest channels in the archive", "table",
                  [Query("A", "metrics", gauge_now("yt_rss_archive_bytes"), legend="archived", **BY_CHANNEL)],
                  column_units={"A": "bytes"}, height=8),
            Panel("Longest videos on average", "table",
                  [Query("A", "metrics", gauge_now("yt_rss_channel_avg_video_duration"), legend="avg length",
                         **BY_CHANNEL)],
                  column_units={"A": "s"}, height=8, description="Probed videos only."),
        ],
        [
            Panel("Download attempts by outcome", "bar",
                  [Query("A", "metrics", counter_total("yt_rss_downloads"), by=["outcome"],
                         legend="{{outcome}}")], empty_ok=True),
            Panel("Download queue and history", "table",
                  [Query("A", "metrics", gauge_now("yt_rss_download_states"), by=["status", "reason"],
                         order_by="__result", legend="downloads")],
                  description="Every download row by status and skip reason, right now."),
        ],
        [
            Panel("Bytes downloaded", "bar",
                  [Query("A", "metrics", counter_total("yt_rss_downloaded_bytes"), by=["kind"],
                         legend="{{kind}}")], unit="bytes", width=4, empty_ok=True),
            Panel("Download attempt time p95", "timeseries",
                  [Query("A", "metrics", metric("yt_rss_download_duration.bucket", "", "p95"), by=["outcome"],
                         legend="{{outcome}}")], unit="s", width=4, empty_ok=True),
            Panel("Videos with a known length", "number", [
                Query("A", "metrics", gauge_now("yt_rss_videos"), ["duration_known = 'true'"], hidden=True),
                Query("B", "metrics", gauge_now("yt_rss_videos"), hidden=True),
                Formula("F1", "A / B * 100"),
            ], unit="percent", width=4,
                description="Only probed videos have a duration; length panels above cover these."),
        ],
    ],
)

# --- Security ----------------------------------------------------------------
# Host journal entries (the "Host journal" section of alloy/config.alloy) and
# CrowdSec (/services/crowdsec, watch-only). alerts.py alerts on the same
# filters, so they're defined once here.

HOST = "service.namespace = 'host'"
SUDO = [HOST, "service.name = 'sudo'"]
SUDO_COMMAND = [*SUDO, "body CONTAINS 'COMMAND='"]
SSH_LOGIN = [HOST, "service.name = 'sshd'", "body CONTAINS 'Accepted '"]
FIREWALL_BLOCK = [HOST, "service.name = 'kernel'", "body CONTAINS 'UFW BLOCK'"]
# The LAN (192.168.68.0/22) and the tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48).
TRUSTED_SOURCE = (
    "from (192[.]168[.](6[89]|7[01])[.]|100[.](6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])[.]|fd7a:115c:a1e0:)"
)
SUDO_REFUSED = (
    "body CONTAINS 'password is required' OR body CONTAINS 'NOT in sudoers' "
    "OR body CONTAINS 'incorrect password' OR body CONTAINS 'command not allowed'"
)
DOCKER_VIA_SUDO = "body CONTAINS 'COMMAND=/usr/bin/docker'"
PRIVILEGED_FLAGS = "body CONTAINS '--privileged' OR body CONTAINS '--pid=host' OR body CONTAINS 'docker.sock'"
PRIVILEGED_DOCKER = [*SUDO, DOCKER_VIA_SUDO, PRIVILEGED_FLAGS]
# CrowdSec logs "<scope> <value> performed '<scenario>' (N events over T) at <time>"
# once per detection.
CROWDSEC_DETECTION = ["service.name = 'crowdsec'", "body CONTAINS ' performed '"]
# Hub brute-force scenarios end in -bf or _bf (ssh-bf, ssh-slow-bf_user-enum,
# http-generic-401-bf, http-bf-wordpress_bf, ...).
CROWDSEC_BRUTE_FORCE = [*CROWDSEC_DETECTION, "body REGEXP \"performed '[^']+[-_]bf(_[a-z-]+)?'\""]
# Our own scenario (crowdsec/scenarios/): a sensitive-file probe that got a 2xx.
CROWDSEC_FILE_SERVED = [*CROWDSEC_DETECTION, "body CONTAINS \"performed 'acbc/http-sensitive-files-served'\""]
CROWDSEC_PARSED = "cs_parser_hits_ok_total"
CADDY_ACCESS_LOG = "/var/log/host/caddy/access.log"
LOG_FIELDS = [key("service.name", "resource"), key("body", "log")]
# After a restart, Alloy re-reads each container's logs from the second of the
# last line it shipped, so that second's lines land again. CrowdSec and
# Keycloak lines carry their own timestamp, so counting distinct bodies counts
# each event once.
DISTINCT_EVENTS = "count_distinct(body)"

CROWDSEC_POURED = "cs_bucket_poured_total"
# Every request for a file on CrowdSec's sensitive list (the hub's
# sensitive_data.txt) from outside the LAN and tailnet, served or not, and the
# ones that got a 2xx. CrowdSec counts them as events poured into each scenario.
SENSITIVE_REQUESTED = "name = 'crowdsecurity/http-sensitive-files'"
SENSITIVE_SERVED = "name = 'acbc/http-sensitive-files-served'"
# Keycloak's jboss-logging event listener logs failed events at WARN, with the
# IP, client and error: type="LOGIN_ERROR", type="CODE_TO_TOKEN_ERROR", ...
KEYCLOAK = "service.name = 'keycloak'"
KC_EVENT_ERRORS = [KEYCLOAK, "body CONTAINS '[org.keycloak.events]'", "body CONTAINS '_ERROR\"'"]
KC_LOGIN_ERRORS = [KEYCLOAK, "body CONTAINS 'type=\"LOGIN_ERROR\"'"]
KC_HTTP = "http_server_requests_seconds.count"

security = Dashboard(
    name="Security",
    description=(
        "CrowdSec detections (watch-only: nothing is blocked), sensitive-file requests, "
        "Keycloak logins, and sudo, SSH and firewall activity from the host journal. "
        "Alerts on the same data are in alerts.py."
    ),
    rows=[
        [
            Panel("CrowdSec detections", "number", [Query("A", "logs", DISTINCT_EVENTS, CROWDSEC_DETECTION)],
                  width=3, height=3, empty_ok=True),
            Panel("Sensitive-file requests", "number",
                  [Query("A", "metrics", counter_total(CROWDSEC_POURED), [SENSITIVE_REQUESTED])],
                  width=3, height=3, empty_ok=True,
                  description="Requests from outside the LAN and tailnet for files on CrowdSec's sensitive list."),
            Panel("Sensitive files served", "number",
                  [Query("A", "metrics", counter_total(CROWDSEC_POURED), [SENSITIVE_SERVED])],
                  width=3, height=3, empty_ok=True,
                  description="Of those, the ones that got a 2xx. Anything above 0 alerts (critical)."),
            Panel("Failed SSO logins", "number", [Query("A", "logs", DISTINCT_EVENTS, KC_LOGIN_ERRORS)],
                  width=3, height=3, empty_ok=True, description="Keycloak LOGIN_ERROR events."),
        ],
        [
            Panel("Recent CrowdSec detections", "list",
                  [Query("A", "logs", None, CROWDSEC_DETECTION, order_by="timestamp", limit=100)],
                  fields=LOG_FIELDS, width=7, height=7, empty_ok=True,
                  description="Each line names the IP and the scenario it triggered."),
            Panel("Sensitive-file requests / min", "bar", [
                Query("A", "metrics", counter_total(CROWDSEC_POURED), [SENSITIVE_REQUESTED], legend="requested"),
                Query("B", "metrics", counter_total(CROWDSEC_POURED), [SENSITIVE_SERVED], legend="served (2xx)"),
            ], width=5, height=7, empty_ok=True, stacked=False,
                  colors={"requested": "#F5A623", "served (2xx)": "#E5484D"},
                  description=(
                      "Every request for a file on CrowdSec's sensitive list (.env, .git/, backups...) "
                      "from outside the LAN and tailnet, including single probes too small to become a "
                      "detection. Scanners who send enough of them show up in Recent CrowdSec detections."
                  )),
        ],
        [
            Panel("Keycloak error events", "list",
                  [Query("A", "logs", None, KC_EVENT_ERRORS, order_by="timestamp", limit=100)],
                  fields=LOG_FIELDS, width=7, height=7, empty_ok=True,
                  description="Failed logins, token exchanges, etc., with the IP, client and error."),
            Panel("Keycloak password checks / min by outcome", "bar",
                  [Query("A", "metrics", metric("keycloak_credentials_password_hashing_validations_total",
                                                "increase", "sum"), by=["outcome"], legend="{{outcome}}")],
                  width=5, height=7,
                  description="Every password login attempt is hashed and checked once (passkeys aren't)."),
        ],
        [
            Panel("Keycloak HTTP requests / s by status", "timeseries",
                  [Query("A", "metrics", metric(KC_HTTP), by=["status"], legend="{{status}}")], unit="reqps"),
            Panel("Log lines CrowdSec parsed / min by source", "bar",
                  [Query("A", "metrics", counter_total(CROWDSEC_PARSED), by=["source"], legend="{{source}}")],
                  description=(
                      "If Caddy's access log drops to zero, CrowdSec is blind to web attacks "
                      "(alerts.py alerts after an hour)."
                  )),
        ],
        [
            Panel("SSH logins", "number", [Query("A", "logs", "count()", SSH_LOGIN)],
                  width=4, height=3, empty_ok=True),
            Panel("sudo commands", "number", [Query("A", "logs", "count()", SUDO_COMMAND)], width=4, height=3),
            Panel("Firewall blocks", "number", [Query("A", "logs", "count()", FIREWALL_BLOCK)],
                  width=4, height=3, description="Inbound packets UFW dropped (LAN and tailnet)."),
        ],
        [
            Panel("Recent sudo commands", "list",
                  [Query("A", "logs", None, SUDO_COMMAND, order_by="timestamp", limit=200)],
                  fields=LOG_FIELDS, width=12, height=8, empty_ok=True,
                  description="Every sudo invocation with its full command line (sudo docker is root)."),
        ],
        [
            Panel("Recent SSH logins", "list", [Query("A", "logs", None, SSH_LOGIN, order_by="timestamp", limit=50)],
                  fields=LOG_FIELDS, width=6, height=6, empty_ok=True),
            Panel("Refused sudo and privileged docker runs", "list",
                  [Query("A", "logs", None,
                         [*SUDO, f"({SUDO_REFUSED}) OR ({DOCKER_VIA_SUDO} AND ({PRIVILEGED_FLAGS}))"],
                         order_by="timestamp", limit=50)],
                  fields=LOG_FIELDS, width=6, height=6, empty_ok=True),
        ],
        [
            Panel("Firewall blocks / min", "bar", [Query("A", "logs", "count()", FIREWALL_BLOCK, legend="blocked")],
                  width=6),
            Panel("Host warnings and errors / min by source", "bar",
                  [Query("A", "logs", "count()", [HOST, "severity_text IN ('WARN', 'ERROR', 'FATAL')"],
                         by=[key("service.name", "resource")], legend="{{service.name}}")],
                  width=6, empty_ok=True),
        ],
        # Keycloak runtime, from its own metrics and traces.
        [
            Panel("Keycloak average response time by endpoint", "table", [
                Query("A", "metrics", metric("http_server_requests_seconds.sum"), by=["uri"], hidden=True),
                Query("B", "metrics", metric(KC_HTTP), by=["uri"], hidden=True),
                Formula("F1", "A / B", "avg"),
            ], column_units={"F1": "s"}),
            Panel("Keycloak JVM heap used", "timeseries",
                  [Query("A", "metrics", metric("jvm_memory_used_bytes", "avg", "sum"), ["area = 'heap'"],
                         legend="used"),
                   Query("B", "metrics", metric("jvm_memory_committed_bytes", "avg", "sum"), ["area = 'heap'"],
                         legend="committed")], unit="bytes"),
        ],
        [
            Panel("Keycloak DB connection pool", "timeseries",
                  [Query("A", "metrics", metric("agroal_active_count", "avg", "sum"), legend="active"),
                   Query("B", "metrics", metric("agroal_awaiting_count", "avg", "sum"), legend="waiting")]),
            Panel("Keycloak slowest operations (traces)", "table",
                  [Query("A", "traces", "p95(durationNano)", ["serviceName = 'keycloak'", "isRoot = true"],
                         by=["name"], order_by="p95(durationNano)", limit=15, legend="p95"),
                   Query("B", "traces", "count()", ["serviceName = 'keycloak'", "isRoot = true"], by=["name"],
                         legend="count")],
                  column_units={"A": "ns"}),
        ],
    ],
)

# --- Claude Code ---------------------------------------------------------------
# Claude Code's own OpenTelemetry metrics (delta counters) and events (logs),
# sent from the personal machines through otlp.acbc.house. Based on SigNoz's
# library dashboard (SigNoz/dashboards@4cc626eec5 claude-code/). The counters
# only report while Claude Code is in use, so per-step series use fillZero:
# otherwise idle hours are missing buckets that the line is drawn straight
# across.

CC_TOKENS = "claude_code.token.usage"
CC_COST = "claude_code.cost.usage"  # what the same usage would cost on the API, in USD
CC_ACTIVE = "claude_code.active_time.total"
CC_TOOL_RESULT = "body = 'claude_code.tool_result'"
# Monthly price of each subscription, US web prices (claude.com/pricing, 2026-10).
CC_PLANS = {"Pro": 20, "Max 5x": 100, "Max 20x": 200}
SECONDS_PER_MONTH = 365.25 / 12 * 86400
CC_PLAN_COLORS = {"Pro": "#4E8EF7", "Max 5x": "#F5A623", "Max 20x": "#2BB673"}
CC_WINDOW_COLORS = {"1m": "#22c55e", "5m": "#3b82f6", "15m": "#f97316"}


def cc_sum(name: str) -> dict:
    """Total of a Claude Code delta counter: per step in charts, over the range in numbers."""
    return metric(name, "sum", "sum", "sum")


def cc_count(title: str, name: str, description: str) -> Panel:
    return Panel(title, "number", [Query("A", "metrics", cc_sum(name))], description=description,
                 width=2, height=2)


def cc_plan_leverage(plan: str) -> Panel:
    price = CC_PLANS[plan]
    return Panel(f"Leverage: {plan} (${price})", "number", [
        # Average API-equivalent spend per second over the range, idle time included.
        Query("A", "metrics", metric(CC_COST, "rate", "sum", "avg"), functions=["fillZero"], hidden=True),
        Formula("F1", f"A / ({price} / {SECONDS_PER_MONTH:.0f})"),
    ], width=3, height=3,
        description=(f"API-equivalent cost over the selected range / the {plan} plan's ${price} a month "
                     "prorated to the same range. Above 1, the plan costs less than the API would have."))


def cc_cli_user_ratio(step: int, label: str) -> list[Query | Formula]:
    return [
        Query(f"A{label}", "metrics", cc_sum(CC_ACTIVE), ["type = 'cli'"], step=step, hidden=True),
        Query(f"B{label}", "metrics", cc_sum(CC_ACTIVE), ["type = 'user'"], step=step, hidden=True),
        Formula(f"F{label}", f"A{label} / B{label}", f"{label}m"),
    ]


claude_code = Dashboard(
    name="Claude Code Metrics",
    description=(
        "Claude Code usage, tokens, API-equivalent cost and how it compares with the Pro and Max "
        "subscriptions, from the personal machines' OpenTelemetry."
    ),
    sections=[
        Section("Overview", [
            [
                cc_count("Sessions", "claude_code.session.count", "Number of Claude Code sessions"),
                cc_count("Active Time", CC_ACTIVE, "Total active time in Claude Code (seconds)"),
                cc_count("Lines Changed", "claude_code.lines_of_code.count", "Lines of code modified"),
                cc_count("Tool Calls", "claude_code.code_edit_tool.decision",
                         "Accept/reject decisions for code edits"),
                cc_count("Commits", "claude_code.commit.count", "Git commits created"),
                cc_count("PRs", "claude_code.pull_request.count", "Pull requests created"),
            ],
            [
                Panel("Token Usage Over Time", "timeseries",
                      [Query("A", "metrics", cc_sum(CC_TOKENS), functions=["fillZero"], legend="Tokens")],
                      description="Tokens per step, all types (input, output, cache read and creation).",
                      height=5),
                Panel("Cost Over Time (USD)", "timeseries",
                      [Query("A", "metrics", cc_sum(CC_COST), functions=["fillZero"], legend="Cost (USD)")],
                      description="API-equivalent cost per step.", height=5),
            ],
            [
                Panel("Cumulative Tokens", "timeseries",
                      [Query("A", "metrics", cc_sum(CC_TOKENS), functions=["fillZero", "cumulativeSum"],
                             legend="Tokens")],
                      description="Running total since the start of the selected range.", height=5),
                Panel("Cumulative Cost (USD)", "timeseries",
                      [Query("A", "metrics", cc_sum(CC_COST), functions=["fillZero", "cumulativeSum"],
                             legend="Cost (USD)")],
                      description="Running API-equivalent total since the start of the selected range.",
                      height=5),
            ],
        ]),
        Section("Token Breakdown", [
            [
                Panel("Tokens by Model", "bar",
                      [Query("A", "metrics", cc_sum(CC_TOKENS), by=[key("model", "attribute")],
                             legend="{{model}}")],
                      description="Token consumption by model", stacked=False, height=4),
                Panel("Tokens by Type", "bar",
                      [Query("A", "metrics", cc_sum(CC_TOKENS), by=[key("type", "attribute")],
                             legend="{{type}}")],
                      description="Input, output, cache read, cache creation", stacked=False, height=4),
            ],
        ]),
        Section("Cost & Productivity", [
            [
                Panel("Cost by Model", "bar",
                      [Query("A", "metrics", cc_sum(CC_COST), by=[key("model", "attribute")],
                             legend="{{model}}")],
                      description="Cost breakdown by model", stacked=False, height=4),
                Panel("CLI vs User Time", "timeseries",
                      [Query("A", "metrics", cc_sum(CC_ACTIVE), by=[key("type", "attribute")],
                             functions=["fillZero"], legend="{{type}}")],
                      description="Time spent by Claude (CLI) vs waiting for user input", height=4),
            ],
            [
                Panel("Cache Efficiency %", "timeseries", [
                    Query("A", "metrics", cc_sum(CC_TOKENS), ["type = 'cacheRead'"], hidden=True),
                    Query("B", "metrics", cc_sum(CC_TOKENS), ["type = 'input'"], hidden=True),
                    Formula("F1", "(A / (A + B)) * 100", "Cache Efficiency %"),
                ], description="Percentage of tokens served from cache", break_gaps=True, height=7),
                Panel("Usage Leverage", "number", [
                    Query("A", "metrics", cc_sum(CC_ACTIVE), ["type = 'cli'"], hidden=True),
                    Query("B", "metrics", cc_sum(CC_ACTIVE), ["type = 'user'"], hidden=True),
                    Formula("F1", "A / B", "Usage Leverage"),
                ], description="CLI:User time ratio (e.g. 350 = Claude worked 350x more)", width=2, height=4),
                Panel("Leverage Over Time", "timeseries",
                      [*cc_cli_user_ratio(60, "1"), *cc_cli_user_ratio(300, "5"), *cc_cli_user_ratio(900, "15")],
                      description="Rolling CLI:User ratio at 1m, 5m, and 15m windows",
                      colors=CC_WINDOW_COLORS, break_gaps=True, width=4, height=6),
            ],
        ]),
        Section("Cost Analysis (vs Claude plans)", [
            [
                Panel("API Cost (Total)", "number", [Query("A", "metrics", cc_sum(CC_COST))],
                      description="Total hypothetical API cost for selected window", width=3, height=3),
                *[cc_plan_leverage(plan) for plan in CC_PLANS],
            ],
            [
                Panel("Cost Leverage Over Time by Plan", "timeseries", [
                    Query("A", "metrics", metric(CC_COST, "rate", "sum"), functions=["fillZero"], step=900,
                          hidden=True),
                    *[Formula(f"F{i}", f"A / ({price} / {SECONDS_PER_MONTH:.0f})", plan)
                      for i, (plan, price) in enumerate(CC_PLANS.items(), 1)],
                ], colors=CC_PLAN_COLORS, width=12,
                    description=(
                        "API-equivalent spend in each 15 minutes (or each step, on long ranges) / each "
                        "plan's price prorated to the same time. 0 while idle."
                    )),
            ],
        ]),
        Section("Log Insights (from Events)", [
            [
                Panel("Tool Usage Frequency", "bar",
                      [Query("A", "logs", "count()", [CC_TOOL_RESULT], by=[key("tool_name", "attribute")],
                             legend="{{tool_name}}")],
                      description="Which tools are used most often", stacked=False, height=5),
                Panel("Tool Success", "pie",
                      [Query("A", "logs", "count()", [CC_TOOL_RESULT], by=[key("success", "attribute")],
                             legend="{{success}}")],
                      description="Success vs failure rate", width=3, height=5),
                Panel("Recent API Calls", "list",
                      [Query("A", "logs", None, ["body = 'claude_code.api_request'"], order_by="timestamp",
                             limit=10)],
                      fields=[key("model", "attribute"), key("cost_usd", "attribute")],
                      description="Recent API calls with cost", width=3, height=5),
            ],
        ]),
    ],
)
# Claude Code only reports while it's in use, so a quiet hour isn't a broken query.
for panel in claude_code.panels():
    panel.empty_ok = True

DASHBOARDS = [infrastructure, service_health, jobs, youtube_rss, security, claude_code]
# Old dashboard names to delete. Keycloak's panels moved into Security; Ingress,
# Logs and Databases into Infrastructure. HTTP API Monitoring was a library
# dashboard that added little over Service Health.
RETIRED = ["Keycloak", "Ingress (Caddy)", "Logs", "Databases", "HTTP API Monitoring"]

if __name__ == "__main__":
    main()
