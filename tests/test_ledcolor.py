"""Tests for kicadtools.ledcolor: colour names, bands, LED kind, supplier
values. The strings are the ones on the boards and in the supplier data
recorded in LED_PLAN.md."""
from __future__ import annotations

import pytest

from kicadtools import ledcolor
from kicadtools.ledcolor import (
    MULTICOLOR,
    PLAIN,
    SMART,
    band_for,
    band_text,
    color,
    colors_of,
    in_band,
    is_led_footprint,
    led_kind,
    parse_color,
    parse_colors,
    parse_labels,
    parse_mcd,
    parse_nm,
    parse_vf,
)


# --- colour names -------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("LED_Green", ("green",)),
    ("LED_Red", ("red",)),
    ("Red 630nm", ("red",)),
    ("Emerald Green", ("green",)),
    ("Yellow Green", ("yellow green",)),
    ("yellow-green", ("yellow green",)),
    ("Warm White 0603", ("warm white",)),
    ("blue green", ("cyan",)),
    ("Orange Red, Yellow Green", ("orange", "red", "yellow green")),
    ("LED_RG_LTSTC155GEKT", ()),          # letters need the LED context flag
    ("1515 rgb led", ()),
    ("LED", ()),
    ("", ()),
    (None, ()),
])
def test_parse_colors(text, expected):
    assert parse_colors(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("G", ("green",)),
    ("R", ("red",)),
    ("A", ("amber",)),
    ("LED_RG_LTSTC155GEKT", ("red", "green")),
    ("RGB", ("red", "green", "blue")),
    ("LED", ()),
    ("GG", ()),            # repeated letter: not a colour code
    ("g", ()),             # lower case: not a colour code
    ("0.1W", ()),
])
def test_parse_colors_letters(text, expected):
    assert parse_colors(text, letters=True) == expected


def test_parse_color_needs_exactly_one():
    assert parse_color("LED_Green") == "green"
    assert parse_color("red/green") is None
    assert parse_color("LED") is None


@pytest.mark.parametrize("name, expected", [
    ("Green", "green"), ("emerald green", "green"), ("UV", "ultraviolet"),
    ("purple", "ultraviolet"), ("IR", "infrared"), ("Cool White", "cool white"),
    ("lime", "yellow green"), ("magenta", None), ("", None),
])
def test_color_lookup(name, expected):
    c = color(name)
    assert (c.name if c else None) == expected


def test_every_spelling_names_a_colour():
    for spelling, name in ledcolor.SPELLINGS.items():
        assert name in ledcolor.COLORS, spelling
    for name in ledcolor.LETTERS.values():
        assert name in ledcolor.COLORS


@pytest.mark.parametrize("fp, expected", [
    ("LED_SMD:LED_0603_1608Metric", True),
    ("LEDs:LED_0603", True),
    ("LED_THT:LED_D5.0mm", True),
    ("Resistor_SMD:R_0603_1608Metric", False),
    ("StockedComponents:LTST-C155GEKT", False),
    ("", False),
])
def test_is_led_footprint(fp, expected):
    assert is_led_footprint(fp) is expected


def test_colors_of_reads_letters_only_in_led_context():
    assert colors_of({"Value": "G",
                      "Footprint": "LED_SMD:LED_0603_1608Metric"}) == ("green",)
    assert colors_of({"Value": "G",
                      "Footprint": "Resistor_SMD:R_0603"}) == ()
    # The word LED in the Value is context enough.
    assert colors_of({"Value": "LED_RG_LTSTC155GEKT",
                      "Footprint": "StockedComponents:LTST-C155GEKT"}) \
        == ("red", "green")


# --- kind ---------------------------------------------------------------- #

@pytest.mark.parametrize("props, expected", [
    # Board symbols (LED_PLAN.md, "Evidence from the boards").
    ({"Reference": "D1", "Value": "LED_Green",
      "Footprint": "LEDs:LED_0603"}, PLAIN),
    ({"Reference": "D1", "Value": "LED_Red",
      "Footprint": "LED_SMD:LED_0603_1608Metric"}, PLAIN),
    ({"Reference": "D3", "Value": "G",
      "Footprint": "LED_SMD:LED_0603_1608Metric"}, PLAIN),
    ({"Reference": "D1", "Value": "LED_RG_LTSTC155GEKT",
      "Footprint": "StockedComponents:LTST-C155GEKT"}, MULTICOLOR),
    # An RGB LED with no driver name is multicolour, whatever its reference.
    ({"Reference": "U5", "Value": "1515 rgb led",
      "Footprint": "StockedComponents:4492_ADA"}, MULTICOLOR),
    # Others.
    ({"Reference": "D2", "Value": "LED", "Footprint": "LED_SMD:LED_0805"},
     PLAIN),
    ({"Reference": "D2", "Value": "RGB LED", "Footprint": "LED_SMD:LED_RGB_5050"},
     MULTICOLOR),
    ({"Reference": "D2", "Value": "WS2812B", "Footprint": "LED_SMD:LED_WS2812B"},
     SMART),
    ({"Reference": "D2", "Value": "SK6812MINI"}, SMART),
    ({"Reference": "D2", "Value": "LED", "Description": "addressable RGB LED"},
     SMART),
    ({"Reference": "D2", "Value": "Bicolor LED"}, MULTICOLOR),
    ({"Reference": "D2", "Value": "LED_Red_Green"}, MULTICOLOR),
])
def test_led_kind(props, expected):
    assert led_kind(props) == expected


