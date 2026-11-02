import pytest

from kicadtools.intent import (
    ConnectorIntent,
    ElectricalIntent,
    LedIntent,
    ResistorIntent,
    SpecificPartIntent,
    TransistorIntent,
)


def _base(**extra):
    return {"Reference": "J1", "dnp": False, "in_bom": True, **extra}


def _led(**extra):
    return {"Reference": "D1", "dnp": False, "in_bom": True,
            "Intent:Type": "led", **extra}


def test_led_registered_under_intent_type():
    d = ElectricalIntent.from_dict(_led())
    assert isinstance(d, LedIntent)
    assert d.classification_note is None
    assert d.description() == "D1: LED"


def test_led_parses_colour_and_wavelength_from_description():
    d = ElectricalIntent.from_dict(_led(Description="Red LED, 630nm"))
    assert d.color == "red"
    assert d.wavelength == "630nm"
    assert d.description() == "D1: red LED (630nm)"


def test_led_colour_survives_underscore_in_value():
    # "LED_Green" — the underscore mustn't hide the colour token.
    d = ElectricalIntent.from_dict(_led(Value="LED_Green"))
    assert d.color == "green"


def test_led_rgb_value():
    d = ElectricalIntent.from_dict(_led(Reference="U5", Value="1515 rgb led"))
    assert d.color == "rgb"
    assert d.description() == "U5: rgb LED"


def _r(**extra):
    return {"Reference": "R1", "dnp": False, "in_bom": True,
            "Intent:Type": "resistor", **extra}


def test_resistor_registered_under_intent_type():
    r = ElectricalIntent.from_dict(_r(Value="10k"))
    assert isinstance(r, ResistorIntent)
    assert r.classification_note is None
    assert r.power_min is None
    assert r.description() == "R1: 10k resistor"


def test_resistor_reads_power_min():
    r = ElectricalIntent.from_dict(_r(Value="10k", **{"Power:Min": "0.25W"}))
    assert r.power_min == "0.25W"
    assert r.description() == "R1: 10k 0.25W resistor"


def test_resistor_power_min_and_tolerance_together():
    r = ElectricalIntent.from_dict(
        _r(Value="10k", **{"Tolerance:Preferred": "1%", "Power:Min": "0.1W"})
    )
    assert r.tolerance_preferred == ["1%"]
    assert r.power_min == "0.1W"
    assert r.description() == "R1: 10k ±1% 0.1W resistor"


def test_resistor_without_power_min_still_classifies():
    # Power:Min is optional — a resistor specifying only Value must stay typed.
    r = ElectricalIntent.from_dict(_r(Value="4k7"))
    assert isinstance(r, ResistorIntent)
    assert r.power_min is None


def _q(**extra):
    return {"Reference": "Q1", "dnp": False, "in_bom": True,
            "Intent:Type": "transistor", **extra}


def test_transistor_registered_under_intent_type():
    q = ElectricalIntent.from_dict(_q())
    assert isinstance(q, TransistorIntent)
    assert q.classification_note is None
    assert q.description() == "Q1: transistor"


def test_transistor_parses_channel_and_family():
    q = ElectricalIntent.from_dict(_q(Description="-12A Id, -40V Vds, P-Channel MOSFET"))
    assert q.device == "MOSFET"
    assert q.polarity == "P-channel"
    assert q.description() == "Q1: P-channel MOSFET"


def test_transistor_npn_implies_bjt():
    q = ElectricalIntent.from_dict(_q(Description="NPN general purpose"))
    assert q.device == "BJT"
    assert q.polarity == "NPN"
    assert q.description() == "Q1: NPN BJT"


def test_transistor_bare_nmos_implies_fet():
    # NMOS is a MOS field-effect device even without the word FET/MOSFET.
    q = ElectricalIntent.from_dict(_q(Description="NMOS 50V"))
    assert q.polarity == "N-channel"
    assert q.device == "FET"


def test_transistor_array_from_value():
    q = ElectricalIntent.from_dict(_q(Reference="U3", Value="ULN2003 transistor array"))
    assert q.device == "transistor array"
    assert q.description() == "U3: transistor array"


def test_connector_registered_under_intent_type():
    c = ElectricalIntent.from_dict(_base(**{"Intent:Type": "connector"}))
    assert isinstance(c, ConnectorIntent)
    assert c.classification_note is None


def test_connector_parses_positions_and_pitch_from_footprint():
    c = ElectricalIntent.from_dict(
        _base(
            **{
                "Intent:Type": "connector",
                "Value": "QwiicH",
                "Footprint": "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
            }
        )
    )
    assert isinstance(c, ConnectorIntent)
    assert c.positions == 4
    assert c.pitch == "1.00mm"
    assert c.description() == "J1: QwiicH connector, 4-pin, 1.00mm pitch"


def test_connector_2xN_footprint_counts_both_rows():
    c = ElectricalIntent.from_dict(
        _base(**{"Intent:Type": "connector", "Footprint": "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"})
    )
    assert c.positions == 6
    assert c.pitch == "2.54mm"


def test_connector_without_footprint_hints_is_still_valid():
    c = ElectricalIntent.from_dict(
        _base(**{"Intent:Type": "connector", "Footprint": "Connector_USB:USB_C_Receptacle_Amphenol_12401948E412A"})
    )
    assert isinstance(c, ConnectorIntent)
    assert c.positions is None
    assert c.pitch is None
    assert c.description() == "J1: connector"


