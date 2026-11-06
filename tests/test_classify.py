import pytest

from kicadtools.classify import guess_intent_type


def test_footprint_library_wins():
    g = guess_intent_type({"Footprint": "Resistor_SMD:R_0603_1608Metric"})
    assert g is not None
    assert g.intent_type == "resistor"
    assert g.strategy == "footprint"


def test_footprint_led_beats_diode_refdes():
    # A part with a D refdes but an LED footprint is an LED, not a diode:
    # footprint (higher priority) should win.
    g = guess_intent_type(
        {"Reference": "D4", "Footprint": "LED_SMD:LED_0603_1608Metric"}
    )
    assert g.intent_type == "led"
    assert g.strategy == "footprint"


def test_description_used_when_footprint_uninformative():
    # Custom footprint library gives no signal; fall through to the description.
    g = guess_intent_type(
        {"Footprint": "ECG_Footprints:MPD_BC2032", "Description": "Schottky diode, low leakage"}
    )
    assert g.intent_type == "diode"
    assert g.strategy == "description"


def test_ambiguous_description_is_ignored():
    # Two type words -> not confident -> don't guess from the description.
    g = guess_intent_type({"Description": "resistor and capacitor network"})
    assert g is None


def test_refdes_is_last_resort():
    g = guess_intent_type({"Reference": "BT1", "Footprint": "ECG_Footprints:MPD_BC2032"})
    assert g.intent_type == "battery"
    assert g.strategy == "refdes"


def test_nothing_to_go_on_returns_none():
    assert guess_intent_type({"Reference": "Z9"}) is None


def test_led_in_diode_footprint_is_caught_by_description():
    # An LED drawn with a plain diode footprint: the footprint says "diode", but
    # the description names it, so it's promoted to an LED.
    g = guess_intent_type(
        {"Reference": "D3", "Footprint": "Diode_SMD:D_0603_1608Metric",
         "Description": "Red LED, 630nm"}
    )
    assert g.intent_type == "led"
    assert g.strategy == "footprint"


def test_led_colour_in_diode_footprint_is_enough():
    # No "LED" word — just a colour — but the diode footprint makes it trustworthy.
    g = guess_intent_type(
        {"Footprint": "Diode_SMD:D_0603_1608Metric", "Description": "Green indicator"}
    )
    assert g.intent_type == "led"


def test_plain_diode_stays_a_diode():
    g = guess_intent_type(
        {"Footprint": "Diode_SMD:D_0603_1608Metric", "Description": "Schottky diode"}
    )
    assert g.intent_type == "diode"


def test_led_signal_in_value_beats_refdes():
    # Custom (uninformative) footprint, U refdes would say "ic" — but the Value
    # text carries the LED signal, which the description strategy runs first.
    g = guess_intent_type(
        {"Reference": "U5", "Footprint": "StockedComponents:4492_ADA",
         "Value": "1515 rgb led"}
    )
    assert g.intent_type == "led"
    assert g.strategy == "description"


def test_bare_colour_without_diode_context_is_not_an_led():
    # A colour alone (no diode footprint, no "LED" word) must NOT be read as an
    # LED — falls through to the refdes.
    g = guess_intent_type({"Reference": "D9", "Description": "green"})
    assert g.intent_type == "diode"
    assert g.strategy == "refdes"


@pytest.mark.parametrize(
    "description",
    [
        "-12A Id, -40V Vds, P-Channel MOSFET",
        "40V 12A Trench FET",
        "NPN general purpose transistor",
        "PNP small signal",
        "N-Channel 50V NMOS",
        "Darlington transistor array",
        "IGBT 600V",
    ],
)
def test_transistor_guessed_from_description(description):
    # The footprint (a shared TO/SOT package) is uninformative, so the
    # description is what identifies these as transistors.
    g = guess_intent_type(
        {"Reference": "Q9", "Footprint": "Package_TO_SOT_SMD:SOT-23",
         "Description": description}
    )
    assert g.intent_type == "transistor"
    assert g.strategy == "description"


def test_transistor_bare_q_refdes_is_last_resort():
    g = guess_intent_type({"Reference": "Q7"})
    assert g.intent_type == "transistor"
    assert g.strategy == "refdes"


def test_light_emitting_diode_description_is_an_led():
    # LabBoard D1-D4: KiCad's LED symbol, no footprint yet. "Light emitting
    # diode" names one type: the diode keyword does not make it ambiguous.
    g = guess_intent_type({"Reference": "D1", "Value": "Green",
                           "Description": "Light emitting diode"})
    assert g.intent_type == "led"
    assert g.strategy == "description"
