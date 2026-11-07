"""Проверка Google ID-токена: сертификаты Google не скачиваются на каждый запрос.

google-auth сам их не кэширует, поэтому verify_token передаёт ему
CachingTransport. Сеть google-auth здесь подменена счётчиком походов к Google.
"""
import types

import pytest

from conftest import FakeRequest

CERTS = "https://www.googleapis.com/oauth2/v1/certs"


class FakeResponse:
    def __init__(self, status=200, cache_control="public, max-age=20000", age=None):
        self.status = status
        self.headers = {"Cache-Control": cache_control}
        if age is not None:
            self.headers["Age"] = str(age)
        self.data = b"{}"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def google(api_module, monkeypatch):
    """Сеть google-auth: считает походы к Google, отдаёт google.response."""
    net = types.SimpleNamespace(calls=0, response=FakeResponse())

    def request_factory():
        def send(url, method="GET", **kwargs):
            net.calls += 1
            return net.response
        return send

    monkeypatch.setattr(api_module, "google_requests",
                        types.SimpleNamespace(Request=request_factory))
    return net


def test_fresh_certs_are_fetched_once(api_module, google):
    clock = Clock()
    transport = api_module.CachingTransport(clock)
    first = transport(CERTS)
    clock.now += 19999
    assert transport(CERTS) is first
    assert google.calls == 1


def test_stale_certs_are_fetched_again(api_module, google):
    clock = Clock()
    transport = api_module.CachingTransport(clock)
    transport(CERTS)
    clock.now += 20000
    transport(CERTS)
    assert google.calls == 2


def test_age_counts_against_freshness(api_module, google):
    """Ответ уже пролежал в кэше по дороге: свежим он остаётся max-age − Age."""
    google.response = FakeResponse(age=19000)
    clock = Clock()
    transport = api_module.CachingTransport(clock)
    transport(CERTS)
    clock.now += 1000
    transport(CERTS)
    assert google.calls == 2


@pytest.mark.parametrize("response", [
    FakeResponse(status=500),
    FakeResponse(cache_control=""),
    FakeResponse(cache_control="no-store, max-age=600"),
], ids=["error", "no-max-age", "no-store"])
def test_response_unfit_for_cache_is_not_kept(api_module, google, response):
    google.response = response
    transport = api_module.CachingTransport(Clock())
    transport(CERTS)
    transport(CERTS)
    assert google.calls == 2


def test_two_api_requests_fetch_certs_once(api_module, google, monkeypatch):
    def verify_oauth2_token(token, request, audience):
        request(CERTS, method="GET")      # как google-auth: за сертификатами
        return {"sub": "u1", "aud": audience}

    monkeypatch.setattr(api_module, "_GOOGLE_TRANSPORT", api_module.CachingTransport())
    monkeypatch.setattr(api_module.id_token, "verify_oauth2_token",
                        verify_oauth2_token, raising=False)
    assert api_module.verify_token(FakeRequest())["sub"] == "u1"
    assert api_module.verify_token(FakeRequest())["sub"] == "u1"
    assert google.calls == 1
