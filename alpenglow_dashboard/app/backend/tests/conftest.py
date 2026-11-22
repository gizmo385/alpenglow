import os

# Mock + no-auth mode for the whole test session, set before app import.
os.environ.setdefault("MOCK_DATA", "1")
os.environ.setdefault("DEV_NO_AUTH", "1")
os.environ.setdefault("COOKIE_SECURE", "0")

import pytest
from fastapi.testclient import TestClient

from alpenglow_dashboard.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def no_registry_lookups(monkeypatch):
    """The rebuilt-tag check looks every pinned tag up in its registry; tests
    that want it stub ``_current_digest`` themselves."""
    from alpenglow_dashboard.integrations import follows

    async def none(client, name, tag):
        return None

    monkeypatch.setattr(follows, "_current_digest", none)
    follows.invalidate()
    yield
    follows.invalidate()
