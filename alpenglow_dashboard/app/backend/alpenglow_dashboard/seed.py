"""One-time SSO→tag seed (work package F1).

Before SSO auto-detection was removed, the live detector classified each service
into one of ``keycloak | self | native | none``. F1 converts that signal into
user-maintained tags: every service detected as ``keycloak`` gets the
**"Keycloak SSO"** tag; the ``keycloak`` service itself (detected ``self``) gets
**"Identity provider"**. States ``native`` / ``none`` seed no tag (absence =
not SSO'd).

The seed is idempotent and **preserves any tags the operator already added** — it
only ever *adds* the SSO tag to a service's existing tag list, never removes.

:func:`seed_sso_tags` is pure/testable (takes the detection map + the current
store and returns the merged store). :func:`apply_seed` reads and writes the live
settings store via :mod:`.tags`.
"""

from __future__ import annotations

import asyncio

from . import tags as settings_store

KEYCLOAK_TAG = "Keycloak SSO"
IDENTITY_PROVIDER_TAG = "Identity provider"


def tag_for_state(state: str) -> str | None:
    """The SSO tag a detected state seeds, or ``None`` (native/none → no tag)."""
    if state == "keycloak":
        return KEYCLOAK_TAG
    if state == "self":
        return IDENTITY_PROVIDER_TAG
    return None


def seed_sso_tags(
    detection: dict[str, str],
    current: dict[str, "settings_store.ServiceSettings"],
) -> dict[str, "settings_store.ServiceSettings"]:
    """Return a new settings map with SSO tags merged in.

    ``detection`` maps service_id → detected sso state. ``current`` is the store
    as read (``{id: {tags, category}}``). Existing tags/categories are preserved;
    the SSO tag is appended only if not already present.
    """
    out: dict[str, settings_store.ServiceSettings] = {
        sid: {"tags": list(s["tags"]), "category": s["category"]}
        for sid, s in current.items()
    }
    for sid, state in detection.items():
        tag = tag_for_state(state)
        if tag is None:
            continue
        entry = out.get(sid) or {"tags": [], "category": None}
        if tag not in entry["tags"]:
            entry["tags"] = [*entry["tags"], tag]
        out[sid] = entry
    return out


async def apply_seed(detection: dict[str, str]) -> dict[str, list[str]]:
    """Merge SSO tags into the live store; return the {id: tags} that changed."""
    changed: dict[str, list[str]] = {}
    for sid, state in detection.items():
        tag = tag_for_state(state)
        if tag is None:
            continue
        existing = await settings_store.get_tags(sid)
        if tag in existing:
            continue
        stored = await settings_store.set_tags(sid, [*existing, tag])
        changed[sid] = stored
    return changed


# Detection snapshot captured from the live app (MOCK_DATA=0) at F1 time, the
# last output of the now-removed SSO detector. Used to run the one-time seed
# against the deployed store. Only keycloak/self states matter (others seed no tag).
LIVE_DETECTION: dict[str, str] = {
    "alpenglow_dashboard": "keycloak",
    "beszel": "keycloak",
    "copyparty": "keycloak",
    "freshrss": "keycloak",
    "home_assistant": "keycloak",
    "immich": "keycloak",
    "jellyfin": "keycloak",
    "keycloak": "self",
    "linkwarden": "keycloak",
    "nextcloud": "keycloak",
    "tandoor": "keycloak",
    "trek": "keycloak",
    "youtube_rss_manager": "keycloak",
}


if __name__ == "__main__":  # pragma: no cover
    result = asyncio.run(apply_seed(LIVE_DETECTION))
    for sid in sorted(result):
        print(f"seeded {sid}: {result[sid]}")
    print(f"total seeded: {len(result)}")
