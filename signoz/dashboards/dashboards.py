#!/usr/bin/env python3
"""SigNoz dashboards as code.

Builds the dashboards defined at the bottom of this file in SigNoz's v2
(Perses) format and creates or updates them, matched by display name, through
the SigNoz API. Every query is first run against live data, so a panel can't
silently point at a field or metric that doesn't exist.

    ./dashboards.py            # check every query, then create/update
    ./dashboards.py --check    # only check the queries

Needs a service-account key with the Editor role in ../.api_env
(SIGNOZ_API_KEY=...). Dashboards edited in the UI are overwritten on the next
run, so make lasting changes here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://signoz.alpenglow.acbc.house"
KEY_FILE = Path(__file__).resolve().parent.parent / ".api_env"
# Unmodified dashboards from https://github.com/SigNoz/dashboards, published
# as-is alongside the ones defined here.
LIBRARY_DIR = Path(__file__).resolve().parent / "library"
CHECK_WINDOW_MINUTES = 60
NO_DATA = "no data in the last hour"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _api_key() -> str:
    for line in KEY_FILE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "SIGNOZ_API_KEY":
            return value.strip()
    sys.exit(f"SIGNOZ_API_KEY not found in {KEY_FILE}")


def api(method: str, path: str, body: object = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"SIGNOZ-API-KEY": _api_key(), "Content-Type": "application/json"},
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

    def spec(self, drop_variables: bool = False) -> dict:
        clauses = [c for c in self.where if not (drop_variables and "$" in c)]
        spec = {
            "name": self.name,
            "stepInterval": 60,
            "signal": self.signal,
            "source": "",
            "disabled": self.hidden,
            "filter": {"expression": " AND ".join(f"({c})" for c in clauses)},
            "groupBy": [k if isinstance(k, dict) else key(k) for k in self.by],
            "order": [],
            "having": {"expression": ""},
            "functions": [],
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

    def plugin(self) -> dict:
        formatting = {"unit": self.unit, "decimalPrecision": "2"}
        visualization = {"timePreference": "global_time"}
        if self.kind == "timeseries":
            return {
                "kind": "signoz/TimeSeriesPanel",
                "spec": {
                    "visualization": {**visualization, "fillSpans": False},
                    "formatting": formatting,
                    "chartAppearance": {
                        "lineInterpolation": "spline",
                        "showPoints": False,
                        "lineStyle": "solid",
                        "fillMode": "none",
                        "spanGaps": {"fillOnlyBelow": False, "fillLessThan": ""},
                    },
                    "axes": AXES,
                    "legend": LEGEND,
                    "thresholds": None,
                },
            }
        if self.kind == "bar":
            return {
                "kind": "signoz/BarChartPanel",
                "spec": {
                    "visualization": {**visualization, "fillSpans": False, "stackedBarChart": True},
                    "formatting": formatting,
                    "axes": AXES,
                    "legend": LEGEND,
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
                "spec": {"visualization": visualization, "formatting": formatting, "legend": LEGEND},
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
class Dashboard:
    name: str
    description: str
    rows: list[list[Panel]]
    variables: list[dict] = field(default_factory=list)

    def panels(self) -> list[Panel]:
        return [p for row in self.rows for p in row]

    def to_json(self) -> dict:
        panels, items, y = {}, [], 0
        for row in self.rows:
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
                "layouts": [{"kind": "Grid", "spec": {"items": items}}],
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
        "Database servers are on the Databases dashboard."
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

HOST = "host IN $host"
REQUESTS = "caddy_http_request_duration_seconds.count"
LATENCY = "caddy_http_request_duration_seconds.bucket"

ingress = Dashboard(
    name="Ingress (Caddy)",
    description="Traffic through Caddy per site: request rate, 4xx/5xx and latency (Caddy's per-host metrics).",
    variables=[variable("host", "metrics")],
    rows=[
        [
            Panel("Requests / s", "number", [Query("A", "metrics", metric(REQUESTS), [HOST])],
                  unit="reqps", width=4, height=3),
            Panel("5xx / s", "number", [Query("A", "metrics", metric(REQUESTS), ["code LIKE '5%'", HOST])],
                  unit="reqps", width=4, height=3, empty_ok=True),
            Panel("p95 latency", "number", [Query("A", "metrics", metric(LATENCY, "", "p95"), [HOST])],
                  unit="s", width=4, height=3),
        ],
        [
            Panel("Requests / s by site", "timeseries",
                  [Query("A", "metrics", metric(REQUESTS), [HOST], by=["host"], legend="{{host}}")],
                  unit="reqps"),
            Panel("p95 latency by site", "timeseries",
                  [Query("A", "metrics", metric(LATENCY, "", "p95"), [HOST], by=["host"], legend="{{host}}")],
                  unit="s"),
        ],
        [
            Panel("5xx / s by site", "timeseries",
                  [Query("A", "metrics", metric(REQUESTS), ["code LIKE '5%'", HOST], by=["host"],
                         legend="{{host}}")], unit="reqps", empty_ok=True),
            Panel("4xx / s by site", "timeseries",
                  [Query("A", "metrics", metric(REQUESTS), ["code LIKE '4%'", HOST], by=["host"],
                         legend="{{host}}")], unit="reqps"),
        ],
        [
            Panel("Responses by status code", "bar",
                  [Query("A", "metrics", metric(REQUESTS), [HOST], by=["code"], legend="{{code}}")],
                  unit="reqps"),
            Panel("Upstream health", "table",
                  [Query("A", "metrics", metric("caddy_reverse_proxy_upstreams_healthy", "min", "min"),
                         by=["upstream"], legend="healthy")],
                  description="1 = healthy, 0 = Caddy considers the upstream down."),
        ],
    ],
)

NAMESPACE = "service.namespace IN $service.namespace"
ERRORISH = (
    "body CONTAINS 'error' OR body CONTAINS 'exception' OR body CONTAINS 'traceback' "
    "OR body CONTAINS 'fatal' OR body CONTAINS 'panic'"
)

logs = Dashboard(
    name="Logs",
    description=(
        "Log volume per container and lines mentioning error/exception/traceback/fatal/panic "
        "(a text match: container logs carry no severity until SigNoz log pipelines parse it)."
    ),
    variables=[variable("service.namespace", "logs")],
    rows=[
        [
            Panel("Log lines / min by service", "bar",
                  [Query("A", "logs", "count()", [NAMESPACE], by=[key("service.name", "resource")],
                         legend="{{service.name}}")]),
            Panel("Error-looking lines / min by service", "bar",
                  [Query("A", "logs", "count()", [ERRORISH, NAMESPACE], by=[key("service.name", "resource")],
                         legend="{{service.name}}")]),
        ],
        [
            Panel("Error-looking lines by service", "table", [
                Query("A", "logs", "count()", [ERRORISH, NAMESPACE], by=[key("service.name", "resource")],
                      order_by="count()", limit=25, legend="error-looking"),
                Query("B", "logs", "count()", [NAMESPACE], by=[key("service.name", "resource")],
                      legend="all lines"),
            ], width=4, height=9),
            Panel("Recent error-looking lines", "list",
                  [Query("A", "logs", None, [ERRORISH, NAMESPACE], order_by="timestamp", limit=100)],
                  fields=[key("service.name", "resource"), key("body", "log")], width=8, height=9),
        ],
    ],
)

DB_CLIENT = "spanKind = 'Client' AND dbOperation EXISTS"

databases = Dashboard(
    name="Databases",
    description=(
        "Postgres and Redis as seen by OBI from the server side, plus database calls made by each "
        "app (OBI client spans and the apps' own SQLAlchemy instrumentation)."
    ),
    rows=[
        [
            Panel("Postgres queries / s by operation", "timeseries",
                  [Query("A", "traces", "rate()", ["serviceName = 'postgres'", "spanKind = 'Server'"],
                         by=["dbOperation"], legend="{{dbOperation}}")], unit="reqps"),
            Panel("Postgres p95 query time by operation", "timeseries",
                  [Query("A", "traces", "p95(durationNano)", ["serviceName = 'postgres'", "spanKind = 'Server'"],
                         by=["dbOperation"], legend="{{dbOperation}}")], unit="ns"),
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
                  [Query("A", "traces", "count()", ["serviceName = 'postgres'", "spanKind = 'Server'"],
                         by=["name"], order_by="count()", limit=15, legend="queries"),
                   Query("B", "traces", "p95(durationNano)", ["serviceName = 'postgres'", "spanKind = 'Server'"],
                         by=["name"], legend="p95")],
                  column_units={"B": "ns"}),
        ],
        [
            Panel("Slowest DB calls", "list",
                  [Query("A", "traces", None, [DB_CLIENT], order_by="durationNano", limit=50)],
                  fields=[key("serviceName"), key("name"), key("durationNano", data_type="number"),
                          key("db.statement", "attribute")],
                  width=12, height=8),
        ],
    ],
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

KC_HTTP = "http_server_requests_seconds.count"

keycloak = Dashboard(
    name="Keycloak",
    description="Keycloak's own metrics (HTTP, logins, JVM, DB pool) and traces.",
    rows=[
        [
            Panel("HTTP requests / s by status", "timeseries",
                  [Query("A", "metrics", metric(KC_HTTP), by=["status"], legend="{{status}}")], unit="reqps"),
            Panel("Password checks / min by outcome", "bar",
                  [Query("A", "metrics", metric("keycloak_credentials_password_hashing_validations_total",
                                                "increase", "sum"), by=["outcome"], legend="{{outcome}}")],
                  description="Every password login attempt is hashed and checked once."),
        ],
        [
            Panel("Average response time by endpoint", "table", [
                Query("A", "metrics", metric("http_server_requests_seconds.sum"), by=["uri"], hidden=True),
                Query("B", "metrics", metric(KC_HTTP), by=["uri"], hidden=True),
                Formula("F1", "A / B", "avg"),
            ], column_units={"F1": "s"}),
            Panel("JVM heap used", "timeseries",
                  [Query("A", "metrics", metric("jvm_memory_used_bytes", "avg", "sum"), ["area = 'heap'"],
                         legend="used"),
                   Query("B", "metrics", metric("jvm_memory_committed_bytes", "avg", "sum"), ["area = 'heap'"],
                         legend="committed")], unit="bytes"),
        ],
        [
            Panel("DB connection pool", "timeseries",
                  [Query("A", "metrics", metric("agroal_active_count", "avg", "sum"), legend="active"),
                   Query("B", "metrics", metric("agroal_awaiting_count", "avg", "sum"), legend="waiting")]),
            Panel("Slowest operations (traces)", "table",
                  [Query("A", "traces", "p95(durationNano)", ["serviceName = 'keycloak'", "isRoot = true"],
                         by=["name"], order_by="p95(durationNano)", limit=15, legend="p95"),
                   Query("B", "traces", "count()", ["serviceName = 'keycloak'", "isRoot = true"], by=["name"],
                         legend="count")],
                  column_units={"A": "ns"}),
        ],
    ],
)

# --- YouTube RSS Manager -------------------------------------------------------
# Metrics come from the app itself (youtube_subs_opml/metrics.py for events,
# web/services/library_metrics.py for library gauges refreshed every 5 minutes).

YT_ATTEMPTS = "yt_rss_feed_fetch_attempts"
YT_POLLS = "yt_rss_feed_polls"
YOUTUBE = "platform = 'youtube'"
FAILED_POLL = "outcome != 'ok'"
BY_CHANNEL = {"by": ["channel"], "order_by": "__result", "limit": 20}

youtube_rss = Dashboard(
    name="YouTube RSS Manager",
    description=(
        "Feed polling health (incl. YouTube's intermittent 404s, counted on every "
        "attempt including retries), the library by channel and category, and the "
        "downloader. Video lengths come from the downloader's probe, so length "
        "figures cover probed videos only."
    ),
    rows=[
        [
            Panel("Poll success rate", "number", [
                Query("A", "metrics", counter_total(YT_POLLS), ["outcome = 'ok'"], hidden=True),
                Query("B", "metrics", counter_total(YT_POLLS), hidden=True),
                Formula("F1", "A / B * 100"),
            ], unit="percent", width=2, height=3,
                description="Channel polls that ended in a usable feed, after retries."),
            Panel("404 responses", "number",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), ["status = '404'"])],
                  width=2, height=3, empty_ok=True,
                  description="Every 404 from a feed request, including ones a retry recovered from."),
            Panel("New videos found", "number",
                  [Query("A", "metrics", counter_total("yt_rss_feed_new_videos"))],
                  width=2, height=3, empty_ok=True),
            Panel("Channels followed", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_channels"), ["ignored = 'false'"])],
                  width=2, height=3),
            Panel("Videos recorded", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_channel_videos"))], width=2, height=3),
            Panel("Archive size", "number",
                  [Query("A", "metrics", gauge_now("yt_rss_archive_bytes"))], unit="bytes", width=2, height=3),
        ],
        [
            Panel("Feed requests by status", "bar",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), by=["status"], legend="{{status}}")],
                  description="Every feed request, retries included, by HTTP status (or network_error)."),
            Panel("YouTube 404 rate", "timeseries", [
                Query("A", "metrics", counter_total(YT_ATTEMPTS), [YOUTUBE, "status = '404'"], hidden=True),
                Query("B", "metrics", counter_total(YT_ATTEMPTS), [YOUTUBE], hidden=True),
                Formula("F1", "A / B * 100", "404 %"),
            ], unit="percent", empty_ok=True,
                description="Share of YouTube feed requests answered with 404, per minute of sweeping."),
        ],
        [
            Panel("Channels with failed polls", "table", [
                Query("A", "metrics", counter_total(YT_POLLS), [FAILED_POLL], legend="failed", **BY_CHANNEL),
                Query("B", "metrics", counter_total(YT_POLLS), ["outcome = 'ok'"], by=["channel"], legend="ok"),
            ], width=8, height=8, empty_ok=True,
                description=(
                    "Polls that still failed after retries, per channel, next to its successful polls. "
                    "A channel with failures and no successes over a long range is persistently broken."
                )),
            Panel("Failed polls by final status", "pie",
                  [Query("A", "metrics", counter_total(YT_POLLS), [FAILED_POLL], by=["status"],
                         legend="{{status}}")], width=4, height=8, empty_ok=True),
        ],
        [
            Panel("Requests by attempt number", "bar",
                  [Query("A", "metrics", counter_total(YT_ATTEMPTS), by=["attempt"], legend="attempt {{attempt}}")],
                  description="Attempt 2+ means the first request was throttled or failed and was retried."),
            Panel("Poll time p95 by platform", "timeseries",
                  [Query("A", "metrics", metric("yt_rss_feed_poll_duration.bucket", "", "p95"), by=["platform"],
                         legend="{{platform}}")], unit="s",
                  description="Per channel, including retries and backoff."),
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

DASHBOARDS = [service_health, ingress, logs, databases, jobs, keycloak, youtube_rss]

if __name__ == "__main__":
    main()
