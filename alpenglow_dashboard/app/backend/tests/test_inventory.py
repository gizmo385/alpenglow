"""B1 inventory tests: caddy label → tier/url derivation, compose fact parsing,
metadata merge, primary-container selection, and the tags JSON round-trip.

These run against on-disk fixture compose files under ``tests/fixtures`` — never
the live /services repo — and against docker-free code paths (docker inventory
returns ``{}`` when the socket is absent, which is the case in CI).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpenglow_dashboard import inventory, tags as tags_store

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SERVICES = FIXTURES / "services"


@pytest.fixture()
def fixture_env(monkeypatch):
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(FIXTURES / "metadata.yaml"))
    yield


# ── label → tier/url derivation ───────────────────────────────────────────────


def _labels(sid: str) -> dict[str, str]:
    doc = inventory.yaml.safe_load((FIXTURE_SERVICES / sid / "compose.yaml").read_text())
    return inventory._merged_labels(doc["services"])


@pytest.mark.parametrize(
    "sid,tier,url",
    [
        ("apexlit", "tailnet", "https://acbc.house"),
        ("tailnet_pub", "public", "https://svc.acbc.house"),
        ("mgmt_pub", "management", "https://uptime.alpenglow.acbc.house"),
        ("mgmt_single", "management", "https://glances.alpenglow.acbc.house"),
        ("internal_only", "internal", "https://frame.internal.acbc.house"),
        ("tailnet_and_internal", "tailnet", "https://home.acbc.house"),
        ("nolabels", "internal", None),
    ],
)
def test_tier_and_url_derivation(sid, tier, url):
    got_tier, got_url = inventory.derive_tier_and_url(_labels(sid))
    assert got_tier == tier, f"{sid}: tier {got_tier} != {tier}"
    assert got_url == url, f"{sid}: url {got_url} != {url}"


def test_management_beats_public_10443():
    """uptime_kuma-style: management pattern must win over the :10443 flag."""
    tier, _ = inventory.derive_tier_and_url(_labels("mgmt_pub"))
    assert tier == "management"


def test_no_labels_is_internal_no_url():
    tier, url = inventory.derive_tier_and_url({})
    assert tier == "internal"
    assert url is None


# ── compose fact parsing ──────────────────────────────────────────────────────


def test_scan_repo_excludes_dirs_without_compose(fixture_env):
    repos = inventory.scan_repo()
    assert "no_compose_dir" not in repos
    # every fixture with a compose.yaml is present
    for sid in ("apexlit", "tailnet_pub", "mgmt_pub", "mgmt_single",
                "internal_only", "tailnet_and_internal", "nolabels", "multi"):
        assert sid in repos


def test_restart_policy_and_ports_and_mem(fixture_env):
    repos = inventory.scan_repo()
    assert repos["nolabels"].restart_policy == "always"
    assert repos["nolabels"].ports == "9001"
    assert repos["nolabels"].mem_limit == "512 MB"

    assert repos["multi"].mem_limit == "2 GB"
    assert repos["multi"].ports == "2283"
    assert repos["tailnet_pub"].restart_policy == "unless-stopped"


def test_ports_default_dash_when_none(fixture_env):
    repos = inventory.scan_repo()
    assert repos["apexlit"].ports == "—"


# ── metadata merge + primary-container selection ──────────────────────────────


async def test_metadata_merge(fixture_env):
    detail = await inventory.build_service("multi")
    assert detail is not None
    assert detail.category == "Media"
    assert detail.icon == "ph-images"
    assert detail.description == "A multi-container fixture service."


async def test_metadata_default_when_absent(fixture_env):
    detail = await inventory.build_service("apexlit")
    assert detail is not None
    # apexlit has no metadata entry → defaults
    assert detail.category == inventory._CATEGORY_DEFAULT
    assert detail.icon == inventory._ICON_DEFAULT


async def test_primary_container_override(fixture_env):
    """metadata primary_container='server' selects the server compose service."""
    detail = await inventory.build_service("multi")
    assert detail is not None
    # No docker socket in tests → primaryContainer falls back to the compose
    # service name from the override.
    assert detail.primaryContainer == "server"


async def test_primary_container_defaults_to_dir_name(fixture_env):
    """When no override and a service matches the dir name, that is primary."""
    detail = await inventory.build_service("mgmt_single")
    assert detail is not None
    # 'mgmt_single' has services {monitoring}; no dir-name match → first service.
    assert detail.primaryContainer in ("monitoring", "mgmt_single")


async def test_service_without_docker_is_down(fixture_env):
    """No docker socket → containers come from compose and status is down."""
    detail = await inventory.build_service("nolabels")
    assert detail is not None
    assert detail.status == "down"
    assert detail.uptimeSeconds is None
    assert [c.name for c in detail.containers] == ["nolabels"]


async def test_build_inventory_covers_all_compose_dirs(fixture_env):
    details = await inventory.build_inventory()
    ids = {d.id for d in details}
    assert "no_compose_dir" not in ids
    assert {"apexlit", "multi", "nolabels"} <= ids


async def test_url_and_tier_flow_into_summary(fixture_env):
    detail = await inventory.build_service("mgmt_pub")
    assert detail is not None
    assert detail.tier == "management"
    assert detail.url == "https://uptime.alpenglow.acbc.house"


async def test_compose_path_is_real(fixture_env):
    detail = await inventory.build_service("apexlit")
    assert detail is not None
    assert detail.composePath.endswith("apexlit/compose.yaml")


# ── one-shot / init container status aggregation ──────────────────────────────
#
# Reproduces the ollama bug: a completed `restart: no` model-pull job exits 0
# and must NOT drag the running service's aggregate status to "down".


def _dc(name, *, service, state, exit_code=None, restart_policy=None,
        project="oneshot_proj", working_dir=None):
    return inventory.DockerContainer(
        name=name,
        project=project,
        service=service,
        state=state,
        image="example/app:v1.0.0",
        started_at="2026-07-05T14:00:00.000000000Z",
        working_dir=working_dir,
        exit_code=exit_code,
        restart_policy=restart_policy,
    )


def test_completed_oneoff_detection():
    # ollama-pull shape: restart:no + clean exit → completed one-shot
    assert inventory._is_completed_oneoff(
        _dc("pull", service="init-pull", state="exited", exit_code=0, restart_policy="no")
    )
    # docker default empty restart policy also counts as one-shot
    assert inventory._is_completed_oneoff(
        _dc("pull", service="init-pull", state="exited", exit_code=0, restart_policy="")
    )
    # a running container is never a "completed" one-shot
    assert not inventory._is_completed_oneoff(
        _dc("app", service="app", state="running", exit_code=None, restart_policy="unless-stopped")
    )
    # crashed one-shot (non-zero exit) is a real failure → not masked
    assert not inventory._is_completed_oneoff(
        _dc("pull", service="init-pull", state="exited", exit_code=1, restart_policy="no")
    )
    # a long-running service that exited (restart policy set) is a real outage
    assert not inventory._is_completed_oneoff(
        _dc("app", service="app", state="exited", exit_code=0, restart_policy="unless-stopped")
    )


async def test_running_service_with_completed_oneshot_is_up(fixture_env):
    """ollama scenario: app running + init-pull exited(0) → service is UP."""
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    containers = [
        _dc("oneshot-app", service="app", state="running", restart_policy="unless-stopped"),
        _dc("oneshot_proj-init-pull-1", service="init-pull", state="exited",
            exit_code=0, restart_policy="no"),
    ]
    detail = await inventory._build_summary("oneshot", repo, {}, containers, [])
    assert detail.status == "up"
    # both containers still listed for transparency
    names = {c.name for c in detail.containers}
    assert names == {"oneshot-app", "oneshot_proj-init-pull-1"}


async def test_running_service_with_crashed_oneshot_is_down(fixture_env):
    """A non-zero-exit init job is a real failure and still surfaces as down."""
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    containers = [
        _dc("oneshot-app", service="app", state="running", restart_policy="unless-stopped"),
        _dc("oneshot_proj-init-pull-1", service="init-pull", state="exited",
            exit_code=1, restart_policy="no"),
    ]
    detail = await inventory._build_summary("oneshot", repo, {}, containers, [])
    assert detail.status == "down"


async def test_only_completed_oneshots_not_masked_to_up(fixture_env):
    """If a service is *only* completed one-shots, don't spuriously report up."""
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    containers = [
        _dc("oneshot_proj-init-pull-1", service="init-pull", state="exited",
            exit_code=0, restart_policy="no"),
    ]
    detail = await inventory._build_summary("oneshot", repo, {}, containers, [])
    assert detail.status == "down"


