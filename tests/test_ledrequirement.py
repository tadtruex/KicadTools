"""Tests for kicadtools.ledrequirement: LED requirements and grouping."""
from __future__ import annotations

from pathlib import Path

import pytest

from kicadtools.intent import ElectricalIntent, LedIntent
from kicadtools.ledcolor import MULTICOLOR, SMART
from kicadtools.ledrequirement import (
    CLEAR,
    DIFFUSED,
    LED_KIND,
    NO_COLOR,
    TEXT,
    LedEvidence,
    color_at,
    footprint_tokens,
    group_leds,
    led_requirement,
    lens_kind,
    named_led_identifiers,
)
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    EDITED,
    PART,
    SCHEMATIC,
    UNSET,
    part_defaults,
)
from kicadtools.resolve import PartRecord, iter_bom_part_records

DATA = Path(__file__).parent / "data"
DEFAULTS = part_defaults({})
FP = "LED_SMD:LED_0603_1608Metric"


def _props(ref="D1", **extra):
    props = {"Reference": ref, "dnp": False, "in_bom": True,
             "Intent:Type": "led", "Footprint": FP}
    props.update(extra)
    return props


def _rec(ref="D1", **extra):
    props = _props(ref, **extra)
    return PartRecord(ref, props, Path("board.kicad_sch"), f"uuid-{ref}",
                      ElectricalIntent.from_dict(props))


def _req(part=None, **extra):
    props = _props(**extra)
    intent = ElectricalIntent.from_dict(props)
    assert isinstance(intent, LedIntent)
    return led_requirement(intent, props, DEFAULTS, part=part)


# --- intent fields ------------------------------------------------------- #

def test_led_intent_reads_stated_fields():
    i = ElectricalIntent.from_dict(_props(**{
        "Color:Preferred": "green, yellow green", "Wavelength": "525nm",
        "Intensity:Min": "100mcd", "Vf:Max": "2.2V", "Lens:Preferred": "clear"}))
    assert i.color_preferred == ["green", "yellow green"]
    assert i.wavelength_stated == "525nm"
    assert i.intensity_min == "100mcd"
    assert i.vf_max == "2.2V"
    assert i.lens_preferred == ["clear"]


# --- requirement --------------------------------------------------------- #

def test_colour_from_value():
    r = _req(Value="LED_Green")
    assert r.kind == LED_KIND
    assert r.package.value == "0603" and r.package.source == SCHEMATIC
    assert r.colors.value == ("green",) and r.colors.source == TEXT
    assert r.band_text() == "green 510–540 nm"
    assert r.wavelength.source == ANY
    assert r.intensity_min.source == ANY
    assert r.vf_max.source == ANY
    assert r.lenses.source == ANY
    assert r.searchable


def test_single_letter_value_on_led_footprint():
    r = _req(Value="G")
    assert r.colors.value == ("green",)


def test_no_colour_is_not_searchable():
    r = _req(Value="LED")
    assert r.colors.source == UNSET
    assert r.problem() == NO_COLOR


def test_stated_colour_wins_and_may_list_several():
    r = _req(Value="LED", **{"Color:Preferred": "Emerald Green, lime"})
    assert r.colors.value == ("green", "yellow green")
    assert r.colors.source == SCHEMATIC
    assert r.band_text() == "green 510–540 nm; yellow green 555–575 nm"


def test_stated_colour_that_is_not_a_colour():
    r = _req(Value="LED", **{"Color:Preferred": "magenta"})
    assert r.colors.value == ()
    assert r.problem() == "Color:Preferred on the schematic is not a colour"


def test_stated_colour_against_value_is_a_conflict():
    r = _req(Value="LED_Red", **{"Color:Preferred": "green"})
    assert r.conflicts == ("colour: Color:Preferred says green, the Value says red",)
    assert not r.searchable


def test_wavelength_stated_and_from_text():
    r = _req(Value="LED_Green", Wavelength="525nm")
    assert r.wavelength.value == 525.0 and r.wavelength.source == SCHEMATIC
    assert r.band_text() == "green 515–535 nm"
    r = _req(Value="LED", Description="Red 630nm")
    assert r.wavelength.value == 630.0 and r.wavelength.source == TEXT
    assert r.colors.value == ("red",)
    assert r.bands() == (("red", (620.0, 640.0)),)


