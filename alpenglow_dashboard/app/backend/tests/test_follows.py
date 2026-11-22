"""New builds of pinned images that follow a moving tag (``auto: {follow: latest}``).

These images have no GitHub releases, so the tracker never reports them; the
Updates list and Update's repin get them from ``integrations.follows``. The
registry lookups are stubbed; versions.yaml, the compose scan and the matching
are real.
"""

from __future__ import annotations

import textwrap

import pytest

from alpenglow_dashboard import repin
from alpenglow_dashboard.autoupdate import runner
from alpenglow_dashboard.integrations import follows, updates

IMAGE = "ghcr.io/gizmo385/youtube-rss-manager"
DIGEST = "sha256:" + "1" * 64


@pytest.fixture()
def follow_repo(tmp_path, monkeypatch):
    (tmp_path / "versions.yaml").write_text(textwrap.dedent(f"""\
        images:
          {IMAGE}:
            tag: sha-5c5381c
            digest: {DIGEST}
            policy: auto-eligible
            auto:
              follow: latest
          henrygd/beszel:
            tag: 0.21.1
            policy: approve
    """))
    svc = tmp_path / "youtube_rss_manager"
    svc.mkdir()
    (svc / "compose.yaml").write_text(textwrap.dedent(f"""\
        services:
          youtube_rss_manager:
            image: {IMAGE}:sha-5c5381c@{DIGEST}
    """))
    (tmp_path / "metadata.yaml").write_text(textwrap.dedent("""\
        services:
          youtube_rss_manager:
            name: YouTube RSS
            icon: ph-youtube-logo
    """))
    monkeypatch.setenv("MOCK_DATA", "0")
    monkeypatch.setenv("SERVICES_ROOT", str(tmp_path))
    monkeypatch.setenv("METADATA_PATH", str(tmp_path / "metadata.yaml"))
    follows.invalidate()
    yield tmp_path
    follows.invalidate()


def stub_registry(monkeypatch, points_at: str | Exception):
    calls = []

    async def resolve_follows(units):
        calls.append([u.id for u in units])
        return {u.id: points_at for u in units}

    async def built_at(name, tag):
        return "2026-11-22T04:35:00Z"

    monkeypatch.setattr(runner, "resolve_follows", resolve_follows)
    monkeypatch.setattr(follows, "built_at", built_at)
    return calls


@pytest.mark.asyncio
async def test_a_new_build_is_pending(follow_repo, monkeypatch):
    stub_registry(monkeypatch, "sha-fdfa579")
    [f] = await follows.pending()
    assert (f.name, f.follow_tag, f.current, f.latest) == (IMAGE, "latest", "sha-5c5381c", "sha-fdfa579")
    assert f.built_at == "2026-11-22T04:35:00Z"
    assert f.compare_url == "https://github.com/gizmo385/youtube-rss-manager/compare/5c5381c...fdfa579"


@pytest.mark.asyncio
async def test_nothing_pending_when_the_tag_has_not_moved(follow_repo, monkeypatch):
    stub_registry(monkeypatch, "sha-5c5381c")
    assert await follows.pending() == []


@pytest.mark.asyncio
async def test_a_failed_lookup_hides_the_row(follow_repo, monkeypatch):
    stub_registry(monkeypatch, repin.RepinError("ghcr.io returned 503"))
    assert await follows.pending() == []


@pytest.mark.asyncio
async def test_lookups_are_cached_but_repins_clear_the_row(follow_repo, monkeypatch):
    calls = stub_registry(monkeypatch, "sha-fdfa579")
    assert await follows.pending()
    assert await follows.pending()
    assert len(calls) == 1
    # Update repinned it: the cached lookup now matches versions.yaml.
    lock = follow_repo / "versions.yaml"
    lock.write_text(lock.read_text().replace("sha-5c5381c", "sha-fdfa579"))
    assert await follows.pending() == []
    follows.invalidate()
    await follows.pending()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_updates_list_the_new_build(follow_repo, monkeypatch):
    stub_registry(monkeypatch, "sha-fdfa579")
    result = updates._build_updates({"services": []}, {}, await follows.pending())
    [entry] = result.services
    assert (entry.id, entry.name, entry.image) == ("youtube_rss_manager", "YouTube RSS", IMAGE)
    assert (entry.currentVersion, entry.latestVersion) == ("sha-5c5381c", "sha-fdfa579")
    assert (entry.source, entry.followTag) == ("follow", "latest")
    assert entry.releasedAt == "2026-11-22T04:35:00Z"
    assert entry.verdict is None


@pytest.mark.asyncio
async def test_updates_list_follows_while_the_tracker_is_down(follow_repo, monkeypatch):
    stub_registry(monkeypatch, "sha-fdfa579")

    async def no_tracker():
        return None

    monkeypatch.setattr(updates, "_tracker_cached", no_tracker)
    result = await updates.get_updates()
    assert result.trackerUnavailable
    assert [e.id for e in result.services] == ["youtube_rss_manager"]
    assert await updates.pending_versions() == {"youtube_rss_manager": ["sha-5c5381c → sha-fdfa579"]}
    assert (await updates.service_enrichment())["youtube_rss_manager"]["latestVersion"] == "sha-fdfa579"


@pytest.mark.asyncio
async def test_update_repins_to_the_exact_build(follow_repo, monkeypatch):
    stub_registry(monkeypatch, "sha-fdfa579")

    async def no_tracker():
        return None

    monkeypatch.setattr(updates, "_tracker_cached", no_tracker)
    [t] = await repin.targets("youtube_rss_manager")
    assert (t.name, t.current_tag, t.latest_version) == (IMAGE, "sha-5c5381c", "sha-fdfa579")
    assert t.tags() == ["sha-fdfa579"]
    assert await repin.targets("beszel") == []


@pytest.mark.parametrize("name, current, latest", [
    ("ollama/ollama", "sha-1", "sha-2"),  # not on ghcr
    ("ghcr.io/org/app", "build-1", "build-2"),  # not commit tags
])
def test_no_compare_link_without_a_commit_range(name, current, latest):
    assert follows.FollowUpdate(name, "latest", current, latest, None).compare_url is None
