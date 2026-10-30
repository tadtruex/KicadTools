"""Tests for kicadtools.units: formatters, package codes, dielectric codes.

The parsers are covered by test_confirm.py, which imports them through
kicadtools.confirm; one test here checks that both names are the same objects.
"""
from __future__ import annotations

import pytest

from kicadtools import confirm, substitute, units
from kicadtools.evidence import DescriptionEvidence
from kicadtools.units import (
    chip_size,
    dielectric_tokens,
    dielectric_upgrades,
    fmt_farads,
    fmt_ohms,
    fmt_percent,
    fmt_volts,
    fmt_watts,
    norm_dielectric,
    package_code,
    parse_cap_description,
    parse_cap_value,
)


@pytest.mark.parametrize("v, text", [
    (0.0, "0"), (220.0, "220"), (4.7, "4.7"), (1e3, "1k"), (4.7e3, "4.7k"),
    (999.0, "999"), (1e6, "1M"), (2.2e6, "2.2M"), (10e3 * (1 + 1e-9), "10k"),
])
def test_fmt_ohms(v, text):
    assert fmt_ohms(v) == text


@pytest.mark.parametrize("v, text", [
    (100e-9, "100nF"), (1e-9, "1nF"), (1e-6, "1uF"), (4.7e-6, "4.7uF"),
    (22e-12, "22pF"), (100e-12, "100pF"), (0.1e-6, "100nF"), (470e-6, "470uF"),
])
def test_fmt_farads(v, text):
    assert fmt_farads(v) == text


@pytest.mark.parametrize("v, text", [(25.0, "25V"), (6.3, "6.3V"), (1000.0, "1000V")])
def test_fmt_volts(v, text):
    assert fmt_volts(v) == text


@pytest.mark.parametrize("v, text", [(1.0, "1%"), (0.1, "0.1%"), (10.0, "10%")])
def test_fmt_percent(v, text):
    assert fmt_percent(v) == text


@pytest.mark.parametrize("v, text", [
    (0.1, "100mW"), (0.063, "63mW"), (0.25, "250mW"), (1.0, "1W"), (2.0, "2W"),
])
def test_fmt_watts(v, text):
    assert fmt_watts(v) == text


@pytest.mark.parametrize("v", [100e-9, 4.7e-6, 22e-12, 1e-9])
def test_fmt_farads_round_trips_through_parser(v):
    assert units.parse_capacitance(fmt_farads(v)) == pytest.approx(v)


@pytest.mark.parametrize("text, code", [
    ("Capacitor_SMD:C_0603_1608Metric", "0603"),
    ("Resistor_SMD:R_0402_1005Metric", "0402"),
    ("0805", "0805"),
    ("0603 (1608 Metric)", "0603"),
    ("C_1210_3225Metric", "1210"),
    ("Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm", None),
    ("0402_lib:C_Disc", None),          # library prefix ignored
    ("C_06030", None),                  # digit run longer than a code
    ("", None),
    (None, None),
])
def test_package_code(text, code):
    assert package_code(text) == code


@pytest.mark.parametrize("text, norm", [
    ("X7R", "X7R"), ("x7r", "X7R"), (" X5R ", "X5R"), ("C0G", "C0G"),
    ("COG", "C0G"), ("cog", "C0G"), ("NP0", "NP0"), ("C0G (NP0)", "C0GNP0"),
    ("", ""), (None, ""),
])
def test_norm_dielectric(text, norm):
    assert norm_dielectric(text) == norm


@pytest.mark.parametrize("text, tokens", [
    ("C0G, NP0", ("C0G", "NP0")),
    ("X7R", ("X7R",)),
    ("x5r", ("X5R",)),
    ("C0G/NP0", ("C0G", "NP0")),
    ("COG NP0", ("C0G", "NP0")),
    ("X7R, X5R,", ("X7R", "X5R")),
    (" , / ", ()),
    ("", ()),
    (None, ()),
])
def test_dielectric_tokens(text, tokens):
    assert dielectric_tokens(text) == tokens


def test_confirm_and_substitute_reexport_units():
    for name in ("parse_resistance", "parse_percent", "parse_watts",
                 "parse_capacitance", "parse_volts"):
        assert getattr(confirm, name) is getattr(units, name)
    assert substitute.package_code is units.package_code


@pytest.mark.parametrize("text,codes", [
    ("C0G、NP0", ("C0G", "NP0")),
    ("NP0、C0G", ("NP0", "C0G")),
    ("NP0(C0G)", ("NP0", "C0G")),
    ("C0G-NP0", ("C0G", "NP0")),
    ("COG; NPO", ("C0G", "NP0")),
])
def test_dielectric_tokens_split_jlcpcb_separators(text, codes):
    assert dielectric_tokens(text) == codes


# --------------------------------------------------------------------------- #
# Capacitor Values and Descriptions from the user's boards.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("token,code", [
    ("805", "0805"), ("0805", "0805"), ("603", "0603"), ("0603", "0603"),
    ("402", "0402"), ("0402", "0402"), ("201", "0201"), ("0201", "0201"),
    ("01005", "01005"), ("1206", "1206"), ("1210", "1210"), ("1812", "1812"),
    ("2010", "2010"), ("2220", "2220"), ("2512", "2512"), (" 805 ", "0805"),
    ("1005", None), ("206", None), ("1608", None), ("8050", None), ("1uF", None),
    ("25V", None), ("", None), (None, None), ("X7R", None),
])
def test_chip_size(token, code):
    assert chip_size(token) == code