def test_limits_and_lens():
    r = _req(Value="LED_Red", **{"Intensity:Min": "1.2cd", "Vf:Max": "2.2V",
                                 "Lens:Preferred": "Water Clear, diffused"})
    assert r.intensity_min == type(r.intensity_min)(1200.0, SCHEMATIC)
    assert r.vf_max.value == pytest.approx(2.2)
    assert r.lenses.value == (CLEAR, DIFFUSED)


def test_package_from_default_without_footprint():
    r = _req(Value="LED_Red", Footprint="")
    assert r.package.value == "0603" and r.package.source == DEFAULT


def test_footprint_that_is_not_a_chip_size():
    r = _req(Value="LED_Red", Footprint="LED_SMD:LED_PLCC_2835")
    assert r.problem() == "footprint 'LED_SMD:LED_PLCC_2835' is not a chip size"


def test_package_preferred_against_footprint_is_a_conflict():
    r = _req(Value="LED_Red", **{"Package:Preferred": "0805"})
    assert r.conflicts == ("package: Footprint says 0603, Package:Preferred says 0805",)


# --- existing-part evidence --------------------------------------------- #

GREEN_525 = LedEvidence("current part X (DigiKey)", package="0603",
                        dominant=(525.0,), labels=("green",))
CYANISH = LedEvidence("current part Y (JLCPCB)", package="0603",
                      dominant=(486.0,), labels=("green",))


def test_part_in_band_is_no_conflict():
    assert _req(Value="LED_Green", part=GREEN_525).searchable


def test_part_out_of_band_is_a_conflict():
    r = _req(Value="LED_Green", part=CYANISH)
    assert r.conflicts == (
        "colour: green 510–540 nm, but current part Y (JLCPCB) is at 486 nm",)


def test_part_gives_the_colour_when_no_field_does():
    r = _req(Value="LED", part=CYANISH)
    assert r.colors.value == ("cyan",) and r.colors.source == PART
    assert r.searchable


def test_part_package_against_footprint():
    part = LedEvidence("current part Z (DigiKey)", package="0805", dominant=(625.0,))
    r = _req(Value="LED_Red", part=part)
    assert r.conflicts == (
        "package: Footprint says 0603, current part Z (DigiKey) says 0805",)


def test_two_die_part_on_a_plain_led_is_a_conflict():
    part = LedEvidence("current part B (JLCPCB)", package="0603",
                       dominant=(569.0, 621.0))
    r = _req(Value="LED_Red", part=part)
    assert r.conflicts == ("current part B (JLCPCB) has 2 dies; this is a one-colour LED",)


@pytest.mark.parametrize("nm, expected", [
    (525.0, "green"), (590.0, "amber"), (585.0, "yellow"), (940.0, "infrared"),
    (650.0, "deep red"), (550.0, None),
])
def test_color_at(nm, expected):
    assert color_at(nm) == expected


def test_evidence_colour_for_white_by_label():
    ev = LedEvidence("p", dominant=(), labels=("white",))
    assert ev.color == "white"


@pytest.mark.parametrize("text, expected", [
    ("Water Clear", CLEAR), ("Clear", CLEAR), ("Colorless", CLEAR),
    ("Diffused", DIFFUSED), ("White Diffused", DIFFUSED), ("Milky", DIFFUSED),
    ("Red", None), ("", None),
])
def test_lens_kind(text, expected):
    assert lens_kind(text) == expected


# --- edits --------------------------------------------------------------- #

def test_edited_fields():
    r = _req(Value="LED").edited(colors=["Red"], intensity_min=50, vf_max=2.4,
                                 lenses=["diffused"])
    assert r.colors.value == ("red",) and r.colors.source == EDITED
    assert r.intensity_min.value == 50.0 and r.intensity_min.source == EDITED
    assert r.vf_max.value == 2.4
    assert r.lenses.value == (DIFFUSED,)
    assert r.searchable
    r = r.edited(clear=["intensity_min"])
    assert r.intensity_min.value is None and r.intensity_min.source == ANY
    with pytest.raises(ValueError):
        r.edited(colors=["magenta"])
    with pytest.raises(ValueError):
        r.edited(clear=["colors"])


