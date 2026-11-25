"""The lab inventory web app (kicadtools/web): locations, items, stock rows."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kicadtools.inventorydb import InventoryDb  # noqa: E402
from kicadtools.partsdb import PartsDb  # noqa: E402
from kicadtools.web.app import (create_app, fmt_qty, item_counts, location_url,  # noqa: E402
                                parse_quantity, parse_tags)

HX = {"HX-Request": "true", "HX-Target": "results"}


@pytest.fixture
def inv():
    inv = InventoryDb(PartsDb(":memory:"))
    inv.add_location("school")
    inv.add_location("C12", "school", description="rack by the bench")
    inv.add_location("5", "school/C12", description="dev boards")
    inv.ensure_location("barn/top shelf")
    nano = inv.add_item("Nano Every", location="school/C12/5", quantity=6,
                        category="Dev board", tags=["arduino"], description="ATmega4809 board")
    inv.add_stock(nano.id, "barn/top shelf", 2)
    inv.add_item("M6 x 5 mm pan head screw", location="barn/top shelf", quantity=100,
                 category="Hardware", mpn="M6X5-PAN")
    return inv


@pytest.fixture
def client(inv):
    with TestClient(create_app(inv)) as c:
        yield c


def _item(inv, name):
    return next(i for i in inv.items() if i.name == name)


def _row(inv, name, path):
    return next(s for s in _item(inv, name).stock if s.location_path == path)


# ----- helpers ---------------------------------------------------------------

def test_helpers(inv):
    assert location_url("barn/top shelf") == "/location/barn/top%20shelf"
    assert fmt_qty(None) == "—" and fmt_qty(50) == "~50"
    assert parse_quantity(" ~50 ") == 50 and parse_quantity("") is None
    assert parse_tags("arduino, avr ,, ") == ["arduino", "avr"]
    direct, total = item_counts(inv.items())
    assert direct == {"school/C12/5": 1, "barn/top shelf": 2}
    assert total == {"school": 1, "school/C12": 1, "school/C12/5": 1,
                     "barn": 2, "barn/top shelf": 2}


# ----- search ----------------------------------------------------------------

def test_front_page_and_search(client):
    r = client.get("/")
    assert r.status_code == 200 and "Type to search" in r.text
    assert 'src="/static/htmx.min.js"' in r.text
    r = client.get("/", params={"q": "arduino"})
    assert "Nano Every" in r.text and "M6" not in r.text
    # one hit, both its locations, the total on hand
    assert 'href="/location/school/C12/5"' in r.text
    assert 'href="/location/barn/top%20shelf"' in r.text
    assert "~8" in r.text


def test_htmx_search_returns_only_the_results(client):
    r = client.get("/", params={"q": "screw"}, headers=HX)
    assert "M6 x 5 mm pan head screw" in r.text
    assert "<html" not in r.text and "<form" not in r.text


def test_nothing_found_offers_to_add(client):
    r = client.get("/", params={"q": "stlink v3"})
    assert "Nothing found" in r.text and "/add?name=stlink+v3" in r.text


def test_top_filter_is_remembered_and_shows_the_rows_there(client):
    r = client.get("/", params={"q": "arduino", "top": "school"})
    assert "Nano Every" in r.text and "~6" in r.text
    assert 'href="/location/barn/top%20shelf"' not in r.text
    assert client.cookies.get("top") == "school"
    r = client.get("/", params={"q": "screw"})            # no top: the cookie's
    assert "M6" not in r.text and "search everywhere" in r.text
    r = client.get("/", params={"q": "screw", "top": ""})  # everywhere again
    assert "M6" in r.text


def test_a_remembered_top_that_is_no_location_name_is_ignored(client):
    client.cookies.set("top", "a/b/")
    r = client.get("/", params={"q": "screw"})
    assert r.status_code == 200 and "M6" in r.text


def test_category_filter_alone_lists_the_category(client):
    r = client.get("/", params={"category": "hardware"})
    assert "M6 x 5 mm" in r.text and "Nano Every" not in r.text


def test_names_are_escaped(client, inv):
    inv.add_item("<script>alert(1)</script>")
    r = client.get("/", params={"q": "script"})
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


# ----- items -----------------------------------------------------------------

def test_item_page_lists_its_stock_rows(client, inv):
    r = client.get(f"/item/{_item(inv, 'Nano Every').id}")
    assert r.status_code == 200
    assert "Nano Every" in r.text and 'value="arduino"' in r.text
    assert "school/C12/5" in r.text and "barn/top shelf" in r.text
    assert "~6" in r.text and "~2" in r.text and "~8 in all" in r.text
    assert client.get("/item/9999").status_code == 404


def test_item_save(client, inv):
    item_id = _item(inv, "Nano Every").id
    r = client.post(f"/item/{item_id}", data={
        "name": "Nano Every", "tags": "arduino, avr", "category": "Dev board",
        "description": "", "mpn": "ABX00028"})
    assert r.status_code == 200 and "Saved." in r.text      # followed the redirect
    item = inv.item(item_id)
    assert item.mpn == "ABX00028" and item.tags == ("arduino", "avr")
    assert item.description is None and item.on_hand == 8   # stock untouched


def test_item_save_error_keeps_what_was_typed(client, inv):
    item_id = _item(inv, "Nano Every").id
    r = client.post(f"/item/{item_id}", data={"name": " ", "mpn": "TYPED-MPN"})
    assert r.status_code == 400 and "needs a name" in r.text
    assert 'value="TYPED-MPN"' in r.text
    assert inv.item(item_id).name == "Nano Every"


def test_add_to_another_location(client, inv):
    item_id = _item(inv, "Nano Every").id
    r = client.post(f"/item/{item_id}/stock", data={"location": "home/C3/2", "quantity": "~10"})
    assert r.status_code == 200
    assert inv.item(item_id).locations == ("barn/top shelf", "home/C3/2", "school/C12/5")
    assert inv.location("home/C3") is not None               # a new path is made
    r = client.post(f"/item/{item_id}/stock", data={"location": "home/C3/2", "quantity": "5"})
    assert _row(inv, "Nano Every", "home/C3/2").quantity == 15     # same place: added
    r = client.post(f"/item/{item_id}/stock", data={"location": "", "quantity": "5"})
    assert r.status_code == 400 and "location is needed" in r.text


def test_quantity_buttons_per_row(client, inv):
    row = _row(inv, "Nano Every", "school/C12/5")
    r = client.post(f"/stock/{row.id}/qty", data={"delta": "1"}, headers={"HX-Request": "true"})
    assert r.text.strip() == f'<span id="qty-{row.id}" class="bigqty">~7</span>'
    r = client.post(f"/stock/{row.id}/qty", data={"delta": "-100"})   # no JS: redirect
    assert r.status_code == 200 and inv.stock_row(row.id).quantity == 0
    assert _row(inv, "Nano Every", "barn/top shelf").quantity == 2   # the other untouched
    assert client.post("/stock/9999/qty", data={"delta": "1"}).status_code == 400


def test_stock_row_set_move_and_delete(client, inv):
    item_id = _item(inv, "Nano Every").id
    row = _row(inv, "Nano Every", "barn/top shelf")
    client.post(f"/stock/{row.id}", data={"action": "set", "quantity": "", "notes": "a few"})
    assert (inv.stock_row(row.id).quantity, inv.stock_row(row.id).notes) == (None, "a few")
    client.post(f"/stock/{row.id}", data={"action": "set", "quantity": "3", "notes": ""})
    # Moving onto a location it is already in makes one row of the two.
    r = client.post(f"/stock/{row.id}", data={"action": "move", "location": "school/C12/5"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/item/{item_id}"
    assert [(s.location_path, s.quantity) for s in inv.item(item_id).stock] == [
        ("school/C12/5", 9)]
    only = inv.item(item_id).stock[0]
    client.post(f"/stock/{only.id}", data={"action": "delete"})
    assert inv.item(item_id).stock == ()
    r = client.post(f"/stock/{only.id}", data={"action": "delete"})
    assert r.status_code == 404


def test_stock_row_errors_show_on_the_item_page(client, inv):
    row = _row(inv, "Nano Every", "barn/top shelf")
    r = client.post(f"/stock/{row.id}", data={"action": "set", "quantity": "lots"})
    assert r.status_code == 400 and "whole number" in r.text and "Nano Every" in r.text
    r = client.post(f"/stock/{row.id}", data={"action": "move", "location": ""})
    assert r.status_code == 400 and "location to move to" in r.text


def test_delete_item_goes_back_to_a_location(client, inv):
    item_id = _item(inv, "Nano Every").id
    r = client.post(f"/item/{item_id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/location/barn/top%20shelf"
    assert inv.item(item_id) is None


# ----- quick add -------------------------------------------------------------

def test_quick_add_keeps_the_location_for_the_next_item(client, inv):
    r = client.get("/add", params={"location": "school/C12/5"})
    assert 'value="school/C12/5"' in r.text
    r = client.post("/add", data={"name": "STLink V3", "location": "school/C12/5",
                                  "quantity": "2", "tags": "stm32"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/add?location=school%2FC12%2F5&added=")
    r = client.get(r.headers["location"])
    assert "Added" in r.text and "STLink V3" in r.text and 'value="school/C12/5"' in r.text
    item = _item(inv, "STLink V3")
    assert item.on_hand == 2 and item.tags == ("stm32",) and item.locations == ("school/C12/5",)


def test_quick_add_offers_the_item_already_known(client, inv):
    r = client.post("/add", data={"name": "nano  every", "location": "home/C3", "quantity": "4"})
    assert r.status_code == 200 and "Already known" in r.text
    nano = _item(inv, "Nano Every")
    assert f'action="/item/{nano.id}/stock"' in r.text and "Add 4 in home/C3" in r.text
    assert len(inv.items()) == 2                              # nothing made yet
    # taking the offer: a stock row on the known item
    client.post(f"/item/{nano.id}/stock", data={"location": "home/C3", "quantity": "4"})
    assert _row(inv, "Nano Every", "home/C3").quantity == 4 and len(inv.items()) == 2
    # by part number too; and "make a new item anyway" does
    r = client.post("/add", data={"name": "Pan head M6", "mpn": "m6x5-pan"})
    assert "Already known" in r.text and 'name="new" value="1"' in r.text
    client.post("/add", data={"name": "Pan head M6", "mpn": "m6x5-pan", "new": "1"})
    assert len(inv.items()) == 3


def test_quick_add_errors(client, inv):
    r = client.post("/add", data={"name": " ", "location": "school/C12/5"})
    assert r.status_code == 400 and "needs a name" in r.text
    r = client.post("/add", data={"name": "Uno R3", "quantity": "3"})
    assert r.status_code == 400 and "needs a location" in r.text
    assert len(inv.items()) == 2


def test_add_prefills_the_name_from_a_failed_search(client):
    r = client.get("/add", params={"name": "stlink v3"})
    assert 'value="stlink v3"' in r.text


# ----- locations -------------------------------------------------------------

def test_location_page_lists_everything_inside(client):
    r = client.get("/location/school")
    assert r.status_code == 200
    assert "Nano Every" in r.text and 'href="/location/school/C12"' in r.text
    assert "/add?location=school" in r.text
    r = client.get("/location/school/C12")
    assert "rack by the bench" in r.text and 'href="/location/school/C12/5"' in r.text
    assert "~6" in r.text and "~2" not in r.text   # only the row in this location


def test_location_page_with_a_space_in_its_name(client):
    r = client.get("/location/barn/top%20shelf")
    assert r.status_code == 200 and "M6 x 5 mm" in r.text and "Nano Every" in r.text


def test_missing_location_offers_to_make_it(client, inv):
    r = client.get("/location/school/C12/9")
    assert r.status_code == 404 and "Make location school/C12/9" in r.text
    r = client.post("/locations", data={"name": "9", "parent": "school/C12"})
    assert r.status_code == 200 and inv.location("school/C12/9") is not None
    assert client.get("/location/a//b").status_code == 404


def test_location_actions(client, inv):
    r = client.post("/location/school/C12", data={"action": "save", "description": "north wall",
                                                  "notes": ""})
    assert r.status_code == 200 and inv.location("school/C12").description == "north wall"
    client.post("/location/school/C12", data={"action": "add_location", "name": "7"})
    assert inv.location("school/C12/7") is not None
    r = client.post("/location/school/C12/7", data={"action": "rename", "name": "8"},
                    follow_redirects=False)
    assert r.headers["location"] == "/location/school/C12/8"
    client.post("/location/school/C12/8", data={"action": "move", "parent": ""})
    assert inv.location("8") is not None and inv.location("8").parent_id is None
    r = client.post("/location/8", data={"action": "delete"}, follow_redirects=False)
    assert r.headers["location"] == "/locations" and inv.location("8") is None


def test_location_action_errors_show_on_the_page(client):
    r = client.post("/location/school/C12/5", data={"action": "delete"})
    assert r.status_code == 400 and "holds items" in r.text
    r = client.post("/location/school/C12", data={"action": "move", "parent": "school/C12/5"})
    assert r.status_code == 400 and "inside itself" in r.text


def test_locations_page(client, inv):
    inv.add_item("loose resistor")
    r = client.get("/locations")
    assert r.status_code == 200
    text = re.sub(r"\s+", " ", r.text)
    assert text.index(">barn<") < text.index(">school<")
    assert "In no location" in text and "loose resistor" in text


def test_healthz(client):
    assert client.get("/healthz").text == "ok"
