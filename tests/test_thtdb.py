"""Tests for through-hole parts in the parts database (kicadtools.thtdb,
PartsDb.find_tht; THT_PLAN.md step 8), offline with fake DigiKey clients."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from kicadtools.headersearch import HeaderRun
from kicadtools.namedsearch import find_named_candidates
from kicadtools.partsdb import IcChoice, PartsDb, ThtChoice
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtdb import db_key, signature_key, stored_candidates, tht_choice, value_key
from kicadtools.thtfootprint import parse_signature

H1x04 = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
H1x04_OTHER_LIB = parse_signature("MyLib:PinHeader_1x04_P2.54mm_Vertical")
H1x04_RA = parse_signature("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Horizontal")
JST = parse_signature("Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal")
POT = parse_signature("Potentiometer_THT:Potentiometer_Bourns_3386P_Vertical")


def _p(mpn, stock=10000, status="Active", maker="Sullins Connector Solutions", **params):
    return DigiKeyProduct(
        mpn=mpn, manufacturer=maker, quantity_available=stock, unit_price=0.1,
        product_status=status, digikey_pns=[f"S{mpn}-ND"], parameters=params,
    )


class FakeDigiKey:
    def __init__(self, products=(), search=None):
        self.configured = True
        self.products = {}
        for p in products:
            self.products[p.mpn] = p
            for pn in p.digikey_pns:
                self.products[pn] = p
        self.search = search or {}
        self.lookups, self.keywords, self.category_calls = [], [], []

    def product_for(self, pn):
        self.lookups.append(pn)
        return self.products.get(pn)

    def keyword_search(self, keyword):
        self.keywords.append(keyword)
        return list(self.search.get(keyword, []))

    def category_search(self, keyword, category, filters, *, manufacturers, min_stock, limit):
        self.category_calls.append(dict(filters))
        products = list(self.search.get("category", []))
        return SimpleNamespace(products=products, total=len(products), unmatched=(), error="")


def _accept(db, product, sig, value=""):
    return db.accept_part(tht_choice(product, sig, value))


# --- keys -------------------------------------------------------------------------


def test_signature_key_ignores_the_library():
    assert signature_key(H1x04) == signature_key(H1x04_OTHER_LIB)
    assert signature_key(H1x04) == "family=header;rows=1;per_row=4;pitch=2.54;orientation=vertical"
    assert signature_key(H1x04) != signature_key(H1x04_RA)


def test_named_part_key_holds_the_token_and_the_value():
    assert "mpn=S3B-PH-K" in signature_key(JST)
    assert db_key(POT, "200 k") == (signature_key(POT), "200K")
    assert db_key(H1x04, "Conn_01x04") == (signature_key(H1x04), "")
    assert value_key(" 10k ") == "10K"


def test_tht_choice_from_product():
    c = tht_choice(_p("PREC004SAAN-RC", **{"Number of Positions": "4"}), H1x04, "Conn_01x04")
    assert (c.mpn, c.family, c.value_key, c.positions, c.digikey_pn) == (
        "PREC004SAAN-RC", "header", "", 4, "SPREC004SAAN-RC-ND")
    assert c.footprint == H1x04.lib_id
    assert c.lcsc is None


# --- database -----------------------------------------------------------------------


def test_accept_find_round_trip_and_usage_order():
    db = PartsDb()
    a = _accept(db, _p("PREC004SAAN-RC"), H1x04)
    b = _accept(db, _p("TSW-104-07-T-S", maker="Samtec Inc."), H1x04)
    db.record_usage(b, "BarrelJackAdapter.kicad_sch", ["J2", "J4"])
    found = db.find_tht(*db_key(H1x04, ""))
    assert [r.mpn for r in found] == ["TSW-104-07-T-S", "PREC004SAAN-RC"]
    assert found[0].usage_count == 2 and found[1].part_id == a
    assert found[1].digikey_pn == "SPREC004SAAN-RC-ND"
    assert db.find_tht(*db_key(H1x04_RA, "")) == []


def test_value_key_separates_named_parts():
    db = PartsDb()
    _accept(db, _p("3386P-1-204LF", maker="Bourns Inc."), POT, "200K")
    assert [r.mpn for r in db.find_tht(*db_key(POT, "200K"))] == ["3386P-1-204LF"]
    assert db.find_tht(*db_key(POT, "10K")) == []


def test_rejected_is_not_found():
    db = PartsDb()
    db.reject_part(tht_choice(_p("PREC004SAAN-RC"), H1x04, ""))
    assert db.find_tht(*db_key(H1x04, "")) == []
    assert db.is_rejected("Sullins Connector Solutions", "PREC004SAAN-RC")


def test_type_change_keeps_one_parameter_row():
    db = PartsDb()
    pid = _accept(db, _p("X-1"), H1x04)
    db.accept_part(IcChoice(mpn="X-1", manufacturer="Sullins Connector Solutions"))
    assert db.conn.execute("SELECT COUNT(*) FROM tht WHERE part_id = ?", (pid,)).fetchone()[0] == 0
    assert db.find_tht(*db_key(H1x04, "")) == []


def test_choice_needs_a_signature():
    import pytest
    with pytest.raises(ValueError):
        PartsDb().accept_part(ThtChoice(mpn="A", manufacturer="B", family="header", signature=""))


def test_existing_file_gains_the_table(tmp_path: Path):
    path = tmp_path / "parts.sqlite"
    PartsDb(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE tht")
    conn.commit()
    conn.close()
    db = PartsDb(path)
    _accept(db, _p("PREC004SAAN-RC"), H1x04)
    assert len(db.find_tht(*db_key(H1x04, ""))) == 1


# --- stored candidates and the searches --------------------------------------------


def test_stored_candidates_are_refreshed_and_checked():
    db = PartsDb()
    _accept(db, _p("PREC004SAAN-RC"), H1x04)
    _accept(db, _p("OLD-4"), H1x04)
    _accept(db, _p("GONE-4"), H1x04)
    dk = FakeDigiKey([_p("PREC004SAAN-RC", stock=5000), _p("OLD-4", status="Obsolete")])
    s = stored_candidates(db, dk, H1x04, "", needed=5)
    assert [r.mpn for r, _ in s.parts] == ["PREC004SAAN-RC"]
    assert dict(s.rejected) == {"OLD-4": "DigiKey status Obsolete", "GONE-4": "not found at DigiKey"}
    assert "SPREC004SAAN-RC-ND" in dk.lookups  # looked up by its DigiKey PN


def test_named_search_offers_the_stored_part_without_searching():
    db = PartsDb()
    _accept(db, _p("S3B-PH-K-S", maker="JST Sales America Inc."), JST, "I2C_Conn")
    dk = FakeDigiKey([_p("S3B-PH-K-S", maker="JST Sales America Inc.")])
    r = find_named_candidates(JST, {"Value": "I2C_Conn"}, digikey=dk, needed=5, db=db)
    assert [c.mpn for c in r.candidates] == ["S3B-PH-K-S"]
    assert r.proposed.stored and dk.keywords == []


def test_named_search_runs_when_the_stored_part_fails_and_skips_rejected():
    db = PartsDb()
    _accept(db, _p("S3B-PH-K-S"), JST, "I2C_Conn")
    db.reject_part(tht_choice(_p("S3B-PH-K-K"), JST, "I2C_Conn"))
    hits = [_p("S3B-PH-K-S"), _p("S3B-PH-K-K"), _p("S3B-PH-K-E")]
    full = [_p("S3B-PH-K-S", stock=0), _p("S3B-PH-K-K"), _p("S3B-PH-K-E")]
    dk = FakeDigiKey(full, {"S3B-PH-K": hits})
    r = find_named_candidates(JST, {"Value": "I2C_Conn"}, digikey=dk, needed=5, db=db)
    assert [c.mpn for c in r.candidates] == ["S3B-PH-K-E"]
    reasons = dict(r.rejected)
    assert reasons["S3B-PH-K-K"] == "rejected in the parts database"
    assert reasons["S3B-PH-K-S"].startswith("DigiKey stock 0")


def test_header_run_offers_the_stored_part_without_searching():
    db = PartsDb()
    _accept(db, _p("PREC004SAAN-RC"), H1x04)
    dk = FakeDigiKey([_p("PREC004SAAN-RC")])
    r = HeaderRun(dk, db=db).search(H1x04, needed=5)
    assert r.from_database and [p.mpn for p in r.candidates] == ["PREC004SAAN-RC"]
    assert dk.category_calls == []


def test_header_run_leaves_rejected_parts_out():
    db = PartsDb()
    db.reject_part(tht_choice(_p("CHEAP-4"), H1x04, ""))
    dk = FakeDigiKey(search={"category": [_p("CHEAP-4"), _p("PREC004SAAN-RC")]})
    r = HeaderRun(dk, db=db).search(H1x04, needed=5)
    assert [p.mpn for p in r.candidates] == ["PREC004SAAN-RC"]
    assert r.rejected == [("CHEAP-4", "rejected in the parts database")]
