"""Tests for kicadtools.package: KiCad footprint names and DigiKey package
parameters read into PackageInfo, with family names normalized."""
from __future__ import annotations

import pytest

from kicadtools.package import (
    FAMILIES,
    PackageInfo,
    normalize_family,
    parse_digikey_package,
    parse_footprint,
    standard_pitch,
)

P = PackageInfo
NONE = PackageInfo()


# --------------------------------------------------------------------------- #
# KiCad footprint names.
# --------------------------------------------------------------------------- #

# Every footprint in the IC_PLAN.md evidence table.
@pytest.mark.parametrize("name, info", [
    ("SOT-23-5", P("SOT-23", 5, None, None, False)),
    ("ECG_Footprints:CP_20_10_ADI_NOTH", NONE),
    ("ECG_Footprints:DIP30-ArduinoNano", NONE),
    ("TexasInstruments:RHH0036C", NONE),
    ("LQFP-48_7x7mm_P0.5mm", P("LQFP", 48, (7.0, 7.0), 0.5, False)),
    ("STMicro:LDL212_2x2mm_Pitch0.5mm", P(None, None, (2.0, 2.0), 0.5, None)),
    ("DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm", P("DFN", 6, (2.0, 2.0), 0.65, True)),
    ("TSOT-23-6", P("TSOT-23", 6, None, None, False)),
])
def test_evidence_table_footprints(name, info):
    assert parse_footprint(name) == info


@pytest.mark.parametrize("name, info", [
    # SOT-23 family and aliases.
    ("Package_TO_SOT_SMD:SOT-23", P("SOT-23", 3, None, None, False)),
    ("Package_TO_SOT_SMD:SOT-23-6", P("SOT-23", 6, None, None, False)),
    ("SOT-23-8_Handsoldering", P("SOT-23", 8, None, None, False)),
    ("TSOT-23", P("TSOT-23", 3, None, None, False)),
    ("TSOT-23-5_HandSoldering", P("TSOT-23", 5, None, None, False)),
    ("SOT-563", P("SOT-563", 6, None, None, False)),
    ("SC-70-8", P("SC-70", 8, None, None, False)),
    ("SOT-323_SC-70", P("SC-70", 3, None, None, False)),
    ("SOT-353_SC-70-5", P("SC-70", 5, None, None, False)),
    ("SOT-363_SC-70-6", P("SC-70", 6, None, None, False)),
    ("SOT-223-3_TabPin2", P("SOT-223", 3, None, None, False)),
    # Small outline.
    ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", P("SOIC", 8, (3.9, 4.9), 1.27, False)),
    ("SOIC-14-16_3.9x9.9mm_P1.27mm", P("SOIC", 14, (3.9, 9.9), 1.27, False)),
    ("SOIC-20W_7.5x12.8mm_P1.27mm", P("SOIC", 20, (7.5, 12.8), 1.27, False)),
    ("SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.62x3.51mm_ThermalVias",
     P("SOIC", 8, (3.9, 4.9), 1.27, True)),
    ("SO-16_3.9x9.9mm_P1.27mm", P("SOIC", 16, (3.9, 9.9), 1.27, False)),
    ("MSOP-10_3x3mm_P0.5mm", P("MSOP", 10, (3.0, 3.0), 0.5, False)),
    ("Linear_MSOP-12-16-1EP_3x4mm_P0.5mm", P("MSOP", 12, (3.0, 4.0), 0.5, True)),
    ("VSSOP-8_3x3mm_P0.65mm", P("MSOP", 8, (3.0, 3.0), 0.65, False)),
    ("TSSOP-14_4.4x5mm_P0.65mm", P("TSSOP", 14, (4.4, 5.0), 0.65, False)),
    ("HTSSOP-16-1EP_4.4x5mm_P0.65mm_EP3.4x5mm_Mask2.46x2.31mm",
     P("TSSOP", 16, (4.4, 5.0), 0.65, True)),
    ("SSOP-20_3.9x8.7mm_P0.635mm", P("SSOP", 20, (3.9, 8.7), 0.635, False)),
    # No-lead.
    ("Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm",
     P("QFN", 24, (4.0, 4.0), 0.5, True)),
    ("QFN-16-1EP_4x4mm_P0.65mm_EP2.7x2.7mm_PullBack",
     P("QFN", 16, (4.0, 4.0), 0.65, True)),
    ("HVQFN-24-1EP_4x4mm_P0.5mm_EP2.5x2.5mm", P("QFN", 24, (4.0, 4.0), 0.5, True)),
    ("AMS_QFN-4-1EP_2x2mm_P0.95mm_EP0.7x1.6mm", P("QFN", 4, (2.0, 2.0), 0.95, True)),
    ("Infineon_MLPQ-48-1EP_7x7mm_P0.5mm_EP5.55x5.55mm",
     P("QFN", 48, (7.0, 7.0), 0.5, True)),
    ("ST_UQFN-6L_1.5x1.7mm_Pitch0.5mm", P("QFN", 6, (1.5, 1.7), 0.5, False)),
    ("DFN-8-1EP_3x2mm_P0.5mm_EP1.75x1.45mm", P("DFN", 8, (2.0, 3.0), 0.5, True)),
    ("TDFN-8-1EP_3x2mm_P0.5mm_EP1.3x1.4mm", P("DFN", 8, (2.0, 3.0), 0.5, True)),
    ("OnSemi_UDFN-8_1.2x1.8mm_P0.4mm", P("DFN", 8, (1.2, 1.8), 0.4, False)),
    ("WSON-8-1EP_3x3mm_P0.5mm_EP1.6x2.0mm", P("DFN", 8, (3.0, 3.0), 0.5, True)),
    ("X2SON-8_1.4x1mm_P0.35mm", P("DFN", 8, (1.0, 1.4), 0.35, False)),
    # Quad flat pack.
    ("TQFP-32_7x7mm_P0.8mm", P("TQFP", 32, (7.0, 7.0), 0.8, False)),
    ("LQFP-64-1EP_10x10mm_P0.5mm_EP5x5mm", P("LQFP", 64, (10.0, 10.0), 0.5, True)),
    # DIP: the W is row spacing, not the body.
    ("Package_DIP:DIP-8_W7.62mm", P("DIP", 8, None, None, False)),
    ("DIP-28_W7.62mm_Socket", P("DIP", 28, None, None, False)),
    ("DIP-8-N7_W7.62mm", P("DIP", 8, None, None, False)),
])
def test_kicad_families(name, info):
    assert parse_footprint(name) == info


