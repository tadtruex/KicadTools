"""Offline tests for the supplier clients (kicadtools.suppliers).

``requests`` is monkeypatched, so no network is touched. The focus is the
JLCPCB MPN-verification guarantee: a hit is accepted only when its
``componentModelEn`` actually matches the queried MPN — the guard that stops the
old endpoint's "first result wins" false positives.
"""
import kicadtools.suppliers as suppliers
from kicadtools.suppliers import (
    DigiKeyClient,
    digikey_url,
    jlcpcb_lcsc_for,
    lcsc_url,
)


def test_lcsc_url_from_code():
    assert lcsc_url("C12345") == "https://www.lcsc.com/search?q=C12345"


def test_digikey_url_encodes_pn():
    # DigiKey PNs contain characters that must be percent-encoded in a query.
    assert (
        digikey_url("311-1.0KHRCT-ND")
        == "https://www.digikey.com/en/products/result?keywords=311-1.0KHRCT-ND"
    )
    assert "%2F" in digikey_url("A/B")  # a slash is encoded, not left bare


def test_url_builders_return_none_when_empty():
    assert lcsc_url("") is None
    assert lcsc_url("  ") is None
    assert digikey_url(None) is None


class FakeResp:
    def __init__(self, payload, status=200, *, raise_json=False):
        self._payload = payload
        self.status_code = status
        self._raise_json = raise_json
        self.text = "<html>not json</html>" if raise_json else "ok"

    def json(self):
        if self._raise_json:
            raise ValueError("no JSON")
        return self._payload


def _list_payload(items):
    return {"code": 200, "data": {"componentPageInfo": {"list": items}}}


def _patch_post(monkeypatch, resp):
    monkeypatch.setattr(
        suppliers.requests, "post", lambda *a, **k: resp
    )


def test_exact_mpn_match_returns_code(monkeypatch):
    payload = _list_payload([
        {"componentCode": "C237792", "componentModelEn": "RMCF0603FT100K",
         "stockCount": 5000},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("RMCF0603FT100K") == "C237792"


def test_non_matching_results_are_rejected(monkeypatch):
    # The whole point: an unrelated first result must NOT be returned.
    payload = _list_payload([
        {"componentCode": "C9900027346", "componentModelEn": "SOME-OTHER-PART"},
        {"componentCode": "C111", "componentModelEn": "ALSO-WRONG"},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("RMCF0603FT100K") is None


def test_match_is_separator_and_case_insensitive(monkeypatch):
    payload = _list_payload([
        {"componentCode": "C1", "componentModelEn": "ece-a1eka470i", "stock": 10},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("ECE-A1EKA470I") == "C1"


def test_prefers_in_stock_among_exact_matches(monkeypatch):
    payload = _list_payload([
        {"componentCode": "C-nostock", "componentModelEn": "PART1", "stockCount": 0},
        {"componentCode": "C-stock", "componentModelEn": "PART1", "stockCount": 42},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PART1") == "C-stock"


def test_placeholder_deprioritized_when_both_out_of_stock(monkeypatch):
    # The J8/ECG bug: MPN PH2-10-UA returns a genuine ADAM TECH catalog part and
    # a synthetic "JLCPCB Assembly" C9900 SKU, both out of stock. The old code
    # returned matches[0] (the C9900 placeholder). We must return the real part.
    payload = _list_payload([
        {"componentCode": "C9900016815", "componentModelEn": "PH2-10-UA",
         "stockCount": 0, "componentBrandEn": "JLCPCB Assembly", "lcscGoodsUrl": None},
        {"componentCode": "C5708577", "componentModelEn": "PH2-10-UA",
         "stockCount": 0, "componentBrandEn": "ADAM TECH",
         "lcscGoodsUrl": "https://www.lcsc.com/product-detail/_C5708577.html"},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PH2-10-UA") == "C5708577"


def test_in_stock_placeholder_beats_out_of_stock_genuine(monkeypatch):
    # Assemblability wins: a C9900 SKU that alone has stock is the orderable one.
    payload = _list_payload([
        {"componentCode": "C5708577", "componentModelEn": "PART1",
         "stockCount": 0, "lcscGoodsUrl": "https://lcsc/x"},
        {"componentCode": "C9900016815", "componentModelEn": "PART1",
         "stockCount": 500, "lcscGoodsUrl": None},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PART1") == "C9900016815"


def test_basic_library_preferred_over_extended(monkeypatch):
    payload = _list_payload([
        {"componentCode": "C-ext", "componentModelEn": "PART1", "stockCount": 100,
         "componentLibraryType": "expand"},
        {"componentCode": "C-basic", "componentModelEn": "PART1", "stockCount": 100,
         "componentLibraryType": "base"},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PART1") == "C-basic"


def test_cheaper_part_wins_final_tiebreak(monkeypatch):
    payload = _list_payload([
        {"componentCode": "C-dear", "componentModelEn": "PART1", "stockCount": 100,
         "componentPrices": [{"startNumber": 1, "productPrice": 0.5}]},
        {"componentCode": "C-cheap", "componentModelEn": "PART1", "stockCount": 100,
         "componentPrices": [{"startNumber": 1, "productPrice": 0.1}]},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PART1") == "C-cheap"


def test_c9900_with_catalog_page_not_treated_as_placeholder(monkeypatch):
    # A C9900 code that DOES have an LCSC catalog page is orderable, not synthetic.
    payload = _list_payload([
        {"componentCode": "C9900274507", "componentModelEn": "PART1",
         "stockCount": 0, "lcscGoodsUrl": "https://lcsc/real"},
    ])
    _patch_post(monkeypatch, FakeResp(payload))
    assert jlcpcb_lcsc_for("PART1") == "C9900274507"


def test_empty_list_returns_none(monkeypatch):
    _patch_post(monkeypatch, FakeResp(_list_payload([])))
    assert jlcpcb_lcsc_for("WHATEVER") is None


def test_non_json_returns_none(monkeypatch):
    # The failure mode of the dead GET endpoint (HTML SPA shell) — must not scrape.
    _patch_post(monkeypatch, FakeResp(None, raise_json=True))
    assert jlcpcb_lcsc_for("STM32L552CCT6") is None


def test_http_error_returns_none(monkeypatch):
    _patch_post(monkeypatch, FakeResp(_list_payload([]), status=503))
    assert jlcpcb_lcsc_for("STM32L552CCT6") is None


def test_empty_mpn_short_circuits(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not hit the network for an empty MPN")

    monkeypatch.setattr(suppliers.requests, "post", boom)
    assert jlcpcb_lcsc_for("") is None


# --- DigiKey client -------------------------------------------------------

def test_digikey_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    client = DigiKeyClient()
    assert not client.configured
    assert client.mpn_for("anything-ND") is None


def test_digikey_happy_path(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if url == suppliers.TOKEN_URL:
            return FakeResp({"access_token": "tok", "expires_in": 3600})
        return FakeResp({"Products": [{"ManufacturerProductNumber": "RMCF0603FT100K"}]})

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    client = DigiKeyClient(client_id="id", client_secret="secret")
    assert client.mpn_for("P100KLCT-ND") == "RMCF0603FT100K"
