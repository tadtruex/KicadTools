"""Tests for the part-named footprint search (kicadtools.namedsearch,
THT_PLAN.md step 6), offline with a fake DigiKey client."""
import pytest

from kicadtools.icsearch import MATCH, MISMATCH, UNVERIFIED
from kicadtools.namedsearch import (
    FOOTPRINT,
    MAX_LOOKUPS,
    MPN_FIELD,
    NO_DIGIKEY,
    NO_TEXT,
    VALUE,
    find_named_candidates,
    mpn_fits,
    named_texts,
    pad_check,
    positions_of,
    search_pattern,
)
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtfootprint import parse_signature

JST = parse_signature("Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal")
CUI = parse_signature("digikey-footprints:CUI_PJ-002B")
SAMTEC = parse_signature("Samtech:SAMTEC_USB-A-S-X-X-TH")
POT = parse_signature("Potentiometer_THT:Potentiometer_Bourns_3386P_Vertical")
DIN = parse_signature("StockedComponents:DIN5_MIDI")


def _p(mpn, stock=10000, price=0.1, status="Active", maker="JST Sales America Inc.", **params):
    return DigiKeyProduct(
        mpn=mpn, manufacturer=maker, quantity_available=stock, unit_price=price,
        product_status=status, digikey_pns=[f"{mpn}-ND"], parameters=params,
    )


class FakeDigiKey:
    def __init__(self, products=(), search=None, configured=True):
        self.configured = configured
        self.products = {p.mpn: p for p in products}
        self.search = search if search is not None else {}
        self.lookups = []
        self.keywords = []

    def product_for(self, pn):
        self.lookups.append(pn)
        return self.products.get(pn)

    def keyword_search(self, keyword):
        self.keywords.append(keyword)
        return list(self.search.get(keyword, []))


# --- texts and matching -----------------------------------------------------------


def test_texts_in_order_without_repeats():
    props = {"MPN": "S3B-PH-K-S", "Value": "I2C_Conn PH-3"}
    assert named_texts(JST, props) == [
        (MPN_FIELD, "MPN S3B-PH-K-S", "S3B-PH-K-S"),
        (FOOTPRINT, "footprint S3B-PH-K", "S3B-PH-K"),
    ]


def test_value_token_when_the_footprint_names_nothing():
    assert named_texts(DIN, {"Value": "SDS-50J"}) == [(VALUE, "Value SDS-50J", "SDS-50J")]


def test_value_that_fits_the_footprint_goes_first():
    props = {"Value": "USB-A-S-F-B-TH"}
    assert [k for k, _, _ in named_texts(SAMTEC, props)] == [VALUE, FOOTPRINT]
    assert named_texts(SAMTEC, props)[0][2] == "USB-A-S-F-B-TH"


def test_value_tokens():
    from kicadtools.namedsearch import value_tokens
    assert value_tokens("Conn_01x04") == ()
    assert value_tokens("USB-A-S-F-B-TH") == ("USB-A-S-F-B-TH",)
    assert value_tokens("I2C_Conn") == ()


def test_placeholder_pattern():
    keyword, pattern = search_pattern("USB-A-S-X-X-TH")
    assert keyword == "USB-A-S"
    assert pattern is not None
    assert mpn_fits("USB-A-S-F-B-TH", "USB-A-S-X-X-TH")
    assert mpn_fits("USB-A-S-S-W-TH-P", "USB-A-S-X-X-TH")
    assert not mpn_fits("USB-A-S-F-B-SM", "USB-A-S-X-X-TH")


def test_prefix_match_is_normalized():
    assert mpn_fits("3386P-1-204LF", "3386P")
    assert mpn_fits("s3b-ph-k-s", "S3B-PH-K")
    assert not mpn_fits("S3BPHK", "S3B-PH-K")  # punctuation is kept
    assert not mpn_fits("B3B-PH-K", "S3B-PH-K")


# --- checks -------------------------------------------------------------------------


def test_positions_from_positions_or_pins():
    assert positions_of(_p("A", **{"Number of Positions": "3"})) == 3
    assert positions_of(_p("A", **{"Number of Pins": "8"})) == 8
    assert positions_of(_p("A")) is None


@pytest.mark.parametrize(
    "pads, positions, result",
    [(3, "3", MATCH), (5, "3", UNVERIFIED), (2, "3", MISMATCH), (None, "3", UNVERIFIED), (3, None, UNVERIFIED)],
)
def test_pad_check(pads, positions, result):
    params = {"Number of Positions": positions} if positions else {}
    assert pad_check(pads, _p("A", **params)).result == result


# --- search ---------------------------------------------------------------------------


