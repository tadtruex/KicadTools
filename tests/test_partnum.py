"""Tests for canonical part-number extraction (kicadtools.partnum)."""
import pytest

from kicadtools.partnum import PartNumbers, extract_part_numbers


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