@pytest.mark.parametrize("name", [
    None, "", "Lib:", "Module:Arduino_Nano", "Connector:Conn_01x04",
    "SOT-223x", "DRQFN-44", "Texas_R_PUQFN-N12",
])
def test_unrecognized_family(name):
    info = parse_footprint(name)
    assert info.family is None
    assert info.pins is None


def test_conflicting_fixed_pins_keep_family_only():
    # SOT-323 is the 3-pin SC-70; a "-5" suffix contradicts it.
    assert parse_footprint("SOT-323-5") == P("SC-70", None, None, None, False)


def test_library_name_cannot_match():
    assert parse_footprint("QFN-24_Lib:Custom") == NONE
    assert parse_footprint("SOIC:LDL212") == NONE


def test_ep_field_without_family():
    assert parse_footprint("Texas_RGV_S-PVQFN-N16_EP2.1x2.1mm") == P(ep=True)


def test_known():
    assert not NONE.known
    assert P(ep=False).known
    assert parse_footprint("SOT-23-5").known


# --------------------------------------------------------------------------- #
# DigiKey parameters.
# --------------------------------------------------------------------------- #

def dk(case=None, sdp=None, **extra):
    params = dict(extra)
    if case is not None:
        params["Package / Case"] = case
    if sdp is not None:
        params["Supplier Device Package"] = sdp
    return params


