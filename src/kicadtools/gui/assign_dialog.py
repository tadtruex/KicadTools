"""The "Assign Part" dialog for one capacitor requirement group.

The header lists the group's references, its quantity, the build quantity, the
units the build needs and the part already on the schematic (what the
existing-part lookup found). Below it the four editable requirement fields
(Package, Voltage:Min, Dielectric, Tolerance) each name their source; a value
from a default, an edit, the existing part or the Description is written to
the group's symbols on accept, and its label says so. A field stated on the schematic is read-only: the write plan
never overwrites a stated value, so it is changed on the schematic (or with
*Edit Electrical Parameters* on the Intents tab). Package is also fixed by the
footprint when the symbol has one.

The dialog runs no network code. A search is requested with the
``search_requested(requirement, search_suppliers)`` signal; the window runs it
(:func:`kicadtools.capsearch.assess`) on a worker thread and passes the
outcome to :meth:`show_result` or :meth:`show_error`. The first request sends
``None`` as the requirement: assess derives it from the group and its existing
part. The requirement that result names, or, when the dialog was given
*defaults*, the group's requirement rebuilt from the first lookup
(:func:`kicadtools.capsearch.requirement_from_lookup`), becomes the dialog's base requirement (:attr:`original`); the
lookup is kept (:attr:`existing_lookup`) for later requests. Field text the
user has typed, and fields the opening requirement held as edited, are kept
over the new base, so an edit made while the lookup ran is merged with the
part's evidence rather than replacing it. When the merged requirement differs
from the one the shown candidates were searched for, the search is requested
again. A requirement with conflicts lists them in amber, shows no candidates
and cannot be searched. Every request is numbered (:attr:`request_id`), and an
outcome tagged with an older number is ignored, so a slow search cannot
overwrite a newer one. Field edits request the last kind of search again about
400 ms after the last keystroke, once Voltage:Min parses.

The candidates shown belong to the requirement they were searched for. While
the fields differ from that requirement (an edit whose search has not
finished, or a field that does not parse), no candidate can be accepted, so a
part is never accepted against a requirement it was not checked for. A
candidate whose write plan fails (a missing part number, or no footprint for
the package) cannot be accepted either; the Accept button's tooltip gives the
reason.

Right-click a candidate to reject it (``reject_requested(candidate)``); the
window records the rejection, removes the row with :meth:`drop_candidate` and
requests the search again. *Accept* closes the dialog; :meth:`chosen` and
:meth:`current_requirement` give the pick and the requirement it was accepted
against.

Cell and label text comes from the module-level pure helpers
(:func:`field_label`, :func:`parse_field_values`, :func:`candidate_cells`,
:func:`stock_shortfall`), testable without a window.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kicadtools.assign import assignment_plan
from kicadtools.capsearch import (
    CURRENT,
    DATABASE,
    Candidate,
    ExistingLookup,
    SearchResult,
    requirement_for,
    requirement_from_lookup,
)
from kicadtools.evidence import ExistingPart
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    SCHEMATIC,
    CapacitorRequirement,
    RequirementGroup,
    Sourced,
)
from kicadtools.suppliers import digikey_url, lcsc_url
from kicadtools.units import (
    dielectric_tokens,
    fmt_farads,
    fmt_percent,
    chip_size,
    fmt_volts,
    package_code,
    parse_percent,
    parse_volts,
)

CANDIDATE_COLUMNS = [
    "MPN", "Manufacturer", "Source", "LCSC", "JLC library", "JLC stock",
    "DigiKey PN", "DigiKey stock", "Unit price",
    "Capacitance", "Voltage", "Dielectric", "Tolerance",
]
COL_LCSC = CANDIDATE_COLUMNS.index("LCSC")
COL_DIGIKEY = CANDIDATE_COLUMNS.index("DigiKey PN")

# The same amber as the LCSC tab's "fewer than the build needs" tint.
_SHORT_TINT = QColor(230, 160, 0, 70)

# Delay between the last field edit and the search it requests.
DEBOUNCE_MS = 400


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def field_label(sourced: Sourced, n_symbols: int) -> str:
    """The source note beside a requirement field: ``"from the schematic"``,
    ``"default — written to 3 symbols"``, ``"edited — written to 1 symbol"``,
    ``"from current part — written to 3 symbols"``,
    ``"from description — written to 3 symbols"``,
    ``"required — written to 3 symbols"`` for a field with no value yet, or
    ``"from the schematic, not readable"`` for a stated value that does not
    parse."""
    noun = "symbol" if n_symbols == 1 else "symbols"
    if sourced.source == SCHEMATIC:
        if sourced.value in (None, "", ()):
            return "from the schematic, not readable"
        return "from the schematic"
    if sourced.source == DEFAULT:
        return f"default — written to {n_symbols} {noun}"
    if sourced.source == EDITED:
        return f"edited — written to {n_symbols} {noun}"
    if sourced.source == PART:
        return f"from current part — written to {n_symbols} {noun}"
    if sourced.source == DESCRIPTION:
        return f"from description — written to {n_symbols} {noun}"
    if sourced.source == ANY:
        return f"any — the chosen part's value is written to {n_symbols} {noun}"
    return f"required — written to {n_symbols} {noun}"


def field_text(sourced: Sourced, kind: str) -> str:
    """The text a field starts with: the bare value, ``""`` when there is none.
    *kind* is ``"package"``, ``"voltage"``, ``"dielectric"`` or ``"tolerance"``."""
    v = sourced.value
    if v in (None, "", ()):
        return ""
    if kind == "voltage":
        return fmt_volts(v)
    if kind == "tolerance":
        return fmt_percent(v)
    if kind == "dielectric":
        return ", ".join(v)
    return str(v)


def parse_field_values(
    package: str,
    voltage: str,
    dielectric: str,
    tolerance: str,
    *,
    original: CapacitorRequirement,
) -> Union[Dict[str, Any], str]:
    """Parse the four field texts against *original* (the group's
    requirement). Returns the keyword arguments for
    :meth:`CapacitorRequirement.edited` — only the fields whose parsed value
    differs from *original*, so an unchanged field keeps its source — or a
    one-line error when a field does not parse. A field stated on the
    schematic (source ``schematic``) is not read: stated values are never
    edited here.

    A blank Voltage:Min, Dielectric or Tolerance field, or one holding
    ``any``, is cleared (the ``clear`` keyword): the search applies no limit
    on it and lists the values found (:attr:`SearchResult.facets`). A blank
    Voltage:Min or Dielectric that had no value to begin with is an error
    instead, so a voltage floor is never left out by accident; ``any`` clears
    it."""
    kwargs: Dict[str, Any] = {}
    clear: List[str] = []

    def is_any(text: str, sourced: Sourced) -> bool:
        if text.lower() == "any":
            return True
        return not text and sourced.value not in (None, "", ()) or (
            not text and sourced.source == ANY)

    if not original.footprint and original.package.source != SCHEMATIC:
        text = package.strip()
        if text:
            code = package_code(text) or chip_size(text)
            if code is None:
                return f"Package '{text}' is not a chip size such as 0603"
            if code != original.package.value:
                kwargs["package"] = code

    text = voltage.strip()
    if original.voltage_min_v.source == SCHEMATIC:
        pass
    elif is_any(text, original.voltage_min_v):
        if original.voltage_min_v.source != ANY:
            clear.append("voltage_min_v")
    elif text:
        v = parse_volts(text)
        if v is None or v <= 0:
            return f"Voltage:Min '{text}' is not a voltage"
        if original.voltage_min_v.value is None or not math.isclose(
            v, float(original.voltage_min_v.value), rel_tol=1e-9
        ):
            kwargs["voltage_min_v"] = v
    elif original.voltage_min_v.value is None:
        return "Enter Voltage:Min (or 'any') to search"

    text = dielectric.strip()
    if original.dielectrics.source == SCHEMATIC:
        pass
    elif is_any(text, original.dielectrics):
        if original.dielectrics.source != ANY:
            clear.append("dielectrics")
    elif text:
        codes = []
        for code in dielectric_tokens(text):
            if code not in codes:
                codes.append(code)
        if not codes:
            return f"Dielectric '{text}' names no dielectric such as X7R"
        if tuple(codes) != tuple(original.dielectrics.value or ()):
            kwargs["dielectrics"] = tuple(codes)
    elif not original.dielectrics.value and original.capacitance_f:
        return "Enter a dielectric (or 'any') to search"

    text = tolerance.strip()
    if original.tolerance_max_pct.source == SCHEMATIC:
        pass
    elif is_any(text, original.tolerance_max_pct):
        if original.tolerance_max_pct.source != ANY:
            clear.append("tolerance_max_pct")
    elif text:
        t = parse_percent(text)
        if t is None or t <= 0:
            return f"Tolerance '{text}' is not a percentage"
        if original.tolerance_max_pct.value is None or not math.isclose(
            t, float(original.tolerance_max_pct.value), rel_tol=1e-9
        ):
            kwargs["tolerance_max_pct"] = t
    if clear:
        kwargs["clear"] = tuple(clear)
    return kwargs


# The facet titles shown in the values popup, by SearchResult.facets key.
FACET_TITLES = {"voltage": "Voltage", "dielectric": "Dielectric", "tolerance": "Tolerance"}


def facet_rows(result: SearchResult, req: Optional[CapacitorRequirement]
               ) -> List[Tuple[str, List[Tuple[str, int]]]]:
    """The facets the values popup shows for *result*, as (field key, rows):
    the fields cleared in *req* (source ``any``); when none is cleared and the
    result has no candidates, every field. Empty when the supplier search did
    not run."""
    if not result.facets:
        return []
    cleared = []
    if req is not None:
        for key, sourced in (("voltage", req.voltage_min_v),
                             ("dielectric", req.dielectrics),
                             ("tolerance", req.tolerance_max_pct)):
            if sourced.source == ANY:
                cleared.append(key)
    if not cleared:
        if result.candidates:
            return []
        cleared = list(FACET_TITLES)
    return [(key, result.facets.get(key, [])) for key in cleared]


class FacetPopup(QDialog):
    """The values popup: for each field shown, the values the JLCPCB parts
    have and how many have each (counting parts that meet the other fields).
    Double-click a value to put it in its field (``value_chosen(key, text)``)."""

    value_chosen = Signal(str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Matching values")
        self.setModal(False)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Value", "Parts"])
        self._tree.itemDoubleClicked.connect(self._double_clicked)
        layout = QVBoxLayout(self)
        layout.addWidget(self._note)
        layout.addWidget(self._tree)
        self.resize(320, 360)

    def set_facets(self, rows: List[Tuple[str, List[Tuple[str, int]]]], pool: int) -> None:
        self._note.setText(
            f"{pool} JLCPCB part(s) of this value and package, in stock, from the "
            "listed manufacturers (DigiKey not checked). Each count meets the "
            "other fields. Double-click a value to use it."
        )
        self._tree.clear()
        for key, values in rows:
            top = QTreeWidgetItem([FACET_TITLES.get(key, key), str(sum(n for _, n in values))])
            top.setData(0, Qt.UserRole, None)
            for text, n in values:
                child = QTreeWidgetItem([text, str(n)])
                child.setData(0, Qt.UserRole, key)
                top.addChild(child)
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)
        self._tree.resizeColumnToContents(0)

    def _double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        key = item.data(0, Qt.UserRole)
        if key:
            self.value_chosen.emit(key, item.text(0))


def _count(n: Optional[int]) -> str:
    return "" if n is None else f"{n:,}"


def candidate_cells(candidate: Candidate) -> List[str]:
    """The table row for *candidate*, in :data:`CANDIDATE_COLUMNS` order."""
    c = candidate
    p = c.params
    price = "" if c.jlc_price is None or math.isinf(c.jlc_price) else f"${c.jlc_price:.4f}"
    library = {"basic": "Basic", "extended": "Extended"}.get(c.jlc_library or "", "")
    if library and c.jlc_preferred:
        library += " (preferred)"
    source = "current part" if c.source == CURRENT else c.source
    if c.source == DATABASE and c.usage_count:
        source += f" ({c.usage_count} use{'s' * (c.usage_count != 1)})"
    return [
        c.mpn,
        c.manufacturer,
        source,
        c.lcsc or "",
        library,
        _count(c.jlc_stock),
        c.digikey_pn or "",
        _count(c.digikey_stock),
        price,
        fmt_farads(p.capacitance_f) if p.capacitance_f else "",
        fmt_volts(p.voltage_v) if p.voltage_v else "",
        p.dielectric or "",
        fmt_percent(p.tolerance_pct) if p.tolerance_pct is not None else "",
    ]


def stock_shortfall(candidate: Candidate, needed: int) -> str:
    """``""`` when every known stock count covers *needed*, else a one-line
    note naming the supplier(s) short of it. An unknown count is not short."""
    notes = []
    if candidate.jlc_stock is not None and candidate.jlc_stock < needed:
        notes.append(f"JLCPCB has {candidate.jlc_stock:,}")
    if candidate.digikey_stock is not None and candidate.digikey_stock < needed:
        notes.append(f"DigiKey has {candidate.digikey_stock:,}")
    if not notes:
        return ""
    return f"The build needs {needed:,}; " + ", ".join(notes) + "."


def current_part_text(existing: ExistingPart,
                      lookup: Optional[ExistingLookup] = None) -> str:
    """The header's Current part line: the lookup's notes, one per line; before
    a lookup, the part numbers on the schematic; ``"none on the schematic"``
    when the symbols hold no part number."""
    if lookup is not None and lookup.notes:
        return "\n".join(lookup.notes)
    if existing.empty:
        return "none on the schematic"
    pns = [f"{name} {v}" for name, v in (
        ("MPN", existing.mpn), ("DigiKeyPN", existing.digikey_pn),
        ("LCSC", existing.lcsc)) if v]
    text = ", ".join(pns)
    return text if lookup is not None else text + " (looking up…)"


def conflict_text(req: CapacitorRequirement) -> str:
    """The amber note for a requirement's conflicts and Description warnings,
    ``""`` when it has neither. Warnings do not block a search."""
    blocks = []
    if req.conflicts:
        blocks.append("Conflict — fix on the schematic before a search:\n"
                      + "\n".join(req.conflicts))
    if req.warnings:
        blocks.append("\n".join(req.warnings))
    return "\n".join(blocks)


def value_header(req: CapacitorRequirement) -> str:
    """The header's Value line: the Value as written; with no Value, the
    capacitance from the Description or the existing part, naming it."""
    if req.value_text:
        return req.value_text
    if req.capacitance_f and req.capacitance_source == DESCRIPTION:
        return f"{fmt_farads(req.capacitance_f)} (from description)"
    if req.capacitance_f and req.capacitance_source == PART:
        return f"{fmt_farads(req.capacitance_f)} (from current part)"
    return "—"


def symbol_count(group: RequirementGroup) -> int:
    """The number of distinct symbols the group's write plan touches (a
    subsheet symbol instantiated twice counts once)."""
    return len({
        (m.source_file, m.symbol_uuid)
        for m in group.members
        if m.source_file is not None and m.symbol_uuid
    })


def result_summary(result: SearchResult) -> str:
    """The status line for a search outcome."""
    n = len(result.candidates)
    from_db = sum(1 for c in result.candidates if c.source == DATABASE)
    current = sum(1 for c in result.candidates if c.source == CURRENT)
    lead = "the current part, " if current else ""
    parts = [f"{n} candidate{'s' * (n != 1)} ({lead}{from_db} from the database, "
             f"{n - from_db - current} from supplier search)"]
    if result.examined:
        parts.append(f"{result.examined} search hits checked")
    if result.rejected:
        parts.append(f"rejected {len(result.rejected)}")
    if result.dropped_by_limit:
        parts.append(f"{result.dropped_by_limit} past the DigiKey lookup limit, not checked")
    text = "; ".join(parts) + "."
    problem = result.problem.strip().rstrip(".")
    if problem:
        text += f" {problem[0].upper()}{problem[1:]}."
    return text


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

class AssignDialog(QDialog):
    search_requested = Signal(object, bool)   # (CapacitorRequirement or None, search_suppliers)
    reject_requested = Signal(object)         # Candidate

    def __init__(
        self,
        group: RequirementGroup,
        requirement: CapacitorRequirement,
        build_qty: int,
        parent=None,
        *,
        defaults: Optional[Mapping[str, str]] = None,
        existing: Optional[ExistingLookup] = None,
    ) -> None:
        """*defaults* (the capacitor settings) let the dialog rebuild the
        group's requirement from a lookup that arrives with the result of an
        edited request. *existing* is a lookup recorded earlier for the group;
        it is reused by every request."""
        super().__init__(parent)
        refs = group.references
        self.setWindowTitle(f"Assign Part — {requirement.value_text or '?'} — {', '.join(refs)}")
        self.resize(1100, 560)
        self._group = group
        self._original = requirement
        self._n_symbols = symbol_count(group)
        self.needed = group.quantity * max(1, int(build_qty))
        self._candidates: List[Candidate] = []
        self._request_id = 0
        self._last_suppliers = False
        # The requirement of the latest request, and the one the shown
        # candidates were searched for.
        self._requested_req: Optional[CapacitorRequirement] = None
        self._shown_req: Optional[CapacitorRequirement] = None
        self._result: Optional[SearchResult] = None
        # The existing-part lookup from the first result, reused by later
        # requests; set once any result has been shown.
        self._existing: Optional[ExistingLookup] = existing
        self._defaults: Optional[Dict[str, str]] = (
            dict(defaults) if defaults is not None else None)
        self._got_result = False
        # True once the user has typed in a field: a later result's
        # requirement is then not adopted.
        self._user_edited = False

        head = QFormLayout()
        refs_label = QLabel(", ".join(refs))
        refs_label.setWordWrap(True)
        head.addRow("References", refs_label)
        self._value_label = QLabel(value_header(requirement))
        head.addRow("Value", self._value_label)
        self._current_label = QLabel(current_part_text(group.existing, existing))
        self._current_label.setWordWrap(True)
        self._current_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addRow("Current part", self._current_label)
        head.addRow("Quantity", QLabel(f"{group.quantity} per board"))
        head.addRow("Build quantity", QLabel(f"{int(build_qty):,} boards"))
        head.addRow("Units needed", QLabel(f"{self.needed:,}"))

        # Requirement fields, each with its source note.
        self._fields: Dict[str, QLineEdit] = {}
        self._notes: Dict[str, QLabel] = {}
        form = QFormLayout()
        specs = (
            ("package", "Package"),
            ("voltage", "Voltage:Min"),
            ("dielectric", "Dielectric"),
            ("tolerance", "Tolerance"),
        )
        for kind, label in specs:
            edit = QLineEdit()
            note = QLabel()
            row = QWidget()
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(edit, 1)
            box.addWidget(note, 2)
            form.addRow(label, row)
            edit.textEdited.connect(self._field_edited)
            self._fields[kind] = edit
            self._notes[kind] = note
        self._fields["voltage"].setPlaceholderText("e.g. 16V")
        self._fields["dielectric"].setPlaceholderText("comma-separated, e.g. X7R, X5R")

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.rerun)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        # Shown only when the default tolerance limit was dropped.
        self._relaxed = QLabel("")
        self._relaxed.setWordWrap(True)
        self._relaxed.setStyleSheet("color: #c07000; font-weight: bold;")
        self._relaxed.hide()
        # Shown while the requirement has conflicts between its sources.
        self._conflicts = QLabel("")
        self._conflicts.setWordWrap(True)
        self._conflicts.setStyleSheet("color: #c07000; font-weight: bold;")
        self._conflicts.hide()

        self._table = QTableWidget(0, len(CANDIDATE_COLUMNS))
        self._table.setHorizontalHeaderLabels(CANDIDATE_COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        self._table.cellClicked.connect(self._cell_clicked)
        self._table.cellDoubleClicked.connect(self._double_clicked)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._search_btn = self._buttons.addButton("Search suppliers", QDialogButtonBox.ActionRole)
        self._search_btn.clicked.connect(lambda: self.request_search(True))
        self._values_btn = self._buttons.addButton("Values…", QDialogButtonBox.ActionRole)
        self._values_btn.setToolTip("Voltages, dielectrics and tolerances of the JLCPCB "
                                    "parts of this value and package")
        self._values_btn.setEnabled(False)
        self._values_btn.clicked.connect(lambda: self._show_facets(force=True))
        self._facet_popup: Optional[FacetPopup] = None
        self._accept_btn = self._buttons.addButton("Accept", QDialogButtonBox.AcceptRole)
        self._accept_btn.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addLayout(form)
        layout.addWidget(self._status)
        layout.addWidget(self._relaxed)
        layout.addWidget(self._conflicts)
        layout.addWidget(self._table)
        layout.addWidget(self._buttons)

        self._fill_fields()

    # ---- requirement -------------------------------------------------
    @property
    def group(self) -> RequirementGroup:
        return self._group

    @property
    def original(self) -> CapacitorRequirement:
        """The requirement the fields are edited against: the one the dialog
        was opened with, or the one adopted from a result."""
        return self._original

    @property
    def existing_lookup(self) -> Optional[ExistingLookup]:
        """The existing-part lookup from the first result, ``None`` before."""
        return self._existing

    def _fill_fields(self) -> None:
        """Set each field's text, read-only state and tooltip from
        :attr:`original`, and the conflict note and Search button with it."""
        req = self._original
        values = {
            "package": req.package,
            "voltage": req.voltage_min_v,
            "dielectric": req.dielectrics,
            "tolerance": req.tolerance_max_pct,
        }
        for kind, sourced in values.items():
            edit = self._fields[kind]
            edit.setText(field_text(sourced, kind))
            stated = sourced.source == SCHEMATIC
            edit.setReadOnly(stated)
            edit.setToolTip(
                "Stated on the schematic. Change it there, or with Edit "
                "Electrical Parameters on the Intents tab." if stated else ""
            )
        pkg = self._fields["package"]
        pkg.setEnabled(not req.footprint)
        if req.footprint:
            pkg.setToolTip(f"Set by the footprint {req.footprint}.")
        self._value_label.setText(value_header(req))
        self._show_conflicts(req)
        self._refresh_notes()

    def _show_conflicts(self, req: CapacitorRequirement) -> None:
        text = conflict_text(req)
        self._conflicts.setText(text)
        self._conflicts.setVisible(bool(text))
        self._search_btn.setEnabled(not req.conflicts)
        if req.conflicts:
            self._search_btn.setToolTip("The requirement has conflicts; fix them on the schematic.")
        else:
            self._search_btn.setToolTip("")

    @staticmethod
    def _field_value(req: CapacitorRequirement, kind: str) -> Sourced:
        return {
            "package": req.package,
            "voltage": req.voltage_min_v,
            "dielectric": req.dielectrics,
            "tolerance": req.tolerance_max_pct,
        }[kind]

    def _adopt(self, req: CapacitorRequirement) -> None:
        """Make *req* (a requirement merged with the existing part's evidence)
        the dialog's base requirement. A field whose text differs from what
        the old base showed (typed by the user), or whose old value was an
        edit, keeps its text, unless *req* states that field on the schematic
        or fixes the package by a footprint."""
        old = self._original
        keep: Dict[str, str] = {}
        for kind, edit in self._fields.items():
            was = self._field_value(old, kind)
            if was.source == SCHEMATIC or (kind == "package" and old.footprint):
                continue
            if edit.text() != field_text(was, kind) or was.source == EDITED:
                keep[kind] = edit.text()
        self._original = req
        self._fill_fields()
        for kind, text in keep.items():
            if self._field_value(req, kind).source == SCHEMATIC:
                continue
            if kind == "package" and req.footprint:
                continue
            self._fields[kind].setText(text)
        self._refresh_notes()

    def _merged_base(self, result: SearchResult, stale: bool,
                     first_lookup: bool) -> Optional[CapacitorRequirement]:
        """The base requirement *result* implies, ``None`` when it implies
        none: the result's own requirement for a current answer to a request
        that sent ``None``; else, for the first lookup and with *defaults*
        known, the group's requirement rebuilt from that lookup."""
        if not stale and self._requested_req is None and result.requirement is not None:
            return result.requirement
        if first_lookup and self._defaults is not None and result.existing is not None:
            return requirement_from_lookup(self._group, result.existing, self._defaults)[0]
        return None

    def _parsed(self) -> Union[Dict[str, Any], str]:
        f = self._fields
        return parse_field_values(
            f["package"].text(), f["voltage"].text(), f["dielectric"].text(),
            f["tolerance"].text(), original=self._original,
        )

    def current_requirement(self) -> Optional[CapacitorRequirement]:
        """The group's requirement with the field edits applied, or ``None``
        while a field does not parse."""
        parsed = self._parsed()
        if isinstance(parsed, str):
            return None
        return self._original.edited(**parsed)

    def _refresh_notes(self) -> None:
        req = self.current_requirement() or self._original
        fields = {
            "package": req.package,
            "voltage": req.voltage_min_v,
            "dielectric": req.dielectrics,
            "tolerance": req.tolerance_max_pct,
        }
        for kind, sourced in fields.items():
            self._notes[kind].setText(field_label(sourced, self._n_symbols))

    def _field_edited(self, _text: str) -> None:
        self._user_edited = True
        self._refresh_notes()
        self._selection_changed()
        self._debounce.start()

    # ---- search ------------------------------------------------------
    @property
    def request_id(self) -> int:
        """The number of the latest search request."""
        return self._request_id

    def request_search(self, search_suppliers: bool = False) -> bool:
        """Emit ``search_requested`` for the current requirement. Returns
        ``False`` (with the reason in the status line) when a field does not
        parse or the requirement is not searchable.

        Until a result has been shown, a request with the fields unchanged
        sends ``None`` (assess derives the requirement from the group and its
        existing part), when the group has an existing part to look up or the
        requirement is searchable as it stands."""
        self._debounce.stop()
        parsed = self._parsed()
        if self._first_request(parsed):
            self._request_id += 1
            self._last_suppliers = search_suppliers
            self._requested_req = None
            self._status.setText(
                "Looking up the current part and searching…"
                if self._existing is None and not self._group.existing.empty
                else "Searching the parts database…"
            )
            self.search_requested.emit(None, search_suppliers)
            return True
        if isinstance(parsed, str):
            self._status.setText(f"{parsed}.")
            return False
        req = self._original.edited(**parsed)
        if not req.searchable:
            self._status.setText(f"Can't search: {req.problem()}.")
            return False
        self._request_id += 1
        self._last_suppliers = search_suppliers
        self._requested_req = req
        self._status.setText(
            "Searching the parts database and suppliers…" if search_suppliers
            else "Searching the parts database…"
        )
        self.search_requested.emit(req, search_suppliers)
        return True

    def _first_request(self, parsed: Union[Dict[str, Any], str]) -> bool:
        """True when the next request sends ``None``: no result yet, no field
        changed, and an existing part to look up or a searchable requirement."""
        if self._got_result:
            return False
        if isinstance(parsed, str):
            pristine = not self._user_edited
        else:
            pristine = self._original.edited(**parsed) == self._original
        if not pristine:
            return False
        return not self._group.existing.empty or self._original.searchable

    def rerun(self) -> bool:
        """Request the last search again (after a rejection)."""
        return self.request_search(self._last_suppliers)

    def _stale(self, tag: Optional[int]) -> bool:
        return tag is not None and tag != self._request_id

    def show_result(self, result: SearchResult, tag: Optional[int] = None) -> None:
        """Show *result*. When *tag* names an older request, only its
        existing-part lookup is used (the first one is kept and merged into
        the requirement, see the module notes); its candidates are not shown."""
        stale = self._stale(tag)
        first_lookup = result.existing is not None and self._existing is None
        if first_lookup:
            self._existing = result.existing
            self._current_label.setText(current_part_text(self._group.existing, result.existing))
        base = self._merged_base(result, stale, first_lookup)
        changed = base is not None and base != self._original
        if changed:
            self._adopt(base)
        if stale:
            if changed:
                self._search_again()
            return
        self._got_result = True
        req = result.requirement
        if req is not None:
            self._shown_req = req
        else:
            self._shown_req = self._requested_req or self._original
        self._show_conflicts(self._original)
        # A requirement with conflicts has no candidates.
        self._candidates = [] if self._original.conflicts else list(result.candidates)
        self._result = result
        self._status.setText(result_summary(result))
        self._relaxed.setText(result.relaxed_note + "." if result.relaxed_note else "")
        self._relaxed.setVisible(bool(result.relaxed_note))
        if result.rejected:
            lines = [f"{label}: {why}" for label, why in result.rejected[:40]]
            if len(result.rejected) > 40:
                lines.append(f"… and {len(result.rejected) - 40} more")
            self._status.setToolTip("\n".join(lines))
        else:
            self._status.setToolTip("")

        self._table.clearSelection()
        self._table.setRowCount(len(self._candidates))
        link = self.palette().link().color()
        for r, cand in enumerate(self._candidates):
            short = stock_shortfall(cand, self.needed)
            for col, text in enumerate(candidate_cells(cand)):
                item = QTableWidgetItem(text)
                if short:
                    item.setBackground(_SHORT_TINT)
                    item.setToolTip(short)
                url = self._url(cand, col)
                if url:
                    item.setForeground(link)
                    item.setToolTip(url)
                self._table.setItem(r, col, item)
        if self._candidates:
            current = [r for r, c in enumerate(self._candidates) if c.source == CURRENT]
            self._table.selectRow(current[0] if current else 0)
        self._selection_changed()
        self._values_btn.setEnabled(bool(result.facets))
        self._show_facets()
        if changed:
            self._search_again()

    def _show_facets(self, force: bool = False) -> None:
        """Open (or update) the values popup: automatically when a field is
        cleared or the search found nothing; with *force*, for every field."""
        result = self._result
        if result is None or not result.facets:
            return
        rows = facet_rows(result, self._shown_req)
        if force:
            rows = [(key, result.facets.get(key, [])) for key in FACET_TITLES]
        if not rows:
            if self._facet_popup is not None and self._facet_popup.isVisible():
                self._facet_popup.set_facets(
                    [(key, result.facets.get(key, [])) for key in FACET_TITLES],
                    result.pool_size)
            return
        if self._facet_popup is None:
            self._facet_popup = FacetPopup(self)
            self._facet_popup.value_chosen.connect(self._use_facet_value)
        self._facet_popup.set_facets(rows, result.pool_size)
        self._facet_popup.show()
        self._facet_popup.raise_()

    def _use_facet_value(self, key: str, text: str) -> None:
        """Put a value from the popup in its field, as if typed."""
        edit = self._fields.get(key)
        if edit is None or edit.isReadOnly():
            return
        edit.setText(text)
        self._field_edited(text)

    def _search_again(self) -> None:
        """Request the last kind of search for the merged requirement when it
        is searchable and differs from the one the shown candidates belong
        to."""
        cur = self.current_requirement()
        if cur is not None and cur.searchable and cur != self._shown_req:
            self.request_search(self._last_suppliers)

    def show_error(self, message: str, tag: Optional[int] = None) -> None:
        if self._stale(tag):
            return
        self._status.setText(f"Search failed: {message}")

    # ---- table -------------------------------------------------------
    @staticmethod
    def _url(cand: Candidate, col: int) -> Optional[str]:
        if col == COL_LCSC:
            return lcsc_url(cand.lcsc or "")
        if col == COL_DIGIKEY:
            return digikey_url(cand.digikey_pn or "")
        return None

    def _cell_clicked(self, row: int, col: int) -> None:
        if 0 <= row < len(self._candidates):
            url = self._url(self._candidates[row], col)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _double_clicked(self, row: int, col: int) -> None:
        if col in (COL_LCSC, COL_DIGIKEY):
            return
        if 0 <= row < len(self._candidates):
            self.accept()

    def _context_menu(self, pos) -> None:
        item = self._table.itemAt(pos)
        if item is None or not 0 <= item.row() < len(self._candidates):
            return
        cand = self._candidates[item.row()]
        menu = QMenu(self)
        act = menu.addAction(f"Reject {cand.mpn}")
        if menu.exec(self._table.viewport().mapToGlobal(pos)) is act:
            self.reject_requested.emit(cand)

    def drop_candidate(self, candidate: Candidate) -> None:
        """Remove *candidate*'s row (after it has been rejected)."""
        for r, c in enumerate(self._candidates):
            if c is candidate:
                del self._candidates[r]
                self._table.removeRow(r)
                break
        self._selection_changed()

    def accept_problem(self, candidate: Candidate) -> str:
        """``""`` when *candidate* can be accepted for the current
        requirement, else a one-line reason."""
        req = self.current_requirement()
        if req is not None and req.conflicts:
            return "The requirement has conflicts; fix them on the schematic."
        if req is None or req != self._shown_req:
            return "The fields have changed since this search; wait for the new results."
        try:
            assignment_plan(self._group, self.requirement_for(candidate), candidate)
        except ValueError as exc:
            return str(exc)
        return ""

    def requirement_for(self, candidate: Candidate) -> Optional[CapacitorRequirement]:
        """The requirement to write when *candidate* is accepted: the current
        one, with the part's own tolerance when the search dropped the default
        tolerance limit. ``None`` while a field does not parse."""
        req = self.current_requirement()
        if req is None or self._result is None:
            return req
        return requirement_for(req, self._result, candidate)

    def _selected(self) -> Optional[Candidate]:
        rows = self._table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        r = rows[0].row()
        return self._candidates[r] if 0 <= r < len(self._candidates) else None

    def _selection_changed(self) -> None:
        cand = self._selected()
        problem = self.accept_problem(cand) if cand is not None else ""
        self._accept_btn.setEnabled(cand is not None and not problem)
        self._accept_btn.setToolTip(problem)

    def chosen(self) -> Optional[Candidate]:
        """The selected candidate when it can be accepted for the current
        requirement (see :meth:`accept_problem`), else ``None``."""
        cand = self._selected()
        if cand is None or self.accept_problem(cand):
            return None
        return cand

    def accept(self) -> None:
        """Close with *Accept* only when :meth:`chosen` has a candidate (a
        double-click reaches here with the button disabled)."""
        if self.chosen() is None:
            return
        super().accept()

    def done(self, result: int) -> None:
        # A pending debounce would request a search after the dialog closed.
        self._debounce.stop()
        super().done(result)

