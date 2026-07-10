import pytest

from kicadtools.intent import (
    ConnectorIntent,
    ElectricalIntent,
    LedIntent,
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
