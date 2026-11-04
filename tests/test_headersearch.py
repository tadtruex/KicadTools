"""Tests for the header and socket search (kicadtools.headersearch,
THT_PLAN.md step 5), offline with a fake DigiKey client."""
from types import SimpleNamespace

import pytest

from kicadtools.headersearch import (
    HEADER_CATEGORY,
    HEADER_MAKERS,
    SOCKET_CATEGORY,
    HeaderRun,
    header_filters,
    rank_for_series,
    series_from_pick,
    stock_note,
)
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtfootprint import parse_signature

H1x04 = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
H1x07 = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x07_P2.54mm_Vertical")
H1x06_RA = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Horizontal")
H2x05 = parse_signature("Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical")
H1x01 = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical")
S1x08 = parse_signature("Connector_PinSocket_2.54mm:PinSocket_1x08_P2.54mm_Vertical")
H1x04_SMD = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical_SMD")

SERIES_PARAMS = {
    "Contact Finish - Mating": "Tin",
    "Connector Type": "Header, Breakaway",
    "Contact Length - Mating": '0.230" (5.84mm)',
    "Contact Length - Post": '0.120" (3.05mm)',
}


def _p(mpn, price=0.1, stock=10000, maker="Sullins Connector Solutions", **params):
    return DigiKeyProduct(
        mpn=mpn, manufacturer=maker, unit_price=price, quantity_available=stock,
        parameters={**SERIES_PARAMS, **params},
    )


# --- filters ------------------------------------------------------------------


def test_header_filters():
    assert header_filters(H1x04) == {
        "Contact Type": "Male Pin", "Number of Positions": 4, "Number of Rows": 1,
        "Pitch - Mating": 2.54, "Mounting Type": "Through Hole",
    }
    assert header_filters(H1x06_RA)["Mounting Type"] == "Through Hole, Right Angle"
    assert header_filters(H2x05)["Number of Positions"] == 10
    assert header_filters(H1x04_SMD)["Mounting Type"] == "Surface Mount"


def test_single_pin_has_no_pitch_filter():
    assert "Pitch - Mating" not in header_filters(H1x01)


def test_socket_filters_leave_rows_out():
    f = header_filters(S1x08)
    assert f["Contact Type"] == "Female Socket"
    assert "Number of Rows" not in f


# --- series -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mpn, sig, pattern, predicted_for_7",
    [
        ("PRPC004SAAN-RC", H1x04, r"PRPC\d{3}SAAN\-RC", "PRPC007SAAN-RC"),
        ("TSW-104-07-G-S", H1x04, r"TSW\-1\d{2}\-07\-G\-S", "TSW-107-07-G-S"),
        ("PPTC041LFBN-RC", parse_signature("Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical"),
         r"PPTC\d{2}1LFBN\-RC", "PPTC071LFBN-RC"),
        ("PRPC005DAAN-RC", H2x05, r"PRPC\d{3}DAAN\-RC", "PRPC007DAAN-RC"),  # per row
    ],
)
def test_series_pattern(mpn, sig, pattern, predicted_for_7):
    series = series_from_pick(_p(mpn), sig)
    assert series.pattern == pattern
    assert series.predicted_mpn(7) == predicted_for_7


def test_series_without_position_digits():
    series = series_from_pick(_p("ABC-XYZ"), H1x04)
    assert series.pattern is None and series.predicted_mpn(7) is None


def test_series_parameters_and_orientation():
    series = series_from_pick(_p("PRPC004SAAN-RC", **{"Contact Finish - Mating": "Gold"}), H1x04)
    assert series.manufacturer == "Sullins Connector Solutions"
    assert dict(series.parameters) == {"Contact Finish - Mating": "Gold", "Connector Type": "Header, Breakaway"}
    assert set(series.filters_for(H1x07)) == set(SERIES_PARAMS)  # same orientation: lengths too
    assert set(series.filters_for(H1x06_RA)) == {"Contact Finish - Mating", "Connector Type"}


def test_series_skips_dash_values():
    series = series_from_pick(_p("PRPC004SAAN-RC", **{"Connector Type": "-"}), H1x04)
    assert "Connector Type" not in dict(series.parameters)


def test_rank_for_series():
    series = series_from_pick(_p("PRPC004SAAN-RC"), H1x04)
    hits = [_p("PEC07SAAN", 0.05), _p("PRPC007SFAN-RC", 0.08), _p("PRPC007SAAN-RC", 0.12),
            _p("PRPC017SAAN-RC", 0.2)]
    ranked = [p.mpn for p in rank_for_series(hits, series, H1x07)]
    assert ranked == ["PRPC007SAAN-RC", "PRPC017SAAN-RC", "PEC07SAAN", "PRPC007SFAN-RC"]


