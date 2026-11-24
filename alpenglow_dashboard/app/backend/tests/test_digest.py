"""The daily Discord digest (digest.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from alpenglow_dashboard import digest, security, upgrades
from alpenglow_dashboard.integrations import update_summary, updates

UPGRADES = {
    "title": "Upgrades", "link_label": "Open card",
    "groups": [{"name": "Easy", "emoji": "🟢"}, {"name": "Hold", "emoji": "🔴"}, {"name": "Waiting", "emoji": "⏳"}],
    "items": [
        {"repo": "dawarich/dawarich", "title": "Dawarich 1.20.0 → 1.20.1", "group": "Easy",
         "note": "Bug-fix patch.", "url": "https://board/tasks/1"},
        {"repo": "immich-app/immich", "title": "Immich 2.1 → 3.0", "group": "Hold", "note": "", "url": None},
    ],
}
SECURITY = {
    "title": "Security", "link_label": "Open card", "accent": "red",
    "groups": [{"name": "Act now", "emoji": "🔴"}],
    "items": [{"repo": "", "title": "Keycloak: CVE-2026-1", "group": "Act now", "note": "Exposed.", "url": None}],
}


def svc(name, repo, updates_=True, waiting=(), **kw):
    owner, _, r = repo.partition("/")
    return {"name": name, "owner": owner, "repo": r, "has_updates": updates_, "current_version": "1.0",
            "latest_version": "1.1", "html_url": f"https://github.com/{repo}/releases",
            "waiting": [{"tag": t} for t in waiting], **kw}


TRACKER = {"services": [
    svc("Dawarich", "dawarich/dawarich"),
    svc("Beszel", "henrygd/beszel"),
    svc("Redis", "redis/redis", updates_=False, waiting=["8.12.0"]),
]}


def test_cards_are_grouped_by_verdict_in_board_order():
    embed = digest.annotations_embed(UPGRADES)
    assert embed["title"] == "Upgrades"
    assert embed["description"] == (
        "### 🟢 Easy\n\n**Dawarich 1.20.0 → 1.20.1**\nBug-fix patch.\n[Open card](https://board/tasks/1)"
        "\n\n### 🔴 Hold\n\n**Immich 2.1 → 3.0**"
    )
    assert embed["footer"]["text"] == "1 easy · 1 hold"


QUIET_GROUPS = [{"name": "Act now", "emoji": "🔴"}, {"name": "Next upgrade", "emoji": "🟡"},
                {"name": "Waiting on upstream", "emoji": "🔵", "quiet": True},
                {"name": "Not affected", "emoji": "🟢", "quiet": True}]


def sec_item(name, group, note="Why."):
    return {"name": name, "title": f"{name}: 71 open security findings", "group": group, "note": note, "url": None}


def test_security_lists_what_needs_a_step_and_only_names_the_rest():
    embed = digest.annotations_embed({"title": "Security", "groups": QUIET_GROUPS, "items": [
        sec_item("FreshRSS", "Act now", "Brotli bug is reachable."),
        sec_item("Kopia", "Waiting on upstream"), sec_item("Caddy", "Waiting on upstream"),
        sec_item("Keycloak", "Not affected"),
    ]})
    assert embed["description"] == (
        "### 🔴 Act now\n\n**FreshRSS**\nBrotli bug is reachable."
        "\n\n🔵 **Waiting on upstream, nothing to do (2):** Kopia, Caddy"
        "\n\n🟢 **Not affected, nothing to do (1):** Keycloak"
    )
    assert "findings" not in embed["description"]
    assert embed["footer"]["text"] == "1 act now · 2 waiting on upstream · 1 not affected"


def test_security_with_nothing_to_do_has_no_message():
    assert digest.annotations_embed({"title": "Security", "groups": QUIET_GROUPS, "items": [
        sec_item("Kopia", "Waiting on upstream"), sec_item("Keycloak", "Not affected")]}) is None


def test_a_board_with_nothing_open_has_no_message():
    assert digest.annotations_embed({"title": "Security", "items": []}) is None


def test_the_digest_is_security_then_upgrades_then_the_rest():
    sec, ups, other = digest.build(UPGRADES, SECURITY, TRACKER)
    assert (sec["title"], sec["color"]) == ("Security", digest._RED)
    assert ups["footer"]["text"] == "1 easy · 1 hold · 1 other below"
    # Dawarich has a card, so only Beszel is left. Redis is waiting on its
    # image, which isn't something to act on, so it isn't mentioned.
    assert other["title"] == "Other updates"
    assert other["description"] == (
        "**Beszel** `1.0` → `1.1` · [Releases](https://github.com/henrygd/beszel/releases)"
    )


def test_the_image_tag_is_shown_when_the_tracker_found_one():
    tracker = {"services": [svc("ClickHouse", "ClickHouse/ClickHouse", current_version="26.10.9.12",
                                latest_version="v26.10.10.3-stable", latest_tag="26.10.10")]}
    [only] = digest.build({"items": []}, {}, tracker)
    assert "`26.10.9.12` → `26.10.10`" in only["description"]


def test_without_cards_the_tracker_updates_lead():
    [only] = digest.build({"items": []}, {}, TRACKER)
    assert only["title"] == "Docker Services — Update Digest"
    assert "**Dawarich**" in only["description"] and "**Beszel**" in only["description"]


def test_nothing_pending_means_no_digest():
    quiet = {"services": [svc("Redis", "redis/redis", updates_=False, waiting=["8.12.0"])]}
    assert digest.build({"items": []}, {"items": []}, quiet) == []
    assert digest.build({"items": []}, {}, None) == []


def test_a_long_list_is_cut_with_a_count():
    items = [{"repo": f"o/r{n}", "title": f"Service {n} " + "x" * 200, "group": "Easy"} for n in range(40)]
    embed = digest.annotations_embed({"title": "Upgrades", "groups": [{"name": "Easy"}], "items": items})
    assert len(embed["description"]) <= 4096
    assert embed["description"].rsplit("\n\n", 1)[-1].startswith("…and ")


@pytest.mark.parametrize("raw, expected", [
    (None, (9, 0)), ("07:30", (7, 30)), ("off", None), ("25:00", (9, 0)), ("soon", (9, 0)),
])
def test_send_time(monkeypatch, raw, expected):
    monkeypatch.delenv("DIGEST_AT", raising=False)
    if raw is not None:
        monkeypatch.setenv("DIGEST_AT", raw)
    assert digest.send_time() == expected


def test_next_send_is_today_or_tomorrow():
    assert digest.next_send(datetime(2026, 11, 22, 8, 0), (9, 0)) == datetime(2026, 11, 22, 9, 0)
    assert digest.next_send(datetime(2026, 11, 22, 9, 0), (9, 0)) == datetime(2026, 11, 23, 9, 0)


def test_digest_cron_sends_only_on_its_days(monkeypatch):
    monkeypatch.delenv("DIGEST_CRON", raising=False)
    assert digest.cron() is None
    monkeypatch.setenv("DIGEST_CRON", "0 9 * * 1,4")  # Monday and Thursday
    schedule = digest.cron()
    tue = datetime(2026, 11, 24, 9, 30)
    assert digest.next_send(tue, (9, 0), schedule) == datetime(2026, 11, 26, 9, 0)   # Thursday
    assert digest.next_send(datetime(2026, 11, 26, 9, 0), (9, 0), schedule) == datetime(2026, 11, 30, 9, 0)
    aware = tue.astimezone()
    assert digest.next_send(aware, (9, 0), schedule).tzinfo == aware.tzinfo
    for bad in ("nonsense", "daily", "0 9 * *"):  # falls back to every day rather than going quiet
        monkeypatch.setenv("DIGEST_CRON", bad)
        assert digest.cron() is None


def test_the_webhook_falls_back_to_the_update_summarys(monkeypatch):
    monkeypatch.delenv("DIGEST_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("UPDATE_SUMMARY_WEBHOOK_URL", raising=False)
    assert digest.webhook_url() is None
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", "https://discord.test/summary")
    assert digest.webhook_url() == "https://discord.test/summary"
    monkeypatch.setenv("DIGEST_WEBHOOK_URL", "https://discord.test/digest")
    assert digest.webhook_url() == "https://discord.test/digest"


def test_send_posts_one_message_per_section(monkeypatch):
    posted, rescans = [], []

    async def post_embed(embed, url=None):
        posted.append((embed["title"], url))

    async def rescan():
        rescans.append(1)

    async def tracker():
        return TRACKER

    async def annotations():
        return []

    async def sec():
        return SECURITY

    monkeypatch.setenv("MOCK", "0")
    monkeypatch.setattr(digest.mock, "mock_enabled", lambda: False)
    monkeypatch.setenv("DIGEST_WEBHOOK_URL", "https://discord.test/digest")
    monkeypatch.setattr(update_summary, "post_embed", post_embed)
    monkeypatch.setattr(updates, "rescan", rescan)
    monkeypatch.setattr(updates, "_tracker_cached", tracker)
    monkeypatch.setattr(upgrades, "annotations", annotations)
    monkeypatch.setattr(security, "get_annotations", sec)
    assert asyncio.run(digest.send()) == 2
    assert posted == [("Security", "https://discord.test/digest"),
                      ("Docker Services — Update Digest", "https://discord.test/digest")]
    assert rescans == [1]


def test_no_webhook_sends_nothing(monkeypatch):
    monkeypatch.delenv("DIGEST_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("UPDATE_SUMMARY_WEBHOOK_URL", raising=False)
    assert asyncio.run(digest.send()) == 0
