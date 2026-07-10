"""Tests for canonical part-number extraction (kicadtools.partnum)."""
import pytest

from kicadtools.partnum import (
    CANONICAL_KEYS,
    PartNumbers,
    extract_part_numbers,
    normalization_edits,
)


def _by_op(edits):
    """Group edits by op for order-independent assertions."""
    out = {"rename": [], "set": [], "remove": []}
    for e in edits:
        out[e.op].append(e)
    return out


def test_empty_props_yield_all_none():
    pn = extract_part_numbers({})
    assert pn == PartNumbers()
    assert pn.lcsc is None and pn.mpn is None and pn.digikey_pn is None


@pytest.mark.parametrize(
    "key",
    [
        "DigiKey PN",
        "Digikey",
        "DigikeyPartNumber",
        "DigikeyPN",
        "Digi-Key_PN",
        "DigiKey",
        "DigiKeyPN",
        "DigiKey PN ",  # trailing space
        "Digikey PN",
        "DigiKey Part Number",
        "digikey",
    ],
)
def test_every_digikey_spelling_maps_to_digikey_pn(key):
    pn = extract_part_numbers({key: "P10.0KLDKR-ND"})
    assert pn.digikey_pn == "P10.0KLDKR-ND"
    assert pn.source_key["digikey_pn"] == key


@pytest.mark.parametrize(
    "key", ["MPN", "Manufacturer_PN", "Manufacturer PN", "Mfr_PN"]
)
def test_mpn_spellings(key):
    assert extract_part_numbers({key: "RMCF0603FT100K"}).mpn == "RMCF0603FT100K"


def test_lcsc_and_manufacturer():
    pn = extract_part_numbers({"LCSC": "C541465", "Manufacturer": "Yageo"})
    assert pn.lcsc == "C541465"
    assert pn.manufacturer == "Yageo"


def test_manufacturer_pn_is_not_manufacturer_name():
    # "Manufacturer_PN" must resolve as an MPN, never as the manufacturer name.
    pn = extract_part_numbers({"Manufacturer_PN": "CL10B105KA8NFNC"})
    assert pn.mpn == "CL10B105KA8NFNC"
    assert pn.manufacturer is None


def test_whitespace_only_value_is_ignored():
    assert extract_part_numbers({"LCSC": "   "}).lcsc is None


def test_non_string_values_ignored():
    # dnp / in_bom bools are folded into props by the parser; must not crash.
    pn = extract_part_numbers({"dnp": False, "in_bom": True, "MPN": "ABC123"})
    assert pn.mpn == "ABC123"


def test_populated_spelling_wins_over_empty_sibling():
    # The real MotorDriver case: an empty DigikeyPN alongside a populated
    # "DigiKey PN" — both normalize to "digikeypn"; the non-empty value wins.
    props = {
        "DigikeyPN": "",
        "DigikeyPartNumber": "",
        "DigiKey": "",
        "Digikey": "",
        "Digi-Key_PN": "",
        "DigiKey PN": "13-CC0402JRX7R8BB104CT-ND",
    }
    pn = extract_part_numbers(props)
    assert pn.digikey_pn == "13-CC0402JRX7R8BB104CT-ND"


def test_all_four_fields_together():
    props = {
        "LCSC": "C52923",
        "MPN": "RNCP0603FTD10K0",
        "DigiKey PN": "P10.0KLDKR-ND",
        "Manufacturer": "Stackpole",
    }
    pn = extract_part_numbers(props)
    assert (pn.lcsc, pn.mpn, pn.digikey_pn, pn.manufacturer) == (
        "C52923",
        "RNCP0603FTD10K0",
        "P10.0KLDKR-ND",
        "Stackpole",
    )


# ----------------------------------------------------------------------
# normalization_edits — planning the key rewrites (no schematic I/O)
# ----------------------------------------------------------------------
def test_no_edits_when_already_canonical():
    props = {CANONICAL_KEYS[f]: v for f, v in (
        ("lcsc", "C1"), ("mpn", "M1"), ("digikey_pn", "D1-ND"),
        ("manufacturer", "Acme"),
    )}
    assert normalization_edits(props) == []


def test_empty_variant_is_left_untouched():
    # No value to relocate -> the planner must not churn a blank property.
    assert normalization_edits({"DigikeyPN": ""}) == []
    assert normalization_edits({"Manufacturer_PN": "  "}) == []


def test_variant_with_value_is_renamed_to_canonical():
    edits = normalization_edits({"DigikeyPN": "296-1-ND"})
    assert len(edits) == 1
    (e,) = edits
    assert (e.op, e.old_key, e.key, e.value) == (
        "rename", "DigikeyPN", "DigiKeyPN", "296-1-ND",
    )


def test_manufacturer_pn_variant_renames_to_mpn():
    edits = normalization_edits({"Manufacturer_PN": "CL10B472KB8NNNC"})
    assert [e.op for e in edits] == ["rename"]
    assert edits[0].key == "MPN" and edits[0].old_key == "Manufacturer_PN"


def test_populated_variant_fills_existing_empty_canonical():
    # Canonical key already present but blank, value under a variant spelling:
    # set the canonical value and drop the variant (no duplicate left behind).
    edits = normalization_edits({"DigiKeyPN": "", "Digi-Key_PN": "36-232-ND"})
    grouped = _by_op(edits)
    assert len(grouped["set"]) == 1 and grouped["set"][0].value == "36-232-ND"
    assert grouped["set"][0].key == "DigiKeyPN"
    assert {e.key for e in grouped["remove"]} == {"Digi-Key_PN"}
    assert grouped["rename"] == []


def test_empty_variant_beside_populated_canonical_is_removed():
    # Real ECG case: MPN carries the value, an empty Manufacturer_PN sits beside it.
    edits = normalization_edits({"MPN": "150060RS75000", "Manufacturer_PN": ""})
    assert [(e.op, e.key) for e in edits] == [("remove", "Manufacturer_PN")]


def test_edits_are_pure_and_do_not_mutate_input():
    props = {"DigikeyPN": "296-1-ND", "MANUFACTURER": "MPD"}
    before = dict(props)
    normalization_edits(props)
    assert props == before
