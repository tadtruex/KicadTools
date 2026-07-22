"""Tests for the per-LCSC rotation override store (kicadtools.partrotation)."""
from kicadtools.partrotation import PartRotationStore, manual_rotation_for_refs


def _store():
    return PartRotationStore(":memory:")


# ---- store round-trip ------------------------------------------------

def test_unset_part_returns_none():
    assert _store().get("C123456") is None


def test_set_and_get_round_trip():
    s = _store()
    s.set("C123456", 180.0)
    assert s.get("C123456") == 180.0


def test_set_overwrites():
    s = _store()
    s.set("C1", 90.0)
    s.set("C1", 270.0)
    assert s.get("C1") == 270.0


def test_set_normalizes_mod_360():
    s = _store()
    s.set("C1", 540.0)  # 540 % 360 == 180
    assert s.get("C1") == 180.0


def test_set_zero_clears_entry():
    s = _store()
    s.set("C1", 180.0)
    s.set("C1", 0.0)
    assert s.get("C1") is None
    assert "C1" not in s.all()


def test_set_full_turn_clears_entry():
    s = _store()
    s.set("C1", 90.0)
    s.set("C1", 360.0)  # == 0 mod 360, so it's a no-override
    assert s.get("C1") is None


def test_clear():
    s = _store()
    s.set("C1", 180.0)
    s.clear("C1")
    assert s.get("C1") is None


def test_empty_lcsc_is_ignored():
    s = _store()
    s.set("", 180.0)
    assert s.get("") is None
    assert s.all() == {}


def test_all_lists_every_override():
    s = _store()
    s.set("C1", 90.0)
    s.set("C2", 180.0)
    assert s.all() == {"C1": 90.0, "C2": 180.0}


def test_persists_across_connections(tmp_path):
    path = tmp_path / "cache.sqlite"
    s1 = PartRotationStore(path)
    s1.set("C1", 180.0)
    s1.close()
    s2 = PartRotationStore(path)
    assert s2.get("C1") == 180.0


# ---- manual_rotation_for_refs bridge ---------------------------------

def test_manual_rotation_maps_refs_with_overrides():
    s = _store()
    s.set("C1", 180.0)
    ref_to_lcsc = {"Q1": "C1", "Q2": "C1", "R1": "C2"}
    assert manual_rotation_for_refs(ref_to_lcsc, s) == {"Q1": 180.0, "Q2": 180.0}


def test_manual_rotation_omits_refs_without_override():
    s = _store()
    ref_to_lcsc = {"R1": "C2", "R2": ""}
    assert manual_rotation_for_refs(ref_to_lcsc, s) == {}
