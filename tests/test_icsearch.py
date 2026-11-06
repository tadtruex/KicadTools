"""Tests for IC candidate search, the footprint check and the IC write plan
(kicadtools.icsearch). Every supplier is a fake: no network."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pytest

from kicadtools.components import build_intent
from kicadtools.footprints import pad_count, parse_kicad_mod_pads
from kicadtools.icrequirement import group_ics, ic_requirement
from kicadtools.icsearch import (
    CURRENT,
    DATABASE,
    IC_STOCK_MARGIN,
    IC_STOCK_MIN_FACTOR,
    MATCH,
    MAX_IC_LOOKUPS,
    MISMATCH,
    MISMATCH_TEXT,
    NOT_AT_JLCPCB,
    NO_FOOTPRINT_REASON,
    SEARCH,
    UNVERIFIED,
    UNVERIFIED_TEXT,
    FootprintCheck,
    IcCandidate,
    assess_ic,
    board_pads,
    check_footprint,
    find_ic_candidates,
    ic_assignment_plan,
    ic_rank_key,
    stock_floor,
    stock_minimum,
    ic_requirement_for,
)
from kicadtools.package import PackageInfo, parse_digikey_package, parse_footprint
from kicadtools.partsdb import IcChoice, PartsDb
from kicadtools.resolve import PartRecord
from kicadtools.suppliers import DigiKeyProduct, JlcPart

NEEDED = 10
FLOOR = NEEDED + IC_STOCK_MARGIN
LEAST = NEEDED * IC_STOCK_MIN_FACTOR


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _dk(mpn, *, pns=None, stock=5000, status="Active", mfr="Texas Instruments",
        case="36-VFQFN Exposed Pad", sdp="36-VQFN (6x6)", price=3.5):
    return DigiKeyProduct(
        mpn=mpn, manufacturer=mfr,
        digikey_pns=list(pns if pns is not None else [f"{mpn}CT-ND", f"{mpn}TR-ND"]),
        datasheet_url=f"https://dk/{mpn}.pdf", quantity_available=stock,
        product_status=status, description=f"IC {mpn}",
        parameters={"Package / Case": case, "Supplier Device Package": sdp},
        unit_price=price,
    )


def _jlc(lcsc, mpn, *, mfr="TI", stock=5000, library="expand", no_buy="", price=1.2):
    return JlcPart(lcsc=lcsc, mpn=mpn, manufacturer=mfr, library=library,
                   stock=stock, unit_price=price, no_buy_reason=no_buy)


class FakeDigiKey:
    def __init__(self, products: Dict[str, DigiKeyProduct],
                 keyword: Optional[Dict[str, List[DigiKeyProduct]]] = None,
                 configured: bool = True):
        self.products = products
        self.keyword = keyword or {}
        self.configured = configured
        self.product_calls: List[str] = []
        self.keyword_calls: List[str] = []

    def product_for(self, pn):
        self.product_calls.append(pn)
        return self.products.get(pn)

    def keyword_search(self, text):
        self.keyword_calls.append(text)
        return list(self.keyword.get(text, []))


class FakeJlc:
    def __init__(self, parts: List[JlcPart]):
        self.parts = parts
        self.search_calls: List[str] = []

    def part(self, lcsc):
        return next((p for p in self.parts if p.lcsc == lcsc), None)

    def search(self, keyword):
        self.search_calls.append(keyword)
        return [p for p in self.parts if keyword.upper() in p.mpn.upper()]


def _db() -> PartsDb:
    return PartsDb(":memory:", now=lambda: "2026-10-30T12:00:00+00:00")


def _req(footprint="TexasInstruments:RHH0036C", pads=None, **props):
    req = ic_requirement({"Footprint": footprint, **props})
    from dataclasses import replace
    return replace(req, package=parse_footprint(footprint), pad_count=pads)


def _find(req, dk, jlc, db=None, needed=NEEDED):
    return find_ic_candidates(req, db=db or _db(), digikey=dk, jlc_search=jlc.search,
                              jlc_part=jlc.part, needed=needed)


# --------------------------------------------------------------------------- #
# Footprint check (step 6)
# --------------------------------------------------------------------------- #

def _pkg(case, sdp, **extra):
    return parse_digikey_package({"Package / Case": case, "Supplier Device Package": sdp,
                                  **extra})


def test_custom_footprint_matches_on_pad_count_with_exposed_pad():
    # DRV8313RHHR: 36 pins + exposed pad on a custom TI footprint with 37 pads.
    part = _pkg("36-VFQFN Exposed Pad", "36-VQFN (6x6)")
    check = check_footprint(parse_footprint("TexasInstruments:RHH0036C"), 37, part)
    assert check == FootprintCheck(MATCH)
    assert check.message == ""


def test_pad_count_differs_from_pin_count_is_a_mismatch():
    part = _pkg("24-VFQFN Exposed Pad", "24-QFN (4x4)")
    check = check_footprint(PackageInfo(), 36, part)
    assert check.result == MISMATCH
    assert check.reasons == ("36 pads vs 24 pins + exposed pad",)
    assert check.message.startswith(MISMATCH_TEXT)
    assert "36 pads vs 24 pins + exposed pad" in check.message


def test_pad_count_without_exposed_pad():
    part = _pkg("8-SOIC (0.154\", 3.90mm Width)", "8-SOIC")
    assert check_footprint(PackageInfo(), 8, part).result == MATCH
    check = check_footprint(PackageInfo(), 9, part)
    assert check.reasons == ("9 pads vs 8 pins",)


def _mod_pads(name, numbers):
    # A .kicad_mod with KiCad's pad numbering, in its order.
    pads = " ".join(f'(pad "{n}" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
                    for n in numbers)
    return pad_count(parse_kicad_mod_pads(f'(footprint "{name}" {pads})'))


# DigiKey's SOT-223 (counts the tab: read as 3 leads).
_SOT223 = {"Package / Case": "TO-261-4, TO-261AA", "Supplier Device Package": "SOT-223-4"}


@pytest.mark.parametrize("footprint, numbers", [
    # KiCad's Package_TO_SOT_SMD pad numbering, in file order.
    ("Package_TO_SOT_SMD:SOT-223", ["4", "2", "3", "1"]),          # tab is pad 4
    ("Package_TO_SOT_SMD:SOT-223-3_TabPin2", ["2", "2", "3", "1"]),  # tab is pad 2
])
def test_sot223_tab_numbered_or_shared_matches(footprint, numbers):
    pads = _mod_pads(footprint, numbers)
    check = check_footprint(parse_footprint(footprint), pads, parse_digikey_package(_SOT223))
    assert check == FootprintCheck(MATCH)


def test_tab_family_pad_count_tolerance():
    sot223 = parse_digikey_package(_SOT223)
    # 5 pads is neither the leads nor the leads and the tab.
    assert check_footprint(PackageInfo(), 5, sot223) == FootprintCheck(
        MISMATCH, ("5 pads vs 3 pins",))
    assert check_footprint(PackageInfo(), 2, sot223).result == MISMATCH
    # SOT-223-6_TabPin3: named 6, 5 distinct pad numbers (tab shares pad 3);
    # SOT-223-6: 6 pads.
    six = PackageInfo("SOT-223", 6, None, None, False)
    tab_pin3 = _mod_pads("SOT-223-6_TabPin3", ["4", "2", "3", "3", "5", "1"])
    assert tab_pin3 == 5
    assert check_footprint(parse_footprint("SOT-223-6_TabPin3"), tab_pin3, six).result == MATCH
    assert check_footprint(parse_footprint("SOT-223-6"), 6, six).result == MATCH
    # SOT-89-3: the tab shares pad 2.
    sot89 = parse_digikey_package({"Package / Case": "TO-243AA", "Supplier Device Package": "SOT-89-3"})
    assert check_footprint(parse_footprint("SOT-89-3"), 3, sot89).result == MATCH
    # Not a tab family: the pad count is exact.
    soic = _pkg('8-SOIC (0.154", 3.90mm Width)', "8-SOIC")
    assert check_footprint(PackageInfo(), 9, soic).result == MISMATCH


def test_exposed_pad_unknown_on_both_sides_accepts_either_count():
    part = parse_digikey_package({"Supplier Device Package": "6-DFN (2x2)"})
    assert part.ep is None
    assert check_footprint(PackageInfo(), 6, part).result == MATCH
    assert check_footprint(PackageInfo(), 7, part).result == MATCH
    assert check_footprint(PackageInfo(), 8, part).result == MISMATCH


def test_standard_name_match():
    fp = parse_footprint("Package_QFP:LQFP-48_7x7mm_P0.5mm")
    part = _pkg("48-LQFP", "48-LQFP (7x7)")
    assert check_footprint(fp, 48, part).result == MATCH
    # The name alone (no pad count) is enough when it states the pins.
    assert check_footprint(fp, None, part).result == MATCH


def test_tqfp_part_on_lqfp_footprint_matches():
    fp = parse_footprint("LQFP-48_7x7mm_P0.5mm")
    part = _pkg("48-TQFP", "48-TQFP (7x7)")
    assert check_footprint(fp, 48, part).result == MATCH


def test_family_body_and_pins_disagreement_named():
    fp = parse_footprint("QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm")
    part = _pkg("20-VFQFN Exposed Pad", "20-QFN (3x3)")
    check = check_footprint(fp, 25, part)
    assert check.result == MISMATCH
    assert "body 4x4 mm vs 3x3 mm" in check.reasons
    assert "24 pins vs 20 pins" in check.reasons
    assert "25 pads vs 20 pins + exposed pad" in check.reasons
    fp = parse_footprint("SOIC-8_3.9x4.9mm_P1.27mm")
    check = check_footprint(fp, 8, _pkg("8-TSSOP (0.173\", 4.40mm Width)", "8-TSSOP"))
    assert check.reasons[0] == "package SOIC vs TSSOP"


def test_pitch_disagreement_named_when_both_state_it():
    fp = parse_footprint("STMicro:LDL212_2x2mm_Pitch0.5mm")
    part = PackageInfo("DFN", 6, (2.0, 2.0), 0.65, True)
    check = check_footprint(fp, 7, part)
    assert check.result == MISMATCH
    assert check.reasons == ("pitch 0.5 mm vs 0.65 mm",)
    assert "pitch 0.5 mm vs 0.65 mm" in check.message


# LDL212PV33R as DigiKey lists it (live check, 2026-10-30): no pitch.
_LDL212 = {"Package / Case": "6-WFDFN Exposed Pad", "Supplier Device Package": "6-DFN (2x2)"}


@pytest.mark.parametrize("footprint, pitch", [
    ("STMicro:LDL212_2x2mm_Pitch0.5mm", "0.5"),                     # MotorDriver U4
    ("Package_DFN_QFN:DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm", "0.65"),  # BarrelJackAdapter U2
])
def test_ldl212_footprint_pitch_not_stated_by_digikey_is_unverified(footprint, pitch):
    # Both footprints have 7 pads (6 + exposed pad) and a 2x2 mm body, as the
    # part does; only the pitch differs, and DigiKey does not state it. A
    # match would hide that one of the two is wrong, and no field disagrees,
    # so neither is a mismatch: both boards are "cannot verify" with the
    # footprint's pitch named (IC_PLAN.md steps 6 and 9).
    check = check_footprint(parse_footprint(footprint), 7, parse_digikey_package(_LDL212))
    assert check == FootprintCheck(UNVERIFIED, (f"pitch {pitch} mm not stated by DigiKey",))
    assert check.message == (f"{UNVERIFIED_TEXT} Not compared: pitch {pitch} mm not "
                             "stated by DigiKey.")


def test_qfn_footprint_pitch_unverified_soic_and_lqfp_pitch_compared():
    # MotorDriver U3 (STUSB4500QTR): a 4x4 mm QFN-24 is made in more than
    # one pitch, so the footprint's 0.5 mm cannot be checked.
    part = _pkg("24-VFQFN Exposed Pad", "24-QFN (4x4)")
    check = check_footprint(parse_footprint("QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm"), 25, part)
    assert check.reasons == ("pitch 0.5 mm not stated by DigiKey",)
    # SOIC is 1.27 mm and a 48-pin 7x7 mm LQFP 0.5 mm by the standard.
    soic = _pkg('8-SOIC (0.154", 3.90mm Width)', "8-SOIC")
    assert check_footprint(parse_footprint("SOIC-8_3.9x4.9mm_P1.27mm"), 8, soic).result == MATCH
    check = check_footprint(PackageInfo("SOIC", 8, None, 0.65, False), 8, soic)
    assert check.reasons == ("pitch 0.65 mm vs 1.27 mm",)
    lqfp = _pkg("48-LQFP", "48-LQFP (7x7)")
    check = check_footprint(parse_footprint("LQFP-48_7x7mm_P0.8mm"), 48, lqfp)
    assert check.reasons == ("pitch 0.8 mm vs 0.5 mm",)


def test_footprint_without_pitch_still_matches():
    # BarrelJackAdapter U1 (TPS563207DRLR): SOT-563 names no pitch.
    part = _pkg("SOT-563, SOT-666", "SOT-563")
    assert check_footprint(parse_footprint("Package_TO_SOT_SMD:SOT-563"), 6, part).result == MATCH


def test_exposed_pad_disagreement_named():
    fp = parse_footprint("DFN-6_2x2mm_P0.65mm")      # no -1EP: ep False
    part = _pkg("6-UFDFN Exposed Pad", "6-DFN (2x2)")
    check = check_footprint(fp, 6, part)
    assert "exposed pad no vs yes" in check.reasons


def test_cannot_verify_custom_name_without_pad_count():
    part = _pkg("20-WFQFN Exposed Pad, CSP", "20-LFCSP-WQ (4x4)")
    check = check_footprint(parse_footprint("ECG_Footprints:CP_20_10_ADI_NOTH"), None, part)
    assert check.result == UNVERIFIED
    assert check.message.startswith(UNVERIFIED_TEXT)
    assert "pad count is unknown" in check.message


def test_cannot_verify_when_digikey_states_no_package():
    check = check_footprint(parse_footprint("DIP30-ArduinoNano"), 30, PackageInfo())
    assert check.result == UNVERIFIED
    assert check.reasons == ("DigiKey states no package",)


def test_check_ranks():
    assert FootprintCheck(MATCH).rank < FootprintCheck(UNVERIFIED).rank \
        < FootprintCheck(MISMATCH).rank


# --------------------------------------------------------------------------- #
# Exact lookups
# --------------------------------------------------------------------------- #

def test_digikey_pn_and_lcsc_resolve_to_one_candidate():
    # MotorDriver U1: DigiKey PN and LCSC for DRV8313RHHR.
    dk = FakeDigiKey({"296-DRV8313RHHR-ND": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C92482", "DRV8313RHHR")])
    req = _req(pads=37, Value="DRV8313RHHR", DigiKeyPN="296-DRV8313RHHR-ND", LCSC="C92482")
    result = _find(req, dk, jlc)
    assert [c.mpn for c in result.candidates] == ["DRV8313RHHR"]
    cand = result.candidates[0]
    assert cand.source == CURRENT and cand.current
    assert cand.fields == ("DigiKeyPN", "LCSC", "Value")
    assert (cand.lcsc, cand.at_jlcpcb, cand.jlc_text) == ("C92482", True, "C92482")
    assert cand.digikey_pn == "DRV8313RHHRCT-ND"
    assert cand.manufacturer == "Texas Instruments"
    assert cand.pins == 36 and cand.package == "36-VQFN (6x6)"
    assert cand.footprint.result == MATCH
    assert result.conflicts == []
    # The LCSC part's MPN was already found: no second DigiKey lookup, and
    # no JLCPCB search (the LCSC lookup gave the part).
    assert dk.product_calls == ["296-DRV8313RHHR-ND"]
    assert dk.keyword_calls == [] and jlc.search_calls == []


def test_digikey_only_part_is_not_at_jlcpcb():
    dk = FakeDigiKey({"497-19548-2-ND": _dk("LDL212PV33R", pns=["497-19548-2-ND"],
                                             mfr="STMicroelectronics",
                                             case="6-UFDFN Exposed Pad",
                                             sdp="6-DFN (2x2)")})
    jlc = FakeJlc([])
    req = _req("DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm", pads=7,
               Value="LDL212PV33R", DigiKeyPN="497-19548-2-ND")
    result = _find(req, dk, jlc)
    cand = result.candidates[0]
    assert cand.lcsc is None and not cand.at_jlcpcb
    assert cand.jlc_text == NOT_AT_JLCPCB
    assert cand.price == 3.5          # DigiKey's price when not at JLCPCB
    assert cand.digikey_pn == "497-19548-2-ND"
    assert jlc.search_calls == ["LDL212PV33R"]
    assert f"MPN LDL212PV33R: {NOT_AT_JLCPCB}" in result.notes
    # The 0.65 mm pitch of the footprint is not one DigiKey states.
    assert cand.footprint == FootprintCheck(
        UNVERIFIED, ("pitch 0.65 mm not stated by DigiKey",))
    assert cand.choice().lcsc is None


def test_lcsc_only_part_looked_up_at_digikey_by_its_mpn():
    dk = FakeDigiKey({"DRV8313RHHR": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C92482", "DRV8313RHHR")])
    req = _req(pads=37, Value="motor driver", LCSC="C92482")
    result = _find(req, dk, jlc)
    assert [c.mpn for c in result.candidates] == ["DRV8313RHHR"]
    assert result.candidates[0].fields == ("LCSC",)
    assert dk.product_calls == ["DRV8313RHHR"]


def test_jlcpcb_placeholder_listing_skipped():
    dk = FakeDigiKey({"LMC7101BYM5": _dk("LMC7101BYM5", case="SC-74A, SOT-753",
                                         sdp="SOT-23-5")})
    jlc = FakeJlc([_jlc("C9900013546", "LMC7101BYM5", mfr="JLCPCB Assembly", stock=0)])
    req = _req("Package_TO_SOT_SMD:SOT-23-5", MPN="LMC7101BYM5")
    cand = _find(req, dk, jlc).candidates[0]
    assert cand.lcsc is None


def test_jlcpcb_exact_mpn_only():
    dk = FakeDigiKey({"LMC7101BYM5": _dk("LMC7101BYM5", case="SC-74A, SOT-753",
                                         sdp="SOT-23-5")})
    jlc = FakeJlc([_jlc("C1", "LMC7101BYM5X")])
    req = _req("Package_TO_SOT_SMD:SOT-23-5", MPN="LMC7101BYM5")
    assert _find(req, dk, jlc).candidates[0].lcsc is None


# --------------------------------------------------------------------------- #
# Checks and the stock floor
# --------------------------------------------------------------------------- #

def test_digikey_stock_below_minimum_is_rejected():
    dk = FakeDigiKey({"X": _dk("TPS1234", stock=LEAST - 1)})
    result = _find(_req(DigiKeyPN="X"), dk, FakeJlc([]))
    assert result.candidates == []
    assert result.rejected == [("TPS1234", f"DigiKey stock {LEAST - 1}, need at least {LEAST}")]


def test_digikey_stock_between_minimum_and_floor_is_kept_with_a_warning():
    for stock in (LEAST, FLOOR - 1):
        dk = FakeDigiKey({"X": _dk("TPS1234", stock=stock)})
        (cand,) = _find(_req(DigiKeyPN="X"), dk, FakeJlc([])).candidates
        assert cand.digikey_warning == f"DigiKey stock {stock}, below {FLOOR}"
        assert cand.below_floor and cand.stock_warnings == (cand.digikey_warning,)
    dk = FakeDigiKey({"X": _dk("TPS1234", stock=FLOOR)})
    (cand,) = _find(_req(DigiKeyPN="X"), dk, FakeJlc([])).candidates
    assert cand.digikey_warning == "" and not cand.below_floor


def test_stock_floor_and_minimum():
    assert stock_floor(25) == 1025 and stock_minimum(25) == 250
    # A build quantity above 111 puts the minimum above the floor: the
    # minimum still applies and no part gets a warning.
    assert stock_minimum(200) == 2000 > stock_floor(200)
    assert stock_minimum(0) == IC_STOCK_MIN_FACTOR


def test_part_below_floor_ranks_after_part_meeting_it():
    low = IcCandidate(mpn="A", manufacturer="M", digikey_stock=500,
                      digikey_warning="DigiKey stock 500, below 1010")
    ok = IcCandidate(mpn="B", manufacturer="M", digikey_stock=2000)
    assert sorted([low, ok], key=ic_rank_key) == [ok, low]


def test_digikey_status_must_be_active():
    dk = FakeDigiKey({"X": _dk("TPS1234", status="Obsolete")})
    result = _find(_req(DigiKeyPN="X"), dk, FakeJlc([]))
    assert result.rejected == [("TPS1234", "DigiKey status 'Obsolete'")]


def test_part_not_at_digikey_is_rejected():
    jlc = FakeJlc([_jlc("C5", "ONLYJLC1")])
    result = _find(_req(LCSC="C5"), FakeDigiKey({}), jlc)
    assert result.candidates == []
    assert result.rejected == [("C5 ONLYJLC1", "not found at DigiKey")]


def test_jlcpcb_below_minimum_keeps_candidate_but_not_at_jlcpcb():
    dk = FakeDigiKey({"X": _dk("TPS1234")})
    jlc = FakeJlc([_jlc("C7", "TPS1234", stock=33)])
    cand = _find(_req(DigiKeyPN="X"), dk, jlc).candidates[0]
    assert cand.lcsc == "C7" and not cand.at_jlcpcb
    assert cand.jlc_note == f"JLCPCB stock 33, need at least {LEAST}"
    assert cand.jlc_text == f"C7 (JLCPCB stock 33, need at least {LEAST})"
    assert not cand.below_floor


def test_jlcpcb_between_minimum_and_floor_is_at_jlcpcb_with_a_warning():
    dk = FakeDigiKey({"X": _dk("TPS1234")})
    jlc = FakeJlc([_jlc("C7", "TPS1234", stock=500)])
    cand = _find(_req(DigiKeyPN="X"), dk, jlc).candidates[0]
    assert cand.at_jlcpcb and cand.jlc_note == ""
    assert cand.jlc_warning == f"JLCPCB stock 500, below {FLOOR}"
    assert cand.jlc_text == f"C7 (JLCPCB stock 500, below {FLOOR})"
    assert cand.below_floor and cand.choice().lcsc == "C7"


def test_lcsc_field_naming_a_placeholder_is_searched_by_mpn():
    # BarrelJackAdapter's C9900013546: the LCSC field names JLCPCB's own
    # placeholder listing; the search by its MPN finds the real C222.
    dk = FakeDigiKey({"DRV8313RHHR": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C9900013546", "DRV8313RHHR", mfr="JLCPCB Assembly", stock=0,
                        no_buy="no longer manufactured"),
                   _jlc("C222", "DRV8313RHHR", stock=9000)])
    result = _find(_req(pads=37, LCSC="C9900013546"), dk, jlc)
    (cand,) = result.candidates
    assert (cand.lcsc, cand.at_jlcpcb, cand.manufacturer) == ("C222", True, "Texas Instruments")
    assert jlc.search_calls == ["DRV8313RHHR"]


def test_lcsc_field_naming_only_a_placeholder_is_not_at_jlcpcb():
    dk = FakeDigiKey({"DRV8313RHHR": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C9900013546", "DRV8313RHHR", mfr="JLCPCB Assembly", stock=0)])
    (cand,) = _find(_req(pads=37, LCSC="C9900013546"), dk, jlc).candidates
    assert cand.lcsc is None


def test_lcsc_field_below_floor_replaced_by_an_orderable_listing():
    dk = FakeDigiKey({"DRV8313RHHR": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C7", "DRV8313RHHR", stock=33), _jlc("C8", "DRV8313RHHR", stock=9000)])
    (cand,) = _find(_req(pads=37, LCSC="C7"), dk, jlc).candidates
    assert (cand.lcsc, cand.at_jlcpcb) == ("C8", True)
    # Only the stated listing: it is kept, below the floor.
    jlc = FakeJlc([_jlc("C7", "DRV8313RHHR", stock=33)])
    (cand,) = _find(_req(pads=37, LCSC="C7"), dk, jlc).candidates
    assert (cand.lcsc, cand.at_jlcpcb) == ("C7", False)


def test_part_without_digikey_pn_is_rejected():
    dk = FakeDigiKey({"X": _dk("TPS1234", pns=[])})
    result = _find(_req(DigiKeyPN="X"), dk, FakeJlc([]))
    assert result.candidates == []
    assert result.rejected == [("TPS1234", "no DigiKey part number")]


def test_jlcpcb_unbuyable_noted():
    dk = FakeDigiKey({"X": _dk("TPS1234")})
    jlc = FakeJlc([_jlc("C7", "TPS1234", no_buy="discontinued")])
    cand = _find(_req(DigiKeyPN="X"), dk, jlc).candidates[0]
    assert cand.jlc_note == "not orderable at JLCPCB (discontinued)"
    assert cand.jlc_stock == 0


def test_rejected_in_database():
    db = _db()
    db.reject_part(IcChoice(mpn="TPS1234", manufacturer="Texas Instruments"))
    result = _find(_req(DigiKeyPN="X"), FakeDigiKey({"X": _dk("TPS1234")}), FakeJlc([]), db=db)
    assert result.rejected == [("TPS1234", "rejected in the parts database")]


def test_not_searchable_makes_no_calls():
    dk = FakeDigiKey({})
    result = _find(_req(Value="Arduino Nano"), dk, FakeJlc([]))
    assert result.problem == "no part number"
    assert dk.product_calls == [] and dk.keyword_calls == []


def test_symbol_without_footprint_is_searched_and_left_unverified():
    # LabBoard U1: no footprint yet; the part is found and its footprint
    # check says why it cannot be checked.
    dk = FakeDigiKey({"X": _dk("TPS1234")})
    result = _find(_req(footprint="", DigiKeyPN="X"), dk, FakeJlc([]))
    assert result.problem == ""
    (cand,) = result.candidates
    assert cand.mpn == "TPS1234"
    assert cand.footprint == FootprintCheck(UNVERIFIED, (NO_FOOTPRINT_REASON,))


def test_digikey_unconfigured_reports_problem():
    dk = FakeDigiKey({"X": _dk("TPS1234")}, configured=False)
    result = _find(_req(DigiKeyPN="X"), dk, FakeJlc([]))
    assert result.candidates == [] and "DigiKey credentials" in result.problem
    assert dk.product_calls == []
    assert "DigiKeyPN X: not looked up, DigiKey credentials are not set" in result.notes


def test_failed_lookup_becomes_a_note():
    class Boom(FakeDigiKey):
        def product_for(self, pn):
            raise RuntimeError("timeout")
    result = _find(_req(DigiKeyPN="X"), Boom({}), FakeJlc([]))
    assert result.candidates == []
    assert any("failed: timeout" in n for n in result.notes)
    # A failed call is not "not found": no conflict line for it.
    assert result.conflicts == []
    assert result.problem == ("no part found for the symbol's identifiers; "
                              "a supplier lookup failed")


# --------------------------------------------------------------------------- #
# The problem line when no part is offered
# --------------------------------------------------------------------------- #

def test_no_candidate_problem_names_the_rejected_part():
    # MotorDriver U1, U2 (live check): DRV8313RHHR at DigiKey stock 164,
    # two per board on a 25-board build.
    dk = FakeDigiKey({"296-44591-2-ND": _dk("DRV8313RHHR", pns=["296-44591-2-ND"],
                                             stock=164)})
    jlc = FakeJlc([_jlc("C165154", "DRV8313RHHR", stock=599)])
    req = _req(pads=37, DigiKeyPN="296-44591-2-ND", LCSC="C165154", Value="DRV8313RHHR")
    result = _find(req, dk, jlc, needed=50)
    assert result.candidates == []
    assert result.problem == ("no part passed: C165154 DRV8313RHHR DigiKey stock 164, "
                              "need at least 500")


def test_no_candidate_problem_names_three_rejected_parts_then_counts():
    dk = FakeDigiKey(
        {"161-LTC6992HS6-2#WTRMPBFCT-ND": _dk("LTC6992HS6-2#WTRMPBF", stock=200)},
        keyword={"LTC6992CS6-2": [_dk("LTC6992CS6-2#PBF", status="Discontinued at Digi-Key"),
                                  _dk("LTC6992CS6-2#TRPBF", stock=0),
                                  _dk("LTC6992CS6-2#TRMPBF", stock=0)]})
    req = _req("TSOT-23-6", pads=6, DigiKeyPN="161-LTC6992HS6-2#WTRMPBFCT-ND",
               Value="LTC6992CS6-2")
    result = _find(req, dk, FakeJlc([]), needed=25)
    assert result.candidates == [] and len(result.rejected) == 4
    assert result.problem == (
        "no part passed: LTC6992HS6-2#WTRMPBF DigiKey stock 200, need at least 250; "
        "LTC6992CS6-2#PBF DigiKey status 'Discontinued at Digi-Key'; "
        "LTC6992CS6-2#TRPBF DigiKey stock 0, need at least 250; and 1 more")


def test_buckconverter_u1_h_grade_is_kept_below_floor():
    # BuckConverter U1 (live check): the H grade at DigiKey stock 798 is
    # above the minimum (250 for 25 units) and below the floor (1025).
    dk = FakeDigiKey(
        {"161-LTC6992HS6-2#WTRMPBFCT-ND": _dk("LTC6992HS6-2#WTRMPBF", stock=798)},
        keyword={"LTC6992CS6-2": [_dk("LTC6992CS6-2#TRPBF", stock=0)]})
    req = _req("TSOT-23-6", pads=6, DigiKeyPN="161-LTC6992HS6-2#WTRMPBFCT-ND",
               Value="LTC6992CS6-2")
    result = _find(req, dk, FakeJlc([]), needed=25)
    (cand,) = result.candidates
    assert cand.mpn == "LTC6992HS6-2#WTRMPBF"
    assert cand.digikey_warning == "DigiKey stock 798, below 1025"


def test_no_candidate_problem_when_nothing_found():
    result = _find(_req(DigiKeyPN="NOPE-ND"), FakeDigiKey({}), FakeJlc([]))
    assert (result.candidates, result.rejected) == ([], [])
    assert result.problem == "no part found for the symbol's identifiers"


def test_problem_empty_when_a_part_is_offered():
    result = _find(_req(DigiKeyPN="X"), FakeDigiKey({"X": _dk("TPS1234")}), FakeJlc([]))
    assert result.candidates and result.problem == ""


# --------------------------------------------------------------------------- #
# An identifier that names no part
# --------------------------------------------------------------------------- #

def _tps563207(**kw):
    return _dk("TPS563207DRLR", pns=["296-TPS563207DRLRCT-ND", "296-TPS563207DRLRTR-ND"],
               case="SOT-563, SOT-666", sdp="SOT-563", **kw)


def test_digikey_pn_that_names_nothing_is_a_conflict():
    # BarrelJackAdapter U1 (live check): the DigiKeyPN lacks the 296- prefix
    # and DigiKey does not list it; the MPN field finds the part.
    dk = FakeDigiKey({"TPS563207DRLR": _tps563207()})
    req = _req("Package_TO_SOT_SMD:SOT-563", pads=6, MPN="TPS563207DRLR",
               DigiKeyPN="TPS563207DRLRCT-ND", Value="TPS563207DRLR")
    result = _find(req, dk, FakeJlc([]))
    assert "DigiKeyPN TPS563207DRLRCT-ND: not found at DigiKey" in result.notes
    assert result.conflicts == [
        "DigiKeyPN TPS563207DRLRCT-ND names no DigiKey part; "
        "MPN TPS563207DRLR names TPS563207DRLR"]
    (cand,) = result.candidates
    assert cand.digikey_pn == "296-TPS563207DRLRCT-ND"


def test_lcsc_that_names_nothing_is_a_conflict():
    dk = FakeDigiKey({"296-TPS563207DRLRCT-ND": _tps563207()})
    req = _req("SOT-563", pads=6, DigiKeyPN="296-TPS563207DRLRCT-ND", LCSC="C404")
    result = _find(req, dk, FakeJlc([]))
    assert result.conflicts == [
        "LCSC C404 names no JLCPCB part; DigiKeyPN 296-TPS563207DRLRCT-ND names TPS563207DRLR"]


def test_mpn_field_found_only_as_partial_is_not_a_conflict():
    # The MPN field is not an exact DigiKey MPN but its partial search finds
    # the part: it names a part, so no line for it.
    dk = FakeDigiKey({"TPS563207DRLR": _tps563207()},
                     keyword={"TPS563207": [_tps563207()]})
    result = _find(_req("SOT-563", pads=6, MPN="TPS563207"), dk, FakeJlc([]))
    assert "MPN TPS563207: not found at DigiKey" in result.notes
    assert [c.mpn for c in result.candidates] == ["TPS563207DRLR"]
    assert result.conflicts == []


def test_identifier_that_names_nothing_alone_is_the_problem_not_a_conflict():
    result = _find(_req("SOT-563", DigiKeyPN="TPS563207DRLRCT-ND"), FakeDigiKey({}),
                   FakeJlc([]))
    assert result.conflicts == []
    assert result.problem == "no part found for the symbol's identifiers"


# --------------------------------------------------------------------------- #
# Partial MPN
# --------------------------------------------------------------------------- #

def _stm(mpn, **kw):
    return _dk(mpn, mfr="STMicroelectronics", case="48-LQFP", sdp="48-LQFP (7x7)", **kw)


def test_value_token_found_as_partial_mpn():
    # MotorDriver U9 without a DigiKey PN: the Value names STM32L552CCT6.
    hits = [_stm("STM32L552CET6"), _stm("STM32L552CCT6TR"), _stm("STM32L552CCT6"),
            _stm("STM32L562CCT6")]
    dk = FakeDigiKey({"STM32L552CCT6": _stm("STM32L552CCT6", stock=9000),
                      "STM32L552CCT6TR": _stm("STM32L552CCT6TR", stock=2000)},
                     keyword={"STM32L552CCT6": hits})
    req = _req("LQFP-48_7x7mm_P0.5mm", pads=48, Value="MotorControl_STM32L552CCT6_V2")
    result = _find(req, dk, FakeJlc([]))
    assert dk.keyword_calls == ["STM32L552CCT6"]
    # Only MPNs that start with the partial; the exact one first.
    assert dk.product_calls == ["STM32L552CCT6", "STM32L552CCT6TR"]
    assert [c.mpn for c in result.candidates] == ["STM32L552CCT6", "STM32L552CCT6TR"]
    first, second = result.candidates
    assert first.current and first.source == SEARCH
    assert not second.current
    assert first.footprint.result == MATCH
    assert result.conflicts == []


def test_digikey_client_without_keyword_search_notes_partial_not_searched():
    class ProductOnly:
        configured = True

        def __init__(self):
            self.product_calls: List[str] = []

        def product_for(self, pn):
            self.product_calls.append(pn)
            return None

    dk = ProductOnly()
    req = _req("LQFP-48_7x7mm_P0.5mm", pads=48, Value="MotorControl_STM32L552CCT6_V2")
    result = _find(req, dk, FakeJlc([]))
    assert result.candidates == []
    assert "partial MPN not searched: the DigiKey client has no keyword search" in result.notes


def test_partial_mpn_lookups_capped():
    hits = [_stm(f"STM32L552CCT6X{i}") for i in range(MAX_IC_LOOKUPS + 3)]
    dk = FakeDigiKey({}, keyword={"STM32L552CCT6": hits})
    result = _find(_req("LQFP-48_7x7mm_P0.5mm", Value="STM32L552CCT6"), dk, FakeJlc([]))
    assert len(dk.product_calls) == MAX_IC_LOOKUPS
    assert result.dropped_by_limit == 3
    # product_for found nothing: the keyword hit itself is used.
    assert len(result.candidates) == MAX_IC_LOOKUPS


def test_partial_mpn_tries_tokens_until_one_finds_a_part():
    dk = FakeDigiKey({}, keyword={"AB12345": [_dk("AB12345-Q1")]})
    result = _find(_req(Value="XYZ98765 AB12345"), dk, FakeJlc([]))
    assert dk.keyword_calls == ["XYZ98765", "AB12345"]
    assert [c.mpn for c in result.candidates] == ["AB12345-Q1"]
    assert "Value XYZ98765: no DigiKey part starts with XYZ98765" in result.notes


def test_partial_not_run_when_exact_lookup_agrees():
    dk = FakeDigiKey({"X": _dk("AD8232ACPZ-R7")})
    _find(_req(Value="AD8232ACPZ-R7", DigiKeyPN="X"), dk, FakeJlc([]))
    assert dk.keyword_calls == []


def test_mpn_field_not_found_exactly_tried_as_partial():
    dk = FakeDigiKey({}, keyword={"LMC7101BYM5": [_dk("LMC7101BYM5/NOPB")]})
    result = _find(_req(MPN="LMC7101BYM5"), dk, FakeJlc([]))
    assert dk.product_calls[0] == "LMC7101BYM5"
    assert [c.mpn for c in result.candidates] == ["LMC7101BYM5/NOPB"]


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #

def _ltc(mpn, **kw):
    return _dk(mpn, mfr="Analog Devices", case="SOT-23-6 Thin, TSOT-23-6",
               sdp="TSOT-23-6", **kw)


def test_value_and_digikey_pn_naming_different_grades():
    # BuckConverter U1: the Value says C grade, the DigiKey PN says H grade.
    dk = FakeDigiKey(
        {"161-LTC6992HS6-2#TRMPBFCT-ND": _ltc("LTC6992HS6-2#TRMPBF"),
         "LTC6992CS6-2#TRMPBF": _ltc("LTC6992CS6-2#TRMPBF")},
        keyword={"LTC6992CS6-2": [_ltc("LTC6992CS6-2#TRMPBF"), _ltc("LTC6992HS6-2#TRMPBF")]},
    )
    req = _req("Package_TO_SOT_SMD:TSOT-23-6", pads=6, Value="LTC6992CS6-2",
               DigiKeyPN="161-LTC6992HS6-2#TRMPBFCT-ND")
    result = _find(req, dk, FakeJlc([]))
    assert {c.mpn for c in result.candidates} == {"LTC6992HS6-2#TRMPBF", "LTC6992CS6-2#TRMPBF"}
    assert result.conflicts == [
        "DigiKeyPN 161-LTC6992HS6-2#TRMPBFCT-ND names LTC6992HS6-2#TRMPBF; "
        "Value LTC6992CS6-2 names LTC6992CS6-2#TRMPBF"
    ]
    # The part the DigiKey PN names exactly ranks first.
    assert result.candidates[0].mpn == "LTC6992HS6-2#TRMPBF"
    assert all(c.footprint.result == MATCH for c in result.candidates)


def test_mpn_and_digikey_pn_naming_different_parts():
    dk = FakeDigiKey({"X": _dk("TPS1234"), "LM358N": _dk("LM358N")})
    result = _find(_req(DigiKeyPN="X", MPN="LM358N"), dk, FakeJlc([]))
    assert len(result.candidates) == 2
    assert result.conflicts == ["DigiKeyPN X names TPS1234; MPN LM358N names LM358N"]


def test_lcsc_naming_another_part_is_a_conflict():
    dk = FakeDigiKey({"X": _dk("TPS1234"), "LM358N": _dk("LM358N")})
    jlc = FakeJlc([_jlc("C9", "LM358N")])
    result = _find(_req(DigiKeyPN="X", LCSC="C9"), dk, jlc)
    assert result.conflicts == ["DigiKeyPN X names TPS1234; LCSC C9 names LM358N"]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def _accept(db, mpn="DRV8313RHHR", lcsc="C92482", dkpn="DRV8313RHHRCT-ND"):
    part_id = db.accept_part(IcChoice(mpn=mpn, manufacturer="Texas Instruments",
                                      package="36-VQFN (6x6)",
                                      package_case="36-VFQFN Exposed Pad", pins=36,
                                      digikey_pn=dkpn, lcsc=lcsc, jlc_library="extended"))
    db.record_usage(part_id, "MotorDriver/MotorDriver.kicad_sch", ["U1", "U2"])
    return part_id


def test_database_part_merged_with_exact_lookup():
    db = _db()
    part_id = _accept(db)
    dk = FakeDigiKey({"296-X": _dk("DRV8313RHHR")})
    result = _find(_req(pads=37, DigiKeyPN="296-X"), dk, FakeJlc([_jlc("C92482", "DRV8313RHHR")]),
                   db=db)
    cand = result.candidates[0]
    assert (cand.source, cand.db_part_id, cand.usage_count) == (DATABASE, part_id, 2)
    assert cand.lcsc == "C92482"


def test_database_part_found_from_value_is_refreshed():
    db = _db()
    _accept(db)
    dk = FakeDigiKey({"DRV8313RHHRCT-ND": _dk("DRV8313RHHR", stock=7777)})
    jlc = FakeJlc([_jlc("C92482", "DRV8313RHHR", stock=4321)])
    result = _find(_req(pads=37, Value="DRV8313RHHR"), dk, jlc, db=db)
    cand = result.candidates[0]
    assert cand.source == DATABASE
    assert (cand.digikey_stock, cand.jlc_stock) == (7777, 4321)
    assert dk.keyword_calls == []    # the database found it: no partial search
    assert cand.footprint.result == MATCH


def test_database_part_ranks_first():
    # The Value names the recorded DRV8313RHHR; the DigiKeyPN names another
    # part with far more stock. Both are offered, the database part first.
    db = _db()
    _accept(db)
    dk = FakeDigiKey({"X": _dk("TPS1234", stock=90000),
                      "DRV8313RHHRCT-ND": _dk("DRV8313RHHR")})
    result = _find(_req(pads=37, Value="DRV8313RHHR", DigiKeyPN="X"), dk, FakeJlc([]),
                   db=db)
    assert [(c.mpn, c.source) for c in result.candidates] == [
        ("DRV8313RHHR", DATABASE), ("TPS1234", CURRENT)]
    assert all(c.current for c in result.candidates)


def test_database_lcsc_no_longer_listed_is_searched_by_mpn():
    # The recorded C111 now lists another MPN; the JLCPCB search by MPN finds
    # C222, and C111 is neither offered nor written.
    db = _db()
    _accept(db, lcsc="C111")
    dk = FakeDigiKey({"DRV8313RHHRCT-ND": _dk("DRV8313RHHR")})
    jlc = FakeJlc([_jlc("C111", "OTHERPART"), _jlc("C222", "DRV8313RHHR", stock=9000)])
    result = _find(_req(pads=37, Value="DRV8313RHHR"), dk, jlc, db=db)
    (cand,) = result.candidates
    assert (cand.lcsc, cand.at_jlcpcb) == ("C222", True)
    assert jlc.search_calls == ["DRV8313RHHR"]


def test_database_lcsc_no_longer_listed_and_not_found_is_not_at_jlcpcb():
    db = _db()
    _accept(db, lcsc="C111")
    dk = FakeDigiKey({"DRV8313RHHRCT-ND": _dk("DRV8313RHHR")})
    result = _find(_req(pads=37, Value="DRV8313RHHR"), dk, FakeJlc([]), db=db)
    (cand,) = result.candidates
    assert cand.lcsc is None and cand.jlc_text == NOT_AT_JLCPCB


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def _cand(**kw):
    base = dict(mpn="X", manufacturer="M", digikey_stock=5000,
                footprint=FootprintCheck(MATCH))
    base.update(kw)
    return IcCandidate(**base)


def test_rank_order():
    ordered = [
        _cand(mpn="db", db_part_id=1, usage_count=3),
        _cand(mpn="db-less-used", db_part_id=2, usage_count=1),
        _cand(mpn="current-mismatch", current=True, footprint=FootprintCheck(MISMATCH)),
        _cand(mpn="match-jlc-basic", lcsc="C1", jlc_library="basic"),
        _cand(mpn="match-jlc-ext", lcsc="C2", jlc_library="extended"),
        _cand(mpn="match-dk-more", digikey_stock=9000),
        _cand(mpn="match-dk-cheap", digikey_price=1.0),
        _cand(mpn="match-dk-dear", digikey_price=2.0),
        _cand(mpn="unverified", footprint=FootprintCheck(UNVERIFIED)),
        _cand(mpn="mismatch", footprint=FootprintCheck(MISMATCH)),
    ]
    shuffled = list(reversed(ordered))
    assert [c.mpn for c in sorted(shuffled, key=ic_rank_key)] == [c.mpn for c in ordered]


def test_footprint_check_ranks_but_never_removes():
    # The DigiKeyPN names a 24-pin part with more stock (a mismatch on the
    # 37-pad footprint); the Value names the 36-pin part that fits. Both are
    # offered, the match first.
    dk = FakeDigiKey({"X": _dk("TPS1234", case="24-VFQFN Exposed Pad", sdp="24-QFN (4x4)",
                               stock=90000),
                      "DRV8313RHHR": _dk("DRV8313RHHR")},
                     keyword={"DRV8313RHHR": [_dk("DRV8313RHHR")]})
    result = _find(_req(pads=37, DigiKeyPN="X", Value="DRV8313RHHR"), dk, FakeJlc([]))
    assert [(c.mpn, c.footprint.result) for c in result.candidates] == [
        ("DRV8313RHHR", MATCH), ("TPS1234", MISMATCH)]


# --------------------------------------------------------------------------- #
# Group entry point and write plan
# --------------------------------------------------------------------------- #

def _rec(ref, uuid=None, **props):
    props = {"Reference": ref, "dnp": False, "in_bom": True, **props}
    return PartRecord(reference=ref, props=props, source_file=Path("x.kicad_sch"),
                      symbol_uuid=uuid or f"uuid-{ref}", intent=build_intent(props))


class FakePads:
    def __init__(self, counts):
        self.counts = counts
        self.calls = []

    def count(self, reference, lib_id=None):
        self.calls.append((reference, lib_id))
        return self.counts.get(reference)


def test_ic_requirement_for_fills_package_and_pad_count():
    (group,) = group_ics([_rec("U2", Value="DRV8313RHHR", Footprint="TI:RHH0036C"),
                          _rec("U1", Value="DRV8313RHHR", Footprint="TI:RHH0036C")])
    pads = FakePads({"U2": 37})
    req = ic_requirement_for(group, pads)
    assert req.pad_count == 37
    assert req.package == parse_footprint("TI:RHH0036C")
    assert pads.calls == [("U1", "TI:RHH0036C"), ("U2", "TI:RHH0036C")]
    assert ic_requirement_for(group).pad_count is None


def test_board_pads_reads_the_sibling_pcb(tmp_path):
    # An empty standard library under tmp_path, so the lookup below reads
    # nothing outside it (default_standard_dir finds this one first).
    (tmp_path / "Libraries" / "kicad-footprints").mkdir(parents=True)
    pads = board_pads(tmp_path / "Board.kicad_sch")
    assert pads.standard_dir == tmp_path / "Libraries" / "kicad-footprints"
    assert pads.pcb_path == tmp_path / "Board.kicad_pcb"
    assert pads.count("U1", "Nope:Missing") is None
    assert board_pads(None) is None


def test_assess_ic_runs_the_search_with_the_footprint():
    (group,) = group_ics([_rec("U1", Value="DRV8313RHHR", Footprint="TI:RHH0036C",
                               DigiKeyPN="296-X")])
    dk = FakeDigiKey({"296-X": _dk("DRV8313RHHR")})
    jlc = FakeJlc([])
    result = assess_ic(group, db=_db(), digikey=dk, jlc_search=jlc.search,
                       jlc_part=jlc.part, needed=NEEDED, pads=FakePads({"U1": 36}))
    assert result.requirement.pad_count == 36
    assert result.candidates[0].footprint.reasons == ("36 pads vs 36 pins + exposed pad",)
    # An earlier requirement is reused as given.
    again = assess_ic(group, db=_db(), digikey=dk, jlc_search=jlc.search,
                      jlc_part=jlc.part, needed=NEEDED, requirement=result.requirement)
    assert again.requirement is result.requirement


def test_ic_assignment_plan_writes_part_numbers_only():
    recs = [_rec("U1", Value="DRV8313RHHR", Footprint="TI:RHH0036C",
                 **{"Digikey Part Number": "296-X", "LCSC Part": "", "mpn": ""})]
    (group,) = group_ics(recs)
    cand = _cand(mpn="DRV8313RHHR", manufacturer="Texas Instruments",
                 digikey_pn="296-DRV8313RHHRCT-ND", lcsc="C92482")
    plan = ic_assignment_plan(group, cand)
    props = plan[Path("x.kicad_sch")]["uuid-U1"]
    assert props == {
        "Intent:Type": "ic",           # guessed from the U prefix
        "MPN": "DRV8313RHHR",
        "Manufacturer": "Texas Instruments",
        "DigiKeyPN": "296-DRV8313RHHRCT-ND",
        "LCSC": "C92482",
        "Digikey Part Number": None,
        "LCSC Part": None,
        "mpn": None,
    }
    for key in ("Footprint", "Value", "Datasheet", "Reference"):
        assert key not in props


def test_ic_assignment_plan_writes_every_unit_of_a_multi_unit_symbol():
    # MotorDriver U9 is drawn as two units: one reference, two symbols.
    recs = [_rec("U9", Value="STM32L552CCT6", Footprint="LQFP-48_7x7mm_P0.5mm"),
            _rec("U9", Value="STM32L552CCT6", Footprint="LQFP-48_7x7mm_P0.5mm")]
    recs[1].symbol_uuid = "uuid-U9-unit2"
    (group,) = group_ics(recs)
    assert (group.references, group.quantity) == (["U9"], 1)
    cand = _cand(mpn="STM32L552CCT6", manufacturer="STMicroelectronics",
                 digikey_pn="497-STM32L552CCT6-ND")
    plan = ic_assignment_plan(group, cand)
    assert set(plan[Path("x.kicad_sch")]) == {"uuid-U9", "uuid-U9-unit2"}


def test_ic_assignment_plan_not_at_jlcpcb_removes_lcsc():
    recs = [_rec("U1", Value="LDL212PV33R", Footprint="F:DFN", LCSC="C123",
                 **{"Intent:Type": "ic"})]
    (group,) = group_ics(recs)
    cand = _cand(mpn="LDL212PV33R", manufacturer="ST", digikey_pn="497-1-ND")
    props = ic_assignment_plan(group, cand)[Path("x.kicad_sch")]["uuid-U1"]
    assert props["LCSC"] is None
    assert "Intent:Type" not in props    # stated, not guessed


def test_ic_assignment_plan_below_jlcpcb_floor_leaves_lcsc_out():
    recs = [_rec("U1", Value="TPS1234", Footprint="F:DFN", LCSC="C7")]
    (group,) = group_ics(recs)
    dk = FakeDigiKey({"X": _dk("TPS1234")})
    cand = _find(_req(DigiKeyPN="X"), dk, FakeJlc([_jlc("C7", "TPS1234", stock=33)])).candidates[0]
    assert cand.lcsc == "C7" and not cand.at_jlcpcb
    props = ic_assignment_plan(group, cand)[Path("x.kicad_sch")]["uuid-U1"]
    assert props["LCSC"] is None
    choice = cand.choice()
    assert (choice.lcsc, choice.jlc_library) == (None, None)


def test_ic_assignment_plan_not_at_jlcpcb_without_lcsc_field():
    (group,) = group_ics([_rec("U1", Value="LDL212PV33R", Footprint="F:DFN")])
    cand = _cand(mpn="LDL212PV33R", manufacturer="ST", digikey_pn="497-1-ND")
    props = ic_assignment_plan(group, cand)[Path("x.kicad_sch")]["uuid-U1"]
    # None on a symbol without LCSC leaves it unchanged, and replaces an LCSC
    # the LCSC tab resolved for the old part when the window merges the tabs.
    assert props["LCSC"] is None
    lcsc_tab = {"LCSC": "C12345", "MPN": "LDL212PV50R"}
    assert {**lcsc_tab, **props}["LCSC"] is None


def test_ic_assignment_plan_requires_mpn_manufacturer_and_digikey_pn():
    (group,) = group_ics([_rec("U1", Value="X12345", Footprint="F:X")])
    with pytest.raises(ValueError, match="DigiKeyPN"):
        ic_assignment_plan(group, _cand(mpn="X12345", manufacturer="M"))


def test_ic_assignment_plan_skips_members_without_uuid():
    recs = [_rec("U1", Value="X12345", Footprint="F:X")]
    recs[0] = PartRecord(reference="U1", props=recs[0].props, source_file=None,
                         symbol_uuid=None, intent=recs[0].intent)
    (group,) = group_ics(recs)
    plan = ic_assignment_plan(group, _cand(mpn="X12345", manufacturer="M", digikey_pn="D"))
    assert plan == {}


def test_choice_records_ic_fields():
    cand = _cand(mpn="DRV8313RHHR", manufacturer="TI", package="36-VQFN (6x6)",
                 package_case="36-VFQFN Exposed Pad", pins=36,
                 digikey_pn="296-DRV8313RHHRCT-ND", lcsc="C92482", jlc_library="extended")
    choice = cand.choice()
    assert choice == IcChoice(mpn="DRV8313RHHR", manufacturer="TI", package="36-VQFN (6x6)",
                              package_case="36-VFQFN Exposed Pad", pins=36,
                              digikey_pn="296-DRV8313RHHRCT-ND", digikey_packaging="cut tape",
                              lcsc="C92482", jlc_library="extended")
    db = _db()
    db.accept_part(choice)
    (hit,) = db.find_ics("DRV8313RHHR")
    assert (hit.pins, hit.lcsc) == (36, "C92482")
    assert cand.package_info() == parse_digikey_package(
        {"Package / Case": "36-VFQFN Exposed Pad", "Supplier Device Package": "36-VQFN (6x6)"})
