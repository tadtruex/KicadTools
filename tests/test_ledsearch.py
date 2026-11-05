"""Tests for kicadtools.ledsearch: plain-LED search with fake suppliers."""
from __future__ import annotations

from pathlib import Path

from kicadtools.intent import ElectricalIntent
from kicadtools.ledrequirement import group_leds
from kicadtools.ledsearch import (
    JLC_LED_CATEGORY,
    assess_led,
    cached_listing,
    check_led,
    jlc_evidence,
)
from kicadtools.partsearch import CURRENT, DIGIKEY_UNCONFIGURED, SEARCH
from kicadtools.requirement import part_defaults
from kicadtools.resolve import PartRecord
from kicadtools.suppliers import DigiKeyProduct, JlcPart

DEFAULTS = part_defaults({})


def _jlc(lcsc, mpn, *, maker="Lite-On", nm="525nm", color="Green", stock=50_000,
         library="expand", price=0.01, mcd="100mcd", vf="2V", lens="Water Clear"):
    attrs = {"Illumination Color": color, "Wavelength - Dominant": nm,
             "Luminous Intensity": mcd, "Voltage - Forward(Vf)": vf,
             "Lens Color": lens}
    return JlcPart(lcsc=lcsc, mpn=mpn, manufacturer=maker, library=library,
                   stock=stock, unit_price=price, package="0603",
                   attributes={k: v for k, v in attrs.items() if v})


def _dk(mpn, *, nm="525nm", stock=10_000, status="Active"):
    return DigiKeyProduct(mpn=mpn, digikey_pns=[f"{mpn}CT-ND"], product_status=status,
                          quantity_available=stock,
                          parameters={"Package / Case": "0603 (1608 Metric)",
                                      "Wavelength - Dominant": nm, "Color": "Green"})


class FakeDigiKey:
    configured = True

    def __init__(self, products):
        self.products = {p.mpn.upper(): p for p in products}
        self.calls = []

    def product_for(self, pn):
        self.calls.append(pn)
        return self.products.get(pn.upper().replace("CT-ND", ""))


class FakeDb:
    def __init__(self, rejected=()):
        self.rejected = {m.upper() for m in rejected}

    def is_rejected(self, manufacturer, mpn):
        return mpn.upper() in self.rejected


def _group(**props):
    p = {"Reference": "D1", "dnp": False, "in_bom": True, "Intent:Type": "led",
         "Value": "LED_Green",
         "Footprint": "LED_SMD:LED_0603_1608Metric", **props}
    rec = PartRecord("D1", p, Path("b.kicad_sch"), "u1", ElectricalIntent.from_dict(p))
    plain, _ = group_leds([rec], DEFAULTS)
    return plain[0]


def _run(group, listing, dk, db=None, **kw):
    return assess_led(group, DEFAULTS, db=db or FakeDb(), jlc_listing=lambda pkg: listing,
                      jlc_part=lambda code: None, jlc_search=lambda kw: [],
                      digikey=dk, needed=25, **kw)


def test_search_filters_by_band_maker_and_stock_and_confirms_at_digikey():
    listing = [
        _jlc("C1", "LTST-C191GKT", nm="569nm"),             # yellow green, out of band
        _jlc("C2", "LTST-C191KGKT", nm="525nm"),            # in band
        _jlc("C3", "XL-1608UGC", maker="XINGLIGHT"),        # maker not listed
        _jlc("C4", "LTST-LOW", stock=200),                  # JLCPCB stock floor
        _jlc("C5", "LTST-NOWL", nm=""),                     # no wavelength
        _jlc("C6", "LTST-NODK"),                            # not at DigiKey
        _jlc("C7", "LTST-BASIC", library="base"),           # Basic ranks first
    ]
    dk = FakeDigiKey([_dk("LTST-C191KGKT"), _dk("LTST-BASIC")])
    r = _run(_group(), listing, dk)
    assert [c.mpn for c in r.candidates] == ["LTST-BASIC", "LTST-C191KGKT"]
    assert all(c.source == SEARCH and c.kind == "led" for c in r.candidates)
    assert r.candidates[0].digikey_pn == "LTST-BASICCT-ND"
    why = dict(r.rejected)
    assert why["C1 LTST-C191GKT"] == "JLCPCB 569 nm, need green 510–540 nm"
    assert why["C3 XL-1608UGC"] == "manufacturer 'XINGLIGHT' is not in the list"
    assert why["C4 LTST-LOW"] == "JLCPCB stock 200, need at least 1000"
    assert why["C5 LTST-NOWL"] == "JLCPCB lists no wavelength or colour"
    assert why["C6 LTST-NODK"] == "not found at DigiKey"