def test_stock_note():
    assert stock_note(_p("X", stock=155), 5) == "DigiKey stock 155, below 1005"
    assert stock_note(_p("X", stock=5000), 5) == ""
    assert stock_note(_p("X", stock=None), 5) == ""


# --- HeaderRun -----------------------------------------------------------------------


class FakeClient:
    """Answers category_search from a function of its arguments and logs the calls."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def category_search(self, keyword, category, filters, *, manufacturers, min_stock, limit):
        call = dict(keyword=keyword, category=category, filters=dict(filters),
                    manufacturers=list(manufacturers), min_stock=min_stock, limit=limit)
        self.calls.append(call)
        products, unmatched = self.answer(call)
        return SimpleNamespace(products=products, total=len(products), unmatched=unmatched, error="")


def test_first_group_uses_maker_list_then_series():
    def answer(call):
        n = call["filters"]["Number of Positions"]
        if call["manufacturers"] == list(HEADER_MAKERS):
            return [_p(f"PREC00{n}SAAN-RC"), _p(f"OTHER-{n}", maker="Samtec Inc.")], ()
        return [_p(f"PEC0{n}SAAN", 0.05), _p(f"PREC00{n}SAAN-RC", 0.12)], ()

    client = FakeClient(answer)
    run = HeaderRun(client)
    first = run.search(H1x04, needed=5)
    assert first.series is None
    assert first.proposed.mpn == "PREC004SAAN-RC"
    assert client.calls[0]["category"] == HEADER_CATEGORY
    assert client.calls[0]["min_stock"] == 50  # 10 × the build quantity
    run.pick(first.proposed, H1x04)

    later = run.search(H1x07, needed=5)
    assert later.series is run.series["header"]
    assert later.proposed.mpn == "PREC007SAAN-RC"  # predicted MPN before the cheaper one
    assert client.calls[1]["manufacturers"] == ["Sullins Connector Solutions"]
    assert client.calls[1]["filters"]["Contact Finish - Mating"] == "Tin"

    run.pick(_p("OTHER-7", maker="Samtec Inc."), H1x07)  # later pick: series unchanged
    assert run.series["header"].mpn == "PREC004SAAN-RC"


def test_series_without_match_falls_back_to_makers():
    def answer(call):
        if call["manufacturers"] == ["Sullins Connector Solutions"]:
            return [], ()
        return [_p("TSW-112-07-G-D", maker="Samtec Inc.")], ()

    client = FakeClient(answer)
    run = HeaderRun(client)
    run.series["header"] = series_from_pick(_p("PREC004SAAN-RC"), H1x04)
    result = run.search(H2x05, needed=5)
    assert result.series is None
    assert result.proposed.mpn == "TSW-112-07-G-D"
    assert result.notes[0].startswith("series PREC004SAAN-RC (Sullins Connector Solutions): no part in stock")
    assert len(client.calls) == 2


def test_sockets_have_their_own_series_and_rows_after_search():
    def answer(call):
        assert call["category"] == SOCKET_CATEGORY
        assert call["limit"] == 50
        return [_p("PPTC042LFBN-RC", 0.3, **{"Number of Rows": "2"}),
                _p("PPTC081LFBN-RC", 0.5, **{"Number of Rows": "1"})], ()

    client = FakeClient(answer)
    run = HeaderRun(client)
    run.series["header"] = series_from_pick(_p("PREC004SAAN-RC"), H1x04)
    result = run.search(S1x08, needed=5)
    assert client.calls[0]["manufacturers"] == list(HEADER_MAKERS)  # no socket series yet
    assert [p.mpn for p in result.candidates] == ["PPTC081LFBN-RC"]  # 2-row hit dropped


def test_not_a_header():
    run = HeaderRun(FakeClient(lambda call: ([], ())))
    result = run.search(parse_signature("Capacitor_THT:CP_Radial_D6.3mm_P2.50mm"), needed=5)
    assert result.error.startswith("not a header or socket footprint")


def test_nothing_found_note_and_unmatched():
    run = HeaderRun(FakeClient(lambda call: ([], ("Number of Positions = 40",))))
    result = run.search(H1x04, needed=5)
    assert result.unmatched == ("Number of Positions = 40",)
    assert result.notes == []
    run = HeaderRun(FakeClient(lambda call: ([], ())))
    assert run.search(H1x04, needed=5).notes == ["no header in stock (at least 50) from the maker list"]