def test_footprint_token_finds_exact_part_first():
    hits = [_p("S3B-PH-K-S", stock=None), _p("S3B-PH-K", stock=None), _p("B3B-PH-K")]
    full = [_p("S3B-PH-K-S", stock=36895, price=0.15, **{"Number of Positions": "3"}),
            _p("S3B-PH-K", stock=50000, price=0.07, **{"Number of Positions": "3"})]
    dk = FakeDigiKey(full, {"S3B-PH-K": hits})
    r = find_named_candidates(JST, {"Value": "I2C_Conn"}, digikey=dk, needed=5, pad_count=3)
    assert [c.mpn for c in r.candidates] == ["S3B-PH-K", "S3B-PH-K-S"]
    assert r.proposed.exact and r.proposed.pads.result == MATCH
    assert r.proposed.digikey_pn == "S3B-PH-K-ND"
    assert dk.keywords == ["S3B-PH-K"]
    assert not r.problem


def test_mpn_field_is_looked_up_exactly_and_ends_the_search():
    dk = FakeDigiKey([_p("PJ-002B", maker="Same Sky")])
    r = find_named_candidates(CUI, {"MPN": "PJ-002B"}, digikey=dk, needed=5)
    assert [c.mpn for c in r.candidates] == ["PJ-002B"]
    assert dk.keywords == []


def test_mpn_field_not_found_falls_back_to_footprint():
    dk = FakeDigiKey([_p("PJ-002B")], {"PJ-002B": [_p("PJ-002B")]})
    r = find_named_candidates(CUI, {"MPN": "PJ-002X"}, digikey=dk, needed=5)
    assert [c.mpn for c in r.candidates] == ["PJ-002B"]
    assert any("MPN PJ-002X: not found" in n for n in r.notes)


def test_rejects_inactive_and_short_stock():
    hits = [_p("3386P-1-204LF"), _p("3386P-1-103LF"), _p("3386P-1-501")]
    full = [_p("3386P-1-204LF", stock=20), _p("3386P-1-103LF", status="Obsolete"),
            _p("3386P-1-501", stock=900)]
    dk = FakeDigiKey(full, {"3386P": hits})
    r = find_named_candidates(POT, {"Value": "200K"}, digikey=dk, needed=5)
    assert [c.mpn for c in r.candidates] == ["3386P-1-501"]
    assert r.candidates[0].stock_warning == "DigiKey stock 900, below 1005"
    assert dict(r.rejected) == {
        "3386P-1-204LF": "DigiKey stock 20, need at least 50",
        "3386P-1-103LF": "DigiKey status Obsolete",
    }


def test_lookup_budget():
    hits = [_p(f"3386P-1-{i:03d}") for i in range(MAX_LOOKUPS + 3)]
    dk = FakeDigiKey(hits, {"3386P": hits})
    r = find_named_candidates(POT, {}, digikey=dk, needed=1)
    assert len(r.candidates) == MAX_LOOKUPS
    assert r.dropped_by_limit == 3


def test_placeholder_search():
    hits = [_p("USB-A-S-F-B-TH", maker="Samtec Inc."), _p("USB-A-S-F-B-SM", maker="Samtec Inc.")]
    dk = FakeDigiKey(hits, {"USB-A-S": hits})
    r = find_named_candidates(SAMTEC, {}, digikey=dk, needed=5)
    assert [c.mpn for c in r.candidates] == ["USB-A-S-F-B-TH"]
    assert dk.keywords == ["USB-A-S"]


def test_more_pads_ranks_after_a_match():
    hits = [_p("PJ-002B", **{"Number of Positions": "2"}), _p("PJ-002BH", **{"Number of Positions": "3"})]
    dk = FakeDigiKey(hits, {"PJ-002B": hits})
    r = find_named_candidates(CUI, {}, digikey=dk, needed=5, pad_count=3)
    assert [c.mpn for c in r.candidates] == ["PJ-002B", "PJ-002BH"]  # exact first
    assert r.candidates[0].pads.result == UNVERIFIED
    assert r.candidates[1].pads.result == MATCH


@pytest.mark.parametrize(
    "dk, sig, props, problem",
    [
        (FakeDigiKey(configured=False), JST, {}, NO_DIGIKEY),
        (FakeDigiKey(), DIN, {"Value": "MIDI_IN"}, NO_TEXT),
    ],
)
def test_problems(dk, sig, props, problem):
    assert find_named_candidates(sig, props, digikey=dk, needed=5).problem == problem


def test_nothing_found():
    r = find_named_candidates(JST, {}, digikey=FakeDigiKey(), needed=5)
    assert r.problem == "no DigiKey part found for S3B-PH-K"
