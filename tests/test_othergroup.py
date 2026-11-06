"""Tests for the Parts-tab rows of BOM parts with no assignment path
(othergroup.py), from LabBoard symbols."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadtools.intent import ElectricalIntent
from kicadtools.othergroup import (
    FOOTPRINT_NOT_FOUND,
    NO_FOOTPRINT,
    NO_PART,
    NOT_SUPPORTED,
    claimed,
    group_others,
)
from kicadtools.pcb import SMD, UNKNOWN


def _rec(ref, intent_type="", **props):
    data = {"Reference": ref, "dnp": False, "in_bom": True, **props}
    if intent_type:
        data["Intent:Type"] = intent_type
    return SimpleNamespace(reference=ref, props=data, source_file=Path("/b/x.kicad_sch"),
                           symbol_uuid=f"u-{ref}", intent=ElectricalIntent.from_dict(data))


JUMPER = {"Value": "Jumper", "Description": "Jumper, 2-pole, open",
          "ki_fp_filters": "Jumper* TestPoint*2Pads* TestPoint*Bridge*"}


@pytest.mark.parametrize("rec, reason, what", [
    (_rec("H1", Value="MountingHole", Footprint="MountingHole:MountingHole_3.2mm_M3"),
     NO_PART, "mounting hole"),
    (_rec("JP7", Value="STBY", Description="Solder Jumper, 2-pole, open",
          ki_fp_filters="SolderJumper*Open*"), NO_PART, "solder jumper"),
    (_rec("J5", "connector", Value="TMP235 (JST PH 3)"), NO_FOOTPRINT, ""),
    (_rec("Q1", "transistor", Value="KSC2073"), NOT_SUPPORTED, ""),
    (_rec("JP1", **JUMPER), NOT_SUPPORTED, ""),
])
def test_reason(rec, reason, what):
    (group,) = group_others([rec], set())
    assert group.requirement.reason == reason
    assert group.requirement.what == what
    assert group.requirement.problem()


def test_claimed_records_are_skipped_and_like_parts_share_a_row():
    recs = [_rec(f"JP{i}", **JUMPER) for i in range(1, 7)] + [_rec("Q1", "transistor", Value="KSC2073")]
    taken = claimed([SimpleNamespace(members=[recs[-1]])])
    (group,) = group_others(recs, taken)
    assert group.references == [f"JP{i}" for i in range(1, 7)]
    assert group.requirement.type_name == "unknown"


class _Board:
    def __init__(self, kind):
        self.kind = kind

    def mounting(self, reference, lib_id):
        return SimpleNamespace(kind=self.kind)


def test_connector_footprint_mounting():
    rec = _rec("J5", "connector", Value="Conn_01x02", Footprint="ECG_Footprints:691137710002")
    (unknown,) = group_others([rec], set(), _Board(UNKNOWN))
    assert unknown.requirement.reason == FOOTPRINT_NOT_FOUND
    assert "not on the board or in the libraries" in unknown.requirement.problem()
    (smd,) = group_others([rec], set(), _Board(SMD))
    assert smd.requirement.reason == NOT_SUPPORTED
    assert "surface-mount" in smd.requirement.problem()


def test_named_part_reads_as_assigned():
    rec = _rec("Q1", "transistor", Value="KSC2073", MPN="KSC2073TU",
               Manufacturer="onsemi", DigiKeyPN="KSC2073TU-ND")
    (group,) = group_others([rec], set())
    assert group.assigned
