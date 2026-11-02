import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard import upgrades
from alpenglow_dashboard.integrations import updates as updates_mod
from alpenglow_dashboard.upgrades import cards as c
from alpenglow_dashboard.upgrades import verdicts as v
from alpenglow_dashboard.upgrades.vikunja import Card


def svc(name="Tandoor", owner="TandoorRecipes", repo="recipes", current="2.7.2", latest="2.8.0",
        image="vabene1111/recipes:2.7.2@sha256:abc", has_updates=True):
    return c.Service(name, owner, repo, image, current, latest if has_updates else current, has_updates,
                     f"https://github.com/{owner}/{repo}/releases", 2 if has_updates else 0)


def card(id, target, done=False, labels=()):
    s = svc(latest=target)
    return Card.from_api({"id": id, "title": c.card_title(s), "done": done,
                          "description": c.card_description(s, None),
                          "labels": [{"title": t} for t in labels]})


# ── cards ───────────────────────────────────────────────────────────────────


def test_image_key_and_strip_tag():
    assert c.image_key("vabene1111/recipes:2.7.2@sha256:abc") == "vabene1111/recipes"
    assert c.image_key("docker.io/library/postgres:17") == "postgres"
    assert c.strip_tag("registry:5000/team/app:1.0") == "registry:5000/team/app"


def test_service_from_tracker_json():
    s = c.Service.from_api({"name": "Dawarich", "owner": "Freika", "repo": "dawarich", "image": "freikin/dawarich:1.20.0",
                            "current_version": "1.20.0", "latest_version": "1.20.1", "has_updates": True,
                            "html_url": "https://github.com/Freika/dawarich/releases", "releases": [{"tag": "1.20.1"}]})
    assert (s.full_repo, s.releases, c.card_title(s)) == ("Freika/dawarich", 1, "Dawarich 1.20.0 → 1.20.1")


def test_description_round_trips():
    lock = c.LockEntry(policy="approve", compatibility=("Kitshn",))
    card_ = Card.from_api({"id": 1, "title": "t", "description": c.card_description(svc(), lock)})
    assert (card_.repo, card_.target) == ("TandoorRecipes/recipes", "2.8.0")
    assert "Kitshn" in card_.description and "approve" in card_.description


def test_plan_rules():
    assert c.plan([svc()], [], {}).create == [svc()]
    assert c.plan([svc()], [card(1, "2.8.0", done=True)], {}).create == []  # closed for this version
    p = c.plan([svc()], [card(1, "2.7.3")], {})
    assert [(x.id, s.latest_version) for x, s in p.retarget] == [(1, "2.8.0")]
    p = c.plan([svc(has_updates=False)], [card(1, "2.8.0")], {})
    assert [x.id for x, _ in p.close] == [1]


def test_plan_respects_versions_yaml():
    lock = {"vabene1111/recipes": c.LockEntry(policy="float")}
    assert c.plan([svc()], [], lock, listed_only=True).create == []
    assert c.plan([svc()], [], {}, listed_only=True).create == []
    assert c.plan([svc()], [], {"vabene1111/recipes": c.LockEntry(policy="approve")}, listed_only=True).create == [svc()]


def test_load_lock(tmp_path: Path):
    p = tmp_path / "versions.yaml"
    p.write_text("images:\n  docker.io/vabene1111/recipes:\n    tag: 2.7.2\n    policy: approve\n    compatibility: [Kitshn]\n")
    assert c.load_lock(p)["vabene1111/recipes"] == c.LockEntry("approve", "", ("Kitshn",))
    assert c.load_lock(None) == {}


# ── verdicts ────────────────────────────────────────────────────────────────


def test_verdict_from_labels():
    assert v.verdict_from_labels([{"title": "x"}, {"title": "Verdict: needs care"}]) == "Needs care"
    assert v.verdict_from_labels([{"title": "Verdict: Maybe"}]) is None


@pytest.mark.parametrize("body,expected", [
    ("<p><strong>Verdict:</strong> Easy. 1.20.1 is a bug-fix patch.</p>", "1.20.1 is a bug-fix patch."),
    ("<p>Verdict: Easy</p><p>Only bug fixes.</p>", "Only bug fixes."),
    ("<p>Verdict - Easy - drop-in.</p>", "drop-in."),
])
def test_reason_from_comments(body, expected):
    comments = [{"comment": "<p>Verdict: Easy. Old.</p>"}, {"comment": "<p>Working on this</p>"}, {"comment": body}]
    assert v.reason_from_comments(comments, "Easy") == expected


