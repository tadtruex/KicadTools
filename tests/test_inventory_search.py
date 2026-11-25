"""Inventory search (inventory.py), on SQLite and, when configured, MariaDB
(tests/dbbackends.py)."""
from __future__ import annotations

import pytest

from kicadtools.inventory import fold, query_words, search
from kicadtools.inventorydb import InventoryDb
from tests.dbbackends import BACKENDS, new_db, use_backend


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


@pytest.fixture
def inv():
    inv = InventoryDb(new_db())
    inv.ensure_location("school")
    inv.add_location("C12", "school", description="rack by the bench, top shelf")
    inv.add_location("3", "school/C12")
    inv.add_location("5", "school/C12", description="dev boards")
    inv.ensure_location("barn/shelf")
    inv.ensure_location("school/57")
    inv.ensure_location("school/58")
    nano = inv.add_item("Nano Every", location="school/C12/5", quantity=6,
                        category="Dev board", manufacturer="Arduino", tags=["arduino", "avr"],
                        description="ATmega4809 board, 5 V")
    inv.add_stock(nano.id, "home/C3/2", 10)
    inv.add_item("Uno R3", location="school/C12/5", quantity=0, category="Dev board",
                 tags=["arduino"])
    inv.add_item("STLink V3", location="school/C12/3", quantity=2, category="Programmer",
                 manufacturer="STMicroelectronics", mpn="STLINK-V3MINIE",
                 description="ST-LINK/V3 debugger for STM32")
    inv.add_item("M6 x 5 mm pan head screw", location="barn/shelf", quantity=100,
                 category="Hardware")
    inv.add_item("M3 x 10 mm screw", location="barn/shelf", category="Hardware")
    inv.add_item("0.1µF 0603", location="school/57", quantity=500, category="Capacitor",
                 description="50V X7R")
    inv.add_item("0.1uF 0603", location="school/58", category="Capacitor",
                 description="16V X5R")
    inv.add_item("1uF 0805", category="Capacitor")          # in no location
    inv.add_item("USB cable", location="school/C12/3", quantity=4,
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
    assert sorted(h.item.locations for h in hits) == [("school/57",), ("school/58",)]
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
    assert [h.item.locations for h in search(inv, "0603")] == [("school/57",), ("school/58",)]


def test_location_fields_are_searched(inv):
    assert set(names(search(inv, "bench"))) == {"Nano Every", "Uno R3",
                                                 "STLink V3", "USB cable"}
    assert set(names(search(inv, "dev boards"))) == {"Nano Every", "Uno R3"}
    assert names(search(inv, "barn m3")) == ["M3 x 10 mm screw"]
    assert names(search(inv, "home")) == ["Nano Every"]     # its second location


def test_one_hit_per_item_with_its_rows(inv):
    hit = search(inv, "nano every")[0]      # the USB cable's description says it too
    assert hit.item.name == "Nano Every"
    assert [r.location_path for r in hit.rows] == ["home/C3/2", "school/C12/5"]
    assert hit.on_hand == 16


def test_top_and_location_filters_keep_the_rows_there(inv):
    (hit,) = search(inv, "nano", top="HOME")
    assert [r.location_path for r in hit.rows] == ["home/C3/2"] and hit.on_hand == 10
    assert set(names(search(inv, "screw", top="barn"))) == {
        "M6 x 5 mm pan head screw", "M3 x 10 mm screw"}
    assert names(search(inv, "screw", top="school")) == []
    assert set(names(search(inv, "", location=" school/c12 ", category="dev board"))) == {
        "Nano Every", "Uno R3"}
    # an item in no location is left out by a location filter
    assert "1uF 0805" in names(search(inv, "capacitor"))
    assert "1uF 0805" not in names(search(inv, "capacitor", top="school"))


def test_in_stock_looks_at_the_rows_the_filters_kept(inv):
    assert names(search(inv, "arduino", in_stock=True)) == ["Nano Every"]
    # unknown quantity counts as in stock
    assert "M3 x 10 mm screw" in names(search(inv, "screw", in_stock=True))
    inv.update_stock(inv.items("home")[0].stock_in("home")[0].id, quantity=0)
    assert names(search(inv, "arduino", top="home", in_stock=True)) == []
    assert names(search(inv, "arduino", top="school", in_stock=True)) == ["Nano Every"]


def test_empty_query_lists_everything_by_name(inv):
    hits = search(inv, "  ")
    assert len(hits) == 9
    assert names(hits)[:3] == ["0.1uF 0603", "0.1µF 0603", "1uF 0805"]


def test_query_words_and_fold():
    assert query_words("  M6  m6 Screw ") == ["m6", "screw"]
    assert fold("4.7kΩ 10µF") == "4.7kohm 10uf"
