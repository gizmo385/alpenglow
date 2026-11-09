"""The Discord summary of finished Updates (integrations/update_summary.py)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from opentelemetry.instrumentation.utils import is_instrumentation_enabled

from alpenglow_dashboard import repin
from alpenglow_dashboard.integrations import update_summary, updates
from alpenglow_dashboard.integrations.update_summary import Result, build_embed

SECRET_URL = "https://discord.test/api/webhooks/1/secret-webhook-token"


def ok(name="Keycloak", versions=("26.8.1 → 26.8.2",), user="chris@acbc.house"):
    return Result(service=name.lower(), name=name, user=user, ok=True, versions=list(versions))


def failed(detail, name="Immich", user="chris@acbc.house"):
    return Result(service=name.lower(), name=name, user=user, ok=False, detail=detail)


def test_an_all_good_summary():
    embed = build_embed([ok(), ok("Trek", ["1.0 → 1.1"])])
    assert embed["description"] == "✅ **Keycloak** 26.8.1 → 26.8.2\n✅ **Trek** 1.0 → 1.1"
    assert embed["footer"]["text"] == "2 updated · by chris"
    assert embed["color"] == update_summary._GREEN


def test_a_failure_turns_it_red_and_gives_the_reason():
    embed = build_embed([ok(), failed("pulling\nmanifest unknown")])
    assert "❌ **Immich**: manifest unknown" in embed["description"]
    assert embed["footer"]["text"] == "1 updated · 1 failed · by chris"
    assert embed["color"] == update_summary._RED


def test_a_failure_after_the_repin_says_it_wasnt_rolled_back():
    detail = "manifest unknown\n[update] versions.yaml now pins x to 2 (was 1); not rolled back"
    assert "manifest unknown (new pin not rolled back)" in build_embed([failed(detail)])["description"]


def test_a_failure_with_no_detail_still_reads():
    assert "Update failed" in build_embed([failed("")])["description"]


def test_a_long_reason_and_a_long_summary_are_cut():
    embed = build_embed([failed("x" * 5000)])
    assert len(embed["description"]) < 300
    many = [ok(f"Service{i}", ["1.0.0 → 2.0.0" * 20]) for i in range(50)]
    assert len(build_embed(many)["description"]) == update_summary._DESCRIPTION_LIMIT


def test_only_the_name_of_each_person_is_shown():
    embed = build_embed([ok(), ok("Trek", user="adam@acbc.house")])
    assert embed["footer"]["text"].endswith("by adam, chris")
    assert "@" not in embed["footer"]["text"]


def _capture(monkeypatch, status=204):
    seen = []
    transport = httpx.MockTransport(
        lambda request: seen.append(
            (str(request.url), json.loads(request.content), is_instrumentation_enabled())
        ) or httpx.Response(status)
    )
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("timeout", None)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return seen


@pytest.mark.asyncio
async def test_nothing_is_posted_without_a_webhook(monkeypatch):
    monkeypatch.delenv("UPDATE_SUMMARY_WEBHOOK_URL", raising=False)
    seen = _capture(monkeypatch)
    await update_summary.post([ok()])
    assert seen == []


@pytest.mark.asyncio
async def test_nothing_is_posted_with_no_results(monkeypatch):
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", SECRET_URL)
    seen = _capture(monkeypatch)
    await update_summary.post([])
    assert seen == []


@pytest.mark.asyncio
async def test_a_post_pings_nobody_and_isnt_traced(monkeypatch):
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", SECRET_URL)
    seen = _capture(monkeypatch)
    await update_summary.post([failed("@everyone broke it")])
    url, body, traced = seen[0]
    assert url == SECRET_URL
    assert body["allowed_mentions"] == {"parse": []}
    assert traced is False


@pytest.mark.asyncio
async def test_a_refused_post_is_logged_without_the_url(monkeypatch, caplog):
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", SECRET_URL)
    _capture(monkeypatch, status=404)
    with caplog.at_level(logging.DEBUG):
        await update_summary.post([ok()])
    assert "Discord returned 404" in caplog.text
    assert "secret-webhook-token" not in caplog.text


@pytest.mark.asyncio
async def test_an_unreachable_webhook_is_logged_without_the_url(monkeypatch, caplog):
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", SECRET_URL)

    def refuse(request):
        raise httpx.ConnectError(f"can't reach {request.url}")

    real_init = httpx.AsyncClient.__init__
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__",
        lambda self, *a, **k: real_init(self, transport=httpx.MockTransport(refuse)),
    )
    with caplog.at_level(logging.WARNING, logger="alpenglow.update_summary"):
        await update_summary.post([ok()])
    assert "ConnectError" in caplog.text
    assert "secret-webhook-token" not in caplog.text


def test_pin_doesnt_inherit_the_webhook(monkeypatch):
    monkeypatch.setenv("UPDATE_SUMMARY_WEBHOOK_URL", SECRET_URL)
    assert "UPDATE_SUMMARY_WEBHOOK_URL" not in repin._env(None)


@pytest.mark.asyncio
async def test_pending_versions_labels_images_only_when_a_service_has_several(monkeypatch):
    payload = {"services": []}
    entries = {
        "keycloak": [{"current_version": "1", "image": "quay.io/keycloak/keycloak:1"}],
        "signoz": [
            {"current_version": "2", "image": "signoz/signoz:2@sha256:abc"},
            {"current_version": "3", "image": "signoz/signoz-otel-collector:3"},
        ],
    }

    async def cached():
        return payload

    monkeypatch.setattr(updates, "_tracker_cached", cached)
    monkeypatch.setattr(updates, "_match_entries", lambda p: (entries, []))
    monkeypatch.setattr(updates, "_entry_fields", lambda e: {"latestVersion": "9"})
    assert await updates.pending_versions() == {
        "keycloak": ["1 → 9"],
        "signoz": ["signoz 2 → 9", "signoz-otel-collector 3 → 9"],
    }


@pytest.mark.asyncio
async def test_pending_versions_without_the_tracker(monkeypatch):
    async def cached():
        return None

    monkeypatch.setattr(updates, "_tracker_cached", cached)
    assert await updates.pending_versions() == {}
