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


# A realistic (trimmed) v4 productdetails Product object for a 10k resistor.
_DETAILS_PRODUCT = {
    "ManufacturerProductNumber": "RC0402FR-0710KL",
    "DatasheetUrl": "https://example.com/ds.pdf",
    "ProductUrl": "https://www.digikey.com/en/products/detail/x",
    "QuantityAvailable": 1234,
    "ProductStatus": {"Id": 0, "Status": "Active"},
    "Description": {"ProductDescription": "RES 10K OHM 1% 1/16W 0402"},
    "ProductVariations": [
        {"DigiKeyProductNumber": "311-10.0KLRTR-ND"},
        {"DigiKeyProductNumber": "311-10.0KLRCT-ND"},
    ],
    "Parameters": [
        {"ParameterText": "Resistance", "ValueText": "10 kOhms"},
        {"ParameterText": "Tolerance", "ValueText": "±1%"},
        {"ParameterText": "Power (Watts)", "ValueText": "0.063W, 1/16W"},
        {"ParameterText": "Supplier Device Package", "ValueText": "0402"},
    ],
}


def _fake_dk(monkeypatch, *, details=_DETAILS_PRODUCT, status=200):
    """Wire a DigiKeyClient whose token POST works and whose productdetails GET
    returns *details* wrapped as ``{"Product": ...}`` (or an error status)."""
    def fake_post(url, **kwargs):
        assert url == suppliers.TOKEN_URL
        return FakeResp({"access_token": "tok", "expires_in": 3600})

    def fake_get(url, **kwargs):
        body = {"Product": details} if details is not None else {}
        return FakeResp(body, status=status)

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    monkeypatch.setattr(suppliers.requests, "get", fake_get)
    return DigiKeyClient(client_id="id", client_secret="secret")


def test_digikey_mpn_for_delegates_to_product_for(monkeypatch):
    client = _fake_dk(monkeypatch)
    assert client.mpn_for("311-10.0KLRCT-ND") == "RC0402FR-0710KL"


def test_digikey_product_for_parses_parameters(monkeypatch):
    client = _fake_dk(monkeypatch)
    p = client.product_for("311-10.0KLRCT-ND")
    assert p is not None
    assert p.mpn == "RC0402FR-0710KL"
    assert p.param("Resistance") == "10 kOhms"
    assert p.param("Power (Watts)") == "0.063W, 1/16W"
    assert p.param("Supplier Device Package") == "0402"
    assert p.quantity_available == 1234
    assert p.product_status == "Active"
    assert p.datasheet_url == "https://example.com/ds.pdf"
    assert "311-10.0KLRCT-ND" in p.digikey_pns


def test_digikey_product_for_404_returns_none(monkeypatch):
    client = _fake_dk(monkeypatch, status=404)
    assert client.product_for("no-such-part") is None
    assert client.mpn_for("no-such-part") is None


def test_digikey_product_for_empty_pn_short_circuits(monkeypatch):
    def boom_get(*a, **k):
        raise AssertionError("should not hit the network for an empty PN")

    monkeypatch.setattr(suppliers.requests, "get", boom_get)
    client = DigiKeyClient(client_id="id", client_secret="secret")
    client._token = "tok"  # pretend already authed
    client._token_expires_at = float("inf")
    assert client.product_for("") is None


# ---- JLCPCB catalog entries (stock, attributes) ------------------------------

def _hit(code, model="M1", **extra):
    hit = {
        "componentCode": code,
        "componentModelEn": model,
        "componentBrandEn": "Yageo",
        "componentLibraryType": "base",
        "stockCount": 1234,
        "componentSpecificationEn": "0603",
        "componentPrices": [{"productPrice": 0.002}, {"productPrice": 0.001}],
        "attributes": [
            {"attribute_name_en": "Capacitance", "attribute_value_name": "100nF"},
            {"attribute_name_en": "Voltage Rating", "attribute_value_name": "50V"},
        ],
    }
    hit.update(extra)
    return hit


def test_jlcpcb_part_parses_stock_and_attributes(monkeypatch):
    _patch_post(monkeypatch, FakeResp(_list_payload([_hit("C14663")])))
    part = suppliers.jlcpcb_part("c14663")
    assert part.lcsc == "C14663" and part.mpn == "M1" and part.manufacturer == "Yageo"
    assert part.is_basic and part.stock == 1234 and part.unit_price == 0.001
    assert part.package == "0603"
    assert part.attributes == {"Capacitance": "100nF", "Voltage Rating": "50V"}
    assert part.orderable


def test_jlcpcb_part_requires_exact_code(monkeypatch):
    _patch_post(monkeypatch, FakeResp(_list_payload([_hit("C146630")])))
    assert suppliers.jlcpcb_part("C14663") is None
    assert suppliers.jlcpcb_part("") is None


def test_jlcpcb_part_not_orderable_with_no_buy_reason(monkeypatch):
    hit = _hit("C9900013546", stockCount=0, attributes=None,
               noBuyReason="This product is no longer manufactured.")
    _patch_post(monkeypatch, FakeResp(_list_payload([hit])))
    part = suppliers.jlcpcb_part("C9900013546")
    assert part.attributes == {} and not part.orderable
    assert part.no_buy_reason.startswith("This product")


def test_jlcpcb_search_pages_until_a_short_page(monkeypatch):
    pages = {1: [_hit("C1"), _hit("C2")], 2: [_hit("C3")]}
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(json["currentPage"])
        return FakeResp(_list_payload(pages.get(json["currentPage"], [])))

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    parts = suppliers.jlcpcb_search("100nF 0603", pages=5, page_size=2)
    assert [p.lcsc for p in parts] == ["C1", "C2", "C3"]
    assert seen == [1, 2]


