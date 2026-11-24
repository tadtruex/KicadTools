"""Shared test setup."""
import pytest

from kicadtools import suppliers


@pytest.fixture(autouse=True)
def unspaced_digikey_limiter(monkeypatch):
    """A fresh DigiKey rate limiter with no spacing between calls for each
    test, so faked DigiKey calls do not wait 0.6 s each and a limiter stopped
    by one test does not carry over to the next; and an empty cache of
    category filter options."""
    monkeypatch.setattr(suppliers, "DIGIKEY_LIMITER", suppliers.DigiKeyRateLimiter(per_minute=None))
    monkeypatch.setattr(suppliers, "_CATEGORY_OPTIONS", {})


@pytest.fixture(autouse=True)
def no_digikey_credentials(monkeypatch):
    """No test reads real DigiKey credentials from the environment, so none
    can call the real API (and spend the 1000-a-day limit)."""
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)


@pytest.fixture(autouse=True)
def no_machine_config(monkeypatch, tmp_path):
    """No test reads the machine's ``~/.kicadtools/config.toml``: with the
    config naming the server, a CLI test would open a tunnel and fail when the
    server cannot be reached. A test that needs a configuration sets its own."""
    monkeypatch.setenv("KICADTOOLS_CONFIG", str(tmp_path / "no-config.toml"))
