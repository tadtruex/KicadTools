"""Tests for capacitor candidate search (kicadtools.capsearch)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from kicadtools.capsearch import (
    CURRENT,
    DATABASE,
    MIN_STOCK,
    SEARCH,
    Candidate,
    ExistingLookup,
    assess,
    MAX_LOOKUP_FACTOR,
    find_candidates,
    read_existing,
    rank_key,
    requirement_for,
    facets,
    manufacturer_allowed,
    parse_manufacturers,
)
from kicadtools.evidence import ExistingPart
from kicadtools.intent import ElectricalIntent
from kicadtools.partsdb import CapacitorChoice, CapacitorParams, PartsDb
from kicadtools.requirement import (
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    SCHEMATIC,
    UNSET,
    CapacitorRequirement,
    Sourced,
    capacitor_defaults,
    group_capacitors,
)
from kicadtools.resolve import PartRecord
from kicadtools.suppliers import DigiKeyProduct, JlcPart

DEFAULTS = capacitor_defaults({})

INF = float("inf")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _req(*, cap=100e-9, package="0603", volts=16.0, diel=("X7R",),
         tol=10.0, value="100nF", footprint="",
         tol_source=SCHEMATIC) -> CapacitorRequirement:
    # Tolerance defaults to stated on the schematic, so the checks below see a
    # single pass; the relaxation tests at the end pass tol_source=DEFAULT.
    return CapacitorRequirement(
        value_text=value,
        capacitance_f=cap,
        package=Sourced(package, SCHEMATIC if footprint else DEFAULT),
        voltage_min_v=Sourced(volts, SCHEMATIC if volts is not None else UNSET),
        dielectrics=Sourced(tuple(diel), DEFAULT),
        tolerance_max_pct=Sourced(tol, tol_source if tol is not None else UNSET),
        footprint=footprint,
    )


def _jlc(lcsc="C14663", mpn="CL10B104KB8NNNC", *, mfr="SAMSUNG",
         cap="100nF", volts="50V", diel="X7R", tol="±10%", package="0603",
         library="base", preferred=False, stock=100000, price=0.002,
         no_buy="", drop=()) -> JlcPart:
    attrs = {"Capacitance": cap, "Voltage Rating": volts,
             "Temperature Coefficient": diel, "Tolerance": tol}
    for k in drop:
        attrs.pop(k)
    return JlcPart(lcsc=lcsc, mpn=mpn, manufacturer=mfr, library=library,
                   preferred=preferred, stock=stock, unit_price=price,
                   package=package, description=f"{cap} {volts} {diel}",
                   no_buy_reason=no_buy, attributes=attrs)


def _dk(mpn="CL10B104KB8NNNC", *, cap="0.1 µF", volts="50V", diel="X7R",
        tol="±10%", package="0603 (1608 Metric)", status="Active",
        pns=("1276-1000-2-ND", "1276-1000-1-ND"), qty=500000,
        drop=()) -> DigiKeyProduct:
    params = {"Capacitance": cap, "Voltage - Rated": volts,
              "Temperature Coefficient": diel, "Tolerance": tol,
              "Package / Case": package}
    for k in drop:
        params.pop(k)
    return DigiKeyProduct(mpn=mpn, digikey_pns=list(pns),
                          datasheet_url="https://dk/ds.pdf",
                          quantity_available=qty, product_status=status,
                          description="CAP CER 0.1UF 50V X7R 0603",
                          parameters=params)


class FakeJlc:
    def __init__(self, by_keyword: Optional[Dict[str, List[JlcPart]]] = None,
                 default: Optional[List[JlcPart]] = None,
                 parts: Optional[Dict[str, JlcPart]] = None):
        self.by_keyword = by_keyword or {}
        self.default = default or []
        self.parts = parts or {}
        self.searches: List[str] = []
        self.lookups: List[str] = []

    def search(self, keyword: str) -> List[JlcPart]:
        self.searches.append(keyword)
        return list(self.by_keyword.get(keyword, self.default))

    def part(self, lcsc: str) -> Optional[JlcPart]:
        self.lookups.append(lcsc)
        return self.parts.get(lcsc)

    def mlcc(self, value: str, package: str) -> List[JlcPart]:
        """The filtered MLCC search: *default*, plus every part listed under a
        keyword that starts with the value and package, once per LCSC code."""
        key = f"{value} {package}"
        self.searches.append(key)
        out: List[JlcPart] = []
        seen = set()
        lists = [self.default] + [v for k, v in self.by_keyword.items()
                                  if k == key or k.startswith(key + " ")]
        for hits in lists:
            for h in hits:
                if h.lcsc not in seen:
                    seen.add(h.lcsc)
                    out.append(h)
        return out


class FakeDigiKey:
    def __init__(self, products: Optional[Dict[str, DigiKeyProduct]] = None,
                 configured: bool = True):
        self.products = products or {}
        self.configured = configured
        self.calls: List[str] = []

    def product_for(self, pn: str) -> Optional[DigiKeyProduct]:
        self.calls.append(pn)
        return self.products.get(pn)


def _db() -> PartsDb:
    return PartsDb(":memory:", now=lambda: "2026-10-29T12:00:00+00:00")


def _choice(mpn="GRM188R71H104KA93D", *, mfr="Murata", dk="490-1519-1-ND",
            lcsc="C77047", lib="extended", v=50.0, diel="X7R", tol=10.0):
    return CapacitorChoice(
        mpn=mpn, manufacturer=mfr, package="0603",
        params=CapacitorParams(100e-9, v, diel, tol),
        digikey_pn=dk, digikey_packaging="cut tape", lcsc=lcsc, jlc_library=lib,
    )


def _live_grm(**kw) -> JlcPart:
    """The JLCPCB entry of the database part from :func:`_choice`."""
    kw.setdefault("library", "expand")
    return _jlc("C77047", "GRM188R71H104KA93D", **kw)


def _run(req=None, *, db=None, jlc=None, dk=None, needed=10, **kw):
    jlc = jlc or FakeJlc()
    dk = dk if dk is not None else FakeDigiKey()
    return find_candidates(
        req or _req(), db=db or _db(), jlc_mlcc=jlc.mlcc,
        jlc_part=jlc.part, digikey=dk, needed=needed, **kw,
    )


def _reasons(result) -> Dict[str, str]:
    return dict(result.rejected)


# --------------------------------------------------------------------------- #
# Keywords and problems
# --------------------------------------------------------------------------- #

def test_one_filtered_search_by_value_and_package():
    jlc = FakeJlc()
    _run(_req(diel=("X7R", "X5R")), jlc=jlc)
    assert jlc.searches == ["100nF 0603"]
    jlc = FakeJlc()
    _run(_req(cap=22e-12, diel=("C0G", "NP0")), jlc=jlc)
    assert jlc.searches == ["22pF 0603"]


def test_manufacturer_matching():
    allowed = parse_manufacturers("Murata, Samsung , ,Holy Stone, Wurth")
    assert allowed == ("Murata", "Samsung", "Holy Stone", "Wurth")
    assert manufacturer_allowed("Samsung Electro-Mechanics", allowed)
    assert manufacturer_allowed("SAMSUNG", allowed)
    assert manufacturer_allowed("IHHEC(HOLY STONE ENTERPRISE CO., LTD)", allowed)
    assert manufacturer_allowed("Würth Elektronik", allowed)
    assert not manufacturer_allowed("CCTC", allowed)
    assert manufacturer_allowed("CCTC", ())


def test_manufacturer_not_listed_is_rejected():
    jlc = FakeJlc(default=[_jlc("C1", "TCC0603X7R104K500CT", mfr="CCTC")])
    res = _run(_req(), jlc=jlc, manufacturers=("Murata", "Samsung"))
    assert _reasons(res)["C1 TCC0603X7R104K500CT"] == "manufacturer 'CCTC' is not in the list"
    assert res.pool_size == 0


def test_facets_count_each_field_against_the_others():
    pool = [
        _jlc("C1", "A", diel="X5R", volts="16V"),
        _jlc("C2", "B", diel="X5R", volts="25V"),
        _jlc("C3", "C", diel="X7R", volts="16V", tol="±20%"),
        _jlc("C4", "D", diel="X7R", volts="6.3V"),
    ]
    f = facets(_req(volts=16.0, diel=("X7R",), tol=10.0), pool)
    # dielectric counted against voltage >= 16 V and tolerance <= 10 %
    assert f["dielectric"] == [("X5R", 2)]
    # voltage counted against X7R and <= 10 %: only D (6.3 V)
    assert f["voltage"] == [("6.3V", 1)]
    # tolerance counted against X7R and >= 16 V: only C (20 %)
    assert f["tolerance"] == [("20%", 1)]


def test_cleared_fields_are_not_checked_and_facets_are_returned():
    jlc = FakeJlc(default=[_jlc("C1", "A", diel="X5R", volts="6.3V"),
                           _jlc("C2", "B", diel="X5R", volts="16V")])
    dk = FakeDigiKey({"A": _dk("A", diel="X5R", volts="6.3V"),
                      "B": _dk("B", diel="X5R", volts="16V")})
    req = _req(volts=16.0, diel=("X7R",)).edited(clear=("dielectrics",))
    res = _run(req, jlc=jlc, dk=dk)
    assert [c.mpn for c in res.candidates] == ["B"]
    assert res.facets["dielectric"] == [("X5R", 1)]
    assert res.pool_size == 2
    # the chosen part's own dielectric is written
    out = requirement_for(req, res, res.candidates[0])
    assert out.dielectrics == Sourced(("X5R",), RELAXED)
    # a cleared voltage: any rating passes
    res = _run(_req(volts=16.0, diel=("X5R",)).edited(clear=("voltage_min_v",)),
               jlc=jlc, dk=dk)
    assert sorted(c.mpn for c in res.candidates) == ["A", "B"]


def test_not_searchable_returns_problem_without_calls():
    jlc, dk = FakeJlc(default=[_jlc()]), FakeDigiKey()
    res = _run(_req(volts=None), jlc=jlc, dk=dk)
    assert res.candidates == []
    assert res.problem == "no minimum voltage (Voltage:Min)"
    assert jlc.searches == [] and dk.calls == []


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def test_database_hit_returned_without_supplier_search():
    db = _db()
    pid = db.accept_capacitor(_choice())
    db.record_usage(pid, "ECG/ECG.kicad_sch", ["C1", "C2"])
    live = _jlc("C77047", "GRM188R71H104KA93D", library="expand",
                preferred=True, stock=4000, price=0.004)
    jlc = FakeJlc(default=[_jlc()], parts={"C77047": live})
    dk = FakeDigiKey({"490-1519-1-ND": _dk("GRM188R71H104KA93D", qty=1234)})
    res = _run(db=db, jlc=jlc, dk=dk)
    assert jlc.searches == []
    assert jlc.lookups == ["C77047"]
    assert dk.calls == ["490-1519-1-ND"]
    [c] = res.candidates
    assert c.source == DATABASE
    assert c.db_part_id == pid and c.usage_count == 2
    assert (c.jlc_stock, c.jlc_price, c.jlc_preferred) == (4000, 0.004, True)
    assert c.jlc_library == "extended"
    assert c.digikey_stock == 1234
    assert res.problem == ""


def test_database_hit_without_live_data_fails_stock_floor():
    # Unknown stock does not meet the stock floor; with no candidate left the
    # supplier search is attempted, which needs DigiKey.
    db = _db()
    db.accept_capacitor(_choice(lib="basic"))
    res = _run(db=db, dk=FakeDigiKey(configured=False))
    assert res.candidates == []
    assert _reasons(res) == {"C77047 GRM188R71H104KA93D": "JLCPCB stock unknown"}
    assert "DigiKey credentials are not set" in res.problem


def test_search_suppliers_merges_and_dedupes_by_mpn():
    db = _db()
    db.accept_capacitor(_choice())
    jlc = FakeJlc(default=[
        _jlc("C77047", "GRM188R71H104KA93D", library="expand"),   # in the database
        _jlc("C14663", "CL10B104KB8NNNC"),
        _jlc("C99999", "cl10b104kb8nnnc", library="expand"),      # same MPN, other code
    ], parts={"C77047": _live_grm()})
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(),
                      "490-1519-1-ND": _dk("GRM188R71H104KA93D")})
    res = _run(db=db, jlc=jlc, dk=dk, search_suppliers=True)
    assert [(c.mpn, c.source) for c in res.candidates] == [
        ("CL10B104KB8NNNC", SEARCH), ("GRM188R71H104KA93D", DATABASE)]
    assert res.candidates[0].lcsc == "C14663"       # the Basic code wins
    assert res.examined == 3
    assert res.rejected == []
    # One DigiKey refresh for the database part, one confirm for the search hit.
    assert dk.calls == ["490-1519-1-ND", "CL10B104KB8NNNC"]


def test_database_miss_runs_search():
    jlc = FakeJlc(default=[_jlc()])
    res = _run(jlc=jlc, dk=FakeDigiKey({"CL10B104KB8NNNC": _dk()}))
    [c] = res.candidates
    assert c.source == SEARCH
    assert c.mpn == "CL10B104KB8NNNC" and c.manufacturer == "SAMSUNG"
    assert c.lcsc == "C14663" and c.jlc_library == "basic"
    assert c.digikey_pn == "1276-1000-1-ND"          # cut tape
    assert c.digikey_stock == 500000
    assert c.params.capacitance_f == pytest.approx(100e-9)
    assert (c.params.voltage_v, c.params.dielectric, c.params.tolerance_pct) == (
        50.0, "X7R", 10.0)
    assert c.datasheet_url == "https://dk/ds.pdf"


def test_digikey_unconfigured_sets_problem_database_stock_unknown():
    # Without DigiKey the database part's DigiKey stock is unknown, so it does
    # not meet the stock floor.
    db = _db()
    db.accept_capacitor(_choice())
    jlc = FakeJlc(default=[_jlc()], parts={"C77047": _live_grm()})
    res = _run(db=db, jlc=jlc, dk=FakeDigiKey(configured=False),
               search_suppliers=True)
    assert res.candidates == []
    assert _reasons(res) == {"C77047 GRM188R71H104KA93D": "DigiKey stock unknown"}
    assert "DigiKey credentials are not set" in res.problem
    assert jlc.searches == []


def test_digikey_unconfigured_empty_database():
    res = _run(jlc=FakeJlc(default=[_jlc()]), dk=FakeDigiKey(configured=False))
    assert res.candidates == []
    assert "DigiKey credentials are not set" in res.problem


def test_database_rejected_mpn_dropped():
    db = _db()
    db.reject_capacitor(_choice("CL10B104KB8NNNC", mfr="SAMSUNG"))
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(db=db, jlc=FakeJlc(default=[_jlc()]), dk=dk)
    assert res.candidates == []
    assert _reasons(res) == {"C14663 CL10B104KB8NNNC": "rejected in the parts database"}
    assert dk.calls == []


# --------------------------------------------------------------------------- #
# JLCPCB attribute filter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("part,reason", [
    (_jlc(cap="10nF"), "JLCPCB capacitance 10nF, need 100nF"),
    (_jlc(volts="10V"), "JLCPCB rating 10V, need at least 16V"),
    (_jlc(diel="X5R"), "JLCPCB dielectric X5R is not one of X7R"),
    (_jlc(tol="±20%"), "JLCPCB tolerance 20%, need at most 10%"),
    (_jlc(package="0805"), "JLCPCB package '0805', need 0603"),
    (_jlc(drop=("Capacitance",)), "JLCPCB lists no Capacitance"),
    (_jlc(drop=("Voltage Rating",)), "JLCPCB lists no Voltage Rating"),
    (_jlc(drop=("Temperature Coefficient",)),
     "JLCPCB lists no Temperature Coefficient"),
    (_jlc(drop=("Tolerance",)), "JLCPCB lists no Tolerance"),
    (_jlc(tol="±10 ohm"), "JLCPCB Tolerance '±10 ohm' does not parse"),
    (_jlc(stock=0), "not orderable at JLCPCB (out of stock)"),
    (_jlc(no_buy="This product is no longer manufactured."),
     "not orderable at JLCPCB (This product is no longer manufactured.)"),
])
def test_jlc_attribute_failure_rejected(part, reason):
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(jlc=FakeJlc(default=[part]), dk=dk)
    assert res.candidates == []
    assert res.examined == 1
    assert _reasons(res) == {"C14663 CL10B104KB8NNNC": reason}
    assert dk.calls == []


def test_jlc_passes_higher_rating_tighter_tolerance_and_listed_dielectric():
    part = _jlc(volts="100V", tol="±5%", diel="X7R")
    res = _run(_req(diel=("X5R", "X7R")), jlc=FakeJlc(default=[part]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk(volts="100V", tol="±5%")}))
    [c] = res.candidates
    assert (c.params.voltage_v, c.params.dielectric, c.params.tolerance_pct) == (
        100.0, "X7R", 5.0)


def test_unconstrained_tolerance_admits_missing_attribute():
    part = _jlc(drop=("Tolerance",))
    res = _run(_req(tol=None), jlc=FakeJlc(default=[part]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk(drop=("Tolerance",))}))
    [c] = res.candidates
    assert c.params.tolerance_pct is None


def test_two_code_dielectric_matches_either():
    part = _jlc(cap="22pF", diel="C0G/NP0", tol="±5%")
    res = _run(_req(cap=22e-12, diel=("NP0",), tol=5.0, value="22pF"),
               jlc=FakeJlc(default=[part]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk(cap="22 pF", diel="C0G, NP0",
                                                      tol="±5%")}))
    [c] = res.candidates
    assert c.params.dielectric == "C0G, NP0"


# --------------------------------------------------------------------------- #
# DigiKey confirmation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("product,reason", [
    (None, "not found at DigiKey"),
    (_dk("CL10B104KB8NNNE"), "DigiKey returned MPN 'CL10B104KB8NNNE'"),
    (_dk(status="Obsolete"), "DigiKey status 'Obsolete'"),
    (_dk(status=None), "DigiKey status 'unknown'"),
    (_dk(cap="1 µF"), "DigiKey capacitance 1uF, need 100nF"),
    (_dk(volts="10V"), "DigiKey rating 10V, need at least 16V"),
    (_dk(diel="X5R"), "DigiKey dielectric X5R is not one of X7R"),
    (_dk(tol="±20%"), "DigiKey tolerance 20%, need at most 10%"),
    (_dk(package="0805 (2012 Metric)"),
     "DigiKey package '0805 (2012 Metric)', need 0603"),
    (_dk(drop=("Package / Case",)), "DigiKey lists no Package / Case"),
    (_dk(drop=("Voltage - Rated",)), "DigiKey lists no Voltage - Rated"),
    (_dk(pns=()), "DigiKey lists no part number"),
])
def test_digikey_disagreement_drops_candidate(product, reason):
    dk = FakeDigiKey({"CL10B104KB8NNNC": product} if product else {})
    res = _run(jlc=FakeJlc(default=[_jlc()]), dk=dk)
    assert res.candidates == []
    assert _reasons(res) == {"C14663 CL10B104KB8NNNC": reason}
    assert dk.calls == ["CL10B104KB8NNNC"]


def test_digikey_mpn_match_ignores_punctuation_and_case():
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk("cl10b104kb8-nnnc")})
    res = _run(jlc=FakeJlc(default=[_jlc()]), dk=dk)
    assert len(res.candidates) == 1


def test_digikey_calls_never_exceed_limit():
    parts = [_jlc(f"C{1000 + i}", f"MPN{i}", stock=1000 + i) for i in range(8)]
    dk = FakeDigiKey({f"MPN{i}": _dk(f"MPN{i}") for i in range(8)})
    res = _run(jlc=FakeJlc(default=parts), dk=dk, limit=3)
    assert len(dk.calls) == 3
    assert len(res.candidates) == 3
    assert res.dropped_by_limit == 5
    assert res.examined == 8


def test_limit_keeps_best_ranked_hits():
    parts = [
        _jlc("C1", "EXT1", library="expand", price=0.001),
        _jlc("C2", "BASIC1", library="base", price=0.01),
        _jlc("C3", "EXT2", library="expand", preferred=True, price=0.01),
    ]
    dk = FakeDigiKey({m: _dk(m) for m in ("EXT1", "BASIC1", "EXT2")})
    res = _run(jlc=FakeJlc(default=parts), dk=dk, limit=2)
    assert dk.calls == ["BASIC1", "EXT2"]
    assert res.dropped_by_limit == 1


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def _cand(mpn, **kw) -> Candidate:
    base = dict(mpn=mpn, manufacturer="M", package="0603",
                params=CapacitorParams(100e-9, 50.0, "X7R", 10.0),
                lcsc=None, jlc_library="extended", jlc_preferred=False,
                jlc_stock=0, jlc_price=1.0, digikey_pn=None, digikey_stock=0,
                source=SEARCH)
    base.update(kw)
    return Candidate(**base)


def test_rank_key_order():
    needed = 100
    cands = [
        _cand("price_hi", jlc_price=2.0),
        _cand("price_lo", jlc_price=0.5),
        _cand("dk_stock", digikey_stock=100),
        _cand("jlc_stock", jlc_stock=100),
        _cand("preferred", jlc_preferred=True),
        _cand("basic", jlc_library="basic"),
        _cand("used", usage_count=3, jlc_price=9.0),
        _cand("unknown_stock", jlc_stock=None, digikey_stock=None, jlc_price=INF),
    ]
    order = [c.mpn for c in sorted(cands, key=lambda c: rank_key(c, needed))]
    assert order == ["used", "basic", "preferred", "jlc_stock", "dk_stock",
                     "price_lo", "price_hi", "unknown_stock"]


def test_final_list_ranked():
    db = _db()
    db.accept_capacitor(_choice(lib="extended"))
    parts = [_jlc("C2", "EXT", library="expand"), _jlc("C1", "BASIC", library="base")]
    dk = FakeDigiKey({"EXT": _dk("EXT"), "BASIC": _dk("BASIC"),
                      "490-1519-1-ND": _dk("GRM188R71H104KA93D")})
    jlc = FakeJlc(default=parts, parts={"C77047": _live_grm(price=0.004)})
    res = _run(db=db, jlc=jlc, dk=dk, search_suppliers=True)
    # An unused database part is ranked on the same terms as the search hits:
    # it is Extended and dearer than EXT, so EXT ranks above it.
    assert [c.mpn for c in res.candidates] == ["BASIC", "EXT", "GRM188R71H104KA93D"]


def test_database_usage_ranks_first():
    db = _db()
    pid = db.accept_capacitor(_choice(lib="extended"))
    db.record_usage(pid, "b", ["C1"])
    parts = [_jlc("C1", "BASIC", library="base")]
    dk = FakeDigiKey({"BASIC": _dk("BASIC"),
                      "490-1519-1-ND": _dk("GRM188R71H104KA93D")})
    jlc = FakeJlc(default=parts, parts={"C77047": _live_grm()})
    res = _run(db=db, jlc=jlc, dk=dk, search_suppliers=True)
    assert [c.mpn for c in res.candidates] == ["GRM188R71H104KA93D", "BASIC"]


# --------------------------------------------------------------------------- #
# Candidate.choice
# --------------------------------------------------------------------------- #

def test_choice_round_trips_into_database():
    res = _run(jlc=FakeJlc(default=[_jlc()]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk()}))
    [c] = res.candidates
    choice = c.choice()
    assert choice.digikey_packaging == "cut tape"
    assert choice.jlc_library == "basic"
    db = _db()
    pid = db.accept_capacitor(choice)
    [hit] = db.find_capacitors(capacitance_f=100e-9, package="0603",
                               voltage_min_v=16.0, dielectrics=["X7R"],
                               tolerance_max_pct=10.0)
    assert hit.part_id == pid
    assert (hit.mpn, hit.manufacturer) == ("CL10B104KB8NNNC", "SAMSUNG")
    assert (hit.digikey_pn, hit.lcsc, hit.jlc_library) == (
        "1276-1000-1-ND", "C14663", "basic")
    assert hit.params == c.params
    # A second search now finds it in the database.
    res2 = _run(db=db, jlc=FakeJlc(default=[_jlc()], parts={"C14663": _jlc()}),
                dk=FakeDigiKey({"1276-1000-1-ND": _dk()}))
    assert [x.source for x in res2.candidates] == [DATABASE]


def test_choice_packaging_none_for_reel_pn():
    c = _cand("X", digikey_pn="1276-1000-2-ND")
    assert c.choice().digikey_packaging is None
    c = _cand("X", digikey_pn="399-1281-CT-ND")
    assert c.choice().digikey_packaging == "cut tape"
    assert replace(c, digikey_pn=None).choice().digikey_packaging is None


# --------------------------------------------------------------------------- #
# Review additions
# --------------------------------------------------------------------------- #

def test_absolute_pf_tolerance_converted_to_percent():
    # ±0.25 pF on 10 pF is 2.5 %, inside a 5 % limit.
    part = _jlc(cap="10pF", diel="C0G", tol="±0.25pF")
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(cap="10 pF", diel="C0G, NP0",
                                              tol="±0.25pF")})
    res = _run(_req(cap=10e-12, diel=("C0G", "NP0"), tol=5.0, value="10pF"),
               jlc=FakeJlc(default=[part]), dk=dk)
    [c] = res.candidates
    assert c.params.tolerance_pct == pytest.approx(2.5)


def test_absolute_pf_tolerance_too_loose_rejected():
    # ±0.5 pF on 4.7 pF is 10.6 %, outside a 5 % limit.
    part = _jlc(cap="4.7pF", diel="C0G", tol="±0.5pF")
    res = _run(_req(cap=4.7e-12, diel=("C0G",), tol=5.0, value="4.7pF"),
               jlc=FakeJlc(default=[part]), dk=FakeDigiKey())
    assert _reasons(res) == {
        "C14663 CL10B104KB8NNNC": "JLCPCB tolerance 10.6383%, need at most 5%"}


def test_combined_dielectric_entry_in_requirement_is_split():
    # A requirement entry written as one string still matches each code.
    req = _req(diel=("C0G, NP0",), cap=22e-12, tol=5.0, value="22pF")
    part = _jlc(cap="22pF", diel="NP0", tol="±5%")
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(cap="22 pF", diel="NP0", tol="±5%")})
    res = _run(req, jlc=FakeJlc(default=[part]), dk=dk)
    assert len(res.candidates) == 1


def test_greek_mu_and_micro_sign_capacitance_agree():
    part = _jlc(cap="4.7µF", tol="±10%")
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(cap="4.7 μF")})
    res = _run(_req(cap=4.7e-6, value="4.7uF"), jlc=FakeJlc(default=[part]), dk=dk)
    assert len(res.candidates) == 1


def test_same_lcsc_listed_twice_examined_once():
    jlc = FakeJlc(default=[_jlc()], by_keyword={"100nF 0603 X7R": [_jlc()]})
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(_req(diel=("X7R", "X5R")), jlc=jlc, dk=dk)
    assert len(jlc.searches) == 1
    assert res.examined == 1
    assert dk.calls == ["CL10B104KB8NNNC"]
    assert len(res.candidates) == 1


def test_limit_zero_makes_no_digikey_calls():
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(jlc=FakeJlc(default=[_jlc()]), dk=dk, limit=0)
    assert dk.calls == [] and res.candidates == []
    assert res.dropped_by_limit == 1


def test_jlc_mpn_whitespace_stripped():
    part = _jlc(mpn="  CL10B104KB8NNNC ", mfr=" SAMSUNG ")
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(jlc=FakeJlc(default=[part]), dk=dk)
    [c] = res.candidates
    assert (c.mpn, c.manufacturer) == ("CL10B104KB8NNNC", "SAMSUNG")


def test_search_without_mpn_rejected():
    res = _run(jlc=FakeJlc(default=[_jlc(mpn="  ")]), dk=FakeDigiKey())
    assert _reasons(res) == {"C14663": "JLCPCB lists no MPN"}


def test_database_hit_not_orderable_rejected_with_zero_stock():
    db = _db()
    db.accept_capacitor(_choice())
    gone = _jlc("C77047", "GRM188R71H104KA93D", no_buy="discontinued")
    res = _run(db=db, jlc=FakeJlc(parts={"C77047": gone}),
               dk=FakeDigiKey({"490-1519-1-ND": _dk("GRM188R71H104KA93D")}))
    assert res.candidates == []
    assert _reasons(res)["C77047 GRM188R71H104KA93D"] == (
        "JLCPCB stock 0, need at least 1000")


# --------------------------------------------------------------------------- #
# Dropping a default tolerance limit when nothing meets it
# --------------------------------------------------------------------------- #

def _loose():
    return (FakeJlc(default=[_jlc(tol="±20%")]),
            FakeDigiKey({"CL10B104KB8NNNC": _dk(tol="±20%")}))


def test_default_tolerance_dropped_when_nothing_meets_it():
    jlc, dk = _loose()
    res = _run(_req(tol=10.0, tol_source=DEFAULT), jlc=jlc, dk=dk)
    [c] = res.candidates
    assert c.params.tolerance_pct == 20.0
    assert res.relaxed_tolerance_pct == 10.0
    assert "10%" in res.relaxed_note
    # The second pass reuses the first pass's supplier answers.
    assert jlc.searches == ["100nF 0603"]
    assert dk.calls == ["CL10B104KB8NNNC"]


@pytest.mark.parametrize("source", [SCHEMATIC, EDITED])
def test_stated_or_edited_tolerance_is_never_dropped(source):
    jlc, dk = _loose()
    res = _run(_req(tol=10.0, tol_source=source), jlc=jlc, dk=dk)
    assert res.candidates == []
    assert res.relaxed_tolerance_pct is None
    assert res.relaxed_note == ""


def test_default_tolerance_kept_when_a_part_meets_it():
    res = _run(_req(tol=10.0, tol_source=DEFAULT),
               jlc=FakeJlc(default=[_jlc()]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk()}))
    assert len(res.candidates) == 1
    assert res.relaxed_tolerance_pct is None


def test_requirement_for_writes_the_parts_own_tolerance_when_relaxed():
    jlc, dk = _loose()
    req = _req(tol=10.0, tol_source=DEFAULT)
    res = _run(req, jlc=jlc, dk=dk)
    out = requirement_for(req, res, res.candidates[0])
    assert out.tolerance_max_pct == Sourced(20.0, RELAXED)
    assert out.voltage_min_v == req.voltage_min_v


def test_requirement_for_is_unchanged_without_relaxation():
    res = _run(_req(tol=10.0, tol_source=DEFAULT),
               jlc=FakeJlc(default=[_jlc()]),
               dk=FakeDigiKey({"CL10B104KB8NNNC": _dk()}))
    req = _req(tol=10.0, tol_source=DEFAULT)
    assert requirement_for(req, res, res.candidates[0]) is req


# --------------------------------------------------------------------------- #
# Revision 2: stock floor
# --------------------------------------------------------------------------- #

def test_min_stock_value():
    assert MIN_STOCK == 1000


def test_search_hit_low_jlc_stock_rejected_before_digikey():
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(jlc=FakeJlc(default=[_jlc(stock=33)]), dk=dk)
    assert res.candidates == []
    assert _reasons(res) == {
        "C14663 CL10B104KB8NNNC": "JLCPCB stock 33, need at least 1000"}
    assert dk.calls == []


@pytest.mark.parametrize("qty,reason", [
    (999, "DigiKey stock 999, need at least 1000"),
    (None, "DigiKey stock unknown"),
])
def test_search_hit_low_or_unknown_digikey_stock_rejected(qty, reason):
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(qty=qty)})
    res = _run(jlc=FakeJlc(default=[_jlc()]), dk=dk)
    assert res.candidates == []
    assert _reasons(res) == {"C14663 CL10B104KB8NNNC": reason}


def test_stock_floor_is_build_quantity_when_larger():
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(qty=500000)})
    res = _run(jlc=FakeJlc(default=[_jlc(stock=4000)]), dk=dk, needed=5000)
    assert _reasons(res) == {
        "C14663 CL10B104KB8NNNC": "JLCPCB stock 4000, need at least 5000"}


def test_stock_floor_passes_at_exactly_min_stock():
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk(qty=MIN_STOCK)})
    res = _run(jlc=FakeJlc(default=[_jlc(stock=MIN_STOCK)]), dk=dk)
    assert len(res.candidates) == 1


def test_database_hit_low_digikey_stock_rejected_and_search_runs():
    db = _db()
    db.accept_capacitor(_choice())
    jlc = FakeJlc(default=[_jlc()], parts={"C77047": _live_grm()})
    dk = FakeDigiKey({"490-1519-1-ND": _dk("GRM188R71H104KA93D", qty=12),
                      "CL10B104KB8NNNC": _dk()})
    res = _run(db=db, jlc=jlc, dk=dk)
    assert [(c.mpn, c.source) for c in res.candidates] == [("CL10B104KB8NNNC", SEARCH)]
    assert _reasons(res) == {
        "C77047 GRM188R71H104KA93D": "DigiKey stock 12, need at least 1000"}
    assert jlc.searches == ["100nF 0603"]


# --------------------------------------------------------------------------- #
# Revision 2: the existing part
# --------------------------------------------------------------------------- #

TDK = "C1608X6S1C475K080AC"
TDK_DK = "445-9118-1-ND"
TDK_LCSC = "C2905434"
FP_0603 = "Capacitor_SMD:C_0603_1608Metric"


def _tdk_dk(**kw) -> DigiKeyProduct:
    kw.setdefault("cap", "4.7 µF")
    kw.setdefault("volts", "16V")
    kw.setdefault("diel", "X6S")
    kw.setdefault("pns", ("445-9118-2-ND", TDK_DK, "445-9118-6-ND"))
    kw.setdefault("qty", 20000)
    return _dk(TDK, **kw)


def _tdk_jlc(lcsc=TDK_LCSC, **kw) -> JlcPart:
    kw.setdefault("cap", "4.7uF")
    kw.setdefault("volts", "16V")
    kw.setdefault("diel", "X6S")
    kw.setdefault("mfr", "TDK")
    kw.setdefault("library", "expand")
    kw.setdefault("stock", 50000)
    return _jlc(lcsc, TDK, **kw)


def _record(ref="C1", **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True,
            "Intent:Type": "capacitor", "Value": "C_4p7U",
            "Footprint": FP_0603, "Description": "4.7uF, 16V, X6S 603", **props}
    return PartRecord(ref, data, Path("x.kicad_sch"), f"uuid-{ref}",
                      ElectricalIntent.from_dict(data))


def _group(**props):
    [g] = group_capacitors([_record(**props)], DEFAULTS)
    return g


def _assess(group, *, db=None, jlc=None, dk=None, needed=10, **kw):
    jlc = jlc or FakeJlc()
    dk = dk if dk is not None else FakeDigiKey()
    return assess(group, DEFAULTS, db=db or _db(), jlc_search=jlc.search, jlc_mlcc=jlc.mlcc,
                  jlc_part=jlc.part, digikey=dk, needed=needed, **kw)


def _read(existing, jlc, dk):
    return read_existing(existing, jlc_part=jlc.part, jlc_search=jlc.search,
                         digikey=dk)


def _tdk_suppliers(**jlc_kw):
    jlc = FakeJlc(by_keyword={TDK: [_tdk_jlc(**jlc_kw)]},
                  parts={TDK_LCSC: _tdk_jlc(**jlc_kw)})
    product = _tdk_dk()
    dk = FakeDigiKey({TDK: product, TDK_DK: product})
    return jlc, dk


def test_read_existing_empty_makes_no_calls():
    jlc, dk = FakeJlc(), FakeDigiKey()
    out = _read(ExistingPart(), jlc, dk)
    assert out == ExistingLookup()
    assert jlc.searches == jlc.lookups == dk.calls == []


def test_read_existing_evidence_from_digikey_and_current_candidate():
    jlc, dk = _tdk_suppliers()
    out = _read(ExistingPart(mpn=TDK, digikey_pn=TDK_DK), jlc, dk)
    assert len(out.options) == 1 and out.options[0].fields == ("DigiKeyPN", "MPN")
    assert dk.calls == [TDK_DK, TDK]
    ev = out.evidence
    assert ev.label == f"current part {TDK_DK} (DigiKey)"
    assert ev.capacitance_f == pytest.approx(4.7e-6)
    assert (ev.voltage_v, ev.dielectrics, ev.tolerance_pct, ev.package) == (
        16.0, ("X6S",), 10.0, "0603")
    c = out.current
    assert c.source == CURRENT
    assert (c.mpn, c.manufacturer, c.package) == (TDK, "TDK", "0603")
    assert (c.lcsc, c.jlc_library, c.jlc_stock) == (TDK_LCSC, "extended", 50000)
    assert (c.digikey_pn, c.digikey_stock) == (TDK_DK, 20000)
    assert c.params.dielectric == "X6S" and c.unavailable_reason == ""
    # No LCSC field: the LCSC number comes from a JLCPCB search by MPN.
    assert jlc.searches == [TDK] and jlc.lookups == []


def test_read_existing_swapped_fields_both_resolve():
    # BuckConverter: the MPN field holds a DigiKey PN, DigiKeyPN an MPN.
    jlc, dk = _tdk_suppliers()
    out = _read(ExistingPart(mpn=TDK_DK, digikey_pn=TDK), jlc, dk)
    assert len(out.options) == 1
    assert out.current is not None
    assert (out.current.mpn, out.current.digikey_pn) == (TDK, TDK_DK)
    assert sorted(dk.calls) == sorted([TDK, TDK_DK])


def test_read_existing_fields_naming_different_parts_are_two_options():
    jlc, dk = _tdk_suppliers()
    other = _dk("CC0402JRX7R8BB104", pns=("13-CC0402JRX7R8BB104CT-ND",))
    dk.products["13-CC0402JRX7R8BB104CT-ND"] = other
    out = _read(ExistingPart(mpn=TDK, digikey_pn="13-CC0402JRX7R8BB104CT-ND"),
                jlc, dk)
    assert [o.fields for o in out.options] == [("DigiKeyPN",), ("MPN",)]
    # The DigiKeyPN field's part is the first option.
    assert out.evidence.label == "current part 13-CC0402JRX7R8BB104CT-ND (DigiKey)"
    # named by the MPN field value, as written on the schematic
    assert out.options[1].evidence.label == f"current part {TDK} (DigiKey)"
    assert out.options[1].candidate.lcsc == TDK_LCSC


def test_read_existing_lcsc_from_field():
    jlc, dk = _tdk_suppliers()
    out = _read(ExistingPart(mpn=TDK, lcsc=TDK_LCSC), jlc, dk)
    assert jlc.lookups == [TDK_LCSC] and jlc.searches == []
    assert out.current.lcsc == TDK_LCSC


def test_read_existing_lcsc_field_naming_other_mpn_is_second_option():
    jlc, dk = _tdk_suppliers()
    jlc.parts["C1"] = _jlc("C1", "OTHERMPN")
    dk.products["OTHERMPN"] = _dk("OTHERMPN")
    out = _read(ExistingPart(mpn=TDK, lcsc="C1"), jlc, dk)
    assert [o.fields for o in out.options] == [("MPN",), ("LCSC",)]
    # the MPN part gets its own LCSC by search; the LCSC part its own DigiKey PN
    assert out.options[0].candidate.lcsc == TDK_LCSC
    assert out.options[1].candidate.mpn == "OTHERMPN"
    assert out.options[1].candidate.lcsc == "C1"
    assert "OTHERMPN" in dk.calls


def test_read_existing_skips_jlcpcb_placeholder_entries():
    jlc, dk = _tdk_suppliers()
    jlc.by_keyword[TDK] = [_tdk_jlc(lcsc="C9900013546", mfr="JLCPCB Assembly", stock=0)]
    out = _read(ExistingPart(digikey_pn=TDK_DK), jlc, dk)
    assert out.current is None
    assert f"MPN {TDK}: no exact match at JLCPCB" in out.notes


def test_read_existing_lcsc_from_search_prefers_orderable_basic_more_stock():
    jlc, dk = _tdk_suppliers()
    jlc.by_keyword[TDK] = [
        _jlc("C9", "C1608X6S1C475K080AB"),                       # other MPN
        _tdk_jlc("C1", stock=0),                                 # not orderable
        _tdk_jlc("C2", library="expand", stock=900000),
        _tdk_jlc("C3", library="base", stock=5000),
        _tdk_jlc("C4", library="base", stock=8000),
    ]
    out = _read(ExistingPart(mpn=TDK), jlc, dk)
    assert out.current.lcsc == "C4"
    assert out.current.jlc_library == "basic"


def test_read_existing_no_exact_jlc_match_gives_evidence_without_current():
    jlc, dk = _tdk_suppliers()
    jlc.by_keyword[TDK] = [_jlc("C9", "C1608X6S1C475K080AB")]
    out = _read(ExistingPart(mpn=TDK), jlc, dk)
    assert out.evidence is not None and out.current is None
    assert any("no exact match at JLCPCB" in n for n in out.notes)


def test_read_existing_evidence_from_jlc_when_digikey_has_nothing():
    jlc, _ = _tdk_suppliers()
    dk = FakeDigiKey()
    out = _read(ExistingPart(mpn=TDK, digikey_pn=TDK_DK, lcsc=TDK_LCSC), jlc, dk)
    ev = out.evidence
    assert ev.label == f"current part {TDK_LCSC} (JLCPCB)"
    assert ev.capacitance_f == pytest.approx(4.7e-6)
    assert (ev.voltage_v, ev.dielectrics, ev.tolerance_pct, ev.package) == (
        16.0, ("X6S",), 10.0, "0603")
    assert out.current is None
    assert any("not found at DigiKey" in n for n in out.notes)


def test_read_existing_digikey_unconfigured_uses_jlc():
    jlc, _ = _tdk_suppliers()
    dk = FakeDigiKey(configured=False)
    out = _read(ExistingPart(mpn=TDK, lcsc=TDK_LCSC), jlc, dk)
    assert dk.calls == []
    assert out.evidence.label == f"current part {TDK_LCSC} (JLCPCB)"


def test_read_existing_network_failure_becomes_notes():
    def boom(_):
        raise ConnectionError("offline")

    class BoomDigiKey:
        configured = True
        product_for = staticmethod(boom)

    out = read_existing(ExistingPart(mpn=TDK, lcsc=TDK_LCSC), jlc_part=boom,
                        jlc_search=boom, digikey=BoomDigiKey())
    assert out.evidence is None and out.current is None
    assert any("offline" in n for n in out.notes)


def test_read_existing_inactive_product_marked_unavailable():
    jlc, dk = _tdk_suppliers()
    dk.products[TDK] = _tdk_dk(status="Obsolete")
    out = _read(ExistingPart(mpn=TDK), jlc, dk)
    assert out.current.unavailable_reason == "DigiKey status 'Obsolete'"


def test_rank_key_current_first():
    cands = [_cand("used", usage_count=9, jlc_library="basic"),
             _cand("cur", source=CURRENT, jlc_price=9.0)]
    order = [c.mpn for c in sorted(cands, key=lambda c: rank_key(c, 10))]
    assert order == ["cur", "used"]


# ---- assess ---------------------------------------------------------------

def test_assess_current_part_passing_listed_first_no_search():
    jlc, dk = _tdk_suppliers()
    g = _group(MPN=TDK, DigiKeyPN=TDK_DK)
    res = _assess(g, jlc=jlc, dk=dk)
    assert [c.source for c in res.candidates] == [CURRENT]
    assert res.candidates[0].mpn == TDK
    # Only the MPN lookup at JLCPCB; no keyword search.
    assert jlc.searches == [TDK]
    assert res.requirement.voltage_min_v == Sourced(16.0, PART)
    assert res.requirement.dielectrics == Sourced(("X6S", "X7R"), PART)
    assert res.existing.current is not None
    assert res.rejected == []


def test_assess_current_part_listed_before_database_part():
    jlc, dk = _tdk_suppliers()
    db = _db()
    db.accept_capacitor(CapacitorChoice(
        mpn="OTHER", manufacturer="Murata", package="0603",
        params=CapacitorParams(4.7e-6, 25.0, "X7R", 10.0),
        digikey_pn="OTHER-1-ND", digikey_packaging="cut tape", lcsc="C5",
        jlc_library="basic"))
    jlc.parts["C5"] = _jlc("C5", "OTHER", cap="4.7uF", library="base")
    dk.products["OTHER-1-ND"] = _dk("OTHER", cap="4.7 µF")
    res = _assess(_group(MPN=TDK), db=db, jlc=jlc, dk=dk)
    assert [c.source for c in res.candidates] == [CURRENT, DATABASE]


def test_assess_current_part_low_jlc_stock_rejected_and_search_runs():
    jlc, dk = _tdk_suppliers(stock=33)
    alt = _jlc("C7", "ALT", cap="4.7uF", volts="25V", diel="X7R", library="base")
    jlc.by_keyword["4.7uF 0603 X6S"] = []
    jlc.by_keyword["4.7uF 0603 X7R"] = [alt]
    dk.products["ALT"] = _dk("ALT", cap="4.7 µF", volts="25V")
    res = _assess(_group(MPN=TDK), jlc=jlc, dk=dk)
    assert _reasons(res) == {"current part": "JLCPCB stock 33, need at least 1000"}
    assert [c.mpn for c in res.candidates] == ["ALT"]
    assert jlc.searches == [TDK, "4.7uF 0603"]


def test_assess_current_part_failing_requirement_rejected_and_search_runs():
    # Voltage:Min 25 V on the schematic; the current part is rated 16 V.
    jlc, dk = _tdk_suppliers()
    res = _assess(_group(MPN=TDK, **{"Voltage:Min": "25V"}), jlc=jlc, dk=dk)
    assert _reasons(res)["current part"] == "DigiKey rating 16V, need at least 25V"
    assert "4.7uF 0603" in jlc.searches


def test_assess_current_part_inactive_rejected():
    jlc, dk = _tdk_suppliers()
    dk.products[TDK] = _tdk_dk(status="Obsolete")
    res = _assess(_group(MPN=TDK), jlc=jlc, dk=dk)
    assert _reasons(res)["current part"] == "DigiKey status 'Obsolete'"


def test_assess_part_not_fitting_is_ignored_and_the_other_used():
    # DigiKeyPN names a 0.1 uF part; MPN the 4.7 uF part the Value states.
    jlc, dk = _tdk_suppliers()
    other = _dk("CC0402JRX7R8BB104", pns=("13-CC0402JRX7R8BB104CT-ND",))
    dk.products["13-CC0402JRX7R8BB104CT-ND"] = other
    g = _group(MPN=TDK, DigiKeyPN="13-CC0402JRX7R8BB104CT-ND")
    res = _assess(g, jlc=jlc, dk=dk)
    assert res.requirement.conflicts == ()
    assert res.requirement.warnings == (
        "Value says 4.7uF; current part 13-CC0402JRX7R8BB104CT-ND (DigiKey) says 100nF "
        "(current part 13-CC0402JRX7R8BB104CT-ND (DigiKey) not used)",)
    assert [(c.source, c.mpn) for c in res.candidates] == [(CURRENT, TDK)]
    assert "4.7uF 0603" not in jlc.searches


def test_assess_no_part_fits_searches_from_schematic():
    # BuckConverter C9 without a Description: Value 10nF, the part is 4.7 uF.
    jlc, dk = _tdk_suppliers()
    res = _assess(_group(MPN=TDK, Value="10nF", Description="", **{"Voltage:Min": "16V"}),
                  jlc=jlc, dk=dk)
    req = res.requirement
    assert req.conflicts == () and req.searchable
    assert req.warnings[0].startswith("Value says 10nF; current part")
    assert req.warnings[0].endswith("not used)")
    assert req.voltage_min_v == Sourced(16.0, SCHEMATIC)
    assert "current part" not in _reasons(res)
    assert any(k.startswith("10nF 0603") for k in jlc.searches)


def test_assess_reuses_existing_lookup():
    jlc, dk = _tdk_suppliers()
    g = _group(MPN=TDK)
    first = _assess(g, jlc=jlc, dk=dk)
    jlc2, dk2 = FakeJlc(), FakeDigiKey()
    again = _assess(g, jlc=jlc2, dk=dk2, existing=first.existing)
    assert again.existing is first.existing
    assert dk2.calls == [] and jlc2.searches == [] and jlc2.lookups == []
    assert [c.source for c in again.candidates] == [CURRENT]


def test_assess_edited_requirement_used_as_is():
    jlc, dk = _tdk_suppliers()
    g = _group(MPN=TDK)
    edited = g.requirement.edited(voltage_min_v=50.0)
    res = _assess(g, jlc=jlc, dk=dk, requirement=edited)
    assert res.requirement is edited
    assert _reasons(res)["current part"] == "DigiKey rating 16V, need at least 50V"


def test_assess_without_existing_part_searches():
    jlc = FakeJlc(by_keyword={"4.7uF 0603 X6S": [_tdk_jlc()]})
    dk = FakeDigiKey({TDK: _tdk_dk()})
    res = _assess(_group(**{"Voltage:Min": "10V"}), jlc=jlc, dk=dk)
    assert res.existing == ExistingLookup()
    assert res.requirement.dielectrics.source == DESCRIPTION
    assert [c.source for c in res.candidates] == [SEARCH]


# ---- Review fixes and the boards' real cases --------------------------------

SAMSUNG_10U = "CL32B106KBJNNWE"


def _samsung_10u(**kw) -> DigiKeyProduct:
    kw.setdefault("cap", "10 µF")
    kw.setdefault("diel", "X7R")
    kw.setdefault("package", "1210 (3225 Metric)")
    kw.setdefault("pns", ("1276-3388-2-ND", "1276-3388-1-ND", "1276-3388-6-ND"))
    return _dk(SAMSUNG_10U, **kw)


def test_buckconverter_swapped_fields_value_disagrees():
    # MPN field holds the DigiKey PN, DigiKeyPN the MPN; Value 10nF, part 10 uF.
    product = _samsung_10u()
    dk = FakeDigiKey({"1276-3388-1-ND": product, SAMSUNG_10U: product})
    g = _group(Value="10nF", MPN="1276-3388-1-ND", DigiKeyPN=SAMSUNG_10U,
               Description="10uF, 50V, X7R, 1210 package",
               Footprint="Capacitor_SMD:C_1210_3225Metric")
    res = _assess(g, jlc=FakeJlc(), dk=dk)
    assert len(res.existing.options) == 1
    # The label names the part by the field value that is a DigiKey PN.
    assert res.existing.evidence.label == "current part 1276-3388-1-ND (DigiKey)"
    req = res.requirement
    assert req.conflicts == ()
    assert req.warnings == (
        "Description says 10uF; Value says 10nF (Description not used)",
        "Value says 10nF; current part 1276-3388-1-ND (DigiKey) says 10uF "
        "(current part 1276-3388-1-ND (DigiKey) not used)",
    )
    # the search runs from the schematic: 10nF 1210, voltage from the Description
    assert (req.capacitance_f, req.package.value) == (pytest.approx(10e-9), "1210")
    assert req.voltage_min_v == Sourced(50.0, DESCRIPTION)
    assert req.searchable


def test_motordriver_two_digikey_pns_of_fitting_parts_are_both_current():
    a = _dk("CL21A106KAYNNNE", cap="10 µF", pns=("1276-1871-1-ND",))
    b = _dk("CL21B106KOQNNNE", cap="10 µF", pns=("1276-1869-1-ND",))
    dk = FakeDigiKey({"1276-1871-1-ND": a, "1276-1869-1-ND": b})
    jlc = FakeJlc(by_keyword={
        "CL21A106KAYNNNE": [_jlc("C1", "CL21A106KAYNNNE", cap="10uF")],
        "CL21B106KOQNNNE": [_jlc("C2", "CL21B106KOQNNNE", cap="10uF")]})
    g = _group(Value="10uF", MPN="1276-1871-1-ND", DigiKeyPN="1276-1869-1-ND",
               Description="Unpolarized capacitor, small symbol")
    res = _assess(g, jlc=jlc, dk=dk)
    assert res.requirement.conflicts == () and res.requirement.warnings == ()
    assert sorted(c.mpn for c in res.candidates) == ["CL21A106KAYNNNE", "CL21B106KOQNNNE"]
    assert {c.source for c in res.candidates} == {CURRENT}


def test_fitting_parts_give_the_loosest_requirement():
    # MotorDriver C5: DigiKeyPN names a 5 % 25 V part, LCSC a 10 % 50 V X5R one.
    a = _dk("CC0402JPX7R8BB103", cap="10000 pF", volts="25V", tol="±5%",
            package="0402 (1005 Metric)", pns=("13-CC0402JPX7R8BB103CT-ND",))
    b = _dk("CC0402KRX7R8BB103", cap="10000 pF", volts="50V", diel="X5R",
            package="0402 (1005 Metric)", pns=("311-1036-1-ND",))
    dk = FakeDigiKey({"13-CC0402JPX7R8BB103CT-ND": a, "CC0402KRX7R8BB103": b})
    jlc = FakeJlc(parts={"C327025": _jlc("C327025", "CC0402KRX7R8BB103", cap="10nF",
                                         package="0402")})
    g = _group(Value="0.01uF", DigiKeyPN="13-CC0402JPX7R8BB103CT-ND", LCSC="C327025",
               Footprint="Capacitor_SMD:C_0402_1005Metric", Description="")
    res = _assess(g, jlc=jlc, dk=dk)
    req = res.requirement
    assert req.voltage_min_v == Sourced(25.0, PART)
    assert req.tolerance_max_pct == Sourced(10.0, PART)
    assert req.dielectrics.value[:2] == ("X7R", "X5R")
    assert [(c.source, c.mpn) for c in res.candidates] == [(CURRENT, "CC0402KRX7R8BB103")]


def test_two_current_parts_failing_are_labelled_by_mpn():
    a = _dk("CL21A106KAYNNNE", cap="10 µF", pns=("1276-1871-1-ND",))
    b = _dk("CL21B106KOQNNNE", cap="10 µF", pns=("1276-1869-1-ND",), qty=5)
    dk = FakeDigiKey({"1276-1871-1-ND": a, "1276-1869-1-ND": b})
    jlc = FakeJlc(by_keyword={
        "CL21A106KAYNNNE": [_jlc("C1", "CL21A106KAYNNNE", cap="10uF", stock=10)],
        "CL21B106KOQNNNE": [_jlc("C2", "CL21B106KOQNNNE", cap="10uF")]})
    g = _group(Value="10uF", MPN="1276-1871-1-ND", DigiKeyPN="1276-1869-1-ND")
    reasons = _reasons(_assess(g, jlc=jlc, dk=dk))
    assert "current part CL21A106KAYNNNE" in reasons
    assert "current part CL21B106KOQNNNE" in reasons


def test_motordriver_fields_naming_parts_of_different_packages():
    # C36/C37: MPN an 0603 part, DigiKeyPN an 0402 part, footprint 0402,
    # Description 603. The 0402 part is used; the MPN and Description warn.
    a = _dk("CGA3E2X7R1E104K080AA", pns=("445-5667-1-ND",))
    b = _dk("CC0402JRX7R8BB104", package="0402 (1005 Metric)",
            pns=("13-CC0402JRX7R8BB104CT-ND",))
    dk = FakeDigiKey({"CGA3E2X7R1E104K080AA": a, "13-CC0402JRX7R8BB104CT-ND": b})
    g = _group(Value="0.1uF", MPN="CGA3E2X7R1E104K080AA",
               DigiKeyPN="13-CC0402JRX7R8BB104CT-ND",
               Footprint="Capacitor_SMD:C_0402_1005Metric",
               Description="0.1uF, 25V, X7R 603")
    res = _assess(g, dk=dk)
    req = res.requirement
    assert req.conflicts == () and req.searchable
    assert res.existing.evidence.label == "current part 13-CC0402JRX7R8BB104CT-ND (DigiKey)"
    assert req.package == Sourced("0402", SCHEMATIC)
    assert req.voltage_min_v == Sourced(50.0, PART)
    assert req.warnings == (
        "Description says 0603; Footprint says 0402 (Description not used)",
        "Footprint says 0402; current part CGA3E2X7R1E104K080AA (DigiKey) says 0603 "
        "(current part CGA3E2X7R1E104K080AA (DigiKey) not used)",
    )


def test_ecg_100nf_matches_0p1uf_part_no_conflict():
    product = _dk()  # CL10B104KB8NNNC, 0.1 µF, 0603
    dk = FakeDigiKey({"1276-1000-1-ND": product})
    jlc = FakeJlc(by_keyword={"CL10B104KB8NNNC": [_jlc()]})
    g = _group(Value="100nF", DigiKeyPN="1276-1000-1-ND",
               Description="Unpolarized capacitor, small symbol")
    res = _assess(g, jlc=jlc, dk=dk)
    assert res.requirement.conflicts == ()
    assert res.requirement.capacitance_source == SCHEMATIC
    assert res.requirement.voltage_min_v == Sourced(50.0, PART)
    assert [c.source for c in res.candidates] == [CURRENT]
    assert res.candidates[0].digikey_pn == "1276-1000-1-ND"


def test_read_existing_network_failure_adds_no_not_found_note():
    def boom(_):
        raise ConnectionError("offline")

    class BoomDigiKey:
        configured = True
        product_for = staticmethod(boom)

    out = read_existing(ExistingPart(digikey_pn=TDK_DK, lcsc=TDK_LCSC),
                        jlc_part=boom, jlc_search=boom, digikey=BoomDigiKey())
    assert len(out.notes) == 2
    assert all("failed: offline" in n for n in out.notes)


def test_read_existing_search_failure_after_digikey_becomes_note():
    def boom(_):
        raise TimeoutError("slow")

    _, dk = _tdk_suppliers()
    out = read_existing(ExistingPart(mpn=TDK), jlc_part=boom, jlc_search=boom,
                        digikey=dk)
    assert out.evidence is not None and out.current is None
    assert any("failed: slow" in n for n in out.notes)
    assert not any("no exact match" in n for n in out.notes)


def test_read_existing_digikey_unconfigured_finds_jlc_by_mpn_field():
    jlc, _ = _tdk_suppliers()
    dk = FakeDigiKey(configured=False)
    out = _read(ExistingPart(mpn=TDK, digikey_pn=TDK_DK), jlc, dk)
    assert out.evidence.label == f"current part {TDK_LCSC} (JLCPCB)"
    assert out.evidence.capacitance_f == pytest.approx(4.7e-6)
    assert out.current is None
    assert jlc.searches == [TDK]


def test_read_existing_digikey_unconfigured_swapped_fields_find_jlc():
    jlc, _ = _tdk_suppliers()
    dk = FakeDigiKey(configured=False)
    out = _read(ExistingPart(mpn=TDK_DK, digikey_pn=TDK), jlc, dk)
    assert out.evidence.label == f"current part {TDK_LCSC} (JLCPCB)"
    assert jlc.searches == [TDK_DK, TDK]


def test_read_existing_product_without_mpn_is_not_a_conflict():
    jlc, dk = _tdk_suppliers()
    dk.products[TDK_DK] = replace(_tdk_dk(), mpn=None)
    out = _read(ExistingPart(mpn=TDK, digikey_pn=TDK_DK), jlc, dk)
    assert len(out.options) == 1


def test_read_existing_package_from_jlc_when_digikey_lists_none():
    jlc = FakeJlc(by_keyword={TDK: [_tdk_jlc()]})
    dk = FakeDigiKey({TDK: _tdk_dk(drop=("Package / Case",))})
    out = _read(ExistingPart(mpn=TDK), jlc, dk)
    assert out.evidence.package is None
    assert out.current.package == "0603"


def test_current_part_rejected_also_rejects_its_database_entry():
    # The current part is obsolete at DigiKey and recorded in the database.
    jlc, dk = _tdk_suppliers()
    dk.products[TDK] = _tdk_dk(status="Obsolete")
    db = _db()
    db.accept_capacitor(CapacitorChoice(
        mpn=TDK, manufacturer="TDK", package="0603",
        params=CapacitorParams(4.7e-6, 16.0, "X6S", 10.0),
        digikey_pn=TDK_DK, digikey_packaging="cut tape", lcsc=TDK_LCSC,
        jlc_library="extended"))
    res = _assess(_group(MPN=TDK), db=db, jlc=jlc, dk=dk)
    assert all(c.mpn != TDK for c in res.candidates)
    reasons = _reasons(res)
    assert reasons["current part"] == "DigiKey status 'Obsolete'"
    assert reasons[f"{TDK_LCSC} {TDK}"] == "DigiKey status 'Obsolete'"


_REAL_PAIRS = [
    # (Value, Description, Footprint, farads, package, voltage)
    ("C_4p7U", "4.7uF, 16V, X6S 603", "", 4.7e-6, "0603", 16.0),
    ("C_P1U", "0.1uF, 25V, X7R 603", "", 0.1e-6, "0603", 25.0),
    ("C_10U_805", "10uF, 25V, X5R, 805 package", "", 10e-6, "0805", 25.0),
    ("C_22U_1210", "22uF, X5R, 25V, 1210", "", 22e-6, "1210", 25.0),
    ("1uF", "1uF, 25V - 50V", FP_0603, 1e-6, "0603", 25.0),
    ("0.1uF", "0.1uF, 25V, X7R, 0603", "", 0.1e-6, "0603", 25.0),
    ("C_10U_1210", "10uF, 50V, X7R, 1210 package", "", 10e-6, "1210", 50.0),
    ("100nF", "0.1uF, 25V, X7R, 0603", FP_0603, 0.1e-6, "0603", 25.0),
]


@pytest.mark.parametrize("value,desc,fp,farads,package,volts", _REAL_PAIRS)
def test_real_value_and_description_pairs_search(value, desc, fp, farads,
                                                  package, volts):
    jlc = FakeJlc()
    res = _assess(_group(Value=value, Description=desc, Footprint=fp), jlc=jlc)
    req = res.requirement
    assert req.conflicts == ()
    assert req.capacitance_f == pytest.approx(farads)
    assert req.package.value == package
    assert req.voltage_min_v == Sourced(volts, DESCRIPTION)
    # Searchable: the keyword search ran with the merged requirement.
    assert jlc.searches and all(k.split()[1] == package for k in jlc.searches)
    assert len(jlc.searches) == 1


@pytest.mark.parametrize("desc", ["Unpolarized capacitor, small symbol",
                                  "Polarized capacitor, small US symbol", ""])
def test_generic_description_states_nothing(desc):
    res = _assess(_group(Value="C_1U", Description=desc, Footprint=FP_0603))
    req = res.requirement
    assert req.capacitance_f == pytest.approx(1e-6)
    assert req.voltage_min_v.source == UNSET
    assert res.problem == req.problem() and res.candidates == []


def test_digikey_misses_do_not_use_up_the_limit():
    # Two hits not sold at DigiKey rank first; the third is confirmed anyway.
    hits = [_jlc(f"C{i}", f"NODK{i}", library="base") for i in range(2)] + [_jlc()]
    jlc = FakeJlc(default=hits)
    dk = FakeDigiKey({"CL10B104KB8NNNC": _dk()})
    res = _run(jlc=jlc, dk=dk, limit=1)
    assert [c.mpn for c in res.candidates] == ["CL10B104KB8NNNC"]
    assert len(dk.calls) == 3 <= MAX_LOOKUP_FACTOR * 1
    assert res.dropped_by_limit == 0


def test_digikey_lookups_capped_at_factor_times_limit():
    hits = [_jlc(f"C{i}", f"NODK{i}") for i in range(10)]
    dk = FakeDigiKey({})
    res = _run(jlc=FakeJlc(default=hits), dk=dk, limit=2)
    assert len(dk.calls) == MAX_LOOKUP_FACTOR * 2
    assert res.dropped_by_limit == 10 - MAX_LOOKUP_FACTOR * 2


def test_tolerance_from_the_current_part_is_relaxed_too():
    from kicadtools.requirement import PART
    jlc, dk = _loose()
    res = _run(_req(tol=10.0, tol_source=PART), jlc=jlc, dk=dk)
    assert [c.params.tolerance_pct for c in res.candidates] == [20.0]
    assert res.relaxed_tolerance_pct == 10.0
