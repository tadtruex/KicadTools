"""Tests for surface-mount connector rows (smdconnector.py): LabBoard J3
"Stemma QT (LSM303AGR)" with no footprint is searched by part number at both
suppliers and gets an LCSC number and its footprint on accept."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kicadtools.icsearch import IcCandidate, ic_assignment_plan
from kicadtools.intent import ConnectorIntent
from kicadtools.mounting import Mounting
from kicadtools.pcb import SMD, THT
from kicadtools.smdconnector import CONNECTOR_KIND, group_smd_connectors, implied_fits
from kicadtools.thtassign import group_tht

SH = "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal"
GENERIC = "Generic connector, single row, 01x04, script generated"


class _Board:
    """SMD for JST SH and `_SMD` footprints, through-hole for anything else."""

    def __init__(self):
        self.footprints = SimpleNamespace(placed=lambda ref: None, count=lambda ref, lib: 5,
                                          library_footprint=lambda lib: None)

    def mounting(self, ref, lib_id):
        kind = SMD if lib_id and ("JST_SH" in lib_id or lib_id.endswith("_SMD")) else THT
        return Mounting(kind=kind, source="library", attr=kind)


def _rec(ref, value, footprint="", **props):
    data = {"Reference": ref, "Value": value, "Footprint": footprint,
            "Description": GENERIC, **props}
    return SimpleNamespace(reference=ref, source_file=Path("/b/x.kicad_sch"),
                           symbol_uuid=f"u-{ref}", props=data,
                           intent=ConnectorIntent(reference=ref, value=value, dnp=False,
                                                  in_bom=True))


def _cand(mpn, lcsc="C160404"):
    return IcCandidate(mpn=mpn, manufacturer="JST", digikey_pn=f"455-{mpn}CT-ND",
                       lcsc=lcsc)


def test_stemma_qt_is_a_surface_mount_connector_row():
    recs = [_rec("J3", "Stemma QT (LSM303AGR)")]
    (group,) = group_smd_connectors(recs, _Board())
    req = group.requirement
    assert group.kind == CONNECTOR_KIND
    # The footprint's part number only: the Value names the sensor.
    assert req.identifiers.value_mpns == ("SM04B-SRSS-TB",)
    assert req.searchable and req.footprint == "" and req.check_footprint == SH
    assert req.pad_count == 5
    assert group_tht(recs, _Board()) == []        # not a through-hole row


def test_plan_writes_lcsc_and_the_footprint_when_the_part_fits():
    (group,) = group_smd_connectors([_rec("J3", "Stemma QT (LSM303AGR)")], _Board())
    assert implied_fits(group.requirement, "SM04B-SRSS-TB(LF)(SN)")
    props = ic_assignment_plan(group, _cand("SM04B-SRSS-TB(LF)(SN)"))[
        Path("/b/x.kicad_sch")]["u-J3"]
    assert props["LCSC"] == "C160404"
    assert props["Footprint"] == SH
    assert props["MPN"] == "SM04B-SRSS-TB(LF)(SN)"
    # A part the footprint does not name gets no footprint.
    props = ic_assignment_plan(group, _cand("BM04B-SRSS-TB(LF)(SN)"))[
        Path("/b/x.kicad_sch")]["u-J3"]
    assert "Footprint" not in props


def test_smd_footprint_that_names_a_part_takes_the_path():
    (group,) = group_smd_connectors([_rec("J1", "I2C", footprint=SH)], _Board())
    assert group.requirement.footprint == SH and group.requirement.implied is None
    props = ic_assignment_plan(group, _cand("SM04B-SRSS-TB(LF)(SN)"))[
        Path("/b/x.kicad_sch")]["u-J1"]
    assert "Footprint" not in props


def test_through_hole_and_generic_connectors_are_left_alone():
    recs = [_rec("J6", "Photocell 1"),
            _rec("J7", "SMD header", footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical_SMD")]
    assert group_smd_connectors(recs, _Board()) == []


def test_jlcpcb_listing_with_bracketed_suffix_is_the_same_part():
    # JLCPCB's exact "SM04B-SRSS-TB" listings are zero-stock placeholders; the
    # stocked one adds JST's plating codes.
    from kicadtools.partsearch import _best_jlc_hit
    from kicadtools.suppliers import JlcPart
    hits = [JlcPart("C9900000999", "SM04B-SRSS-TB", "JST", stock=0),
            JlcPart("C51940130", "XY-SM04B-SRSS-TB", "XKB", stock=17286),
            JlcPart("C160404", "SM04B-SRSS-TB(LF)(SN)", "JST", stock=56856),
            JlcPart("C28867426", "SM04B-SRSS-TB(LF)(SN)(P)", "JST", stock=0)]
    assert _best_jlc_hit(hits, "SM04B-SRSS-TB").lcsc == "C160404"
    # An orderable exact listing still comes first.
    hits.append(JlcPart("C1", "SM04B-SRSS-TB", "JST", stock=10))
    assert _best_jlc_hit(hits, "SM04B-SRSS-TB").lcsc == "C1"
    assert _best_jlc_hit(hits, "SM05B-SRSS-TB") is None