def test_annotations_sorted_and_linked():
    cards_ = [card(1, "2.8.0", labels=["Verdict: Hold"]), card(2, "2.9.0"),
              card(3, "2.8.1", labels=["Verdict: Easy"]), card(4, "2.8.2", done=True, labels=["Verdict: Easy"])]
    out = v.annotations_from(cards_, {1: [{"comment": "<p>Verdict: Hold. DB migration.</p>"}]}, "https://board.example/")
    assert [(a.card_id, a.group) for a in out] == [(3, "Easy"), (1, "Hold"), (2, v.WAITING)]
    assert out[1].note == "DB migration."
    assert out[0].url == "https://board.example/tasks/3"
    data = v.payload(out)
    assert [g["name"] for g in data["groups"]] == ["Easy", "Needs care", "Hold", v.WAITING]
    assert data["items"][0]["repo"] == "TandoorRecipes/recipes"
    assert data["link_label"] == "Open card"


# ── routes, against a fake Vikunja and tracker ──────────────────────────────


class FakeServer:
    def __init__(self):
        self.tasks = {7: {"id": 7, "title": "old", "done": False, "labels": [{"title": "Verdict: Easy"}],
                          "description": c.card_description(svc(latest="2.7.9"), None)}}
        self.comments = {7: [{"comment": "<p><b>Verdict:</b> Easy. Patch release.</p>"}]}
        self.calls = []
        self.services = [svc()]
        self.down = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        path = req.url.path
        if path == "/api/updates":
            return httpx.Response(200, json={"services": [
                {"name": s.name, "owner": s.owner, "repo": s.repo, "image": s.image, "current_version": s.current_version,
                 "latest_version": s.latest_version, "has_updates": s.has_updates, "html_url": s.html_url,
                 "releases": [{}] * s.releases} for s in self.services]})
        if self.down:
            return httpx.Response(401)
        assert req.headers["authorization"] == "Bearer tok"
        if path == "/v1/projects/2/tasks" and req.method == "GET":
            return httpx.Response(200, json=list(self.tasks.values()), headers={"x-pagination-total-pages": "1"})
        if path.startswith("/v1/tasks/") and path.endswith("/comments"):
            tid = int(path.split("/")[3])
            if req.method == "GET":
                return httpx.Response(200, json=self.comments.get(tid, []))
            self.comments.setdefault(tid, []).append(json.loads(req.content))
            return httpx.Response(201, json={})
        if path.startswith("/v1/tasks/") and req.method == "POST":
            body = json.loads(req.content)
            self.tasks[body["id"]] = body
            return httpx.Response(200, json=body)
        return httpx.Response(404)


@pytest.fixture()
def board(monkeypatch):
    server = FakeServer()
    for k, val in {"MOCK_DATA": "0", "VIKUNJA_API_URL": "http://vikunja/v1", "VIKUNJA_API_TOKEN": "tok",
                   "VIKUNJA_PROJECT_ID": "2", "VIKUNJA_PUBLIC_URL": "https://board.example",
                   "VERSIONS_PATH": "/nonexistent/versions.yaml", "UPDATES_TRACKER_URL": "http://tracker"}.items():
        monkeypatch.setenv(k, val)
    transport = httpx.MockTransport(server)
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    upgrades._ANNOTATIONS_CACHE.invalidate()
    updates_mod._CACHE.invalidate()
    yield server
    upgrades._ANNOTATIONS_CACHE.invalidate()
    updates_mod._CACHE.invalidate()


def test_annotations_route_is_outside_auth_and_cached(board, client, monkeypatch):
    monkeypatch.setenv("DEV_NO_AUTH", "0")  # the bot can't pass oauth2-proxy
    data = client.get("/internal/upgrades/annotations").json()
    assert data["items"] == [{"repo": "TandoorRecipes/recipes", "title": "old", "group": "Easy",
                              "note": "Patch release.", "url": "https://board.example/tasks/7", "card_id": 7}]
    client.get("/internal/upgrades/annotations")
    assert board.calls.count(("GET", "/v1/projects/2/tasks")) == 1
    assert client.post("/api/upgrades/sync").status_code == 401  # but sync is behind auth


def test_sync_route_retargets_and_comments(board, client):
    token = client.get("/api/csrf").json()["token"]
    resp = client.post("/api/upgrades/sync", headers={"X-CSRF-Token": token})
    assert resp.json() == {"filed": 0, "retargeted": 1, "closed": 0}
    assert board.tasks[7]["title"] == "Tandoor 2.7.2 → 2.8.0"
    assert "@claude 2.8.0" in board.comments[7][-1]["comment"]


def test_board_errors_mean_no_verdicts(board, client):
    board.down = True
    assert client.get("/internal/upgrades/annotations").json()["items"] == []


def test_off_without_token(monkeypatch, client):
    monkeypatch.delenv("VIKUNJA_API_TOKEN", raising=False)
    monkeypatch.setenv("MOCK_DATA", "0")
    assert upgrades.Settings.from_env() is None
    upgrades._ANNOTATIONS_CACHE.invalidate()
    assert client.get("/internal/upgrades/annotations").json()["items"] == []