# The parts in the IC_PLAN.md evidence table, as DigiKey states them.
@pytest.mark.parametrize("params, info", [
    # LMC7101BYM5
    (dk("SC-74A, SOT-753", "SOT-23-5"), P("SOT-23", 5, None, None, False)),
    # AD8232ACPZ-R7
    (dk("20-WFQFN Exposed Pad, CSP", "20-LFCSP-WQ (4x4)"),
     P("QFN", 20, (4.0, 4.0), None, True)),
    # DRV8313RHHR
    (dk("36-VFQFN Exposed Pad", "36-VQFN (6x6)"), P("QFN", 36, (6.0, 6.0), None, True)),
    # STM32L552CCT6
    # The 0.5 mm pitch is JEDEC's for a 48-pin 7x7 mm LQFP.
    (dk("48-LQFP", "48-LQFP (7x7)"), P("LQFP", 48, (7.0, 7.0), 0.5, False)),
    # LDL212PV33R: a 2x2 mm DFN-6 is made in 0.5 and 0.65 mm, so no pitch.
    (dk("6-UFDFN Exposed Pad", "6-DFN (2x2)"), P("DFN", 6, (2.0, 2.0), None, True)),
    # LTC6992CS6-2
    (dk("SOT-23-6 Thin, TSOT-23-6", "TSOT-23-6"), P("TSOT-23", 6, None, None, False)),
    # ABX00027 (Arduino module): no package parameters.
    ({}, NONE),
    (None, NONE),
    (dk("Module"), NONE),
])
def test_evidence_table_digikey(params, info):
    assert parse_digikey_package(params) == info


@pytest.mark.parametrize("params, info", [
    (dk("24-VFQFN Exposed Pad", "24-QFN (4x4)"), P("QFN", 24, (4.0, 4.0), None, True)),
    (dk("TO-236-3, SC-59, SOT-23-3", "SOT-23-3"), P("SOT-23", 3, None, None, False)),
    (dk("SOT-23-6", "SC-74"), P("SOT-23", 6, None, None, False)),
    (dk("SC-74, SOT-457"), P("SOT-23", 6, None, None, False)),
    (dk("6-TSSOP, SC-88, SOT-363", "SC-88"),
     P("SC-70", 6, None, None, False, ("TSSOP",))),
    (dk("SC-70, SOT-323", "SC-70-3"), P("SC-70", 3, None, None, False)),
    (dk("SOT-563, SOT-666", "SOT-563"), P("SOT-563", 6, None, None, False)),
    (dk('8-SOIC (0.154", 3.90mm Width)', "8-SOIC"), P("SOIC", 8, None, 1.27, False)),
    # Package / Case lists two families; the Supplier Device Package picks one.
    (dk('8-TSSOP, 8-MSOP (0.118", 3.00mm Width)', "8-VSSOP"),
     P("MSOP", 8, None, None, False, ("TSSOP",))),
    (dk('8-TSSOP, 8-MSOP (0.118", 3.00mm Width) Exposed Pad', "8-MSOP-EP"),
     P("MSOP", 8, None, None, True, ("TSSOP",))),
    (dk('20-PowerTSSOP (0.173", 4.40mm Width)', "20-HTSSOP"),
     P("TSSOP", 20, None, None, True)),
    (dk("8-WDFN Exposed Pad", "8-WSON (2x2)"), P("DFN", 8, (2.0, 2.0), None, True)),
    (dk("8-UFDFN Exposed Pad", "8-DFN (3x2)"), P("DFN", 8, (2.0, 3.0), None, True)),
    (dk("32-LQFP", "32-TQFP (7x7)"),
     P("TQFP", 32, (7.0, 7.0), 0.8, False, ("LQFP",))),
    (dk('8-DIP (0.300", 7.62mm)', "8-PDIP"), P("DIP", 8, None, 2.54, False)),
    # Supplier Device Package alone: whether there is an exposed pad is unknown.
    (dk(sdp="24-QFN (4x4)"), P("QFN", 24, (4.0, 4.0), None, None)),
    # A pin-count parameter overrides the package's.
    (dk("48-LQFP", "48-LQFP (7x7)", **{"Number of Pins": "48"}),
     P("LQFP", 48, (7.0, 7.0), 0.5, False)),
    (dk(sdp="Custom", **{"Number of Pins": "30"}), P(None, 30, None, None, None)),
    # Unrecognized Supplier Device Package: Package / Case still decides.
    (dk("16-VFQFN Exposed Pad", "16-MLF® (3x3)"), P("QFN", 16, (3.0, 3.0), None, True)),
])
def test_digikey_strings(params, info):
    assert parse_digikey_package(params) == info


