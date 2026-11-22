import asyncio
import json
from pathlib import Path

import httpx

from alpenglow_dashboard import models, security
from alpenglow_dashboard.integrations import updates as updates_mod
from alpenglow_dashboard.security import cards as c
from alpenglow_dashboard.security import findings as f
from alpenglow_dashboard.security import notify as n
from alpenglow_dashboard.security import verdicts as v
from alpenglow_dashboard.upgrades.vikunja import Card


def finding(id, severity="High", fixed=("1.1",), kev=False, epss=None, source="grype", package="libssl3"):
    return {"id": id, "severity": severity, "package": package, "version": "1.0", "type": "apk",
            "fixed_in": list(fixed), "fix_state": "fixed" if fixed else "not-fixed", "kev": kev,
            "epss": epss, "source": source}


def image(ref, sid, findings_, error=None):
    return {"image_id": f"sha256:{ref}", "ref": ref, "repo_digests": [f"{ref}@sha256:abc"],
            "containers": [{"name": sid, "working_dir": f"/services/{sid}"}],
            "packages": 10, "error": error, "findings": findings_}


DOC = {
    "scanned_at": "2026-11-22T04:10:00-07:00",
    "db": {"built": "2026-11-21T00:00:00Z"},
    "errors": [],
    "images": [
        image("caddy:1", "caddy", [
            finding("CVE-1"),                                    # high + fix: open
            finding("CVE-2", severity="Low"),                    # below the bar
            finding("CVE-3", severity="Medium", fixed=(), kev=True),  # KEV: open
            finding("CVE-4", severity="Critical", fixed=()),     # no fix: below the bar
            finding("CVE-5", severity="Medium", epss=0.4),       # EPSS: open
            finding("CVE-6"),                                    # ignored
        ]),
        image("caddy-edge:1", "caddy", [finding("CVE-1")]),     # same advisory, second image
        image("immich:v3", "immich", [finding("GHSA-x", severity="Low", source="ghsa", package="immich")]),
        image("vikunja:1", "vikunja", [], error="refused by proxy"),
        image("redis:8", "redis", []),
    ],
}
IGNORES = [f.Ignore("CVE-6", "caddy", "not reachable")]


def scan():
    return f.build(DOC, IGNORES, 0.1)


# ── findings ─────────────────────────────────────────────────────────────────


def test_build_filters_dedupes_and_sorts():
    s = scan()
    caddy = s.services["caddy"]
    assert [o.id for o in caddy.open] == ["CVE-3", "CVE-1", "CVE-5"]  # KEV first, then severity
    assert [o.id for o, _ in caddy.ignored] == ["CVE-6"]
    assert caddy.below_bar == 2
    assert caddy.worst == "High" and caddy.scanned
    assert [o.why for o in s.services["immich"].open] == ["advisory on the app's repo"]
    assert not s.services["vikunja"].scanned
    assert s.services["redis"].scanned and not s.services["redis"].open
    assert s.db_built == "2026-11-21T00:00:00Z"


def test_ignore_matches_image_name_and_aliases():
    fd = f.Finding.from_dict({**finding("GHSA-a"), "aliases": ["CVE-9"]})
    assert f.Ignore("CVE-9").matches({"x"}, fd)
    assert not f.Ignore("CVE-9", "caddy").matches({"redis"}, fd)
    assert f.strip_tag("docker.io/library/redis:8@sha256:x") == "redis"


def test_load_ignores(tmp_path: Path):
    p = tmp_path / "ignore.yaml"
    p.write_text("ignore:\n  - id: CVE-1\n    image: caddy\n    reason: static Go\n  - nope\n")
    assert f.load_ignores(p) == [f.Ignore("CVE-1", "caddy", "static Go")]
    assert f.load_ignores(tmp_path / "missing.yaml") == []


def test_load_missing_or_garbage(tmp_path: Path):
    assert f.load(tmp_path / "none.json", None, 0.1) is None
    (tmp_path / "bad.json").write_text("{")
    assert f.load(tmp_path / "bad.json", None, 0.1) is None


# ── cards ────────────────────────────────────────────────────────────────────


def sec_card(id, sid, covers, done=False, labels=(), raw_title=None):
    desc = f"<p>{c.SERVICE_LINE} {sid}</p><p>{c.FINDINGS_LINE} {', '.join(covers)}</p>"
    return c.SecurityCard.parse(Card.from_api({"id": id, "title": raw_title or f"{sid} card", "done": done,
                                               "description": desc,
                                               "labels": [{"title": t} for t in labels]}))


