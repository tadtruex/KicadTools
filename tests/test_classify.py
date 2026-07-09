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
