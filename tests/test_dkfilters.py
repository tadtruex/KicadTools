"""Tests for DigiKey category filters (kicadtools.dkfilters) and
DigiKeyClient.category_search, offline. The options are a trimmed copy of a
live category 314 (Headers, Male Pins) response from 2026-11-03."""
from types import SimpleNamespace

import pytest

from kicadtools import suppliers
from kicadtools.dkfilters import (
    mm_figure,
    parse_filter_options,
    resolve_filters,
    search_body,
    value_matches,
)
from kicadtools.suppliers import DigiKeyClient


def _param(pid, name, *values):
    return {
        "Category": {"Id": 314, "Value": "Headers, Male Pins"},
        "ParameterType": "String",
        "ParameterId": pid,
        "ParameterName": name,
        "FilterValues": [
            {"ProductCount": 10, "ValueId": vid, "ValueName": vname} for vid, vname in values
        ],
    }


OPTIONS = {
    "FilterOptions": {
        "Manufacturers": [
            {"Id": 1, "Value": "Sullins Connector Solutions", "ProductCount": 900},
            {"Id": 2, "Value": "Würth Elektronik", "ProductCount": 500},
            {"Id": 3, "Value": "TE Connectivity AMP Connectors", "ProductCount": 400},
            {"Id": 4, "Value": "Technetix", "ProductCount": 1},
            {"Id": 5, "Value": "Samtec Inc.", "ProductCount": 800},
        ],
        "ParametricFilters": [
            _param(88, "Number of Positions", ("39665", "1"), ("121326", "2"), ("39667", "4")),
            _param(90, "Number of Rows", ("39665", "1"), ("121326", "2")),
            _param(1790, "Pitch - Mating", ("5212", '0.020" (0.50mm)'), ("5560", '0.100" (2.54mm)')),
            _param(69, "Mounting Type", ("1", "-"), ("400", "Through Hole"), ("401", "Through Hole, Right Angle")),
            _param(512, "Contact Type", ("1", "-"), ("500", "Male Pin")),
        ],
    },
    "Products": [],
    "ProductsCount": 58540,
}


@pytest.fixture
def options():
    return parse_filter_options(OPTIONS, 314)


# --- matching ------------------------------------------------------------------


def test_mm_figure():
    assert mm_figure('0.100" (2.54mm)') == 2.54
    assert mm_figure("4") == 4.0
    assert mm_figure("-") is None
    assert mm_figure("Through Hole") is None


def test_value_matches():
    assert value_matches("Through Hole", "through hole")
    assert not value_matches("Through Hole, Right Angle", "Through Hole")
    assert value_matches('0.100" (2.54mm)', 2.54)
    assert not value_matches('0.020" (0.50mm)', 2.54)
    assert value_matches("4", 4)
    assert value_matches("Male Pin", ["Female Socket", "Male Pin"])
    assert value_matches("Gold", lambda n: n.startswith("Gold"))
    assert not value_matches("1", True)


def test_parse_options(options):
    assert options.category_name == "Headers, Male Pins"
    assert [p.id for p in options.parameters] == [88, 90, 1790, 69, 512]
    assert options.parameter("number of rows").id == 90
    assert options.parameter(1790).name == "Pitch - Mating"
    assert options.parameter("Color") is None


def test_value_ids_are_per_parameter(options):
    # "1" has the same ValueId under positions and rows.
    assert options.value_ids("Number of Positions", 1) == ["39665"]
    assert options.value_ids("Number of Rows", 2) == ["121326"]
    assert options.value_ids("Pitch - Mating", 2.54) == ["5560"]
    assert options.value_ids("Missing", 1) == []


def test_manufacturer_ids_word_prefix_without_accents(options):
    assert options.manufacturer_ids(["Sullins"]) == ["1"]
    assert options.manufacturer_ids(["Wurth"]) == ["2"]
    assert options.manufacturer_ids(["TE"]) == ["3"]  # not Technetix
    assert options.manufacturer_ids(["Samtec", "Harwin"]) == ["5"]
    assert options.manufacturer_ids([" "]) == []


def test_resolve_filters(options):
    resolved = resolve_filters(
        options,
        {"Number of Positions": 4, "Number of Rows": 1, "Pitch - Mating": 2.54, 69: "Through Hole"},
        ["Sullins", "Samtec"],
    )
    assert resolved.parameters == (
        (88, ("39667",)), (90, ("39665",)), (1790, ("5560",)), (69, ("400",)),
    )
    assert resolved.manufacturers == ("1", "5")
    assert resolved.unmatched == ()


