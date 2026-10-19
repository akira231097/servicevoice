"""Offline tests cannot enable remote tracing through a developer's .env file."""
import pytest


@pytest.fixture(autouse=True)
def disable_ambient_langsmith(monkeypatch):
    # A test that exercises activation explicitly can override this afterwards.
    monkeypatch.setenv("SERVICEVOICE_LANGSMITH_ENABLED", "off")
