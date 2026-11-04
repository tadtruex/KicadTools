"""End-to-end offline test of the capacitor assignment flow.

A scratch copy of the ECG fixture is grouped, one group's requirement is
completed with a voltage floor, candidates are found through fake JLCPCB and
DigiKey callables, the chosen part is accepted into an in-memory parts
database, the write plan is applied to the schematic, and the schematic is
reloaded. The reloaded group is assigned, its requirement is the same one now
stated on the schematic, and a second search returns the database part with no
supplier search call. The remembered settings written on accept are read back
as the defaults for the other groups.

The revision-2 case rewrites one symbol as BarrelJackAdapter C1 (library-style
Value with a size suffix, an informative Description, MPN and DigiKey PN
fields, no LCSC). assess lists the existing part first with no keyword search,
the assign dialog selects it, and accept, plan, write and reload give an
assigned group with the LCSC number filled and the part's voltage rating
written. A second case puts the same part below the stock floor.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

import pytest

from kicadtools.assign import assignment_plan
from kicadtools.partsearch import DATABASE, SEARCH, find_candidates
from kicadtools.partnum import extract_part_numbers
from kicadtools.partsdb import PartsDb
from kicadtools.requirement import (
    DEFAULT,
    EDITED,
    SCHEMATIC,
    part_defaults,
    group_parts,
)
from kicadtools.resolve import iter_bom_part_records
from kicadtools.suppliers import DigiKeyProduct, JlcPart
from kicadtools.writer import write_properties

DATA = Path(__file__).parent / "data"

MPN = "CL10B104KB8NNNC"
LCSC = "C14663"


def _jlc_part() -> JlcPart:
    return JlcPart(
        lcsc=LCSC, mpn=MPN, manufacturer="Samsung Electro-Mechanics",
        library="base", preferred=False, stock=4_000_000, unit_price=0.0021,
        package="0603", description="50V 100nF X7R ±10% 0603",
        attributes={
            "Capacitance": "100nF",
            "Voltage Rating": "50V",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
        },
    )


def _dk_product() -> DigiKeyProduct:
    return DigiKeyProduct(
        mpn=MPN,
        digikey_pns=["1276-1000-2-ND", "1276-1000-1-ND", "1276-1000-6-ND"],
        datasheet_url="https://example.invalid/CL10B104KB8NNNC.pdf",
        quantity_available=1_200_000,
        product_status="Active",
        description="CAP CER 0.1UF 50V X7R 0603",
        parameters={
            "Capacitance": "0.1 µF",
            "Voltage - Rated": "50V",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
            "Package / Case": "0603 (1608 Metric)",
        },
    )


class FakeSuppliers:
    """JLCPCB search / part lookup and a DigiKey client, recording each call."""

    def __init__(self) -> None:
        self.searches: List[str] = []
        self.lookups: List[str] = []
        self.dk_lookups: List[str] = []
        self.configured = True

    def jlc_search(self, keyword: str) -> List[JlcPart]:
        self.searches.append(keyword)
        return [_jlc_part()]

    def jlc_filtered(self, value: str, package: str, category=None) -> List[JlcPart]:
        """The filtered category search: recorded like a keyword search."""
        return self.jlc_search(f"{value} {package}")

    def jlc_part(self, code: str) -> Optional[JlcPart]:
        self.lookups.append(code)
        return _jlc_part() if code == LCSC else None

    def product_for(self, pn: str) -> Optional[DigiKeyProduct]:
        self.dk_lookups.append(pn)
        product = _dk_product()
        if pn == MPN or pn in product.digikey_pns:
            return product
        return None


def _find(req, db, fake, needed):
    return find_candidates(
        req, db=db, jlc_filtered=fake.jlc_filtered, jlc_part=fake.jlc_part,
        digikey=fake, needed=needed,
    )


def _group_for(groups, value_text):
    matches = [g for g in groups if g.requirement.value_text == value_text]
    assert len(matches) == 1
    return matches[0]


def test_capacitor_flow_end_to_end(tmp_path):
    parts_gui = pytest.importorskip("kicadtools.gui.parts")

    board = tmp_path / "ECG.kicad_sch"
    shutil.copy(DATA / "ECG.kicad_sch", board)
    db = PartsDb(":memory:", now=lambda: "2026-01-01T00:00:00+00:00")
    defaults = part_defaults(db.settings())

    # ---- group and complete the requirement --------------------------------
    group = _group_for(group_parts(iter_bom_part_records(board), defaults),
                       "100nF")
    assert not group.assigned
    assert group.requirement.rating_min.value is None
    assert not group.requirement.searchable
    req = group.requirement.edited(rating_min=16.0)
    assert req.searchable
    assert req.package.value == "0603" and req.package.source == SCHEMATIC
    assert req.dielectrics.value == ("X7R",) and req.dielectrics.source == DEFAULT
    assert req.tolerance_max_pct.value == 10.0
    assert req.rating_min.source == EDITED

    # ---- first search: empty database, so the suppliers are searched ------
    fake = FakeSuppliers()
    result = _find(req, db, fake, group.quantity)
    assert result.problem == ""
    assert fake.searches == ["100nF 0603"]
    assert [c.mpn for c in result.candidates] == [MPN]
    cand = result.candidates[0]
    assert cand.source == SEARCH
    assert cand.lcsc == LCSC
    assert cand.jlc_library == "basic"
    assert cand.digikey_pn == "1276-1000-1-ND"     # cut tape preferred

    # ---- accept into the database -------------------------------------------
    choice = cand.choice()
    assert choice.digikey_packaging == "cut tape"
    part_id = db.accept_part(choice)
    db.record_usage(part_id, "ECG/ECG.kicad_sch", group.references)
    for key, value in parts_gui.requirement_settings(req).items():
        db.set_setting(key, value)
    assert db.get_setting("cap.voltage_min") == "16V"

    # ---- plan, write, reload -------------------------------------------------
    plan = assignment_plan(group, req, cand)
    assert set(plan) == {board}
    for src, uuid_to_props in plan.items():
        write_properties(src, uuid_to_props)

    defaults = part_defaults(db.settings())
    groups = group_parts(iter_bom_part_records(board), defaults)
    reloaded = _group_for(groups, "100nF")
    assert reloaded.references == group.references
    assert reloaded.assigned
    for m in reloaded.members:
        pn = extract_part_numbers(m.props)
        assert (pn.mpn, pn.digikey_pn, pn.lcsc) == (MPN, "1276-1000-1-ND", LCSC)
        assert pn.manufacturer == "Samsung Electro-Mechanics"
        assert m.props.get("Footprint") == "Capacitor_SMD:C_0603_1608Metric"
        assert m.props.get("Intent:Type") == "capacitor"
        assert m.props.get("Voltage:Min") == "16V"
        assert m.props.get("Class:Preferred") == "X7R"
        assert m.props.get("Tolerance:Preferred") == "10%"
        assert not m.intent.intent_guessed

    # The written values parse back to the same requirement, now stated on the
    # schematic.
    again = reloaded.requirement
    assert again.nominal == pytest.approx(req.nominal)
    assert again.package.value == req.package.value
    assert again.rating_min.value == pytest.approx(16.0)
    assert again.dielectrics.value == req.dielectrics.value
    assert again.tolerance_max_pct.value == pytest.approx(10.0)
    for f in (again.package, again.rating_min, again.dielectrics,
              again.tolerance_max_pct):
        assert f.source == SCHEMATIC

    # ---- second search: the database part, no supplier search --------------
    fake2 = FakeSuppliers()
    result2 = _find(again, db, fake2, reloaded.quantity)
    assert fake2.searches == []
    assert [c.mpn for c in result2.candidates] == [MPN]
    hit = result2.candidates[0]
    assert hit.source == DATABASE
    assert hit.db_part_id == part_id
    assert hit.usage_count == reloaded.quantity
    assert (hit.lcsc, hit.digikey_pn) == (LCSC, "1276-1000-1-ND")
    assert hit.jlc_stock == 4_000_000                 # refreshed via jlc_part
    assert hit.digikey_stock == 1_200_000             # refreshed via DigiKey
    assert fake2.lookups == [LCSC]
    # A database hit is assignable as it stands.
    assert assignment_plan(reloaded, again, hit)

    # ---- remembered settings become the other groups' defaults -------------
    other = _group_for(groups, "1u")
    assert other.requirement.rating_min.value == pytest.approx(16.0)
    assert other.requirement.rating_min.source == DEFAULT
    assert other.requirement.searchable
    db.close()


# --------------------------------------------------------------------------- #
# Revision 2: a symbol with an existing part, modelled on BarrelJackAdapter C1.
# --------------------------------------------------------------------------- #

TY_MPN = "TMK212BBJ106KG-T"
TY_DK = "587-2985-1-ND"
TY_LCSC = "C90146"
TY_DESCRIPTION = "10uF, 25V, X5R, 805 package"


def _ty_jlc_part() -> JlcPart:
    return JlcPart(
        lcsc=TY_LCSC, mpn=TY_MPN, manufacturer="TAIYO YUDEN",
        library="expand", preferred=False, stock=5000, unit_price=0.012,
        package="0805", description="25V 10uF X5R ±10% 0805",
        attributes={
            "Capacitance": "10uF",
            "Voltage Rating": "25V",
            "Temperature Coefficient": "X5R",
            "Tolerance": "±10%",
        },
    )


def _ty_dk_product() -> DigiKeyProduct:
    return DigiKeyProduct(
        mpn=TY_MPN,
        digikey_pns=["587-2985-2-ND", TY_DK, "587-2985-6-ND"],
        datasheet_url="https://example.invalid/TMK212BBJ106KG-T.pdf",
        quantity_available=80_000,
        product_status="Active",
        description="CAP CER 10UF 25V X5R 0805",
        parameters={
            "Capacitance": "10 µF",
            "Voltage - Rated": "25V",
            "Temperature Coefficient": "X5R",
            "Tolerance": "±10%",
            "Package / Case": "0805 (2012 Metric)",
        },
    )


class FakeTaiyoSuppliers(FakeSuppliers):
    """The suppliers for the existing-part case: DigiKey resolves the MPN or
    any of its DigiKey PNs; JLCPCB returns the part for an exact MPN search or
    its LCSC number, and nothing for a parametric keyword search."""

    def jlc_search(self, keyword: str) -> List[JlcPart]:
        self.searches.append(keyword)
        return [_ty_jlc_part()] if keyword == TY_MPN else []

    def jlc_part(self, code: str) -> Optional[JlcPart]:
        self.lookups.append(code)
        return _ty_jlc_part() if code == TY_LCSC else None

    def product_for(self, pn: str) -> Optional[DigiKeyProduct]:
        self.dk_lookups.append(pn)
        product = _ty_dk_product()
        if pn == TY_MPN or pn in product.digikey_pns:
            return product
        return None


def _existing_part_board(tmp_path) -> Path:
    """A copy of the ECG fixture whose C15 (a one-member group) is rewritten
    as BarrelJackAdapter C1: library-style Value with a size suffix, an
    informative Description, MPN and DigiKey PN fields, an empty LCSC field."""
    board = tmp_path / "ECG.kicad_sch"
    shutil.copy(DATA / "ECG.kicad_sch", board)
    defaults = part_defaults({})
    c15 = [m for g in group_parts(iter_bom_part_records(board), defaults)
           for m in g.members if m.reference == "C15"]
    assert len(c15) == 1
    write_properties(board, {c15[0].symbol_uuid: {
        "Value": "C_10U_805",
        "Description": TY_DESCRIPTION,
        "MPN": TY_MPN,
        "DigiKeyPN": TY_DK,
        "LCSC": "",
        "Footprint": "Capacitor_SMD:C_0805_2012Metric",
    }})
    return board


def test_capacitor_flow_existing_part(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from kicadtools.partsearch import CURRENT, assess
    from kicadtools.gui.assign_dialog import AssignDialog
    from kicadtools.gui.parts import requirement_settings
    from kicadtools.requirement import DESCRIPTION, PART

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    board = _existing_part_board(tmp_path)
    db = PartsDb(":memory:", now=lambda: "2026-01-01T00:00:00+00:00")
    defaults = part_defaults(db.settings())

    # ---- the requirement before any lookup: Value, footprint, Description --
    group = _group_for(group_parts(iter_bom_part_records(board), defaults),
                       "C_10U_805")
    assert group.references == ["C15"]
    assert not group.assigned
    assert (group.existing.mpn, group.existing.digikey_pn, group.existing.lcsc) == (
        TY_MPN, TY_DK, None)
    first = group.requirement
    assert first.nominal == pytest.approx(10e-6)
    assert first.nominal_source == SCHEMATIC
    assert first.package.value == "0805" and first.package.source == SCHEMATIC
    assert first.rating_min.value == pytest.approx(25.0)
    assert first.rating_min.source == DESCRIPTION
    assert first.dielectrics.value == ("X5R",)
    assert first.dielectrics.source == DESCRIPTION
    assert first.conflicts == ()
    assert first.searchable

    # ---- assess: the current part first, no keyword search ----------------
    fake = FakeTaiyoSuppliers()
    result = assess(group, defaults, db=db, jlc_search=fake.jlc_search, jlc_filtered=fake.jlc_filtered,
                    jlc_part=fake.jlc_part, digikey=fake, needed=group.quantity)
    assert result.problem == ""
    assert fake.searches == [TY_MPN]            # the exact-MPN lookup only
    assert fake.lookups == []                   # no LCSC field to read
    assert set(fake.dk_lookups) == {TY_MPN, TY_DK}
    lookup = result.existing
    assert lookup is not None and len(lookup.options) == 1
    assert lookup.evidence.label == f"current part {TY_DK} (DigiKey)"
    req = result.requirement
    assert req.rating_min.value == pytest.approx(25.0)
    assert req.rating_min.source == PART
    assert req.dielectrics.value == ("X5R",) and req.dielectrics.source == PART
    assert req.tolerance_max_pct.value == pytest.approx(10.0)
    assert req.tolerance_max_pct.source == PART
    assert [c.source for c in result.candidates] == [CURRENT]
    cand = result.candidates[0]
    assert (cand.mpn, cand.lcsc, cand.digikey_pn) == (TY_MPN, TY_LCSC, TY_DK)
    assert cand.manufacturer == "TAIYO YUDEN"
    assert (cand.jlc_stock, cand.digikey_stock) == (5000, 80_000)

    # ---- the dialog: the current part is selected and acceptable ----------
    dialog = AssignDialog(group, group.requirement, 1, None, defaults=defaults)
    requests = []
    dialog.search_requested.connect(lambda r, s: requests.append((r, s)))
    assert dialog.request_search(False)
    assert requests == [(None, False)]          # assess derives the requirement
    dialog.show_result(result, dialog.request_id)
    assert requests == [(None, False)]          # no second request
    chosen = dialog.chosen()
    assert chosen is cand
    final_req = dialog.requirement_for(chosen)
    assert final_req == req
    dialog.deleteLater()
    # Values from the part and the Description never become settings.
    assert requirement_settings(final_req) == {}

    # ---- accept into the database -------------------------------------------
    choice = chosen.choice()
    assert choice.digikey_packaging == "cut tape"
    part_id = db.accept_part(choice)
    db.record_usage(part_id, "ECG/ECG.kicad_sch", group.references)
    hits = db.find_parts(kind="capacitor", nominal=10e-6, package="0805",
                              rating_min=25.0, dielectrics=("X5R",),
                              tolerance_max_pct=10.0)
    assert [(h.mpn, h.lcsc, h.digikey_pn) for h in hits] == [(TY_MPN, TY_LCSC, TY_DK)]
    assert db.settings() == {}

    # ---- plan, write, reload -------------------------------------------------
    plan = assignment_plan(group, final_req, chosen)
    for src, uuid_to_props in plan.items():
        write_properties(src, uuid_to_props)
    groups = group_parts(iter_bom_part_records(board),
                              part_defaults(db.settings()))
    reloaded = _group_for(groups, "C_10U_805")
    assert reloaded.references == ["C15"]
    assert reloaded.assigned
    (m,) = reloaded.members
    pn = extract_part_numbers(m.props)
    assert (pn.mpn, pn.digikey_pn, pn.lcsc) == (TY_MPN, TY_DK, TY_LCSC)
    assert pn.manufacturer == "TAIYO YUDEN"
    assert m.props.get("Value") == "C_10U_805"
    assert m.props.get("Description") == TY_DESCRIPTION
    assert m.props.get("Footprint") == "Capacitor_SMD:C_0805_2012Metric"
    assert m.props.get("Voltage:Min") == "25V"
    assert m.props.get("Class:Preferred") == "X5R"  # read from the part; X5R or better
    assert m.props.get("Tolerance:Preferred") == "10%"

    again = reloaded.requirement
    assert again.conflicts == ()
    for f in (again.package, again.rating_min, again.dielectrics,
              again.tolerance_max_pct):
        assert f.source == SCHEMATIC
    assert again.rating_min.value == pytest.approx(25.0)

    # ---- second assess: the current part again, now also the database part -
    fake2 = FakeTaiyoSuppliers()
    result2 = assess(reloaded, defaults, db=db, jlc_search=fake2.jlc_search, jlc_filtered=fake2.jlc_filtered,
                     jlc_part=fake2.jlc_part, digikey=fake2,
                     needed=reloaded.quantity)
    assert fake2.searches == []                 # the LCSC field is read instead
    assert TY_LCSC in fake2.lookups
    assert [c.source for c in result2.candidates] == [CURRENT]
    hit = result2.candidates[0]
    assert hit.db_part_id == part_id
    assert hit.usage_count == 1
    db.close()


def test_capacitor_flow_existing_part_below_stock_floor(tmp_path):
    """The same symbol with only 300 at JLCPCB: the current part stays,
    with the stock-floor reason as a warning, and the keyword search runs."""
    from kicadtools.partsearch import CURRENT_LABEL, assess

    class LowStock(FakeTaiyoSuppliers):
        def jlc_search(self, keyword: str) -> List[JlcPart]:
            self.searches.append(keyword)
            if keyword != TY_MPN:
                return []
            part = _ty_jlc_part()
            part.stock = 300
            return [part]

    board = _existing_part_board(tmp_path)
    db = PartsDb(":memory:", now=lambda: "2026-01-01T00:00:00+00:00")
    defaults = part_defaults(db.settings())
    group = _group_for(group_parts(iter_bom_part_records(board), defaults),
                       "C_10U_805")
    fake = LowStock()
    result = assess(group, defaults, db=db, jlc_search=fake.jlc_search, jlc_filtered=fake.jlc_filtered,
                    jlc_part=fake.jlc_part, digikey=fake, needed=group.quantity)
    current = result.candidates[0]
    assert (current.mpn, current.source) == (TY_MPN, "current")
    assert current.stock_warning == "JLCPCB stock 300, need at least 1000"
    assert CURRENT_LABEL not in dict(result.rejected)
    assert fake.searches[0] == TY_MPN
    assert len(fake.searches) > 1               # the keyword search ran
    db.close()
