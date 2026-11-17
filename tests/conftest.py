import pytest

from reactor_render import diagnostics, live


@pytest.fixture(autouse=True)
def development_mode_off(monkeypatch):
    """Runs each test as a normal install does, even from a shell started with REACTOR_DEV_MODE; a test turns it on itself."""
    monkeypatch.setattr(diagnostics, "DEV_MODE", False)
    monkeypatch.setattr(live, "DEV_MODE", False)