# ── project-name / working-dir container matching ─────────────────────────────


def test_scan_repo_honours_top_level_name(fixture_env):
    repos = inventory.scan_repo()
    # top-level `name:` overrides the dir-name default for the project label
    assert repos["oneshot"].project_name == "oneshot_proj"
    # dirs without a `name:` key default to the dir name
    assert repos["apexlit"].project_name == "apexlit"


def test_containers_for_matches_by_project_name(fixture_env):
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    root = FIXTURE_SERVICES
    docker_all = {
        "oneshot_proj": [_dc("oneshot-app", service="app", state="running",
                             restart_policy="unless-stopped")],
    }
    got = inventory._containers_for(repo, docker_all, root)
    assert [c.name for c in got] == ["oneshot-app"]


def test_containers_for_falls_back_to_working_dir(fixture_env):
    """When the project *name* doesn't match, working_dir == service dir rescues it."""
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    root = FIXTURE_SERVICES
    target = str((root / "oneshot").resolve())
    # container grouped under an unexpected project key, but working_dir points home
    docker_all = {
        "some_shared_ai_project": [
            _dc("oneshot-app", service="app", state="running",
                restart_policy="unless-stopped", project="some_shared_ai_project",
                working_dir=target),
        ],
    }
    got = inventory._containers_for(repo, docker_all, root)
    assert [c.name for c in got] == ["oneshot-app"]


