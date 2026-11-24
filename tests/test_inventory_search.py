"""Inventory search (inventory.py, INVENTORY_PLAN.md step 3), on SQLite and,
when configured, MariaDB (tests/dbbackends.py)."""
from __future__ import annotations

import pytest

from kicadtools.inventory import effective_sites, fold, query_words, search
from kicadtools.inventorydb import InventoryDb
from tests.dbbackends import BACKENDS, new_db, use_backend


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


@pytest.fixture
def inv():
    inv = InventoryDb(new_db())
    inv.add_bin("C12", site="school", location="rack by the bench, top shelf")
    inv.add_bin("3", "C12")
    inv.add_bin("5", "C12", description="dev boards")
    inv.add_bin("Barn shelf", site="barn")
    inv.add_bin("57", site="school")
    inv.add_bin("58", site="school")
    inv.add_item("Nano Every", bin="C12/5", quantity=6, category="Dev board",
                 manufacturer="Arduino", tags=["arduino", "avr"],
                 description="ATmega4809 board, 5 V")
    inv.add_item("Uno R3", bin="C12/5", quantity=0, category="Dev board",
                 tags=["arduino"])
    inv.add_item("STLink V3", bin="C12/3", quantity=2, category="Programmer",
                 manufacturer="STMicroelectronics", mpn="STLINK-V3MINIE",
                 description="ST-LINK/V3 debugger for STM32")
    inv.add_item("M6 x 5 mm pan head screw", bin="Barn shelf", quantity=100,
                 category="Hardware")
    inv.add_item("M3 x 10 mm screw", bin="Barn shelf", category="Hardware")
    inv.add_item("0.1µF 0603", bin="57", quantity=500, category="Capacitor",
                 description="50V X7R")
    inv.add_item("0.1uF 0603", bin="58", category="Capacitor", description="16V X5R")
    inv.add_item("1uF 0805", category="Capacitor")          # in no bin
    inv.add_item("USB cable", bin="C12/3", quantity=4,
                 description="for the Nano Every and other boards")
    return inv


def names(hits):
    return [h.item.name for h in hits]


def test_tag_finds_an_item_whose_name_does_not_say_it(inv):
    assert set(names(search(inv, "arduino"))) == {"Nano Every", "Uno R3"}


def test_every_word_must_match(inv):
    assert names(search(inv, "stlink v3")) == ["STLink V3"]
    assert names(search(inv, "m6 screw")) == ["M6 x 5 mm pan head screw"]
    assert names(search(inv, "arduino stm32")) == []


def test_punctuation_and_case_are_ignored(inv):
    assert names(search(inv, "st-link")) == ["STLink V3"]
    assert names(search(inv, "STLINKV3MINIE")) == ["STLink V3"]


def test_micro_sign_matches_u(inv):
    hits = search(inv, "0.1uf 0603")
    assert sorted(h.item.bin_path for h in hits) == ["57", "58"]
    assert len(search(inv, "0.1µF")) == 2


def test_typo_tolerated_on_long_words_only(inv):
    assert set(names(search(inv, "arduiono"))) == {"Nano Every", "Uno R3"}
    assert names(search(inv, "m7")) == []            # short: no typo match
    # a value or part number is never typo-matched: 100nF is not 10nF or 0.1uF
    assert names(search(inv, "0.2uf")) == []
    assert names(search(inv, "STLINK-V4MINIE")) == []


def test_name_match_ranks_above_description_match(inv):
    # "nano" is the Nano Every's name and only in the USB cable's description.
    assert names(search(inv, "nano")) == ["Nano Every", "USB cable"]
    assert search(inv, "nano")[0].matched == ("name",)


def test_quantity_breaks_ties(inv):
    # Both capacitors match the same fields; the one with a quantity first.
    assert [h.item.bin_path for h in search(inv, "0603")] == ["57", "58"]


def test_bin_fields_are_searched(inv):
    assert set(names(search(inv, "bench"))) == {"Nano Every", "Uno R3",
                                                 "STLink V3", "USB cable"}
    assert set(names(search(inv, "dev boards"))) == {"Nano Every", "Uno R3"}
    assert names(search(inv, "barn m3")) == ["M3 x 10 mm screw"]


def test_site_is_inherited_by_nested_bins(inv):
    hit = search(inv, "stlink")[0]
    assert hit.bin.path == "C12/3" and hit.site == "school"
    assert effective_sites(inv.bins())["C12/5"] == "school"


def test_filters(inv):
    assert set(names(search(inv, "screw", site="BARN"))) == {
        "M6 x 5 mm pan head screw", "M3 x 10 mm screw"}
    assert names(search(inv, "screw", site="school")) == []
    assert names(search(inv, "arduino", in_stock=True)) == ["Nano Every"]
    assert set(names(search(inv, "", bin=" c12 ", category="dev board"))) == {
        "Nano Every", "Uno R3"}
    # an item in no bin has no site, so a site filter leaves it out
    assert "1uF 0805" in names(search(inv, "capacitor"))
    assert "1uF 0805" not in names(search(inv, "capacitor", site="school"))


def test_empty_query_lists_everything_by_bin(inv):
    hits = search(inv, "  ")
    assert len(hits) == 9
    # items in no bin first, then by bin path (naturally) and name
    assert [h.item.bin_path for h in hits] == [
        None, "57", "58", "Barn shelf", "Barn shelf", "C12/3", "C12/3", "C12/5", "C12/5"]
    assert names(hits)[-2:] == ["Nano Every", "Uno R3"]


def test_query_words_and_fold():
    assert query_words("  M6  m6 Screw ") == ["m6", "screw"]
    assert fold("4.7kΩ 10µF") == "4.7kohm 10uf"
