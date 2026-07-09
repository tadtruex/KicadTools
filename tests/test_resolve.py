"""Tests for the LCSC resolution pipeline (kicadtools.resolve)."""
from pathlib import Path

from kicadtools.resolve import (
    Cache,
    PartRecord,
    Resolver,
    Status,
    iter_bom_part_records,
)

DATA = Path(__file__).parent / "data"
BOARD = DATA / "MotorDriver.kicad_sch"


class FakeDigiKey:
    """Records calls; maps a DigiKey PN to an MPN from a fixed table."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def mpn_for(self, dk_pn):
        self.calls.append(dk_pn)
        return self.table.get(dk_pn)


def _record(**props):
    props.setdefault("Reference", "R1")
    return PartRecord("R1", props, BOARD, "uuid-1")


def test_state1_existing_lcsc_is_trusted_without_lookups():
    dk = FakeDigiKey({})
    jlc_calls = []
    r = Resolver(digikey=dk, jlcpcb=lambda m: jlc_calls.append(m))
    res = r.resolve(_record(LCSC="C541465", MPN="X", **{"DigiKey PN": "Y"}))
    assert res.status is Status.OK
    assert res.entry == "lcsc"
    assert res.lcsc == "C541465"
    assert dk.calls == [] and jlc_calls == []  # no network touched


def test_state2_mpn_goes_straight_to_jlcpcb():
    dk = FakeDigiKey({})
    r = Resolver(digikey=dk, jlcpcb=lambda m: "C100" if m == "RMCF0603FT100K" else None)
    res = r.resolve(_record(MPN="RMCF0603FT100K"))
    assert res.status is Status.RESOLVED
    assert res.entry == "mpn"
    assert res.lcsc == "C100"
    assert dk.calls == []  # DigiKey never consulted when an MPN is present


def test_state3_digikey_pn_resolves_to_mpn_then_lcsc():
    dk = FakeDigiKey({"P10.0KLDKR-ND": "RNCP0603FTD10K0"})
    r = Resolver(digikey=dk, jlcpcb=lambda m: "C52923" if m == "RNCP0603FTD10K0" else None)
    res = r.resolve(_record(**{"DigiKey PN": "P10.0KLDKR-ND"}))
    assert res.status is Status.RESOLVED
    assert res.entry == "digikey"
    assert res.resolved_mpn == "RNCP0603FTD10K0"
    assert res.lcsc == "C52923"
    assert dk.calls == ["P10.0KLDKR-ND"]


def test_state4_no_part_numbers_is_flagged():
    r = Resolver(digikey=FakeDigiKey({}), jlcpcb=lambda m: None)
    res = r.resolve(_record(Value="10k"))
    assert res.status is Status.FLAGGED
    assert res.entry == "none"
    assert "no LCSC" in res.reason


def test_digikey_miss_is_flagged():
    r = Resolver(digikey=FakeDigiKey({}), jlcpcb=lambda m: "Cxxx")
    res = r.resolve(_record(**{"DigiKey PN": "unknown-ND"}))
    assert res.status is Status.FLAGGED
    assert res.entry == "digikey"
    assert "did not resolve to an MPN" in res.reason


def test_jlcpcb_miss_is_flagged_but_keeps_mpn():
    dk = FakeDigiKey({"dk-1": "MPN-1"})
    r = Resolver(digikey=dk, jlcpcb=lambda m: None)
    res = r.resolve(_record(**{"DigiKey PN": "dk-1"}))
    assert res.status is Status.FLAGGED
    assert res.entry == "digikey"
    assert res.resolved_mpn == "MPN-1"
    assert "no LCSC match" in res.reason


def test_cache_avoids_repeat_network_calls():
    dk = FakeDigiKey({"dk-1": "MPN-1"})
    jlc_calls = []

    def jlc(mpn):
        jlc_calls.append(mpn)
        return "C999"

    cache = Cache(":memory:")
    r = Resolver(digikey=dk, jlcpcb=jlc, cache=cache)
    rec = _record(**{"DigiKey PN": "dk-1"})

    first = r.resolve(rec)
    second = r.resolve(rec)
    assert first.lcsc == second.lcsc == "C999"
    # Each hop consulted the network exactly once despite two resolves.
    assert dk.calls == ["dk-1"]
    assert jlc_calls == ["MPN-1"]


def test_iter_bom_part_records_reads_real_board():
    records = iter_bom_part_records(BOARD)
    assert records, "expected placed BOM parts on the MotorDriver board"
    assert all(not r.reference.startswith("#") for r in records)
    # At least some parts carry a populated LCSC already (state 1).
    from kicadtools.partnum import extract_part_numbers

    assert any(extract_part_numbers(r.props).lcsc for r in records)
    # Provenance is populated for write-back later.
    assert all(r.source_file and r.symbol_uuid for r in records)