def _specific(**extra):
    return {"Reference": "U1", "dnp": False, "in_bom": True,
            "Intent:Type": "specificpart", **extra}


@pytest.mark.parametrize(
    "key", ["DigiKeyPN", "DigikeyPN", "Digi-Key_PN", "DigiKey PN"]
)
def test_specific_part_reads_any_digikey_spelling(key):
    # The latent mismatch fix: SpecificPartIntent honours every DigiKey spelling,
    # not just the one canonical key it used to hard-code.
    c = ElectricalIntent.from_dict(_specific(**{key: "296-1234-5-ND"}))
    assert isinstance(c, SpecificPartIntent)
    assert c.digikey_pn == "296-1234-5-ND"


def test_specific_part_reads_manufacturer_pn_as_mpn():
    c = ElectricalIntent.from_dict(_specific(Manufacturer_PN="CL10B472KB8NNNC"))
    assert isinstance(c, SpecificPartIntent)
    assert c.mpn == "CL10B472KB8NNNC"


def test_specific_part_with_only_empty_variants_is_not_classified():
    # No usable part number -> falls back to a plain, unclassified intent.
    c = ElectricalIntent.from_dict(_specific(DigikeyPN=""))
    assert type(c) is ElectricalIntent


def test_capacitor_tolerance_preferred_parsed_and_described():
    from kicadtools.intent import CapacitorIntent

    c = ElectricalIntent.from_dict({
        "Reference": "C1", "dnp": False, "in_bom": True,
        "Intent:Type": "capacitor", "Value": "100nF",
        "Voltage:Min": "25V", "Tolerance:Preferred": "5%, 10%",
    })
    assert isinstance(c, CapacitorIntent)
    assert c.tolerance_preferred == ["5%", "10%"]
    assert c.description() == "C1: 100nF ±5% capacitor, min-V 25V"

    bare = ElectricalIntent.from_dict({
        "Reference": "C2", "dnp": False, "in_bom": True,
        "Intent:Type": "capacitor", "Value": "1uF",
    })
    assert bare.tolerance_preferred == []
    assert bare.description() == "C2: 1uF capacitor"


def test_capacitor_empty_tolerance_preferred_is_empty_list():
    c = ElectricalIntent.from_dict({
        "Reference": "C3", "dnp": False, "in_bom": True,
        "Intent:Type": "capacitor", "Value": "1nF", "Tolerance:Preferred": " , ",
    })
    assert c.tolerance_preferred == []


def _ic(**extra):
    return {"Reference": "U1", "dnp": False, "in_bom": True,
            "Intent:Type": "ic", **extra}


def test_ic_registered_under_intent_type():
    from kicadtools.intent import IcIntent

    u = ElectricalIntent.from_dict(_ic(Value="LMC7101BYM5"))
    assert isinstance(u, IcIntent)
    assert u.classification_note is None
    assert u.value == "LMC7101BYM5"
    assert u.mpn is None and u.digikey_pn is None and u.lcsc is None
    assert u.description() == "U1: LMC7101BYM5 IC"


def test_ic_intent_type_is_case_insensitive():
    from kicadtools.intent import IcIntent

    assert isinstance(ElectricalIntent.from_dict(_ic(**{"Intent:Type": "IC"})), IcIntent)


def test_ic_without_part_number_or_value_still_builds():
    # A missing part number is reported by the requirement, not the intent.
    from kicadtools.intent import IcIntent

    u = ElectricalIntent.from_dict(_ic())
    assert isinstance(u, IcIntent)
    assert u.value is None
    assert u.description() == "U1: IC"


def test_ic_blank_value_is_none():
    u = ElectricalIntent.from_dict(_ic(Value="  "))
    assert u.value is None


@pytest.mark.parametrize(
    "props, field, expected",
    [
        ({"DigiKey PN": "497-19548-2-ND"}, "digikey_pn", "497-19548-2-ND"),
        ({"Digi-Key_PN": "497-19548-2-ND"}, "digikey_pn", "497-19548-2-ND"),
        ({"DigikeyPartNumber": "497-19548-2-ND"}, "digikey_pn", "497-19548-2-ND"),
        ({"Manufacturer_PN": "LDL212PV33R"}, "mpn", "LDL212PV33R"),
        ({"MPN": "LDL212PV33R"}, "mpn", "LDL212PV33R"),
        ({"JLCPCB": "C165154"}, "lcsc", "C165154"),
        ({"LCSC Part": "C165154"}, "lcsc", "C165154"),
        ({"Mfr": "STMicroelectronics"}, "manufacturer", "STMicroelectronics"),
    ],
)
def test_ic_reads_part_numbers_under_any_alias(props, field, expected):
    u = ElectricalIntent.from_dict(_ic(**props))
    assert getattr(u, field) == expected


def test_ic_empty_alias_does_not_hide_populated_one():
    u = ElectricalIntent.from_dict(
        _ic(DigikeyPN="", **{"DigiKey PN": "296-44591-2-ND"})
    )
    assert u.digikey_pn == "296-44591-2-ND"


def test_ic_description_prefers_mpn_and_lists_supplier_numbers():
    u = ElectricalIntent.from_dict(_ic(
        Value="MotorControl_STM32L552CCT6_V2", MPN="STM32L552CCT6",
        DigiKeyPN="497-STM32L552CCT6-ND", LCSC="C123",
    ))
    assert u.description() == (
        "U1: STM32L552CCT6 IC, DigiKey 497-STM32L552CCT6-ND, LCSC C123"
    )