# --- named LEDs ---------------------------------------------------------- #

@pytest.mark.parametrize("fp, expected", [
    ("StockedComponents:LTST-C155GEKT", ("LTST-C155GEKT",)),
    ("StockedComponents:4492_ADA", ("4492_ADA",)),
    ("LED_SMD:LED_RGB_5050-6", ()),
    ("LED_SMD:LED_0603_1608Metric", ()),
    ("", ()),
])
def test_footprint_tokens(fp, expected):
    assert footprint_tokens(fp) == expected


def test_named_identifiers_footprint_first_without_repeats():
    ids = named_led_identifiers({"Value": "LED_RG_LTSTC155GEKT",
                                 "Footprint": "StockedComponents:LTST-C155GEKT"})
    assert ids.value_mpns == ("LTST-C155GEKT",)
    assert not ids.empty


# --- grouping ------------------------------------------------------------ #

def test_grouping_by_colour_meaning():
    recs = [
        _rec("D1", Value="LED_Green"),
        _rec("D2", Value="G"),                       # same colour, other Value
        _rec("D3", Value="LED_Red"),
        _rec("D4", Value="LED_Green", **{"Intensity:Min": "100mcd"}),
        _rec("D5", Value="LED_Green", MPN="150060GS75000"),
        _rec("D6", Value="LED"),
        _rec("D7", Value="LED", Description="indicator"),
    ]
    plain, named = group_leds(recs, DEFAULTS)
    assert named == []
    assert [g.references for g in plain] == [
        ["D1", "D2"], ["D3"], ["D4"], ["D5"], ["D6", "D7"]]
    assert plain[0].kind == LED_KIND
    assert plain[0].quantity == 2


def test_grouping_named_and_skips_others():
    recs = [
        _rec("D1", Value="LED_RG_LTSTC155GEKT",
             Footprint="StockedComponents:LTST-C155GEKT"),
        _rec("D2", Value="LED_RG_LTSTC155GEKT",
             Footprint="StockedComponents:LTST-C155GEKT"),
        _rec("D3", Value="WS2812B", Footprint="LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm"),
        _rec("D4", Value="LED_Red", Footprint="LED_THT:LED_D5.0mm"),   # through-hole
        _rec("U5", Value="1515 rgb led", Footprint="LED_SMD:LED_RGB_1515"),
        PartRecord("R1", {"Reference": "R1", "Value": "10k"}, None, "u",
                   ElectricalIntent.from_dict(
                       {"Reference": "R1", "Value": "10k", "Intent:Type": "resistor",
                        "dnp": False, "in_bom": True})),
    ]
    plain, named = group_leds(recs, DEFAULTS)
    assert plain == []
    assert [(g.led_kind, g.references) for g in named] == [
        (MULTICOLOR, ["D1", "D2"]), (SMART, ["D3"]), (MULTICOLOR, ["U5"])]
    assert named[0].kind == LED_KIND
    assert named[0].requirement.kind == LED_KIND
    assert named[0].requirement.identifiers.value_mpns == ("LTST-C155GEKT",)
    assert named[1].requirement.identifiers.value_mpns == ("WS2812B",)
    assert named[2].requirement.problem() == "no part number"


def test_ecg_fixture():
    plain, named = group_leds(iter_bom_part_records(DATA / "ECG.kicad_sch"), DEFAULTS)
    assert named == []
    assert [g.references for g in plain] == [["D1", "D4"]]
    r = plain[0].requirement
    assert r.colors.value == ("red",) and r.package.value == "0603"
    assert plain[0].existing.mpn == "150060RS75000"
    assert not plain[0].assigned


def test_motordriver_fixture_rgb_leds():
    _, named = group_leds(iter_bom_part_records(DATA / "MotorDriver.kicad_sch"),
                          DEFAULTS)
    assert [(g.led_kind, g.references) for g in named] == [
        (MULTICOLOR, ["U5", "U6", "U7", "U8"])]
    ids = named[0].requirement.identifiers
    assert ids.lcsc == "C3028831"
    assert named[0].requirement.searchable
