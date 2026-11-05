"""
Parts tab: one row per capacitor, resistor or inductor requirement group, one
per IC group (``IC_PLAN.md`` step 8) and one per through-hole group
(``THT_PLAN.md`` step 9), plus the background workers that search for
candidate parts for one group.

Each row shows the group's requirement (Value, Package, Rating — the voltage
floor of a capacitor, the power floor of a resistor, the saturation-current
floor of an inductor —, Class — the dielectric of a capacitor, the shielding
of an inductor —, Tolerance), its quantity and references, the part number
already on the schematic, its status and the chosen MPN. A requirement cell whose value came
from a default, an edit, the existing part or the Description is marked
``(default)`` / ``(edited)`` / ``(part)`` / ``(description)``; a field with no
value shows a dash.

Status is ``queued`` once a candidate has been accepted for the group this
session (the write plan is held in the model until File > Save), ``conflict``
when two sources state different values or packages or the part-number
fields name different parts, ``assigned`` when every member already has its
four part numbers and a footprint on the schematic, ``not searchable`` for a
footprint with no chip size (an inductor's footprint is the exception: its
body size can be entered), else ``unassigned``. A conflict ranks above
``assigned``: a fully numbered group whose Value disagrees with its part still
needs a schematic fix. :meth:`PartsTableModel.accepted_edits` merges the queued
plans into the ``{file: {uuid: {key: value}}}`` shape the writer applies; a
value of ``None`` removes that property.

The existing-part lookup of a group (from the assign dialog) is kept per row
(:meth:`PartsTableModel.record_lookup`), so the row shows the part's evidence
and warnings after the dialog closes, :meth:`PartsTableModel.set_defaults`
keeps them, and a reopened dialog reuses the lookup.

A through-hole row (:class:`~kicadtools.thtassign.ThtGroup`) shows its Value,
the footprint signature under Package (``header 1x04, 2.54 mm, vertical``),
``through-hole`` under Class, and Rating and Tolerance empty. Its status is
``no part`` for a footprint with nothing to buy (solder-wire pad, mounting
hole, bare test pad), ``not searchable`` for a family not searched yet
(terminal block, loop test point, a footprint that names no part) unless the
symbol has a DigiKeyPN or MPN, ``assigned`` when every member has an MPN,
Manufacturer and DigiKeyPN, else ``unassigned``.
:class:`ThtCandidateWorker` runs its search.

A plain-LED row (:class:`~kicadtools.ledrequirement.LedGroup`, ``LED_PLAN.md``
step 5) shows the colour under Value (``green`` from ``LED_Green`` is marked
``(value)``), the package, the stated intensity minimum and Vf maximum under
Rating, and the wavelength band under Class. It is ``not searchable`` when
the footprint has no chip size; a missing colour leaves it ``unassigned``
(the dialog asks for one). :class:`LedCandidateWorker` runs its search.
Multicolour and smart LEDs are IC rows.

An IC row (:class:`~kicadtools.icrequirement.IcGroup`) shows the MPN field,
else the Value, under Value; the package parsed from the footprint name (or
the queued part's DigiKey package) under Package; Rating, Dielectric and
Tolerance empty. Every row shows its footprint under Footprint (the name
without the library; the full lib id in the tooltip). On an IC row that cell
is coloured by the footprint check (:func:`kicadtools.icsearch.check_footprint`)
of the queued part, or, before one is queued, of the part the symbol names in
the last search recorded for the row (:meth:`PartsTableModel.record_ic_result`):
red for a mismatch, amber when the footprint cannot be verified. An IC row is
``conflict`` when that search found identifiers naming different parts,
``not searchable`` when it has no part number or no footprint. An assigned
IC row can be opened again to see the footprint check.

:class:`CandidateWorker` opens its own parts-database and LCSC-cache
connections on the worker thread and runs :func:`kicadtools.partsearch.assess`
there (existing-part lookup, then the candidate search);
:class:`IcCandidateWorker` does the same for an IC group with
:func:`kicadtools.icsearch.assess_ic`, reading the footprint's pad count from
the board there.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Union

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor

from kicadtools.icrequirement import IcGroup, IcRequirement
from kicadtools.icsearch import (
    MISMATCH,
    UNVERIFIED,
    FootprintCheck,
    IcCandidate,
    IcSearchResult,
    assess_ic,
    board_pads,
)
from kicadtools.ledrequirement import TEXT, LedGroup, LedRequirement
from kicadtools.ledsearch import LedLookup, assess_led
from kicadtools.package import PackageInfo, parse_footprint
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
    package_editable,
    tolerance_key,
)
from kicadtools.resolve import Cache, cached_jlcpcb_part
from kicadtools.suppliers import DigiKeyClient, jlcpcb_category_search, jlcpcb_search
from kicadtools.thtassign import ThtGroup, assess_tht, signature_text
from kicadtools.units import fmt_percent

PARTS_COLUMNS = ["Value", "Package", "Footprint", "Rating", "Class", "Tolerance",
                 "Qty", "References", "Current part", "Status", "Chosen MPN"]
COL_VALUE = PARTS_COLUMNS.index("Value")
COL_PACKAGE = PARTS_COLUMNS.index("Package")
COL_FOOTPRINT = PARTS_COLUMNS.index("Footprint")
COL_RATING = PARTS_COLUMNS.index("Rating")
COL_DIELECTRIC = PARTS_COLUMNS.index("Class")
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
# A through-hole footprint with nothing to buy: solder-wire pad, mounting
# hole, bare test pad (THT_PLAN.md step 9).
STATUS_NO_PART = "no part"

DASH = "—"

# A lighter amber than the LCSC tab's stock tint: an unassigned group is work
# to do, not a stock problem.
_UNASSIGNED_TINT = QColor(230, 160, 0, 40)
# A conflict row needs a schematic change before a search.
_CONFLICT_TINT = QColor(220, 50, 50, 50)
# The Footprint cell of an IC whose footprint does not match its part (red),
# or cannot be checked against it (amber). Stronger than the row tints, so
# the cell stands out on an unassigned row.
MISMATCH_TINT = QColor(220, 50, 50, 110)
UNVERIFIED_TINT = QColor(230, 160, 0, 110)
CHECK_TINTS = {MISMATCH: MISMATCH_TINT, UNVERIFIED: UNVERIFIED_TINT}

# The cell suffix per source; a value stated on the schematic has none.
_SOURCE_MARKERS = {
    DEFAULT: "default",
    EDITED: "edited",
    RELAXED: "limit dropped",
    PART: "part",
    DESCRIPTION: "description",
    TEXT: "value",
}

# The plan shape: {source_file: {symbol_uuid: {property_key: value or None}}}.
Plan = Dict[Path, Dict[str, Dict[str, Optional[str]]]]

# A Parts-tab row: a capacitor/resistor/inductor group, an IC group or a
# through-hole group.
Group = Union[RequirementGroup, IcGroup, ThtGroup, LedGroup]

THT_CLASS = "through-hole"


class _NoClass:
    """Stands in for a PartType in :func:`sourced_cell` for an LED package."""
    has_class = False


_NO_CLASS = _NoClass()


# --------------------------------------------------------------------------- #
# Cell text — pure, so it is testable without a window.
# --------------------------------------------------------------------------- #

def _value_text(field: Sourced, kind: str, ptype: PartType) -> str:
    """The bare value of a requirement field: ``"16V"``, ``"100mW"``,
    ``"X7R, X5R"``, ``"Shielded"``."""
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
    ``"package"``, ``"rating"``, ``"dielectric"`` (the class field) or
    ``"tolerance"``. A class cell of a type without one is empty. A value from a default,
    an edit, the existing part or the Description is suffixed ``(default)`` /
    ``(edited)`` / ``(part)`` / ``(description)``; an unset field is a
    dash; a value stated on the schematic that does not parse reads
    ``unreadable`` (``not a chip size`` for a package from a footprint such as
    a radial electrolytic)."""
    if kind == "dielectric" and not ptype.has_class:
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
    its tolerance under the dielectric class's key, an inductor's shielding
    under ``ind.shielding``, as
    :func:`kicadtools.requirement.part_requirement` reads them."""
    t = req.ptype
    out: Dict[str, str] = {}

    def written(f: Sourced) -> bool:
        return f.source in (DEFAULT, EDITED) and f.value not in (None, "", ())

    if written(req.package):
        out[t.key("package")] = str(req.package.value)
    if written(req.rating_min):
        out[t.key(t.rating_setting)] = t.fmt_rating(req.rating_min.value)
    if t.has_class and written(req.dielectrics) and t.class_setting:
        out[t.key(t.class_setting)] = ", ".join(req.dielectrics.value)
    elif t.has_class and written(req.dielectrics) and req.nominal:
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


def is_ic(group: object) -> bool:
    """True for an IC group (a row searched by part number)."""
    return isinstance(group, IcGroup)


def is_tht(group: object) -> bool:
    """True for a through-hole group (a row searched by footprint)."""
    return isinstance(group, ThtGroup)


def is_led(group: object) -> bool:
    """True for a plain-LED group (a row searched by colour)."""
    return isinstance(group, LedGroup)


def _marked(text: str, source: str) -> str:
    marker = _SOURCE_MARKERS.get(source)
    return f"{text} ({marker})" if marker else text


def led_value_cell(req: LedRequirement) -> str:
    """The colour, marked by source (``green (value)``); the Value followed
    by ``(no colour)`` when no source names one."""
    if req.colors.value:
        return _marked(", ".join(req.colors.value), req.colors.source)
    if req.colors.source == SCHEMATIC:
        return "unreadable"
    return f"{req.value_text or DASH} (no colour)"


def led_rating_cell(req: LedRequirement) -> str:
    """``≥ 100 mcd, ≤ 2.2 V``; ``any`` when neither is stated."""
    parts = []
    if req.intensity_min.value is not None:
        parts.append(_marked(f"≥ {req.intensity_min.value:g} mcd", req.intensity_min.source))
    if req.vf_max.value is not None:
        parts.append(_marked(f"≤ {req.vf_max.value:g} V", req.vf_max.source))
    return ", ".join(parts) or "any"


def led_class_cell(req: LedRequirement) -> str:
    """The wavelength band (``510–540 nm``), with the lens when stated."""
    bands = "; ".join(t.split(" ", 1)[1] if " " in t else t
                      for t in req.band_text().split("; ") if t)
    if req.lenses.value:
        bands = f"{bands}, {', '.join(req.lenses.value)}" if bands else ", ".join(req.lenses.value)
    return bands or DASH


def footprint_name(footprint: str) -> str:
    """A KiCad lib id without its library: ``"SOT-23-5"`` for
    ``"Package_TO_SOT_SMD:SOT-23-5"``; ``""`` stays ``""``."""
    return footprint.split(":", 1)[1] if ":" in footprint else footprint


def package_text(info: Optional[PackageInfo]) -> str:
    """A parsed package as one short line: ``"QFN-24 EP, 4x4 mm, 0.5 mm
    pitch"``; ``""`` when nothing is known. The family carries the pin count
    when both are known; a pin count alone reads ``"24 pins"``."""
    if info is None or not info.known:
        return ""
    head = info.family or ""
    if info.pins is not None:
        head = f"{head}-{info.pins}" if head else f"{info.pins} pins"
    if info.ep:
        head = f"{head} EP" if head else "exposed pad"
    parts = [head] if head else []
    if info.body_mm:
        a, b = info.body_mm
        parts.append(f"{a:g}x{b:g} mm")
    if info.pitch_mm is not None:
        parts.append(f"{info.pitch_mm:g} mm pitch")
    return ", ".join(parts)


def ic_value_cell(req: IcRequirement) -> str:
    """An IC row's Value cell: the MPN field, else the Value as written, else
    a dash."""
    return req.identifiers.mpn or req.value_text or DASH


def ic_package_cell(req: IcRequirement, candidate: Optional[IcCandidate] = None) -> str:
    """An IC row's Package cell: the queued *candidate*'s DigiKey package
    (Supplier Device Package, else Package / Case); else the package parsed
    from the footprint name, marked ``(footprint)``; else a dash."""
    if candidate is not None and (candidate.package or candidate.package_case):
        return candidate.package or candidate.package_case
    info = req.package if req.package is not None else parse_footprint(req.footprint)
    text = package_text(info)
    return f"{text} (footprint)" if text else DASH


def check_cell(check: Optional[FootprintCheck]) -> str:
    """The short footprint-check text: ``"match"``, ``"mismatch"``,
    ``"cannot verify"``; ``""`` before a check."""
    return check.result if check is not None else ""


def identifiers_text(req: IcRequirement) -> str:
    """The identifiers an IC symbol carries, one per field: ``"DigiKeyPN
    497-19548-2-ND, Value LDL212PV33R"``; ``"none"`` when there are none."""
    ids = req.identifiers
    parts = [f"{name} {v}" for name, v in (
        ("MPN", ids.mpn), ("DigiKeyPN", ids.digikey_pn), ("LCSC", ids.lcsc)) if v]
    if ids.value_mpns:
        parts.append("Value " + ", ".join(ids.value_mpns))
    return ", ".join(parts) if parts else "none"