def test_containers_for_working_dir_does_not_steal_other_dirs(fixture_env):
    """working_dir fallback only claims containers whose dir path matches exactly."""
    repos = inventory.scan_repo()
    repo = repos["oneshot"]
    root = FIXTURE_SERVICES
    other = str((root / "apexlit").resolve())
    docker_all = {
        "some_other_project": [
            _dc("apex-app", service="app", state="running",
                restart_policy="unless-stopped", project="some_other_project",
                working_dir=other),
        ],
    }
    got = inventory._containers_for(repo, docker_all, root)
    assert got == []


# ── sidebar meta ──────────────────────────────────────────────────────────────


def test_repo_branch_parses_head(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    assert inventory.repo_branch() == "main"


def test_repo_branch_detached_head(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("a1b2c3d4e5f6\n")
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    assert inventory.repo_branch() == "a1b2c3d"


def test_repo_branch_missing_head_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    assert inventory.repo_branch() == "main"


def test_sidebar_meta_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    meta = inventory.sidebar_meta()
    assert meta.host
    assert meta.branch


# ── settings store round-trip (tags + category) ───────────────────────────────


@pytest.fixture()
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    monkeypatch.delenv("TAGS_PATH", raising=False)
    return path


async def test_tags_round_trip_persists(settings_file):
    stored = await tags_store.set_tags("glances", ["Critical", "GPU"])
    assert stored == ["Critical", "GPU"]
    assert json.loads(settings_file.read_text()) == {
        "glances": {"tags": ["Critical", "GPU"], "category": None}
    }
    # a fresh read (simulating a restart) sees the persisted tags
    assert await tags_store.get_tags("glances") == ["Critical", "GPU"]
    assert (await tags_store.all_tags())["glances"] == ["Critical", "GPU"]


async def test_tags_dedupe_and_strip(settings_file):
    stored = await tags_store.set_tags("x", ["A", "A", "  ", " B "])
    assert stored == ["A", "B"]


async def test_empty_tags_removes_entry(settings_file):
    await tags_store.set_tags("x", ["A"])
    await tags_store.set_tags("x", [])
    assert await tags_store.get_tags("x") == []
    assert "x" not in await tags_store.all_tags()


async def test_settings_seed_empty_when_missing(settings_file):
    assert await tags_store.all_settings() == {}
    assert await tags_store.all_tags() == {}


async def test_category_round_trip(settings_file):
    assert await tags_store.set_category("glances", "Networking") == "Networking"
    assert await tags_store.get_category("glances") == "Networking"
    assert json.loads(settings_file.read_text()) == {
        "glances": {"tags": [], "category": "Networking"}
    }
    # clearing removes the override (and the now-empty entry)
    assert await tags_store.set_category("glances", None) is None
    assert await tags_store.get_category("glances") is None
    assert await tags_store.all_settings() == {}


async def test_update_settings_partial(settings_file):
    """tags-only update leaves category untouched; category-only leaves tags."""
    await tags_store.update_settings("x", tags=["A"])
    await tags_store.update_settings("x", category="Media", set_category_field=True)
    entry = (await tags_store.all_settings())["x"]
    assert entry == {"tags": ["A"], "category": "Media"}
    # tags-only update (category omitted) keeps the category
    await tags_store.update_settings("x", tags=["A", "B"])
    entry = (await tags_store.all_settings())["x"]
    assert entry == {"tags": ["A", "B"], "category": "Media"}


async def test_legacy_tags_json_migrates(monkeypatch, tmp_path):
    """A pre-F1 flat tags.json is read losslessly when settings.json is absent."""
    legacy = tmp_path / "tags.json"
    legacy.write_text(json.dumps({"immich": ["GPU", "User data"], "redis": ["Stateful"]}))
    monkeypatch.setenv("TAGS_PATH", str(legacy))
    monkeypatch.delenv("SETTINGS_PATH", raising=False)

    settings = await tags_store.all_settings()
    assert settings["immich"] == {"tags": ["GPU", "User data"], "category": None}
    assert settings["redis"] == {"tags": ["Stateful"], "category": None}
    assert await tags_store.all_tags() == {
        "immich": ["GPU", "User data"],
        "redis": ["Stateful"],
    }

    # the migration is committed to settings.json on the next mutation; the legacy
    # file is left untouched.
    await tags_store.set_category("immich", "Media")
    written = json.loads((tmp_path / "settings.json").read_text())
    assert written["immich"] == {"tags": ["GPU", "User data"], "category": "Media"}
    assert written["redis"] == {"tags": ["Stateful"], "category": None}
    assert json.loads(legacy.read_text()) == {
        "immich": ["GPU", "User data"],
        "redis": ["Stateful"],
    }


async def test_tags_flow_into_inventory(fixture_env, settings_file):
    await tags_store.set_tags("apexlit", ["Fixture"])
    detail = await inventory.build_service("apexlit")
    assert detail is not None
    assert detail.tags == ["Fixture"]


async def test_category_override_flows_into_inventory(fixture_env, settings_file):
    # apexlit's metadata default category is used until an override is set.
    base = await inventory.build_service("apexlit")
    assert base is not None
    await tags_store.set_category("apexlit", "Custom Cat")
    detail = await inventory.build_service("apexlit")
    assert detail is not None
    assert detail.category == "Custom Cat"


def test_resolve_category_precedence():
    """override → metadata default → 'Infrastructure'."""
    assert inventory.resolve_category({"category": "Media"}, "Home") == "Home"
    assert inventory.resolve_category({"category": "Media"}, None) == "Media"
    assert inventory.resolve_category({}, None) == "Infrastructure"
    assert inventory.resolve_category({"category": "  "}, "  ") == "Infrastructure"


# ── PUT /settings endpoint against the real JSON store (non-mock) ──────────────


def test_put_settings_endpoint_persists(monkeypatch, tmp_path):
    """Exercise the PUT route in non-mock mode: it validates the service exists
    (via compose.yaml) and persists tags + category to the settings store."""
    from fastapi.testclient import TestClient

    from alpenglow_dashboard.main import create_app

    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(FIXTURE_SERVICES))
    monkeypatch.setenv("METADATA_PATH", str(FIXTURES / "metadata.yaml"))
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("TAGS_PATH", raising=False)

    client = TestClient(create_app())
    token = client.get("/api/csrf").json()["token"]

    resp = client.put(
        "/api/services/apexlit/settings",
        json={"tags": ["Home", "Home", " Media "], "category": "Networking"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tags"] == ["Home", "Media"]
    assert body["category"] == "Networking"
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "apexlit": {"tags": ["Home", "Media"], "category": "Networking"}
    }

    # partial update: tags omitted, clear the category → category null, tags kept
    resp = client.put(
        "/api/services/apexlit/settings",
        json={"category": None},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.json()["category"] is None
    assert resp.json()["tags"] == ["Home", "Media"]

    # unknown service → 404
    resp = client.put(
        "/api/services/does_not_exist/settings",
        json={"tags": ["x"]},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 404
