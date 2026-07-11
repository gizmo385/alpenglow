"""F1 one-time SSO→tag seed tests.

Verifies the pure merge (preserves operator tags, adds the right SSO tag per
detected state, native/none seed nothing) and the live store apply path.
"""

from __future__ import annotations

import json

import pytest

from alpenglow_dashboard import seed, tags as settings_store


def test_tag_for_state():
    assert seed.tag_for_state("keycloak") == "Keycloak SSO"
    assert seed.tag_for_state("self") == "Identity provider"
    assert seed.tag_for_state("native") is None
    assert seed.tag_for_state("none") is None


def test_seed_pure_merge_preserves_operator_tags():
    detection = {
        "immich": "keycloak",
        "keycloak": "self",
        "jellyfin": "native",  # seeds nothing
        "glances": "none",  # seeds nothing
    }
    current = {
        # operator already tagged immich → must be preserved, SSO tag appended
        "immich": {"tags": ["GPU", "User data"], "category": "Media"},
        # operator tagged a non-SSO service → untouched
        "postgres": {"tags": ["Critical"], "category": None},
    }
    merged = seed.seed_sso_tags(detection, current)

    assert merged["immich"]["tags"] == ["GPU", "User data", "Keycloak SSO"]
    assert merged["immich"]["category"] == "Media"  # preserved
    assert merged["keycloak"]["tags"] == ["Identity provider"]
    assert "jellyfin" not in merged  # native → no tag, no entry created
    assert "glances" not in merged
    assert merged["postgres"]["tags"] == ["Critical"]  # untouched


def test_seed_is_idempotent():
    detection = {"immich": "keycloak"}
    current = {"immich": {"tags": ["Keycloak SSO"], "category": None}}
    merged = seed.seed_sso_tags(detection, current)
    assert merged["immich"]["tags"] == ["Keycloak SSO"]  # not doubled


async def test_apply_seed_writes_store(monkeypatch, tmp_path):
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("TAGS_PATH", raising=False)

    # Pre-existing operator tag that must survive the seed.
    await settings_store.set_tags("immich", ["GPU"])

    detection = {"immich": "keycloak", "keycloak": "self", "glances": "none"}
    changed = await seed.apply_seed(detection)

    assert changed["immich"] == ["GPU", "Keycloak SSO"]
    assert changed["keycloak"] == ["Identity provider"]
    assert "glances" not in changed

    stored = json.loads((tmp_path / "settings.json").read_text())
    assert stored["immich"]["tags"] == ["GPU", "Keycloak SSO"]
    assert stored["keycloak"]["tags"] == ["Identity provider"]

    # Running again is a no-op (idempotent).
    assert await seed.apply_seed(detection) == {}


def test_live_detection_has_twelve_keycloak_and_one_self():
    states = seed.LIVE_DETECTION
    keycloak = [sid for sid, st in states.items() if st == "keycloak"]
    selves = [sid for sid, st in states.items() if st == "self"]
    assert len(keycloak) == 12
    assert selves == ["keycloak"]