def test_parse_card_markers():
    sc = sec_card(1, "caddy", ["CVE-1", "CVE-3"])
    assert sc.service == "caddy" and sc.covers == {"CVE-1", "CVE-3"}


def test_plan_files_adds_closes():
    s = scan()
    cards_ = [
        sec_card(1, "caddy", ["CVE-1"]),               # open, CVE-3 and CVE-5 are new
        sec_card(2, "immich", ["GHSA-x"], done=True),  # closed by hand: don't refile
        sec_card(3, "redis", ["CVE-7"]),               # redis is clean now: close
        sec_card(4, "vikunja", ["CVE-8"]),             # vikunja failed to scan: leave it
    ]
    p = c.plan(s.services, cards_)
    assert p.create == []
    assert [(sc.card.id, sid, new) for sc, sid, new in p.add] == [(1, "caddy", ["CVE-3", "CVE-5"])]
    assert [(sc.card.id, sid) for sc, sid in p.close] == [(3, "redis")]


def test_plan_prunes_findings_that_are_gone():
    s = scan()
    cards_ = [
        sec_card(1, "caddy", ["CVE-1", "CVE-3", "CVE-5", "CVE-9"]),  # CVE-9 fixed, rest still open
        sec_card(2, "immich", ["GHSA-x"]),                           # unchanged: nothing to do
        sec_card(4, "vikunja", ["CVE-8"]),                           # failed scan: don't trust it
    ]
    p = c.plan(s.services, cards_)
    assert p.add == [] and p.close == []
    assert [(sc.card.id, sid, gone) for sc, sid, gone in p.prune] == [(1, "caddy", ["CVE-9"])]


def test_plan_creates_when_no_card():
    p = c.plan(scan().services, [])
    assert p.create == ["caddy", "immich"]


def test_card_description_round_trips():
    s = scan()
    desc = c.card_description("caddy", c.Context("Caddy", "public", "https://x"), s.services["caddy"])
    sc = c.SecurityCard.parse(Card.from_api({"id": 9, "title": "t", "description": desc}))
    assert sc.service == "caddy" and sc.covers == {"CVE-1", "CVE-3", "CVE-5"}
    assert "known-exploited" in desc and "Public" in desc and "not reachable" in desc


class FakeVikunja:
    def __init__(self, cards_):
        self._cards = cards_
        self.calls = []

    async def cards(self):
        return [sc.card for sc in self._cards]

    async def create(self, title, description):
        self.calls.append(("create", title))

    async def update(self, card, **changes):
        self.calls.append(("update", card.id, sorted(changes)))

    async def comment(self, card, text):
        self.calls.append(("comment", card.id, text))


def test_sync_calls():
    api = FakeVikunja([sec_card(1, "caddy", ["CVE-1"]), sec_card(3, "redis", ["CVE-7"])])
    asyncio.run(c.sync(scan().services, {}, api))
    assert ("create", "immich: 1 open security finding") in api.calls
    comment = next(x for x in api.calls if x[0] == "comment" and x[1] == 1)
    assert comment[2].startswith("@claude " + c.NEW_FINDINGS_MARK)
    closing = next(x for x in api.calls if x[0] == "comment" and x[1] == 3)
    assert not closing[2].startswith("@claude")
    assert ("update", 3, ["done"]) in api.calls


def test_sync_prune_reasks_only_act_now():
    api = FakeVikunja([sec_card(1, "caddy", ["CVE-1", "CVE-3", "CVE-5", "CVE-9"], labels=["Verdict: Act now"]),
                       sec_card(2, "immich", ["GHSA-x", "GHSA-y"], labels=["Verdict: Next upgrade"])])
    asyncio.run(c.sync(scan().services, {}, api))
    assert ("update", 1, ["description", "title"]) in api.calls
    assert ("update", 2, ["description", "title"]) in api.calls
    act = next(x for x in api.calls if x[0] == "comment" and x[1] == 1)
    assert act[2] == f"@claude {c.GONE_FINDINGS_MARK}: CVE-9. The description lists what's still open. Please assess again."
    mild = next(x for x in api.calls if x[0] == "comment" and x[1] == 2)
    assert mild[2].startswith(c.GONE_FINDINGS_MARK + ": GHSA-y.") and "@claude" not in mild[2]


