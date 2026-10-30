"""
Parts tab: one row per capacitor or resistor requirement group, plus the
background worker that searches for candidate parts for one group.

Each row shows the group's requirement (Value, Package, Rating — the voltage
floor of a capacitor, the power floor of a resistor —, Dielectric for a
capacitor, Tolerance), its quantity and references, the part number already on the
schematic, its status and the chosen MPN. A requirement cell whose value came
from a default, an edit, the existing part or the Description is marked
``(default)`` / ``(edited)`` / ``(part)`` / ``(description)``; a field with no
value shows a dash.

Status is ``queued`` once a candidate has been accepted for the group this
session (the write plan is held in the model until File > Save), ``conflict``
when two sources state different values or packages or the part-number
fields name different parts, ``assigned`` when every member already has its
four part numbers and a footprint on the schematic, ``not searchable`` for a
footprint with no chip size, else ``unassigned``. A conflict ranks above
``assigned``: a fully numbered group whose Value disagrees with its part still
needs a schematic fix. :meth:`PartsTableModel.accepted_edits` merges the queued
plans into the ``{file: {uuid: {key: value}}}`` shape the writer applies; a
value of ``None`` removes that property.

The existing-part lookup of a group (from the assign dialog) is kept per row
(:meth:`PartsTableModel.record_lookup`), so the row shows the part's evidence
and warnings after the dialog closes, :meth:`PartsTableModel.set_defaults`
keeps them, and a reopened dialog reuses the lookup.

:class:`CandidateWorker` opens its own parts-database and LCSC-cache
connections on the worker thread and runs :func:`kicadtools.partsearch.assess`
there (existing-part lookup, then the candidate search).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor

from kicadtools.partnum import extract_part_numbers
from kicadtools.partsdb import PartsDb
from kicadtools.partsearch import Candidate, ExistingLookup, assess, requirement_from_lookup
from kicadtools.parttypes import PartType
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    SCHEMATIC,
    UNSET,
    PartRequirement,
    RequirementGroup,
    Sourced,
    dielectric_band,
    tolerance_key,
)
from kicadtools.resolve import Cache, cached_jlcpcb_part
from kicadtools.suppliers import DigiKeyClient, jlcpcb_category_search, jlcpcb_search
from kicadtools.units import fmt_percent

PARTS_COLUMNS = ["Value", "Package", "Rating", "Dielectric", "Tolerance", "Qty",
                 "References", "Current part", "Status", "Chosen MPN"]
COL_VALUE = PARTS_COLUMNS.index("Value")
COL_PACKAGE = PARTS_COLUMNS.index("Package")
COL_RATING = PARTS_COLUMNS.index("Rating")
COL_DIELECTRIC = PARTS_COLUMNS.index("Dielectric")
COL_TOLERANCE = PARTS_COLUMNS.index("Tolerance")
COL_QTY = PARTS_COLUMNS.index("Qty")
COL_REFERENCES = PARTS_COLUMNS.index("References")
COL_CURRENT = PARTS_COLUMNS.index("Current part")
COL_STATUS = PARTS_COLUMNS.index("Status")
COL_MPN = PARTS_COLUMNS.index("Chosen MPN")

STATUS_ASSIGNED = "assigned"
STATUS_QUEUED = "queued"
STATUS_UNASSIGNED = "unassigned"
# Two sources state different values or packages (CAPACITOR_PLAN.md,
# Revision 2). Fixed on the schematic for now.
STATUS_CONFLICT = "conflict"
# A footprint with no chip-size code (radial or axial electrolytic). Those
# packages are a planned addition (CAPACITOR_PLAN.md, TBD 2).
STATUS_NOT_SEARCHABLE = "not searchable"

DASH = "—"

# A lighter amber than the LCSC tab's stock tint: an unassigned group is work
# to do, not a stock problem.
_UNASSIGNED_TINT = QColor(230, 160, 0, 40)
# A conflict row needs a schematic change before a search.
_CONFLICT_TINT = QColor(220, 50, 50, 50)

# The cell suffix per source; a value stated on the schematic has none.
_SOURCE_MARKERS = {
    DEFAULT: "default",
    EDITED: "edited",
    RELAXED: "limit dropped",
    PART: "part",
    DESCRIPTION: "description",
}

# The plan shape: {source_file: {symbol_uuid: {property_key: value or None}}}.
Plan = Dict[Path, Dict[str, Dict[str, Optional[str]]]]


# --------------------------------------------------------------------------- #
# Cell text — pure, so it is testable without a window.
# --------------------------------------------------------------------------- #

def _value_text(field: Sourced, kind: str, ptype: PartType) -> str:
    """The bare value of a requirement field: ``"16V"``, ``"100mW"``,
    ``"X7R, X5R"``."""
    v = field.value
    if kind == "rating":
        return ptype.fmt_rating(v)
    if kind == "tolerance":
        return fmt_percent(v)
    if kind == "dielectric":
        return ", ".join(v)
    return str(v)


def sourced_cell(field: Sourced, kind: str, ptype: PartType) -> str:
    """The cell text for a requirement field of a *ptype* part. *kind* is
    ``"package"``, ``"rating"``, ``"dielectric"`` or ``"tolerance"``. A
    dielectric cell of a type without one is empty. A value from a default,
    an edit, the existing part or the Description is suffixed ``(default)`` /
    ``(edited)`` / ``(part)`` / ``(description)``; an unset field is a
    dash; a value stated on the schematic that does not parse reads
    ``unreadable`` (``not a chip size`` for a package from a footprint such as
    a radial electrolytic)."""
    if kind == "dielectric" and not ptype.has_dielectric:
        return ""
    if field.source == ANY:
        return "any"
    if field.source == UNSET or (field.value in (None, "", ()) and field.source != SCHEMATIC):
        return DASH
    if field.value in (None, "", ()):
        return "not a chip size" if kind == "package" else "unreadable"
    text = _value_text(field, kind, ptype)
    marker = _SOURCE_MARKERS.get(field.source)
    return f"{text} ({marker})" if marker else text


def value_cell(req: PartRequirement) -> str:
    """The Value cell: the symbol's Value as written, followed by the parsed
    value for a library-style Value (``C_4p7U (4.7uF)``, ``R_49p9K
    (49.9k)``); when the symbol has no Value, the value from the Description
    or the existing part with its marker; else a dash."""
    t = req.ptype
    if req.value_text:
        if (t.is_library_value(req.value_text) and req.nominal
                and req.nominal_source == SCHEMATIC):
            return f"{req.value_text} ({t.fmt_nominal(req.nominal)})"
        return req.value_text
    marker = _SOURCE_MARKERS.get(req.nominal_source)
    if req.nominal and marker:
        return f"{t.fmt_nominal(req.nominal)} ({marker})"
    return DASH


def current_part_cell(group: RequirementGroup) -> str:
    """The part number already on the group's symbols: the MPN field, else the
    DigiKey PN field, as written (the two may be swapped on real boards); a
    dash when both are empty."""
    existing = group.existing
    return existing.mpn or existing.digikey_pn or DASH


def requirement_settings(req: PartRequirement) -> Dict[str, str]:
    """The settings a requirement updates on accept: each field whose value
    came from a default or an edit, under its ``cap.*`` / ``res.*`` key.
    Values from the existing part, the Description or a dropped tolerance
    limit are written to the symbols but never become remembered settings. A
    capacitor's dielectrics are stored under the capacitance band's key and
    its tolerance under the dielectric class's key, as
    :func:`kicadtools.requirement.part_requirement` reads them."""
    t = req.ptype
    out: Dict[str, str] = {}

    def written(f: Sourced) -> bool:
        return f.source in (DEFAULT, EDITED) and f.value not in (None, "", ())

    if written(req.package):
        out[t.key("package")] = str(req.package.value)
    if written(req.rating_min):
        out[t.key(t.rating_setting)] = t.fmt_rating(req.rating_min.value)
    if t.has_dielectric and written(req.dielectrics) and req.nominal:
        out[t.key("dielectric." + dielectric_band(req.nominal))] = ", ".join(
            req.dielectrics.value
        )
    if written(req.tolerance_max_pct):
        key = tolerance_key(t, req.dielectrics.value or ())
        out[key] = fmt_percent(req.tolerance_max_pct.value)
    return out


def board_label(board: Path, root: Optional[Path] = None) -> str:
    """The board path as recorded in the usage table: relative to *root*
    (default ``~/Code/Boards``) when the board is inside it, else absolute."""
    root = root if root is not None else Path.home() / "Code" / "Boards"
    board = Path(board).resolve()
    try:
        return board.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(board)


def merge_plans(plans: Sequence[Plan]) -> Plan:
    """*plans* merged in order; a later value for the same key replaces an
    earlier one."""
    merged: Plan = {}
    for plan in plans:
        for src, by_uuid in plan.items():
            for uuid, props in by_uuid.items():
                merged.setdefault(Path(src), {}).setdefault(uuid, {}).update(props)
    return merged


# --------------------------------------------------------------------------- #
# Table model
# --------------------------------------------------------------------------- #

@dataclass
class _Queued:
    """An accepted candidate for one group, waiting for File > Save."""
    requirement: PartRequirement
    candidate: Candidate
    plan: Plan


class PartsTableModel(QAbstractTableModel):
    """Value · Package · Rating · Dielectric · Tolerance · Qty · References ·
    Current part · Status · Chosen MPN, one row per :class:`RequirementGroup`."""

    def __init__(self, groups: Optional[Sequence[RequirementGroup]] = None):
        super().__init__()
        self._groups: List[RequirementGroup] = list(groups or [])
        self._queued: Dict[int, _Queued] = {}
        # Existing-part lookups by row, recorded when an assign dialog closes.
        self._lookups: Dict[int, ExistingLookup] = {}

    # ---- loading -----------------------------------------------------
    def load(self, groups: Sequence[RequirementGroup]) -> None:
        """Replace the rows; the queued choices and recorded lookups are
        cleared."""
        self.beginResetModel()
        self._groups = list(groups)
        self._queued.clear()
        self._lookups.clear()
        self.endResetModel()

    def set_defaults(self, defaults: Mapping[str, str]) -> None:
        """Rebuild each group's requirement from *defaults* (after an accept
        has changed the remembered settings), so a defaulted field shows the
        last value used. Values stated on the schematic are unaffected; a
        queued row keeps the requirement it was accepted against. The
        Description evidence is kept, and so is the fitted evidence of
        a lookup recorded with :meth:`record_lookup`."""
        for row in range(len(self._groups)):
            self._recompute(row, defaults)
        if self._groups:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._groups) - 1, self.columnCount() - 1)
            )

    def _recompute(self, row: int, defaults: Mapping[str, str]) -> None:
        group = self._groups[row]
        lookup = self._lookups.get(row)
        group.requirement = requirement_from_lookup(group, lookup, defaults)[0]

    def record_lookup(self, row: int, lookup: ExistingLookup,
                      defaults: Mapping[str, str]) -> None:
        """Keep *lookup* (an assign dialog's existing-part lookup) for *row*
        and rebuild the row's requirement from it (:func:`requirement_from_lookup`)."""
        self._lookups[row] = lookup
        self._recompute(row, defaults)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def lookup_at(self, row: int) -> Optional[ExistingLookup]:
        """The lookup recorded for *row*, ``None`` before one."""
        return self._lookups.get(row)

    def group_at(self, row: int) -> RequirementGroup:
        """The group backing *row* (a source-model row index)."""
        return self._groups[row]

    def groups(self) -> List[RequirementGroup]:
        return list(self._groups)

    # ---- queue -------------------------------------------------------
    def queue(self, row: int, req: PartRequirement, candidate: Candidate,
              plan: Plan) -> None:
        """Record *candidate* as the choice for *row*, with the requirement as
        accepted and the write plan from :func:`kicadtools.assign.assignment_plan`.
        Queueing a row again replaces its earlier choice."""
        self._queued[row] = _Queued(req, candidate, plan)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def is_queued(self, row: int) -> bool:
        return row in self._queued

    def requirement_at(self, row: int) -> PartRequirement:
        """The requirement as accepted for a queued row, else the group's."""
        q = self._queued.get(row)
        return q.requirement if q else self._groups[row].requirement

    def accepted_edits(self) -> Plan:
        """Every queued plan merged into one ``{file: {uuid: {key: value}}}``
        plan, in row order. ``None`` values (property removals) are kept."""
        return merge_plans([self._queued[r].plan for r in sorted(self._queued)])

    # ---- status ------------------------------------------------------
    def status(self, row: int) -> str:
        if row in self._queued:
            return STATUS_QUEUED
        req = self._groups[row].requirement
        if req.conflicts:
            return STATUS_CONFLICT
        if self._groups[row].assigned:
            return STATUS_ASSIGNED
        if req.footprint and not req.package.value:
            return STATUS_NOT_SEARCHABLE
        return STATUS_UNASSIGNED

    def summary(self) -> Dict[str, int]:
        """Row counts by status."""
        counts = {STATUS_ASSIGNED: 0, STATUS_QUEUED: 0, STATUS_UNASSIGNED: 0,
                  STATUS_CONFLICT: 0, STATUS_NOT_SEARCHABLE: 0}
        for r in range(len(self._groups)):
            counts[self.status(r)] += 1
        return counts

    def chosen_mpn(self, row: int) -> str:
        """The queued candidate's MPN; for an assigned group, the MPN on the
        schematic (``"mixed"`` when the members disagree); else ``""``."""
        q = self._queued.get(row)
        if q is not None:
            return q.candidate.mpn
        mpns = {
            (extract_part_numbers(m.props).mpn or "") for m in self._groups[row].members
        }
        mpns.discard("")
        if len(mpns) == 1:
            return next(iter(mpns))
        return "mixed" if mpns else ""

    # ---- Qt dimensions ----------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._groups)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(PARTS_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return PARTS_COLUMNS[section]
        if role == Qt.ToolTipRole and section == COL_RATING:
            return "Voltage:Min for a capacitor, Power:Min for a resistor"
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def cell_text(self, row: int, col: int) -> str:
        group = self._groups[row]
        req = self.requirement_at(row)
        if col == COL_VALUE:
            return value_cell(req)
        t = req.ptype
        if col == COL_PACKAGE:
            return sourced_cell(req.package, "package", t)
        if col == COL_RATING:
            return sourced_cell(req.rating_min, "rating", t)
        if col == COL_DIELECTRIC:
            return sourced_cell(req.dielectrics, "dielectric", t)
        if col == COL_TOLERANCE:
            return sourced_cell(req.tolerance_max_pct, "tolerance", t)
        if col == COL_QTY:
            return str(group.quantity)
        if col == COL_REFERENCES:
            return ", ".join(group.references)
        if col == COL_CURRENT:
            return current_part_cell(group)
        if col == COL_STATUS:
            return self.status(row)
        if col == COL_MPN:
            return self.chosen_mpn(row)
        return ""

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, col = index.row(), index.column()
        if role == Qt.DisplayRole:
            return self.cell_text(r, col)
        if role == Qt.BackgroundRole:
            status = self.status(r)
            if status == STATUS_UNASSIGNED:
                return _UNASSIGNED_TINT
            return _CONFLICT_TINT if status == STATUS_CONFLICT else None
        if role == Qt.ToolTipRole:
            req = self.requirement_at(r)
            if col == COL_REFERENCES:
                return ", ".join(self._groups[r].references)
            if self.status(r) == STATUS_CONFLICT:
                return ("Conflict:\n" + "\n".join(req.conflicts)
                        + "\nFix it on the schematic. Double-click for details.")
            if col == COL_CURRENT:
                e = self._groups[r].existing
                lines = [f"{name}: {v}" for name, v in (
                    ("MPN", e.mpn), ("DigiKeyPN", e.digikey_pn), ("LCSC", e.lcsc)) if v]
                return "\n".join(lines) if lines else "No part number on the schematic."
            if col == COL_PACKAGE and req.footprint:
                return f"From the footprint {req.footprint}."
            problem = req.problem()
            if self.status(r) == STATUS_NOT_SEARCHABLE:
                return f"Not searchable: {problem}. Packages other than chip sizes are not supported yet."
            if problem and self.status(r) == STATUS_UNASSIGNED:
                return f"Before a search: {problem}. Double-click to assign a part."
            if self.status(r) != STATUS_ASSIGNED:
                return "\n".join(("Double-click to assign a part.",) + tuple(req.warnings))
            if req.warnings:
                return "\n".join(req.warnings)
        return None


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class CandidateWorker(QObject):
    """Runs :func:`kicadtools.partsearch.assess` for one group off the GUI
    thread; emits ``found`` (a ``SearchResult``) or ``failed``.

    *requirement* is the dialog's requirement, or ``None`` for the first
    request (assess then derives it from the group, *defaults* and the
    existing part). *existing* is the :class:`ExistingLookup` of an earlier
    result, reused so the existing part is looked up once per dialog. The
    parts database and the LCSC cache are opened on the worker thread and
    closed before the thread's event loop is ended."""

    found = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        group: RequirementGroup,
        defaults: Mapping[str, str],
        requirement: Optional[PartRequirement],
        needed: int,
        db_path: Path,
        search_suppliers: bool = False,
        cache_path: Optional[Path] = None,
        existing: Optional[ExistingLookup] = None,
    ):
        super().__init__()
        self._group = group
        self._defaults = dict(defaults)
        self._req = requirement
        self._existing = existing
        self._needed = needed
        self._db_path = db_path
        self._search_suppliers = search_suppliers
        self._cache_path = cache_path

    def run(self) -> None:
        db: Optional[PartsDb] = None
        cache: Optional[Cache] = None
        try:
            db = PartsDb(self._db_path)
            if self._cache_path is not None:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache = Cache(self._cache_path)
            result = assess(
                self._group,
                self._defaults,
                db=db,
                jlc_search=jlcpcb_search,
                jlc_filtered=jlcpcb_category_search,
                jlc_part=lambda code: cached_jlcpcb_part(code, cache),
                digikey=DigiKeyClient(),
                needed=self._needed,
                search_suppliers=self._search_suppliers,
                requirement=self._req,
                existing=self._existing,
            )
            self.found.emit(result)
        except Exception as exc:  # noqa: BLE001 — report any failure in the dialog
            self.failed.emit(str(exc))
        finally:
            if cache is not None:
                cache.close()
            if db is not None:
                db.close()
            QThread.currentThread().quit()
