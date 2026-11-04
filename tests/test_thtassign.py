"""Tests for through-hole groups, their search and write plan
(kicadtools.thtassign, THT_PLAN.md step 9), offline."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadtools.headersearch import HeaderRun
from kicadtools.mounting import Mounting
from kicadtools.partsdb import PartsDb
from kicadtools.pcb import SMD, THT
from kicadtools.requirement import Member
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtassign import (
    CURRENT,
    DATABASE,
    SEARCH,
    ThtGroup,
    ThtRequirement,
    assess_tht,
    group_tht,
    signature_text,
    tht_assignment_plan,
)
from kicadtools.thtdb import tht_choice
from kicadtools.thtfootprint import parse_signature

HDR = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
JST = "Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal"
PAD = "Passives:SolderWirePad_single_0-5mmDrill"
LOOP = "TestPoint:TestPoint_Loop_D1.80mm_Drill1.0mm_Beaded"


def _p(mpn, stock=10000, maker="Sullins Connector Solutions", status="Active", **params):
    return DigiKeyProduct(mpn=mpn, manufacturer=maker, quantity_available=stock, unit_price=0.1,
                          product_status=status, digikey_pns=[f"{mpn}-ND"], parameters=params)


class FakeBoard:
    """A BoardMounting stand-in: every reference has the given mounting."""

    def __init__(self, kinds, pads=4):
        self.kinds = kinds
        self.footprints = SimpleNamespace(placed=lambda ref: None,
                                          count=lambda ref, lib: pads)

    def mounting(self, ref, lib_id):
        return Mounting(kind=self.kinds.get(ref, THT), source="board", attr=THT)


def _rec(ref, footprint, value="Conn_01x04", intent=None, **props):
    return SimpleNamespace(reference=ref, intent=intent, source_file=Path("/b/x.kicad_sch"),
                           symbol_uuid=f"u-{ref}",
                           props={"Footprint": footprint, "Value": value, **props})


def _group(footprint, value="Conn_01x04", pads=4, refs=("J1",), **props):
    sig = parse_signature(footprint, pads)
    first = _rec(refs[0], footprint, value, **props)
    req = ThtRequirement(signature=sig, footprint=footprint, value_text=value,
                         pad_count=pads, props=first.props)
    members = [Member(reference=r, source_file=Path("/b/x.kicad_sch"), symbol_uuid=f"u-{r}",
                      props=_rec(r, footprint, value, **props).props, intent=None) for r in refs]
    return ThtGroup(req, members)


class FakeDigiKey:
    def __init__(self, products=(), search=None, category=()):
        self.configured = True
        self.products = {}
        for p in products:
            self.products[p.mpn] = p
            for pn in p.digikey_pns:
                self.products[pn] = p
        self.search = search or {}
        self.category = list(category)
        self.calls = []

    def product_for(self, pn):
        self.calls.append(("product_for", pn))
        return self.products.get(pn)

    def keyword_search(self, keyword):
        self.calls.append(("keyword", keyword))
        return list(self.search.get(keyword, []))

    def category_search(self, keyword, category, filters, *, manufacturers, min_stock, limit):
        self.calls.append(("category", dict(filters)))
        return SimpleNamespace(products=list(self.category), total=len(self.category),
                               unmatched=(), error="")


# --- groups -----------------------------------------------------------------------


def test_group_tht_keeps_through_hole_parts_without_their_own_rows():
    from kicadtools.intent import IcIntent
    records = [
        _rec("J2", HDR), _rec("J4", HDR),
        _rec("J9", HDR, value="Other"),          # another Value: its own group
        _rec("U1", "Package_DIP:DIP-8_W7.62mm", intent=IcIntent.__new__(IcIntent)),
        _rec("J3", "Connector_USB:USB_C_Receptacle"),  # SMD
    ]
    groups = group_tht(records, FakeBoard({"J3": SMD}))
    assert [g.references for g in groups] == [["J2", "J4"], ["J9"]]
    assert groups[0].requirement.pad_count == 4


@pytest.mark.parametrize(
    "footprint, value, props, problem, searchable",
    [
        (HDR, "Conn_01x04", {}, "", True),
        (JST, "I2C_Conn", {}, "", True),
        (PAD, "TestPoint", {}, "", False),                 # no part
        (LOOP, "TestPoint", {}, "test point footprints are not searched yet", False),
        (LOOP, "TestPoint", {"DigiKeyPN": "36-5001-ND"}, "", True),
        ("StockedComponents:DIN5_MIDI", "DIN-5_180degree", {},
         "other footprints are not searched yet", False),
        ("", "X", {}, "no footprint", False),
    ],
)
def test_requirement_problem(footprint, value, props, problem, searchable):
    req = _group(footprint, value, **props).requirement
    assert (req.problem(), req.searchable) == (problem, searchable)


def test_signature_text():
    assert signature_text(parse_signature(HDR)) == "header 1x04, 2.54 mm, vertical"
    assert signature_text(parse_signature(JST)) == "named part JST S3B-PH-K"
    assert signature_text(parse_signature(PAD)) == "no part (solder wire pad)"


def test_assigned_needs_mpn_manufacturer_and_digikey_pn():
    assert not _group(LOOP, DigiKeyPN="36-5001-ND").assigned
    assert _group(LOOP, DigiKeyPN="36-5001-ND", MPN="5001", Manufacturer="Keystone").assigned


# --- search ------------------------------------------------------------------------


def test_current_part_from_digikey_pn_for_any_family():
    dk = FakeDigiKey([_p("5001", maker="Keystone Electronics")])
    dk.products["36-5001-ND"] = dk.products["5001"]
    g = _group(LOOP, "TestPoint", pads=1, DigiKeyPN="36-5001-ND")
    r = assess_tht(g, run=HeaderRun(dk), digikey=dk, db=None, needed=5)
    assert [(c.mpn, c.source) for c in r.candidates] == [("5001", CURRENT)]
    assert dk.calls == [("product_for", "36-5001-ND")]


def test_header_search_with_current_part_first():
    current = _p("PREC004SAAN-RC", **{"Number of Positions": "4"})
    dk = FakeDigiKey([current], category=[_p("CHEAP-4"), current])
    g = _group(HDR, MPN="PREC004SAAN-RC")
    r = assess_tht(g, run=HeaderRun(dk), digikey=dk, db=None, needed=5)
    assert [(c.mpn, c.source) for c in r.candidates] == [
        ("PREC004SAAN-RC", CURRENT), ("CHEAP-4", SEARCH)]
    assert r.candidates[0].pads.result == "match"


def test_stored_part_first_then_rejected_left_out():
    db = PartsDb()
    sig = parse_signature(HDR)
    db.accept_part(tht_choice(_p("PREC004SAAN-RC"), sig, "Conn_01x04"))
    dk = FakeDigiKey([_p("PREC004SAAN-RC")])
    r = assess_tht(_group(HDR), run=HeaderRun(dk), digikey=dk, db=db, needed=5)
    assert [(c.mpn, c.source) for c in r.candidates] == [("PREC004SAAN-RC", DATABASE)]
    assert r.from_database
    assert not any(c[0] == "category" for c in dk.calls)
    # Search DigiKey anyway:
    dk.category = [_p("PREC004SAAN-RC"), _p("OTHER-4")]
    r = assess_tht(_group(HDR), run=HeaderRun(dk), digikey=dk, db=db, needed=5, use_stored=False)
    assert [c.source for c in r.candidates] == [SEARCH, SEARCH]


def test_named_part_search():
    hits = [_p("S3B-PH-K-S", maker="JST")]
    dk = FakeDigiKey(hits, {"S3B-PH-K": hits})
    r = assess_tht(_group(JST, "I2C_Conn", pads=3), run=HeaderRun(dk), digikey=dk, db=None, needed=5)
    assert [c.mpn for c in r.candidates] == ["S3B-PH-K-S"]


def test_not_searchable_and_no_part():
    dk = FakeDigiKey()
    r = assess_tht(_group(PAD, "TestPoint", pads=1), run=HeaderRun(dk), digikey=dk, db=None, needed=5)
    assert r.problem and not r.candidates and dk.calls == []


# --- write plan ----------------------------------------------------------------------


def test_write_plan_sets_numbers_and_removes_lcsc_and_aliases():
    g = _group(HDR, refs=("J2", "J4"), **{"Digikey PN": "OLD-ND", "LCSC": "C123"})
    from kicadtools.thtassign import ThtCandidate
    cand = ThtCandidate(product=_p("PREC004SAAN-RC"), signature=g.requirement.signature,
                        value="Conn_01x04")
    plan = tht_assignment_plan(g, cand)
    props = plan[Path("/b/x.kicad_sch")]["u-J2"]
    assert props["MPN"] == "PREC004SAAN-RC"
    assert props["Manufacturer"] == "Sullins Connector Solutions"
    assert props["DigiKeyPN"] == "PREC004SAAN-RC-ND"
    assert props["LCSC"] is None
    assert props["Digikey PN"] is None
    assert set(plan[Path("/b/x.kicad_sch")]) == {"u-J2", "u-J4"}


def test_write_plan_needs_a_digikey_pn():
    from kicadtools.thtassign import ThtCandidate
    g = _group(HDR)
    bare = DigiKeyProduct(mpn="X", manufacturer="Y")
    with pytest.raises(ValueError):
        tht_assignment_plan(g, ThtCandidate(product=bare, signature=g.requirement.signature, value=""))