# --------------------------------------------------------------------------- #
# Table model
# --------------------------------------------------------------------------- #

@dataclass
class _Queued:
    """An accepted candidate for one group, waiting for File > Save."""
    requirement: Union[PartRequirement, IcRequirement]
    candidate: Union[Candidate, IcCandidate]
    plan: Plan


class PartsTableModel(QAbstractTableModel):
    """Value · Package · Footprint · Rating · Dielectric · Tolerance · Qty ·
    References · Current part · Status · Chosen MPN, one row per
    :class:`RequirementGroup` or :class:`~kicadtools.icrequirement.IcGroup`."""

    def __init__(self, groups: Optional[Sequence[Group]] = None):
        super().__init__()
        self._groups: List[Group] = list(groups or [])
        self._queued: Dict[int, _Queued] = {}
        # Existing-part lookups by row, recorded when an assign dialog closes.
        self._lookups: Dict[int, ExistingLookup] = {}
        # The last IC search by row, recorded when an IC assign dialog closes.
        self._ic_results: Dict[int, IcSearchResult] = {}

    # ---- loading -----------------------------------------------------
    def load(self, groups: Sequence[Group]) -> None:
        """Replace the rows; the queued choices, recorded lookups and
        recorded IC searches are cleared."""
        self.beginResetModel()
        self._groups = list(groups)
        self._queued.clear()
        self._lookups.clear()
        self._ic_results.clear()
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
        if is_ic(group) or is_tht(group):
            return      # an IC or through-hole requirement has no defaults
        if is_led(group):
            lookup = self._lookups.get(row)
            group.requirement = group.requirement_with_part(
                lookup.evidence if lookup is not None else None, defaults)
            return
        lookup = self._lookups.get(row)
        group.requirement = requirement_from_lookup(group, lookup, defaults)[0]

    def record_lookup(self, row: int, lookup: "ExistingLookup | LedLookup",
                      defaults: Mapping[str, str]) -> None:
        """Keep *lookup* (an assign dialog's existing-part lookup) for *row*
        and rebuild the row's requirement from it (:func:`requirement_from_lookup`)."""
        self._lookups[row] = lookup
        self._recompute(row, defaults)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def lookup_at(self, row: int) -> "Optional[ExistingLookup | LedLookup]":
        """The lookup recorded for *row*, ``None`` before one."""
        return self._lookups.get(row)

    def record_ic_result(self, row: int, result: IcSearchResult) -> None:
        """Keep *result* (the last search an IC assign dialog showed) for
        IC *row*: the row then shows its conflicts and the footprint check of
        the part the symbol names, and a reopened dialog starts from its
        requirement (package and pad count already read)."""
        self._ic_results[row] = result
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def ic_result_at(self, row: int) -> Optional[IcSearchResult]:
        """The IC search recorded for *row*, ``None`` before one."""
        return self._ic_results.get(row)

    def group_at(self, row: int) -> Group:
        """The group backing *row* (a source-model row index)."""
        return self._groups[row]

    def groups(self) -> List[Group]:
        return list(self._groups)

    # ---- queue -------------------------------------------------------
    def queue(self, row: int, req: Union[PartRequirement, IcRequirement],
              candidate: Union[Candidate, IcCandidate], plan: Plan) -> None:
        """Record *candidate* as the choice for *row*, with the requirement as
        accepted and the write plan from :func:`kicadtools.assign.assignment_plan`
        (:func:`kicadtools.icsearch.ic_assignment_plan` for an IC).
        Queueing a row again replaces its earlier choice."""
        self._queued[row] = _Queued(req, candidate, plan)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def is_queued(self, row: int) -> bool:
        return row in self._queued

    def requirement_at(self, row: int) -> Union[PartRequirement, IcRequirement]:
        """The requirement as accepted for a queued row; for an IC row, the
        requirement of its recorded search (with the package and pad count);
        else the group's."""
        q = self._queued.get(row)
        if q:
            return q.requirement
        res = self._ic_results.get(row)
        if res is not None and res.requirement is not None:
            return res.requirement
        return self._groups[row].requirement

    def footprint_check(self, row: int) -> Optional[FootprintCheck]:
        """The footprint check shown on IC *row*: the queued candidate's;
        else that of the first candidate the symbol names exactly in the
        recorded search (the only candidate when there is one); ``None``
        before either, and for a capacitor, resistor or inductor row."""
        if not is_ic(self._groups[row]):
            return None
        q = self._queued.get(row)
        if q is not None:
            return q.candidate.footprint
        res = self._ic_results.get(row)
        if res is None or not res.candidates:
            return None
        named = [c for c in res.candidates if c.current]
        if named:
            return named[0].footprint
        return res.candidates[0].footprint if len(res.candidates) == 1 else None

    def conflicts(self, row: int) -> List[str]:
        """The row's conflict lines: a capacitor, resistor or inductor requirement's
        conflicts; an IC's from its recorded search."""
        group = self._groups[row]
        if is_tht(group):
            return []
        if is_ic(group):
            res = self._ic_results.get(row)
            return list(res.conflicts) if res is not None else []
        return list(group.requirement.conflicts)

    def accepted_edits(self) -> Plan:
        """Every queued plan merged into one ``{file: {uuid: {key: value}}}``
        plan, in row order. ``None`` values (property removals) are kept."""
        return merge_plans([self._queued[r].plan for r in sorted(self._queued)])

    # ---- status ------------------------------------------------------
    def status(self, row: int) -> str:
        if row in self._queued:
            return STATUS_QUEUED
        group = self._groups[row]
        req = group.requirement
        if self.conflicts(row):
            return STATUS_CONFLICT
        if group.assigned:
            return STATUS_ASSIGNED
        if is_tht(group):
            if req.no_part:
                return STATUS_NO_PART
            return STATUS_UNASSIGNED if req.searchable else STATUS_NOT_SEARCHABLE
        if is_ic(group):
            return STATUS_NOT_SEARCHABLE if not req.searchable else STATUS_UNASSIGNED
        if is_led(group):
            if req.footprint and not req.package.value:
                return STATUS_NOT_SEARCHABLE
            return STATUS_UNASSIGNED
        if (req.footprint and not req.package.value
                and not package_editable(req.ptype, req.footprint)):
            return STATUS_NOT_SEARCHABLE
        return STATUS_UNASSIGNED

    def summary(self) -> Dict[str, int]:
        """Row counts by status."""
        counts = {STATUS_ASSIGNED: 0, STATUS_QUEUED: 0, STATUS_UNASSIGNED: 0,
                  STATUS_CONFLICT: 0, STATUS_NOT_SEARCHABLE: 0, STATUS_NO_PART: 0}
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
            return ("Voltage:Min for a capacitor, Power:Min for a resistor, "
                    "Current:Min (saturation current) for an inductor")
        if role == Qt.ToolTipRole and section == COL_DIELECTRIC:
            return ("Dielectric for a capacitor, shielding for an inductor; "
                    "through-hole for a part found by its footprint")
        if role == Qt.ToolTipRole and section == COL_FOOTPRINT:
            return ("For an IC: red when the footprint does not match the part, "
                    "amber when it cannot be checked against it")
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def cell_text(self, row: int, col: int) -> str:
        group = self._groups[row]
        req = self.requirement_at(row)
        if col == COL_FOOTPRINT:
            return footprint_name(req.footprint) or DASH
        if is_ic(group):
            return self._ic_cell_text(row, col, group, req)
        if is_tht(group):
            return self._tht_cell_text(row, col, group, req)
        if is_led(group):
            return self._led_cell_text(row, col, group, req)
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

    def _ic_cell_text(self, row: int, col: int, group: IcGroup,
                      req: IcRequirement) -> str:
        if col == COL_VALUE:
            return ic_value_cell(req)
        if col == COL_PACKAGE:
            q = self._queued.get(row)
            return ic_package_cell(req, q.candidate if q is not None else None)
        if col in (COL_RATING, COL_DIELECTRIC, COL_TOLERANCE):
            return ""
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

    def _tht_cell_text(self, row: int, col: int, group: ThtGroup, req) -> str:
        if col == COL_VALUE:
            return req.value_text or DASH
        if col == COL_PACKAGE:
            return signature_text(req.signature)
        if col == COL_DIELECTRIC:
            return THT_CLASS
        if col in (COL_RATING, COL_TOLERANCE):
            return ""
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

    def _led_cell_text(self, row: int, col: int, group: LedGroup,
                       req: LedRequirement) -> str:
        if col == COL_VALUE:
            return led_value_cell(req)
        if col == COL_PACKAGE:
            return sourced_cell(req.package, "package", _NO_CLASS)
        if col == COL_RATING:
            return led_rating_cell(req)
        if col == COL_DIELECTRIC:
            return led_class_cell(req)
        if col == COL_TOLERANCE:
            return ""
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

    def _led_tooltip(self, row: int, col: int) -> Optional[str]:
        group = self._groups[row]
        req = self.requirement_at(row)
        status = self.status(row)
        if col == COL_FOOTPRINT:
            return req.footprint or "No footprint."
        if col == COL_REFERENCES:
            return ", ".join(group.references)
        if status == STATUS_CONFLICT:
            return ("Conflict:\n" + "\n".join(req.conflicts)
                    + "\nFix it on the schematic. Double-click for details.")
        if col == COL_DIELECTRIC:
            return ("Dominant wavelength band a part must fall in"
                    + (f": {req.band_text()}." if req.band_text() else "."))
        if col == COL_CURRENT:
            e = group.existing
            lines = [f"{name}: {v}" for name, v in (
                ("MPN", e.mpn), ("DigiKeyPN", e.digikey_pn), ("LCSC", e.lcsc)) if v]
            return "\n".join(lines) if lines else "No part number on the schematic."
        if status == STATUS_NOT_SEARCHABLE:
            return f"Not searchable: {req.problem()}."
        if status == STATUS_UNASSIGNED:
            problem = req.problem()
            if problem:
                return f"Before a search: {problem}. Double-click to assign a part."
            return "Double-click to assign a part."
        return None

    def _tht_tooltip(self, row: int, col: int) -> Optional[str]:
        group = self._groups[row]
        req = group.requirement
        status = self.status(row)
        if col == COL_FOOTPRINT:
            lines = [req.footprint or "No footprint."]
            if req.pad_count is not None:
                lines.append(f"{req.pad_count} pads.")
            if req.mounting_note:
                lines.append(req.mounting_note)
            return "\n".join(lines)
        if col == COL_REFERENCES:
            return ", ".join(group.references)
        if col == COL_PACKAGE:
            return "Read from the footprint name."
        if col == COL_CURRENT:
            e = group.existing
            lines = [f"{name}: {v}" for name, v in (
                ("MPN", e.mpn), ("Manufacturer", e.manufacturer),
                ("DigiKeyPN", e.digikey_pn)) if v]
            return "\n".join(lines) if lines else "No part number on the schematic."
        if status == STATUS_NO_PART:
            return "Nothing to buy for this footprint (a pad, hole or bare test point)."
        if status == STATUS_NOT_SEARCHABLE:
            return f"Not searchable: {req.problem()}."
        if status == STATUS_ASSIGNED:
            return "MPN, Manufacturer and DigiKeyPN on the schematic."
        if status == STATUS_UNASSIGNED:
            return "Through-hole, soldered by hand: DigiKey only. Double-click to assign a part."
        return None

    def _ic_tooltip(self, row: int, col: int) -> Optional[str]:
        group = self._groups[row]
        req = self.requirement_at(row)
        status = self.status(row)
        if col == COL_FOOTPRINT:
            lines = [req.footprint or "No footprint."]
            check = self.footprint_check(row)
            if check is not None:
                lines.append(check.message or "Footprint matches the part's package.")
            elif status == STATUS_NOT_SEARCHABLE:
                lines.append(f"Not checked: {req.problem()}.")
            elif req.footprint:
                lines.append("Not checked yet. Double-click to search.")
            return "\n".join(lines)
        if col == COL_REFERENCES:
            return ", ".join(group.references)
        if status == STATUS_CONFLICT:
            return ("Identifiers name different parts:\n" + "\n".join(self.conflicts(row))
                    + "\nDouble-click to choose one; fix the other on the schematic.")
        if col == COL_CURRENT:
            return f"Identifiers: {identifiers_text(req)}."
        if status == STATUS_NOT_SEARCHABLE:
            return f"Not searchable: {req.problem()}."
        if status == STATUS_ASSIGNED:
            return "Part numbers on the schematic. Double-click to check the footprint."
        if status == STATUS_UNASSIGNED:
            return "Double-click to assign a part."
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, col = index.row(), index.column()
        if role == Qt.DisplayRole:
            return self.cell_text(r, col)
        if role == Qt.BackgroundRole:
            if col == COL_FOOTPRINT:
                check = self.footprint_check(r)
                if check is not None and check.result in CHECK_TINTS:
                    return CHECK_TINTS[check.result]
            status = self.status(r)
            if status == STATUS_UNASSIGNED:
                return _UNASSIGNED_TINT
            return _CONFLICT_TINT if status == STATUS_CONFLICT else None
        if role == Qt.ToolTipRole:
            if is_ic(self._groups[r]):
                return self._ic_tooltip(r, col)
            if is_tht(self._groups[r]):
                return self._tht_tooltip(r, col)
            if is_led(self._groups[r]):
                return self._led_tooltip(r, col)
            req = self.requirement_at(r)
            if col == COL_FOOTPRINT and req.footprint:
                return req.footprint
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
            t = req.ptype
            if (not req.footprint and t.is_body_size(req.package.value)
                    and self.status(r) == STATUS_UNASSIGNED):
                return (f"No footprint: add one for the {req.package.value} body in "
                        "KiCad. Footprints are written for chip sizes only.")
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


