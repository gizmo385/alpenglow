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
