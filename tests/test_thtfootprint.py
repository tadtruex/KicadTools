"""Tests for footprint signatures (kicadtools.thtfootprint, THT_PLAN.md step 3).
The names are the through-hole footprints on the boards plus samples of each
KiCad standard library family."""
import pytest

from kicadtools.thtfootprint import (
    ANGLED,
    CAPACITOR,
    HEADER,
    HORIZONTAL,
    NAMED_PART,
    NO_PART,
    OTHER,
    RESISTOR,
    SOCKET,
    TERMINAL_BLOCK,
    TEST_POINT,
    VERTICAL,
    parse_signature,
)

# --- headers and sockets -------------------------------------------------------


def test_single_row_header():
    s = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
    assert (s.family, s.rows, s.per_row, s.positions, s.pitch_mm, s.orientation, s.smd) == (
        HEADER, 1, 4, 4, 2.54, VERTICAL, False,
    )


def test_double_row_horizontal_header():
    s = parse_signature("Connector_PinHeader_2.54mm:PinHeader_2x12_P2.54mm_Horizontal")
    assert (s.rows, s.per_row, s.positions, s.orientation) == (2, 12, 24, HORIZONTAL)


def test_socket_and_other_pitch():
    s = parse_signature("Connector_PinSocket_2.00mm:PinSocket_1x10_P2.00mm_Vertical")
    assert (s.family, s.positions, s.pitch_mm) == (SOCKET, 10, 2.0)


def test_smd_variant():
    s = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x11_P2.54mm_Vertical_SMD_Pin1Right")
    assert (s.family, s.positions, s.smd) == (HEADER, 11, True)


# --- terminal blocks -------------------------------------------------------------


def test_phoenix_terminal_block():
    s = parse_signature(
        "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal"
    )
    assert (s.family, s.maker, s.positions, s.pitch_mm, s.orientation) == (
        TERMINAL_BLOCK, "Phoenix", 2, 5.08, HORIZONTAL,
    )


def test_wago_angled():
    s = parse_signature("TerminalBlock_WAGO:TerminalBlock_WAGO_236-408_1x08_P5.00mm_45Degree")
    assert (s.maker, s.positions, s.orientation) == ("WAGO", 8, ANGLED)


def test_terminal_block_positions_from_pads():
    # The boards' digikey-footprints terminal block names no position count.
    s = parse_signature("digikey-footprints:Terminal_Block_D1.3mm_P5mm")
    assert (s.family, s.maker, s.positions, s.pitch_mm) == (TERMINAL_BLOCK, "", None, 5.0)
    s = parse_signature("digikey-footprints:Terminal_Block_D1.3mm_P5mm", pad_count=3)
    assert s.positions == 3


def test_pad_count_does_not_override_name():
    s = parse_signature(
        "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
        pad_count=4,
    )
    assert s.positions == 2


# --- passives ------------------------------------------------------------------


def test_radial_electrolytic():
    s = parse_signature("Capacitor_THT:CP_Radial_D6.3mm_P2.50mm")
    assert (s.family, s.style, s.polarized, s.diameter_mm, s.lead_spacing_mm) == (
        CAPACITOR, "radial", True, 6.3, (2.5,),
    )


def test_ceramic_radial_with_height_and_disc():
    s = parse_signature("Capacitor_THT:C_Radial_D6.3mm_H11.0mm_P2.50mm")
    assert (s.style, s.polarized, s.diameter_mm, s.height_mm, s.pitch_mm) == (
        "radial", False, 6.3, 11.0, 2.5,
    )
    s = parse_signature("Capacitor_THT:C_Disc_D16.0mm_W5.0mm_P10.00mm")
    assert (s.style, s.diameter_mm, s.width_mm, s.pitch_mm) == ("disc", 16.0, 5.0, 10.0)


def test_film_box_two_spacings():
    s = parse_signature("Capacitor_THT:C_Rect_L7.2mm_W8.5mm_P5.00mm_P7.50mm")
    assert (s.style, s.length_mm, s.width_mm, s.lead_spacing_mm) == ("rect", 7.2, 8.5, (5.0, 7.5))


def test_tantalum_and_snap_in():
    assert parse_signature("Capacitor_THT:CP_Radial_Tantal_D10.5mm_P5.00mm").style == "radial tantalum"
    assert parse_signature("Capacitor_THT:CP_Radial_D30.0mm_P10.00mm_SnapIn").style == "radial snap-in"