def test_preferred_digikey_pn_picks_cut_tape():
    assert suppliers.preferred_digikey_pn(
        ["445-5667-2-ND", "445-5667-1-ND", "445-5667-6-ND"]) == "445-5667-1-ND"
    assert suppliers.preferred_digikey_pn(
        ["311-49.9KHRTR-ND", "311-49.9KHRCT-ND", "311-49.9KHRDKR-ND"]) == "311-49.9KHRCT-ND"
    assert suppliers.preferred_digikey_pn(["X-ND"]) == "X-ND"
    assert suppliers.preferred_digikey_pn([]) is None


def test_digikey_quantity_missing_means_zero_when_packagings_listed():
    from kicadtools.suppliers import _quantity
    assert _quantity({"QuantityAvailable": 12}) == 12
    assert _quantity({"ProductVariations": [
        {"QuantityAvailableforPackageType": 0}, {"QuantityAvailableforPackageType": 0}]}) == 0
    assert _quantity({"ProductVariations": [
        {"QuantityAvailableforPackageType": 5}, {"QuantityAvailableforPackageType": 7}]}) == 7
    assert _quantity({}) is None


# --------------------------------------------------------------------------- #
# DigiKey keyword search, manufacturer and unit price
# --------------------------------------------------------------------------- #

def _ic_product(mpn, **extra):
    return {
        "ManufacturerProductNumber": mpn,
        "Manufacturer": {"Id": 497, "Name": "STMicroelectronics"},
        "UnitPrice": 7.25,
        "QuantityAvailable": 900,
        "ProductStatus": {"Id": 0, "Status": "Active"},
        "ProductVariations": [{"DigiKeyProductNumber": f"497-{mpn}-ND"}],
        "Parameters": [{"ParameterText": "Package / Case", "ValueText": "48-LQFP"}],
        **extra,
    }


def _fake_keyword(monkeypatch, body, *, status=200, raise_json=False):
    """A DigiKeyClient whose token POST works and whose keyword-search POST
    returns *body*; returns the client and the list of search request bodies."""
    sent = []

    def fake_post(url, **kwargs):
        if url == suppliers.TOKEN_URL:
            return FakeResp({"access_token": "tok", "expires_in": 3600})
        assert url == suppliers.V4_SEARCH_URL
        sent.append(kwargs.get("json"))
        return FakeResp(body, status=status, raise_json=raise_json)

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    return DigiKeyClient(client_id="id", client_secret="secret"), sent


def test_product_parses_manufacturer_and_unit_price(monkeypatch):
    client = _fake_dk(monkeypatch, details=_ic_product("STM32L552CCT6"))
    p = client.product_for("STM32L552CCT6")
    assert p.manufacturer == "STMicroelectronics"
    assert p.unit_price == 7.25


def test_product_without_manufacturer_or_price(monkeypatch):
    client = _fake_dk(monkeypatch)
    p = client.product_for("311-10.0KLRCT-ND")
    assert p.manufacturer is None and p.unit_price is None


def test_keyword_search_returns_exact_matches_then_products(monkeypatch):
    body = {
        "ExactMatches": [_ic_product("STM32L552CCT6")],
        "Products": [_ic_product("STM32L552CCT6"), _ic_product("STM32L552CCT6TR"),
                     _ic_product("STM32L552CET6")],
    }
    client, sent = _fake_keyword(monkeypatch, body)
    hits = client.keyword_search("STM32L552CCT6", limit=20)
    assert [h.mpn for h in hits] == ["STM32L552CCT6", "STM32L552CCT6TR", "STM32L552CET6"]
    assert hits[0].digikey_pns == ["497-STM32L552CCT6-ND"]
    assert sent == [{"Keywords": "STM32L552CCT6", "Limit": 20, "Offset": 0}]


def test_keyword_search_failures_give_empty_list(monkeypatch):
    client, _ = _fake_keyword(monkeypatch, {}, status=500)
    assert client.keyword_search("X") == []
    client, _ = _fake_keyword(monkeypatch, None, raise_json=True)
    assert client.keyword_search("X") == []


def test_keyword_search_retries_once_with_a_new_token_after_401(monkeypatch):
    # The first token is refused (expired early); a second token is fetched
    # and the search is sent again with it.
    tokens = iter(["old", "new"])
    calls = []

    def fake_post(url, **kwargs):
        if url == suppliers.TOKEN_URL:
            tok = next(tokens)
            calls.append(("token", tok))
            return FakeResp({"access_token": tok, "expires_in": 3600})
        assert url == suppliers.V4_SEARCH_URL
        auth = kwargs["headers"]["Authorization"]
        calls.append(("search", auth))
        if auth == "Bearer old":
            return FakeResp({}, status=401)
        return FakeResp({"Products": [_ic_product("STM32L552CCT6")]})

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    client = DigiKeyClient(client_id="id", client_secret="secret")
    assert [h.mpn for h in client.keyword_search("STM32L552CCT6")] == ["STM32L552CCT6"]
    assert calls == [("token", "old"), ("search", "Bearer old"),
                     ("token", "new"), ("search", "Bearer new")]


def test_keyword_search_needs_credentials_and_keyword(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not hit the network")

    monkeypatch.setattr(suppliers.requests, "post", boom)
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    assert DigiKeyClient().keyword_search("STM32") == []
    assert DigiKeyClient(client_id="id", client_secret="s").keyword_search("  ") == []


def test_keyword_search_network_error_gives_empty_list(monkeypatch):
    def fake_post(url, **kwargs):
        if url == suppliers.TOKEN_URL:
            return FakeResp({"access_token": "tok", "expires_in": 3600})
        raise suppliers.requests.ConnectionError("down")

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    assert DigiKeyClient(client_id="id", client_secret="s").keyword_search("X") == []
