"""Tests for the DigiKey rate limiter (kicadtools.suppliers.DigiKeyRateLimiter)."""
from types import SimpleNamespace

import pytest

from kicadtools import suppliers
from kicadtools.suppliers import DigiKeyClient, DigiKeyRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _limiter(**kwargs):
    fake = FakeClock()
    return DigiKeyRateLimiter(clock=fake.clock, sleep=fake.sleep, **kwargs), fake


def _resp(status=200, **headers):
    return SimpleNamespace(
        status_code=status, headers=headers, text="", json=lambda: {"Product": None}
    )


def test_calls_are_spaced():
    limiter, fake = _limiter(per_minute=100)
    assert limiter.acquire()
    assert fake.slept == []
    fake.now += 0.2
    assert limiter.acquire()
    assert fake.slept == [pytest.approx(0.4)]  # 0.6 s between calls
    fake.now += 5
    assert limiter.acquire()
    assert len(fake.slept) == 1
    assert limiter.calls == 3


def test_no_spacing():
    limiter, fake = _limiter(per_minute=None)
    for _ in range(5):
        assert limiter.acquire()
    assert fake.slept == []


def test_record_reads_headers_and_stops_at_reserve():
    limiter, _ = _limiter(reserve=50)
    limiter.record(_resp(**{"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "999"}))
    assert (limiter.remaining, limiter.daily_limit, limiter.stopped) == (999, 1000, "")
    limiter.record(_resp(**{"x-ratelimit-remaining": "50"}))
    assert limiter.stopped.startswith("DigiKey daily limit: 50 of 1000 calls left")
    assert not limiter.acquire()


def test_record_without_headers_changes_nothing():
    limiter, _ = _limiter()
    limiter.record(_resp())
    limiter.record(object())
    assert (limiter.remaining, limiter.stopped) == (None, "")


def test_retry_after_short_waits_long_stops():
    limiter, fake = _limiter(max_retry_after=60)
    assert limiter.retry_after(_resp(429, **{"Retry-After": "2"}))
    assert fake.slept == [2.0]
    assert not limiter.stopped
    assert not limiter.retry_after(_resp(429, **{"Retry-After": "3600"}))
    assert "429" in limiter.stopped and "3600" in limiter.stopped
    assert not limiter.acquire()


def test_retry_after_missing_stops():
    limiter, _ = _limiter()
    assert not limiter.retry_after(_resp(429))
    assert limiter.stopped


def test_status_line():
    limiter, _ = _limiter()
    limiter.acquire()
    limiter.record(_resp(**{"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "998"}))
    assert limiter.status() == "DigiKey: 1 call(s) this session, 998 of 1000 left today"


# --- through the client ------------------------------------------------------


def _client(monkeypatch, responses, limiter):
    sent = []

    def fake_get(url, **kwargs):
        sent.append(url)
        return responses.pop(0)

    monkeypatch.setattr(suppliers.requests, "get", fake_get)
    client = DigiKeyClient(client_id="id", client_secret="secret", limiter=limiter)
    client._token, client._token_expires_at = "tok", float("inf")
    return client, sent


def test_client_retries_once_after_short_429(monkeypatch):
    limiter, fake = _limiter(per_minute=None)
    client, sent = _client(monkeypatch, [_resp(429, **{"Retry-After": "1"}), _resp(404)], limiter)
    assert client.product_for("X") is None
    assert len(sent) == 2
    assert fake.slept == [1.0]
    assert limiter.calls == 2 and not limiter.stopped


def test_client_stops_after_long_429(monkeypatch):
    limiter, _ = _limiter(per_minute=None)
    client, sent = _client(monkeypatch, [_resp(429, **{"Retry-After": "86400"})], limiter)
    assert client.product_for("X") is None
    assert client.product_for("Y") is None  # no request made
    assert len(sent) == 1
    assert limiter.stopped


def test_client_stops_at_daily_reserve(monkeypatch):
    limiter, _ = _limiter(per_minute=None, reserve=50)
    client, sent = _client(monkeypatch, [_resp(404, **{"x-ratelimit-remaining": "50"})], limiter)
    client.product_for("X")
    client.product_for("Y")
    assert len(sent) == 1


def test_clients_share_the_module_limiter(monkeypatch):
    shared, _ = _limiter(per_minute=None)
    monkeypatch.setattr(suppliers, "DIGIKEY_LIMITER", shared)
    assert DigiKeyClient().limiter is shared
    assert DigiKeyClient().limiter is shared