def test_sync_add_mentions_gone_findings():
    api = FakeVikunja([sec_card(1, "caddy", ["CVE-1", "CVE-9"])])
    asyncio.run(c.sync(scan().services, {}, api))
    text = next(x for x in api.calls if x[0] == "comment" and x[1] == 1)[2]
    assert text.startswith(f"@claude {c.NEW_FINDINGS_MARK}: CVE-3, CVE-5. {c.GONE_FINDINGS_MARK}: CVE-9.")


# ── verdicts ─────────────────────────────────────────────────────────────────


def comment(text):
    return {"comment": f"<p>{text}</p>"}


def test_from_cards_reads_labels_and_staleness():
    cards_ = [
        sec_card(1, "caddy", ["CVE-1"], labels=["Verdict: Next upgrade"]),
        sec_card(2, "immich", ["GHSA-x"], labels=["Verdict: Act now"]),
        sec_card(3, "redis", ["CVE-7"], labels=["Verdict: Not affected"]),
        sec_card(4, "old", ["CVE-8"], done=True, labels=["Verdict: Act now"]),
    ]
    comments = {
        1: [comment("Verdict: Next upgrade. Only the stdlib, not reachable.")],
        2: [comment("**Verdict: Act now** Auth bypass on the public share route.")],
        # Claude answered, then new findings arrived: waiting again.
        3: [comment("Verdict: Not affected. Fine."), comment(f"@claude {c.NEW_FINDINGS_MARK}: CVE-9.")],
    }
    # An Act now card that lost findings is waiting again; a plain note doesn't count.
    assert not v.is_current([comment("Verdict: Act now. Bad."), comment(f"@claude {c.GONE_FINDINGS_MARK}: CVE-1.")])
    assert v.is_current([comment("Verdict: Next upgrade. Meh."), comment(f"{c.GONE_FINDINGS_MARK}: CVE-1.")])
    got = v.from_cards(cards_, comments, "https://board")
    assert [(x.service, x.group) for x in got] == [
        ("immich", v.ACT_NOW), ("caddy", v.NEXT_UPGRADE), ("redis", v.WAITING)]
    assert got[0].note == "Auth bypass on the public share route."
    assert got[0].url == "https://board/tasks/2"


def test_latest_reply_and_payload():
    cs = [comment("Verdict: Act now. One."), comment("thanks"), comment("Verdict: Next upgrade. Two.")]
    assert v.latest_reply(cs) == "Verdict: Next upgrade. Two."
    vs = [v.Verdict("caddy", "Caddy: 2 open", v.ACT_NOW, "why", "u", 1)]
    p = v.payload(vs, {"caddy": "caddyserver/caddy"})
    assert p["accent"] == "red" and p["items"][0]["repo"] == "caddyserver/caddy"
    assert v.payload([], {})["items"] == []


# ── notify ───────────────────────────────────────────────────────────────────


def test_due_pings_act_now_and_public_kev_once():
    s = scan()
    vs = [v.Verdict("immich", "t", v.ACT_NOW, "Auth bypass.", "https://board/tasks/2", 2),
          v.Verdict("caddy", "t", v.NEXT_UPGRADE, "", None, 1)]
    tiers = {"caddy": "public", "immich": "tailnet"}
    got = n.due(vs, s.services, tiers, {"caddy": "Caddy"}, "https://dash", set())
    assert [p.key for p in got] == ["act-now:2", "kev:caddy:CVE-3"]
    assert got[1].url == "https://dash/services/caddy" and "No fix released yet" in got[1].text
    assert n.due(vs, s.services, tiers, {}, None, {p.key for p in got}) == []
    # A KEV on a tailnet service waits for Claude.
    assert [p.key for p in n.due([], s.services, {"caddy": "tailnet"}, {}, None, set())] == []


def test_send_records_and_stops_on_failure(tmp_path: Path):
    state = tmp_path / "pings.json"
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(json.loads(req.content))
        return httpx.Response(204 if len(hits) == 1 else 500)

    pings = [n.Ping("a", "A", "x"), n.Ping("b", "B", "y"), n.Ping("c", "C", "z")]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await n.send(client, "https://discord/hook", pings, "<@&1>", state)

    assert asyncio.run(go()) == 1
    assert n.load_sent(state) == {"a"}
    assert len(hits) == 2 and hits[0]["content"] == "<@&1>"


