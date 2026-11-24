"""The lab inventory web app (kicadtools/web, INVENTORY_PLAN.md step 4)."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kicadtools.inventorydb import InventoryDb  # noqa: E402
from kicadtools.partsdb import PartsDb  # noqa: E402
from kicadtools.web.app import bin_url, create_app, fmt_qty, parse_quantity, parse_tags  # noqa: E402

HX = {"HX-Request": "true", "HX-Target": "results"}


@pytest.fixture
def inv():
    inv = InventoryDb(PartsDb(":memory:"))
    inv.add_bin("C12", site="school", location="rack by the bench")
    inv.add_bin("5", "C12", description="dev boards")
    inv.add_bin("Barn shelf", site="barn")
    inv.add_item("Nano Every", bin="C12/5", quantity=6, category="Dev board",
                 tags=["arduino"], description="ATmega4809 board")
    inv.add_item("M6 x 5 mm pan head screw", bin="Barn shelf", quantity=100,
                 category="Hardware")
    return inv


@pytest.fixture
def client(inv):
    with TestClient(create_app(inv)) as c:
        yield c


def _id(inv, name):
    return next(i.id for i in inv.items() if i.name == name)


# ----- helpers ---------------------------------------------------------------

def test_helpers():
    assert bin_url("Barn shelf/2") == "/bin/Barn%20shelf/2"
    assert fmt_qty(None) == "—" and fmt_qty(50) == "~50"
    assert parse_quantity(" ~50 ") == 50 and parse_quantity("") is None
    assert parse_tags("arduino, avr ,, ") == ["arduino", "avr"]


# ----- search ----------------------------------------------------------------

def test_front_page_and_search(client):
    r = client.get("/")
    assert r.status_code == 200 and "Type to search" in r.text
    assert 'src="/static/htmx.min.js"' in r.text
    r = client.get("/", params={"q": "arduino"})
    assert "Nano Every" in r.text and "M6" not in r.text
    assert 'href="/bin/C12/5"' in r.text and "rack by the bench" in r.text


def test_htmx_search_returns_only_the_results(client):
    r = client.get("/", params={"q": "screw"}, headers=HX)
    assert "M6 x 5 mm pan head screw" in r.text
    assert "<html" not in r.text and "<form" not in r.text


def test_nothing_found_offers_to_add(client):
    r = client.get("/", params={"q": "stlink v3"})
    assert "Nothing found" in r.text and "/add?name=stlink+v3" in r.text


def test_site_filter_is_remembered(client):
    r = client.get("/", params={"q": "screw", "site": "school"})
    assert "M6" not in r.text
    assert client.cookies.get("site") == "school"
    r = client.get("/", params={"q": "screw"})            # no site: the cookie's
    assert "M6" not in r.text and "search every site" in r.text
    r = client.get("/", params={"q": "screw", "site": ""})  # all sites again
    assert "M6" in r.text


def test_category_filter_alone_lists_the_category(client):
    r = client.get("/", params={"category": "hardware"})
    assert "M6 x 5 mm" in r.text and "Nano Every" not in r.text


def test_names_are_escaped(client, inv):
    inv.add_item("<script>alert(1)</script>")
    r = client.get("/", params={"q": "script"})
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


# ----- items -----------------------------------------------------------------

def test_item_page(client, inv):
    r = client.get(f"/item/{_id(inv, 'Nano Every')}")
    assert r.status_code == 200
    assert "Nano Every" in r.text and "~6" in r.text and 'value="arduino"' in r.text
    assert client.get("/item/9999").status_code == 404


def test_item_save_and_move(client, inv):
    item_id = _id(inv, "Nano Every")
    r = client.post(f"/item/{item_id}", data={
        "name": "Nano Every", "quantity": "~4", "bin": "C12/6", "tags": "arduino, avr",
        "category": "Dev board", "description": "", "mpn": "ABX00028"})
    assert r.status_code == 200 and "Saved." in r.text      # followed the redirect
    item = inv.item(item_id)
    assert item.quantity == 4 and item.bin_path == "C12/6" and item.mpn == "ABX00028"
    assert item.tags == ("arduino", "avr") and item.description is None
    assert inv.bin("C12/6") is not None                      # a new bin path is made


def test_item_save_error_keeps_what_was_typed(client, inv):
    item_id = _id(inv, "Nano Every")
    r = client.post(f"/item/{item_id}", data={"name": "Nano Every", "quantity": "lots",
                                              "mpn": "TYPED-MPN"})
    assert r.status_code == 400 and "whole number" in r.text
    assert 'value="lots"' in r.text and 'value="TYPED-MPN"' in r.text
    assert inv.item(item_id).quantity == 6


def test_quantity_buttons(client, inv):
    item_id = _id(inv, "Nano Every")
    r = client.post(f"/item/{item_id}/qty", data={"delta": "1"},
                    headers={"HX-Request": "true"})
    assert r.text.strip() == '<span id="qty" class="bigqty">~7</span>'
    r = client.post(f"/item/{item_id}/qty", data={"delta": "-100"})   # no JS: redirect
    assert r.status_code == 200 and inv.item(item_id).quantity == 0


def test_delete_item_goes_back_to_its_bin(client, inv):
    item_id = _id(inv, "Nano Every")
    r = client.post(f"/item/{item_id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bin/C12/5"
    assert inv.item(item_id) is None


# ----- quick add -------------------------------------------------------------

def test_quick_add_keeps_the_bin_for_the_next_item(client, inv):
    r = client.get("/add", params={"bin": "C12/5"})
    assert 'value="C12/5"' in r.text
    r = client.post("/add", data={"name": "STLink V3", "bin": "C12/5", "quantity": "2",
                                  "tags": "stm32"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/add?bin=C12%2F5&added=")
    r = client.get(r.headers["location"])
    assert "Added" in r.text and "STLink V3" in r.text and 'value="C12/5"' in r.text
    item = inv.item(_id(inv, "STLink V3"))
    assert item.quantity == 2 and item.tags == ("stm32",)


def test_quick_add_without_a_name_is_refused(client, inv):
    r = client.post("/add", data={"name": " ", "bin": "C12/5"})
    assert r.status_code == 400 and "needs a name" in r.text
    assert len(inv.items()) == 2


def test_add_prefills_the_name_from_a_failed_search(client):
    r = client.get("/add", params={"name": "stlink v3"})
    assert 'value="stlink v3"' in r.text


# ----- bins ------------------------------------------------------------------

def test_bin_page_lists_everything_inside(client):
    r = client.get("/bin/C12")
    assert r.status_code == 200
    assert "Nano Every" in r.text and 'href="/bin/C12/5"' in r.text
    assert "rack by the bench" in r.text and "school" in r.text
    assert "/add?bin=C12" in r.text


def test_bin_page_with_a_space_in_its_name(client):
    r = client.get("/bin/Barn%20shelf")
    assert r.status_code == 200 and "M6 x 5 mm" in r.text


def test_missing_bin_offers_to_make_it(client, inv):
    r = client.get("/bin/C12/9")
    assert r.status_code == 404 and "Make bin C12/9" in r.text
    r = client.post("/bins", data={"name": "9", "parent": "C12"})
    assert r.status_code == 200 and inv.bin("C12/9") is not None


def test_bin_actions(client, inv):
    r = client.post("/bin/C12", data={"action": "save", "site": "school",
                                      "location": "north wall", "description": "", "notes": ""})
    assert r.status_code == 200 and inv.bin("C12").location == "north wall"
    client.post("/bin/C12", data={"action": "add_bin", "name": "7"})
    assert inv.bin("C12/7") is not None
    r = client.post("/bin/C12/7", data={"action": "rename", "name": "8"},
                    follow_redirects=False)
    assert r.headers["location"] == "/bin/C12/8"
    client.post("/bin/C12/8", data={"action": "move", "parent": ""})
    assert inv.bin("8") is not None and inv.bin("8").parent_id is None
    r = client.post("/bin/8", data={"action": "delete"}, follow_redirects=False)
    assert r.headers["location"] == "/bins" and inv.bin("8") is None


def test_bin_action_errors_show_on_the_page(client):
    r = client.post("/bin/C12", data={"action": "delete"})
    assert r.status_code == 400 and "holds" in r.text
    r = client.post("/bin/C12", data={"action": "move", "parent": "C12/5"})
    assert r.status_code == 400 and "inside itself" in r.text


def test_bins_page(client, inv):
    inv.add_item("loose resistor")
    r = client.get("/bins")
    assert r.status_code == 200
    text = re.sub(r"\s+", " ", r.text)
    assert text.index("barn") < text.index("school")
    assert "In no bin" in text and "loose resistor" in text


def test_healthz(client):
    assert client.get("/healthz").text == "ok"