def test_resolve_filters_reports_unmatched(options):
    resolved = resolve_filters(
        options, {"Number of Positions": 40, "Color": "Red"}, ["Nobody"]
    )
    assert resolved.unmatched == (
        "Number of Positions = 40", "parameter 'Color'", "manufacturers ['Nobody']",
    )


def test_search_body(options):
    resolved = resolve_filters(options, {"Number of Rows": 1}, ["Sullins"])
    body = search_body("header", 314, resolved, min_stock=100, limit=80)
    assert body["Keywords"] == "header"
    assert body["Limit"] == 50  # DigiKey's maximum
    fo = body["FilterOptionsRequest"]
    assert fo["CategoryFilter"] == [{"Id": "314"}]
    assert fo["StatusFilter"] == [{"Id": 0}]
    assert fo["MinimumQuantityAvailable"] == 100
    assert fo["ManufacturerFilter"] == [{"Id": "1"}]
    assert fo["ParameterFilterRequest"] == {
        "CategoryFilter": {"Id": "314"},
        "ParameterFilters": [{"ParameterId": 90, "FilterValues": [{"Id": "39665"}]}],
    }
    assert body["SortOptions"] == {"Field": "Price", "SortOrder": "Ascending"}
    plain = search_body("header", 314, limit=1, sort_by_price=False)
    assert "SortOptions" not in plain
    assert set(plain["FilterOptionsRequest"]) == {"CategoryFilter", "StatusFilter"}


# --- DigiKeyClient.category_search ---------------------------------------------


def _product(mpn, price):
    return {
        "ManufacturerProductNumber": mpn,
        "Manufacturer": {"Id": 1, "Name": "Sullins Connector Solutions"},
        "UnitPrice": price,
        "QuantityAvailable": 1000,
        "Parameters": [{"ParameterText": "Number of Positions", "ValueText": "4"}],
        "ProductVariations": [{"DigiKeyProductNumber": f"S-{mpn}-ND"}],
    }


@pytest.fixture
def fake_dk(monkeypatch):
    sent = []
    search = {
        "Products": [_product("PRPC004SAAN-RC", 0.1), _product("PREC004SAAN-RC", 0.1)],
        "ExactMatches": [],
        "ProductsCount": 172,
        "FilterOptions": {},
    }

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        sent.append(body)
        data = OPTIONS if body["Limit"] == 1 else search
        return SimpleNamespace(status_code=200, headers={}, text="", json=lambda: data)

    monkeypatch.setattr(suppliers.requests, "post", fake_post)
    client = DigiKeyClient(client_id="id", client_secret="secret")
    client._token, client._token_expires_at = "tok", float("inf")
    return client, sent


def test_category_search(fake_dk):
    client, sent = fake_dk
    result = client.category_search(
        "header", 314, {"Number of Positions": 4, "Pitch - Mating": 2.54},
        manufacturers=["Sullins"], min_stock=100, limit=5,
    )
    assert [p.mpn for p in result.products] == ["PRPC004SAAN-RC", "PREC004SAAN-RC"]
    assert result.total == 172
    assert result.unmatched == () and result.error == ""
    assert len(sent) == 2  # options, then the search
    assert sent[1]["FilterOptionsRequest"]["ManufacturerFilter"] == [{"Id": "1"}]


def test_options_cached_for_the_session(fake_dk):
    client, sent = fake_dk
    client.category_search("header", 314, {"Number of Positions": 4})
    DigiKeyClient(client_id="id", client_secret="secret").category_filters  # another client
    client.category_search("Header ", 314, {"Number of Positions": 2})
    assert len(sent) == 3  # one options call for both searches


def test_unmatched_filter_makes_no_search(fake_dk):
    client, sent = fake_dk
    result = client.category_search("header", 314, {"Number of Positions": 40})
    assert result.products == [] and result.unmatched == ("Number of Positions = 40",)
    assert len(sent) == 1  # the options call only


def test_not_configured_or_no_keyword():
    assert DigiKeyClient(client_id="", client_secret="").category_search("header", 314).error
    assert DigiKeyClient(client_id="id", client_secret="s").category_search(" ", 314).error == "no keyword"


def test_stopped_limiter_gives_error(fake_dk, monkeypatch):
    client, sent = fake_dk
    suppliers.DIGIKEY_LIMITER.stopped = "DigiKey daily limit: test"
    result = client.category_search("header", 314, {"Number of Positions": 4})
    assert result.error == "DigiKey daily limit: test"
    assert sent == []