# ── views ────────────────────────────────────────────────────────────────────


def test_summary_rows_and_counts():
    s = scan()
    by = {"immich": v.Verdict("immich", "t", v.ACT_NOW, "bad", "u", 2)}
    got = security.summary(s, by, {"caddy": "Caddy", "immich": "Immich"})
    assert [r.id for r in got.services] == ["immich", "caddy"]
    assert got.counts == models.SecurityCounts(actNow=1, waiting=1)
    assert got.unscanned == ["vikunja:1"] and got.images == 5
    assert security.summary(None, {}, {}).scannedAt is None


def test_service_view():
    s = scan()
    b = security.Board(
        cards=[sec_card(1, "caddy", ["CVE-1"]), sec_card(5, "caddy", ["CVE-0"], done=True, raw_title="old")],
        comments={1: [comment("Verdict: Next upgrade. Stdlib only.")]},
        verdicts=[v.Verdict("caddy", "t", v.NEXT_UPGRADE, "Stdlib only.", "https://board/tasks/1", 1)],
    )
    got = security.service_view("caddy", s, b, "public", "https://board")
    assert [x.id for x in got.open] == ["CVE-3", "CVE-1", "CVE-5"]
    assert got.ignored[0].ignoredReason == "not reachable"
    assert got.verdict == v.NEXT_UPGRADE and got.canAsk and got.reply.startswith("Verdict")
    assert got.closed == [models.SecurityClosedCard(title="old", url="https://board/tasks/5")]
    none = security.service_view("redis", s, security.EMPTY_BOARD, "internal", None)
    assert none.verdict is None and not none.canAsk and none.open == []


def test_updates_put_flagged_rows_first():
    def entry(sid):
        return models.UpdateEntry(id=sid, name=sid, icon="x", image=sid, currentVersion="1",
                                  latestVersion="2", releasedAt=None, changelogUrl=None)

    sec = {"b": {"securityVerdict": v.NOT_AFFECTED, "securityOpen": 1},
           "c": {"securityVerdict": v.ACT_NOW, "securityOpen": 2},
           "d": {"securityVerdict": v.WAITING, "securityOpen": 1}}
    got = updates_mod._with_security([entry("a"), entry("b"), entry("c"), entry("d")], sec)
    assert [e.id for e in got] == ["c", "d", "b", "a"]
    assert got[0].securityOpen == 2


def test_settings_defaults(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    for k in ("VIKUNJA_API_TOKEN", "SECURITY_PROJECT_ID", "DISCORD_SECURITY_WEBHOOK_URL",
              "SECURITY_FINDINGS_PATH"):
        monkeypatch.delenv(k, raising=False)
    s = security.Settings.from_env()
    assert s is not None and not s.board and s.webhook is None
    assert s.findings_path.name == "findings.json" and s.findings_path.parent.parent.name == "cve-scanner"
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "t")
    monkeypatch.setenv("SECURITY_PROJECT_ID", "7")
    assert security.Settings.from_env().board


def test_load_scan_rereads_on_change(monkeypatch, tmp_path: Path):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(DOC))
    s = security.Settings(findings_path=p, ignore_path=tmp_path / "ignore.yaml")
    first = security.load_scan(s)
    assert security.load_scan(s) is first
    import os
    p.write_text(json.dumps({**DOC, "images": []}))
    os.utime(p, (1, 1))
    assert security.load_scan(s).services == {}


# ── routes (mock mode) ───────────────────────────────────────────────────────


def test_routes_in_mock_mode(client):
    summary = client.get("/api/security").json()
    assert models.SecuritySummary.model_validate(summary).counts.waiting == 1
    detail = client.get("/api/services/caddy/security").json()
    assert models.ServiceSecurity.model_validate(detail).canAsk
    csrf = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
    ask = "/api/services/caddy/security/ask"
    assert client.post(ask, json={"question": "Is this reachable?"}).status_code == 403  # no CSRF token
    assert client.post(ask, json={"question": "Is this reachable?"}, headers=csrf).json()["ok"]
    assert client.post(ask, json={"question": "  "}, headers=csrf).status_code == 422
    assert client.get("/internal/security/annotations").json()["items"] == []
    ov = client.get("/api/overview").json()
    assert ov["updates"]["security"]["nextUpgrade"] == 1
    ups = client.get("/api/updates").json()["services"]
    assert any(u["securityVerdict"] for u in ups)