class IcCandidateWorker(QObject):
    """Runs :func:`kicadtools.icsearch.assess_ic` for one IC group off the GUI
    thread; emits ``found`` (an ``IcSearchResult``) or ``failed``.

    *requirement* is the requirement of an earlier result (package and pad
    count already read), or ``None`` for the first request: the pad count is
    then read here, from *board*'s ``.kicad_pcb`` and the footprint libraries
    (:func:`kicadtools.icsearch.board_pads`). The parts database and the LCSC
    cache are opened on the worker thread and closed before the thread's event
    loop is ended."""

    found = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        group: IcGroup,
        requirement: Optional[IcRequirement],
        needed: int,
        db_path: Path,
        board: Optional[Path] = None,
        cache_path: Optional[Path] = None,
    ):
        super().__init__()
        self._group = group
        self._req = requirement
        self._needed = needed
        self._db_path = db_path
        self._board = board
        self._cache_path = cache_path

    def run(self) -> None:
        db: Optional[PartsDb] = None
        cache: Optional[Cache] = None
        try:
            db = PartsDb(self._db_path)
            if self._cache_path is not None:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache = Cache(self._cache_path)
            result = assess_ic(
                self._group,
                db=db,
                digikey=DigiKeyClient(),
                jlc_search=jlcpcb_search,
                jlc_part=lambda code: cached_jlcpcb_part(code, cache),
                needed=self._needed,
                pads=board_pads(self._board) if self._req is None else None,
                requirement=self._req,
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


class ThtCandidateWorker(QObject):
    """Runs :func:`kicadtools.thtassign.assess_tht` for one through-hole group
    off the GUI thread; emits ``found`` (a ``ThtSearchResult``) or
    ``failed``. *run* is the window's :class:`~kicadtools.headersearch.HeaderRun`
    (one header series per run); the parts database is opened on the worker
    thread and closed before the thread's event loop is ended. With
    *use_stored* false, parts stored in the database are not offered and
    DigiKey is searched."""

    found = Signal(object)
    failed = Signal(str)

    def __init__(self, *, group: ThtGroup, run, needed: int, db_path: Path,
                 use_stored: bool = True):
        super().__init__()
        self._group = group
        self._run = run
        self._needed = needed
        self._db_path = db_path
        self._use_stored = use_stored

    def run(self) -> None:
        db: Optional[PartsDb] = None
        try:
            db = PartsDb(self._db_path)
            result = assess_tht(self._group, run=self._run, digikey=self._run.client,
                                db=db, needed=self._needed, use_stored=self._use_stored)
            self.found.emit(result)
        except Exception as exc:  # noqa: BLE001 — report any failure in the dialog
            self.failed.emit(str(exc))
        finally:
            if db is not None:
                db.close()
            QThread.currentThread().quit()


class LedCandidateWorker(QObject):
    """Runs :func:`kicadtools.ledsearch.assess_led` for one plain-LED group
    off the GUI thread; emits ``found`` (a ``SearchResult``) or ``failed``.
    *listing* is the window's per-run JLCPCB listing
    (:func:`kicadtools.ledsearch.cached_listing`), so each package is listed
    once a session."""

    found = Signal(object)
    failed = Signal(str)

    def __init__(self, *, group: LedGroup, defaults: Mapping[str, str],
                 requirement: Optional[LedRequirement], needed: int, db_path: Path,
                 listing, search_suppliers: bool = False,
                 cache_path: Optional[Path] = None,
                 existing: Optional[LedLookup] = None):
        super().__init__()
        self._group = group
        self._defaults = dict(defaults)
        self._req = requirement
        self._needed = needed
        self._db_path = db_path
        self._listing = listing
        self._search_suppliers = search_suppliers
        self._cache_path = cache_path
        self._existing = existing

    def run(self) -> None:
        db: Optional[PartsDb] = None
        cache: Optional[Cache] = None
        try:
            db = PartsDb(self._db_path)
            if self._cache_path is not None:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache = Cache(self._cache_path)
            result = assess_led(
                self._group, self._defaults, db=db, jlc_listing=self._listing,
                jlc_part=lambda code: cached_jlcpcb_part(code, cache),
                jlc_search=jlcpcb_search, digikey=DigiKeyClient(),
                needed=self._needed, search_suppliers=self._search_suppliers,
                requirement=self._req, existing=self._existing,
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
