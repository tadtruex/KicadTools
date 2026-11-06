"""Tests for footprints implied by a connector symbol's text
(impliedfootprint.py), the through-hole rows built from them and the
footprint written on accept. The texts are LabBoard's."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadtools.impliedfootprint import BARREL_JACK, DEFAULT, VALUE, implied_footprint
from kicadtools.intent import ConnectorIntent
from kicadtools.mounting import Mounting
from kicadtools.pcb import THT
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtassign import ThtCandidate, group_tht, tht_assignment_plan
from kicadtools.thtfootprint import HEADER, NAMED_PART

GENERIC = "Generic connector, single row, 01x{:02d}, script generated"


@pytest.mark.parametrize("value, description, filters, lib_id, source", [
    ("Photocell 1", GENERIC.format(2), "Connector*:*_1x??_*",
     "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", DEFAULT),
    ("AD2", "Generic connector, double row, 02x10, odd/even pin numbering", "",
     "Connector_PinHeader_2.54mm:PinHeader_2x10_P2.54mm_Vertical", DEFAULT),
    ("TMP235 (JST PH 3)", GENERIC.format(3), "",
     "Connector_JST:JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical", VALUE),
    ("JST XH", GENERIC.format(4), "",
     "Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical", VALUE),
    ("Stemma QT (LSM303AGR)", GENERIC.format(4), "",
     "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal", VALUE),
    ("9V DC in", "DC Barrel Jack", "BarrelJack*", BARREL_JACK, DEFAULT),
])
def test_implied_footprint(value, description, filters, lib_id, source):
    got = implied_footprint({"Value": value, "Description": description,
                             "ki_fp_filters": filters})
    assert (got.lib_id, got.source) == (lib_id, source)
    assert got.basis


def test_no_implication():
    assert implied_footprint({"Value": "JST PH", "Description": "connector"}) is None
    assert implied_footprint({"Value": "Speaker", "Description": ""}) is None


class _Board:
    def __init__(self):
        self.footprints = SimpleNamespace(
            placed=lambda ref: None,
            library_footprint=lambda lib: SimpleNamespace(
                pads=[SimpleNamespace(number=str(i)) for i in range(1, 4)]))

    def mounting(self, ref, lib_id):
        return Mounting(kind=THT, source="library", attr=THT)


def _rec(ref, value, n, **props):
    data = {"Reference": ref, "Value": value, "Footprint": "",
            "Description": GENERIC.format(n), **props}
    return SimpleNamespace(reference=ref, source_file=Path("/b/x.kicad_sch"),
                           symbol_uuid=f"u-{ref}", props=data,
                           intent=ConnectorIntent(reference=ref, value=value, dnp=False,
                                                  in_bom=True))


def _cand(group, mpn):
    product = DigiKeyProduct(mpn=mpn, manufacturer="JST Sales America Inc.",
                             digikey_pns=[f"{mpn}-ND"], quantity_available=5000)
    return ThtCandidate(product=product, signature=group.requirement.signature, value="")


def test_footprintless_connectors_become_through_hole_rows():
    j5, j6 = group_tht([_rec("J5", "TMP235 (JST PH 3)", 3), _rec("J6", "Photocell 1", 2)],
                       _Board())
    assert j5.requirement.signature.family == NAMED_PART
    assert j5.requirement.signature.mpn == "B3B-PH-K"
    assert j5.requirement.pad_count == 3
    assert j5.requirement.searchable and j5.requirement.footprint == ""
    assert j6.requirement.signature.family == HEADER
    assert not j5.assigned


def test_plan_writes_the_implied_footprint_when_the_part_fits():
    (j5,) = group_tht([_rec("J5", "TMP235 (JST PH 3)", 3)], _Board())
    props = tht_assignment_plan(j5, _cand(j5, "B3B-PH-K-S(LF)(SN)"))[Path("/b/x.kicad_sch")]["u-J5"]
    assert props["Footprint"] == "Connector_JST:JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical"
    assert props["MPN"] == "B3B-PH-K-S(LF)(SN)"
    # A part the footprint does not name (a side-entry PH) gets no footprint.
    props = tht_assignment_plan(j5, _cand(j5, "S3B-PH-K-S(LF)(SN)"))[Path("/b/x.kicad_sch")]["u-J5"]
    assert "Footprint" not in props


def test_plan_never_replaces_a_footprint():
    (j5,) = group_tht([_rec("J5", "TMP235 (JST PH 3)", 3)], _Board())
    j5.members[0].props["Footprint"] = "Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal"
    props = tht_assignment_plan(j5, _cand(j5, "B3B-PH-K-S(LF)(SN)"))[Path("/b/x.kicad_sch")]["u-J5"]
    assert "Footprint" not in props


def test_plan_writes_the_default_header_footprint():
    (j6,) = group_tht([_rec("J6", "Photocell 1", 2)], _Board())
    props = tht_assignment_plan(j6, _cand(j6, "PREC002SAAN-RC"))[Path("/b/x.kicad_sch")]["u-J6"]
    assert props["Footprint"] == "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"


def test_parts_cells_mark_the_implied_footprint():
    pytest.importorskip("PySide6")
    from kicadtools.gui.parts import COL_FOOTPRINT, STATUS_UNASSIGNED, PartsTableModel
    j5, j6 = group_tht([_rec("J5", "TMP235 (JST PH 3)", 3), _rec("J6", "Photocell 1", 2)],
                       _Board())
    model = PartsTableModel([j5, j6])
    assert model.cell_text(0, COL_FOOTPRINT) == "JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical (from value)"
    assert model.cell_text(1, COL_FOOTPRINT) == "PinHeader_1x02_P2.54mm_Vertical (default)"
    assert model.status(0) == model.status(1) == STATUS_UNASSIGNED