# Package / Case alone, or with an unrecognized Supplier Device Package: the
# specific family DigiKey lists after its generic "N-TSSOP" is the one read.
@pytest.mark.parametrize("params, info", [
    (dk("5-TSSOP, SC-70-5, SOT-353"), P("SC-70", 5, None, None, False, ("TSSOP",))),
    (dk("6-TSSOP, SC-88, SOT-363"), P("SC-70", 6, None, None, False, ("TSSOP",))),
    (dk("6-TSSOP, SC-88, SOT-363", "XYZ"), P("SC-70", 6, None, None, False, ("TSSOP",))),
    (dk('8-TSSOP, 8-MSOP (0.118", 3.00mm Width)'),
     P("MSOP", 8, None, None, False, ("TSSOP",))),
    # Only the generic piece: it is still read.
    (dk('14-TSSOP (0.173", 4.40mm Width)'), P("TSSOP", 14, None, None, False)),
    # A vendor code with the package in parentheses.
    (dk("6-TSSOP, SC-88, SOT-363", "DCK (SC70-6)"),
     P("SC-70", 6, None, None, False, ("TSSOP",))),
])
def test_digikey_specific_over_generic_tssop(params, info):
    assert parse_digikey_package(params) == info


# A Supplier Device Package without a pin suffix takes the count Package / Case
# states for the same family, not the family's 3-pin default.
@pytest.mark.parametrize("params, info", [
    (dk("SC-74A, SOT-753", "SOT-23"), P("SOT-23", 5, None, None, False)),
    (dk("SOT-23-5 Thin, TSOT-23-5", "SOT-23-THIN"), P("TSOT-23", 5, None, None, False)),
    (dk("SOT-23-6 Thin, TSOT-23-6", "TSOT-23"), P("TSOT-23", 6, None, None, False)),
    # Nothing states a count: the default.
    (dk(sdp="SOT-23"), P("SOT-23", 3, None, None, None)),
    (dk("TO-236-3, SC-59, SOT-23-3", "SOT-23"), P("SOT-23", 3, None, None, False)),
])
def test_digikey_stated_pins_beat_default(params, info):
    assert parse_digikey_package(params) == info


def test_digikey_thin_case_makes_sdp_tsot():
    info = parse_digikey_package(dk("SOT-23-6 Thin, TSOT-23-6", "SOT-23-6"))
    assert info == P("TSOT-23", 6, None, None, False, ("SOT-23",))
    assert info.same_family(parse_footprint("TSOT-23-6"))
    assert info.same_family(parse_footprint("SOT-23-6"))


# SOT-223 / SOT-89: the lead count, not counting the tab, on both sides.
@pytest.mark.parametrize("params, info", [
    (dk("TO-261-4, TO-261AA", "SOT-223-4"), P("SOT-223", 3, None, None, False)),
    (dk("TO-261-4, TO-261AA", "SOT-223"), P("SOT-223", 3, None, None, False)),
    (dk("TO-261-4, TO-261AA"), P("SOT-223", 3, None, None, False)),
    (dk(sdp="SOT-223-3"), P("SOT-223", 3, None, None, None)),
    (dk("TO-243AA", "SOT-89-3"), P("SOT-89", 3, None, None, False)),
    (dk("TO-243AA"), P("SOT-89", 3, None, None, False)),
])
def test_digikey_sot223_tab_not_counted(params, info):
    assert parse_digikey_package(params) == info


def test_same_family():
    a = parse_digikey_package(dk("5-TSSOP, SC-70-5, SOT-353"))
    assert a.same_family(parse_footprint("SOT-353_SC-70-5"))
    assert a.same_family(parse_footprint("TSSOP-8_3x3mm_P0.65mm"))
    assert not a.same_family(parse_footprint("SOIC-8_3.9x4.9mm_P1.27mm"))
    msop = parse_digikey_package(dk('8-TSSOP, 8-MSOP (0.118", 3.00mm Width)'))
    assert msop.same_family(parse_footprint("MSOP-8_3x3mm_P0.65mm"))
    assert msop.same_family(parse_footprint("TSSOP-8_3x3mm_P0.65mm"))
    assert not NONE.same_family(NONE)
    assert NONE.families == ()