def test_stated_limits():
    listing = [_jlc("C1", "DIM", mcd="20mcd"), _jlc("C2", "HIGHVF", vf="3.1V"),
               _jlc("C3", "DIFF", lens="Diffused"), _jlc("C4", "GOOD")]
    dk = FakeDigiKey([_dk("GOOD")])
    g = _group(**{"Intensity:Min": "50mcd", "Vf:Max": "2.2V",
                  "Lens:Preferred": "clear"})
    r = _run(g, listing, dk)
    assert [c.mpn for c in r.candidates] == ["GOOD"]
    why = dict(r.rejected)
    assert why["C1 DIM"] == "JLCPCB 20 mcd, need at least 50 mcd"
    assert why["C2 HIGHVF"] == "JLCPCB Vf 3.1 V, need at most 2.2 V"
    assert why["C3 DIFF"] == "JLCPCB lens diffused, need clear"


def test_digikey_wavelength_out_of_band_rejects():
    dk = FakeDigiKey([_dk("LTST-X", nm="565nm")])
    r = _run(_group(), [_jlc("C1", "LTST-X")], dk)
    assert r.candidates == []
    assert dict(r.rejected)["C1 LTST-X"] == "DigiKey 565 nm, need green 510–540 nm"


def test_current_part_first_and_listing_skipped():
    p = {"MPN": "150060GS75000", "LCSC": "C9"}
    product = DigiKeyProduct(mpn="150060GS75000", digikey_pns=["732-4971-1-ND"],
                             product_status="Active", quantity_available=5000,
                             parameters={"Package / Case": "0603 (1608 Metric)",
                                         "Wavelength - Dominant": "525nm",
                                         "Color": "Green"})
    part = _jlc("C9", "150060GS75000", maker="Wurth Elektronik", stock=3000)
    dk = FakeDigiKey([product])
    called = []

    def listing(pkg):
        called.append(pkg)
        return []
    r = assess_led(_group(**p), DEFAULTS, db=FakeDb(), jlc_listing=listing,
                   jlc_part=lambda code: part if code == "C9" else None,
                   jlc_search=lambda kw: [], digikey=dk, needed=25)
    assert [(c.mpn, c.source, c.lcsc) for c in r.candidates] == [
        ("150060GS75000", CURRENT, "C9")]
    assert called == []
    assert r.requirement.searchable


def test_current_part_out_of_band_blocks_the_search():
    p = {"MPN": "LTST-C191GKT"}
    product = _dk("LTST-C191GKT", nm="569nm")
    r = assess_led(_group(**p), DEFAULTS, db=FakeDb(), jlc_listing=lambda pkg: [],
                   jlc_part=lambda c: None, jlc_search=lambda kw: [],
                   digikey=FakeDigiKey([product]), needed=25)
    assert r.candidates == []
    assert r.problem.startswith("colour: green 510–540 nm, but current part")


def test_no_colour_is_not_searched():
    called = []
    r = assess_led(_group(Value="LED"), DEFAULTS, db=FakeDb(),
                   jlc_listing=lambda pkg: called.append(pkg) or [],
                   jlc_part=lambda c: None, jlc_search=lambda kw: [],
                   digikey=FakeDigiKey([]), needed=25)
    assert r.problem == "no colour (Color:Preferred)"
    assert called == []


def test_without_digikey():
    class NoDk:
        configured = False
    r = _run(_group(), [_jlc("C1", "X")], NoDk())
    assert r.problem == DIGIKEY_UNCONFIGURED


def test_rejected_in_database():
    r = _run(_group(), [_jlc("C1", "BAD")], FakeDigiKey([_dk("BAD")]),
             db=FakeDb(rejected=["BAD"]))
    assert dict(r.rejected)["C1 BAD"] == "rejected in the parts database"


def test_cached_listing_fetches_each_package_once():
    calls = []

    def fetch(value, package, category, **kw):
        calls.append((value, package, category))
        return [_jlc("C1", "X")]
    listing = cached_listing(fetch)
    listing("0603")
    listing("0603")
    listing("0805")
    assert calls == [("", "0603", JLC_LED_CATEGORY), ("", "0805", JLC_LED_CATEGORY)]


def test_two_die_part_rejected():
    req = _group().requirement
    ev = jlc_evidence(_jlc("C1", "BI", nm="569nm、621nm", color="Yellow Green, Red"))
    assert check_led(req, ev, "JLCPCB") == "JLCPCB lists 2 dies"