@pytest.mark.parametrize("text,farads,package", [
    ("100nF", 100e-9, None), ("1nF", 1e-9, None), ("1u", 1e-6, None),
    ("4.7n", 4.7e-9, None), ("0.1uF", 0.1e-6, None), ("0.01uF", 0.01e-6, None),
    ("4.7uF", 4.7e-6, None), ("10uF", 10e-6, None), ("47uF", 47e-6, None),
    ("C_4p7U", 4.7e-6, None), ("C_P1U", 0.1e-6, None), ("C_1U", 1e-6, None),
    ("C_10U_805", 10e-6, "0805"), ("C_22U_1210", 22e-6, "1210"),
    ("C_10U_1210", 10e-6, "1210"), ("C-4u7", 4.7e-6, None),
    ("4p7", 4.7e-12, None), ("22p", 22e-12, None), ("4p7n", 4.7e-9, None),
    ("10U", 10e-6, None), ("C_100N_0603", 100e-9, "0603"),
])
def test_parse_cap_value(text, farads, package):
    got, pkg = parse_cap_value(text)
    assert got == pytest.approx(farads, rel=1e-9)
    assert pkg == package


@pytest.mark.parametrize("text", ["", None, "C", "C_", "DNP", "C_X_805", "100nF 25V"])
def test_parse_cap_value_unparseable(text):
    assert parse_cap_value(text)[0] is None


def test_parse_cap_value_package_without_value():
    # The package is read from later parts even when the value is unreadable.
    assert parse_cap_value("C_X_805") == (None, "0805")


@pytest.mark.parametrize("text,farads,volts,dielectrics,package", [
    ("4.7uF, 16V, X6S 603", 4.7e-6, 16.0, ("X6S",), "0603"),
    ("0.1uF, 25V, X7R 603", 0.1e-6, 25.0, ("X7R",), "0603"),
    ("10uF, 25V, X5R, 805 package", 10e-6, 25.0, ("X5R",), "0805"),
    ("22uF, X5R, 25V, 1210", 22e-6, 25.0, ("X5R",), "1210"),
    ("1uF, 25V - 50V", 1e-6, 25.0, (), None),
    ("0.1uF, 25V, X7R, 0603", 0.1e-6, 25.0, ("X7R",), "0603"),
    ("10uF, 50V, X7R, 1210 package", 10e-6, 50.0, ("X7R",), "1210"),
    ("100nF", 100e-9, None, (), None),
    ("22pF C0G", 22e-12, None, ("C0G",), None),
    ("4.7 uF, 6.3V", 4.7e-6, 6.3, (), None),
    ("1nF, 1kV, COG/NPO, NP0", 1e-9, 1000.0, ("C0G", "NP0"), None),
])
def test_parse_cap_description(text, farads, volts, dielectrics, package):
    ev = parse_cap_description(text)
    assert ev.nominal == pytest.approx(farads, rel=1e-9)
    assert ev.rating == volts
    assert ev.dielectrics == dielectrics
    assert ev.package == package


@pytest.mark.parametrize("text", [
    "Unpolarized capacitor, small symbol",
    "Polarized capacitor, small US symbol",
    "",
    None,
])
def test_parse_cap_description_generic_is_empty(text):
    assert parse_cap_description(text) == DescriptionEvidence()


def test_parse_cap_description_value_digits_not_a_package():
    assert parse_cap_description("1uF").package is None
    assert parse_cap_description("805pF").package is None
    assert parse_cap_description("603V").package is None


@pytest.mark.parametrize("code,upgrades", [
    ("X5R", ("X5R", "X7R")), ("X6S", ("X6S", "X7R")), ("X7S", ("X7S", "X7R")),
    ("X7R", ("X7R",)), ("C0G", ("C0G", "NP0")), ("NP0", ("NP0", "C0G")),
    ("COG", ("C0G", "NP0")), ("x5r", ("X5R", "X7R")), ("Y5V", ("Y5V",)),
    ("X8R", ("X8R",)), ("", ()),
])
def test_dielectric_upgrades(code, upgrades):
    assert dielectric_upgrades(code) == upgrades


@pytest.mark.parametrize("text,package", [
    ("0805", "0805"), ("C_805", "0805"), ("C_0603", "0603"), ("1210", "1210"),
])
def test_parse_cap_value_size_code_alone_is_a_package_not_farads(text, package):
    # A bare "805" would otherwise parse as 805 F through parse_capacitance.
    assert parse_cap_value(text) == (None, package)


@pytest.mark.parametrize("text,volts", [
    ("1uF, 25-50V", 25.0),
    ("1uF, 25 - 50V", 25.0),
    ("1uF, 25V - 50V", 25.0),
    ("1uF, 1-2kV", 1000.0),
])
def test_parse_cap_description_voltage_range_takes_lowest(text, volts):
    assert parse_cap_description(text).rating == volts


def test_parse_cap_description_range_digits_not_a_package():
    assert parse_cap_description("1uF, 603-805V").package is None


@pytest.mark.parametrize("text,package", [
    ("100nF (0603 Metric)", None),
    ("100nF 0201 (0603 Metric)", "0201"),
    ("C_0603_1608Metric", "0603"),
])
def test_parse_cap_description_metric_size_not_imperial(text, package):
    assert parse_cap_description(text).package == package


@pytest.mark.parametrize("text", [
    "C1608X6S1C475K080AC", "TMK212BBJ106KG-T", "CL32B106KBJNNWE",
    "CGA3E2X7R1E104K080AA", "445-9118-1-ND",
])
def test_parse_cap_description_part_numbers_state_nothing(text):
    # Codes embedded in an MPN (X6S, X7R, 1E) are not standalone tokens.
    assert parse_cap_description(text) == DescriptionEvidence()


def test_substitute_uses_units_dielectric_upgrades():
    assert not hasattr(substitute, "_DIELECTRIC_UPGRADES")
    assert substitute.dielectric_upgrades is units.dielectric_upgrades