def test_kicad_and_digikey_agree_on_evidence_parts():
    pairs = [
        ("SOT-23-5", dk("SC-74A, SOT-753", "SOT-23-5")),
        ("TSOT-23-6", dk("SOT-23-6 Thin, TSOT-23-6", "TSOT-23-6")),
        ("LQFP-48_7x7mm_P0.5mm", dk("48-LQFP", "48-LQFP (7x7)")),
        ("DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm", dk("6-UFDFN Exposed Pad", "6-DFN (2x2)")),
        ("SOT-223-3_TabPin2", dk("TO-261-4, TO-261AA", "SOT-223-4")),
        ("SOT-89-3", dk("TO-243AA", "SOT-89-3")),
        ("SOT-353_SC-70-5", dk("5-TSSOP, SC-70-5, SOT-353")),
        ("SOT-363_SC-70-6", dk("6-TSSOP, SC-88, SOT-363", "DCK (SC70-6)")),
    ]
    for fp, params in pairs:
        a, b = parse_footprint(fp), parse_digikey_package(params)
        assert (a.family, a.pins, a.body_mm, a.ep) == (b.family, b.pins, b.body_mm, b.ep)


# --------------------------------------------------------------------------- #
# Family normalization.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, family", [
    ("QFN", "QFN"), ("VQFN", "QFN"), ("WQFN", "QFN"), ("VFQFN", "QFN"),
    ("UFQFN", "QFN"), ("HVQFN", "QFN"), ("LFCSP-WQ", "QFN"), ("lfcsp", "QFN"),
    ("DFN", "DFN"), ("UFDFN", "DFN"), ("WSON", "DFN"), ("SON", "DFN"),
    ("SOT-23", "SOT-23"), ("SOT23", "SOT-23"), ("SC-74A", "SOT-23"),
    ("SOT-753", "SOT-23"), ("SC-74", "SOT-23"), ("TSOT-23", "TSOT-23"),
    ("SOT-363", "SC-70"), ("SC-88", "SC-70"), ("SC70", "SC-70"),
    ("SO", "SOIC"), ("SOIC", "SOIC"), ("VSSOP", "MSOP"), ("HTSSOP", "TSSOP"),
    ("PDIP", "DIP"), ("SOT-223", "SOT-223"), ("TO-261AA", "SOT-223"),
    ("TO-243AA", "SOT-89"),
    ("DRQFN", None), ("SOP", None), ("SOT-2233", None), ("", None), (None, None),
])
def test_normalize_family(text, family):
    assert normalize_family(text) == family


def test_families_are_canonical():
    for fam in FAMILIES:
        assert normalize_family(fam) == fam


# --------------------------------------------------------------------------- #
# Pitch fixed by the package standard.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("family, pins, body, pitch", [
    ("SOIC", 8, None, 1.27),
    ("SOIC", 16, None, 1.27),
    ("DIP", 30, None, 2.54),
    ("LQFP", 48, (7.0, 7.0), 0.5),
    ("TQFP", 32, (7.0, 7.0), 0.8),
    ("LQFP", 64, (10.0, 10.0), 0.5),
    ("LQFP", 100, (14.0, 14.0), 0.5),
    # Not in the table, or not square: not fixed.
    ("LQFP", 52, (10.0, 10.0), None),
    ("LQFP", 48, None, None),
    # QFN, DFN and leaded small-outline packages are made in several pitches
    # for one body (a 6-DFN (2x2) in 0.5 mm and 0.65 mm; DigiKey's string for
    # the LDL212 does not say which).
    ("DFN", 6, (2.0, 2.0), None),
    ("QFN", 24, (4.0, 4.0), None),
    ("TSSOP", 20, None, None),
    (None, None, None, None),
])
def test_standard_pitch(family, pins, body, pitch):
    assert standard_pitch(family, pins, body) == pitch


def test_ldl212_digikey_package_states_no_pitch():
    # LDL212PV33R as DigiKey lists it (live check, 2026-10-30).
    info = parse_digikey_package(dk("6-WFDFN Exposed Pad", "6-DFN (2x2)"))
    assert info == P("DFN", 6, (2.0, 2.0), None, True)