def test_axial_resistor():
    s = parse_signature("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
    assert (s.family, s.style, s.length_mm, s.diameter_mm, s.pitch_mm, s.orientation) == (
        RESISTOR, "axial", 6.3, 2.5, 10.16, HORIZONTAL,
    )


def test_power_resistor_and_array():
    s = parse_signature("Resistor_THT:R_Axial_Power_L50.0mm_W9.0mm_P60.96mm")
    assert (s.style, s.length_mm, s.width_mm, s.pitch_mm) == ("axial power", 50.0, 9.0, 60.96)
    s = parse_signature("Resistor_THT:R_Array_SIP8")
    assert (s.style, s.positions, s.pitch_mm) == ("sip", 8, None)


# --- footprints that name a part ---------------------------------------------------


@pytest.mark.parametrize(
    "lib_id, maker, mpn",
    [
        ("Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal", "JST", "S3B-PH-K"),
        ("digikey-footprints:CUI_PJ-002B", "CUI", "PJ-002B"),
        ("Connector_BarrelJack:BarrelJack_CUI_PJ-102AH_Horizontal", "CUI", "PJ-102AH"),
        ("ECG_Footprints:MPD_BC2032-E2", "MPD", "BC2032-E2"),
        ("MountingHole:HARWIN_H9051-01", "Harwin", "H9051-01"),
        ("Connector:CalTest_CT3151", "CalTest", "CT3151"),
        ("Potentiometer_THT:Potentiometer_Bourns_3386P_Vertical", "Bourns", "3386P"),
        ("SAMTEC_USB-A-S-X-X-TH", "Samtec", "USB-A-S-X-X-TH"),
        ("ECG_Footprints:691137710002", "", "691137710002"),
        ("ECG_Footprints:SW_SLW-118562-4A-N-D", "", "SLW-118562-4A-N-D"),
    ],
)
def test_named_part(lib_id, maker, mpn):
    s = parse_signature(lib_id)
    assert (s.family, s.maker, s.mpn) == (NAMED_PART, maker, mpn)


def test_named_part_keeps_geometry():
    s = parse_signature("Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal")
    assert (s.positions, s.pitch_mm, s.orientation) == (3, 2.0, HORIZONTAL)


# --- no part, test points, other ------------------------------------------------------


@pytest.mark.parametrize(
    "lib_id, what",
    [
        ("Passives:SolderWirePad_single_0-5mmDrill", "solder wire pad"),
        ("Connector_Wire:SolderWire-0.5sqmm_1x02_P4.6mm_D0.9mm_OD2.1mm", "solder wire pad"),
        ("MountingHole:MountingHole_2.2mm_M2_Pad", "mounting hole"),
        ("Fiducial:Fiducial_1mm_Mask2mm", "fiducial"),
        ("TestPoint:TestPoint_THTPad_D1.0mm_Drill0.5mm", "test pad"),
        ("TestPoint:TestPoint_Pad_D1.0mm", "test pad"),
        ("TestPoint:TestPoint_Plated_Hole_D2.0mm", "test pad"),
        ("Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm", "solder jumper"),
        ("NetTie:NetTie-2_SMD_Pad0.5mm", "net tie"),
    ],
)
def test_no_part(lib_id, what):
    s = parse_signature(lib_id)
    assert (s.family, s.what) == (NO_PART, what)


def test_loop_test_point_is_a_part():
    s = parse_signature("TestPoint:TestPoint_Loop_D1.80mm_Drill1.0mm_Beaded")
    assert (s.family, s.diameter_mm) == (TEST_POINT, 1.8)


def test_keystone_test_point_is_named():
    s = parse_signature("TestPoint:TestPoint_Keystone_5000-5004_Miniature")
    assert (s.family, s.maker, s.mpn) == (NAMED_PART, "Keystone", "5000-5004")


@pytest.mark.parametrize(
    "lib_id",
    [
        "Button_Switch_THT:SW_PUSH_6mm",
        "Crystal:Crystal_HC49-U_Vertical",
        "Connector_BarrelJack:BarrelJack_Horizontal",
        "Connector_Pin:Pin_D1.0mm_L10.0mm",
        "",
        "NoNickname",
    ],
)
def test_other(lib_id):
    assert parse_signature(lib_id).family == OTHER


@pytest.mark.parametrize(
    "lib_id",
    [
        "Package_TO_SOT_THT:TO-8-2",
        "Package_TO_SOT_THT:TO-92",
        "Package_DIP:Toshiba_11-7A9",
        "Package_SIP:PowerIntegrations_eSIP-7C",
        "OptoDevice:LaserDiode_TO56-3",
        "LED_THT:LED_D5.0mm-3",
        "Package_LCC:PLCC-20_THT-Socket",
        "Buzzer_Beeper:Buzzer_15x7.5RM7.6",
        "Crystal:Crystal_HC35-U",
        "Sensor_Audio:Infineon_PG-LLGA-5-1",
        "Button_Switch_THT:SW_PUSH-12mm",
    ],
)
def test_package_names_are_not_named_parts(lib_id):
    assert parse_signature(lib_id).family == OTHER


@pytest.mark.parametrize(
    "lib_id, mpn",
    [
        ("Display_7Segment:MAN73A", "MAN73A"),
        ("Sensor_Current:LEM_HX15-NP", "HX15-NP"),
        ("Display:HDSP-4832", "HDSP-4832"),
    ],
)
def test_short_part_names_stay_named(lib_id, mpn):
    s = parse_signature(lib_id)
    assert (s.family, s.maker, s.mpn) == (NAMED_PART, "", mpn)


def test_mounting_hole_library_part_is_not_a_hole():
    # Same library as the holes, but a Harwin part.
    assert parse_signature("MountingHole:HARWIN_H9051-01").family == NAMED_PART