# --- supplier values ----------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("625nm", (625.0,)),
    ("569nm、621nm", (569.0, 621.0)),
    ("520nm~530nm", (525.0,)),
    ("620-630nm", (625.0,)),
    ("525 nm", (525.0,)),
    ("-", ()),
    ("", ()),
    (None, ()),
])
def test_parse_nm(text, expected):
    assert parse_nm(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("250mcd", (250.0,)),
    ("1.2cd", (1200.0,)),
    ("100mcd~200mcd", (100.0,)),
    ("45mcd、80mcd", (45.0, 80.0)),
    ("12lm", ()),
])
def test_parse_mcd(text, expected):
    assert parse_mcd(text) == pytest.approx(expected)


@pytest.mark.parametrize("text, expected", [
    ("2V", (2.0,)),
    ("2V、2.1V", (2.0, 2.1)),
    ("2.0V(Typ)", (2.0,)),
    ("1.8V~2.4V", (2.4,)),
    ("3.2", (3.2,)),
    ("", ()),
])
def test_parse_vf(text, expected):
    assert parse_vf(text) == pytest.approx(expected)


def test_parse_labels():
    assert parse_labels("Orange Red, Yellow Green") == ("orange", "yellow green")
    assert parse_labels("White") == ("white",)
    assert parse_labels("Emerald Green") == ("green",)
    assert parse_labels("Water Clear") == (None,)
    assert parse_labels("") == ()


# --- bands --------------------------------------------------------------- #

def test_band_for_and_text():
    assert band_for("green") == (510.0, 540.0)
    assert band_for("green", "525nm") == (515.0, 535.0)
    assert band_for("white") is None
    assert band_for("magenta") is None
    assert band_text("green") == "510–540 nm"
    assert band_text("green", "525nm") == "515–535 nm"
    assert band_text("infrared") == "700–1000 nm peak"
    assert band_text("white") == "by label"
    assert band_text("magenta") == ""


def test_yellow_and_amber_overlap():
    assert in_band("yellow", dominant=[590.0]) is True
    assert in_band("amber", dominant=[590.0]) is True


@pytest.mark.parametrize("kwargs, expected", [
    # The label does not decide: a JLCPCB "Green" at 486 nm is cyan.
    (dict(name="green", dominant=[486.0], labels=["green"]), False),
    (dict(name="green", dominant=[525.0], labels=["green"]), True),
    (dict(name="cyan", dominant=[486.0], labels=["green"]), True),
    (dict(name="red", dominant=[624.0]), True),
    (dict(name="red", dominant=[650.0]), False),
    (dict(name="deep red", dominant=[650.0]), True),
    (dict(name="red", dominant=[]), None),
    # A stated wavelength replaces the band.
    (dict(name="green", dominant=[536.0], wavelength="525nm"), False),
    (dict(name="green", dominant=[530.0], wavelength="525nm"), True),
    # Two dies are never one colour.
    (dict(name="red", dominant=[569.0, 621.0]), False),
    # Infrared: peak first, dominant as a fallback.
    (dict(name="infrared", peak=[940.0]), True),
    (dict(name="infrared", dominant=[850.0]), True),
    (dict(name="infrared", peak=[650.0], dominant=[850.0]), False),
    # Label-only colours.
    (dict(name="white", dominant=[469.0], labels=["white"]), True),
    (dict(name="white", labels=["blue"]), False),
    (dict(name="white", labels=[None]), None),
    (dict(name="white"), None),
    (dict(name="warm white", labels=["white"]), True),
    (dict(name="warm white", labels=["cool white"]), False),
    (dict(name="white", labels=["white", "red"]), False),
    # Unknown colour.
    (dict(name="magenta", dominant=[600.0]), None),
])
def test_in_band(kwargs, expected):
    name = kwargs.pop("name")
    assert in_band(name, **kwargs) is expected
