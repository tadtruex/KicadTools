"""
LCSC tab: a table of each BOM part's LCSC / DigiKey / MPN numbers, plus a
background worker that fills the gaps via :class:`kicadtools.resolve.Resolver`.

The table populates instantly from the part numbers already in the schematic;
pressing *Resolve LCSC* runs the (networked, serial) resolution pipeline on a
worker thread and streams each result back into the model as it lands.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QComboBox, QStyledItemDelegate

from kicadtools.partnum import CANONICAL_KEYS, extract_part_numbers
from kicadtools.partrotation import PartRotationStore
from kicadtools.resolve import Cache, PartRecord, Resolution, Resolver, Status
from kicadtools.suppliers import digikey_url, lcsc_url

# An "Accept" checkbox leads the row (mirroring the Intents tab), then the
# read-only part-number columns, an (editable) JLCPCB rotation override, with the
# (often long) description trailing last.
_LCSC_COLUMNS = ["Accept", "Ref", "LCSC", "DigiKey", "MPN", "Status", "Rotation",
                 "Description"]
_COL_DESCRIPTION = _LCSC_COLUMNS.index("Description")
_COL_LCSC = _LCSC_COLUMNS.index("LCSC")
_COL_DIGIKEY = _LCSC_COLUMNS.index("DigiKey")
_COL_ROTATION = _LCSC_COLUMNS.index("Rotation")

# The rotation-override choices offered in the dropdown (degrees CCW, additive on
# top of the family offset). JLCPCB parts only ever need quarter-turn fixes.
ROTATION_CHOICES = (0, 90, 180, 270)

# Custom data role: the URL a link cell (LCSC / DigiKey) points at, or ``None``.
# The view reads it on click to open the supplier page.
LINK_ROLE = Qt.UserRole + 1

# Row status text. The pre-resolution states describe what the schematic already
# has; the post-resolution states mirror the CLI's ✅ / ➕ / ⚑.
STATUS_HAS = "✅ has LCSC"
STATUS_NEEDS = "… needs lookup"
STATUS_NONE = "⚑ no part #"
STATUS_RESOLVED = "➕ resolved"
STATUS_FLAGGED = "⚑ flagged"
STATUS_ACCEPTED = "✔ accepted"

_RESULT_STATUS = {
    Status.OK: STATUS_HAS,
    Status.RESOLVED: STATUS_RESOLVED,
    Status.FLAGGED: STATUS_FLAGGED,
}


def _link_color() -> QColor:
    """The palette's link colour (theme-aware) when an app is running, else a
    reasonable blue for headless/test use."""
    app = QApplication.instance()
    if app is not None:
        return app.palette().link().color()
    return QColor("#1a6fdb")


def _link_font() -> QFont:
    f = QFont()
    f.setUnderline(True)
    return f


@dataclass
class LcscRow:
    reference: str
    lcsc: str
    digikey_pn: str
    mpn: str
    status: str
    description: str = ""
    reason: str = ""
    # Write-back provenance, carried from the source PartRecord.
    source_file: Optional[Path] = None
    symbol_uuid: Optional[str] = None
    had_mpn: bool = False  # did the symbol already carry an MPN before resolving?
    # Newly discovered values the symbol doesn't already have, as canonical
    # {key: value} ready for write-back. Empty until resolution finds something.
    pending: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: PartRecord) -> "LcscRow":
        pn = extract_part_numbers(record.props)
        if pn.lcsc:
            status = STATUS_HAS
        elif pn.mpn or pn.digikey_pn:
            status = STATUS_NEEDS
        else:
            status = STATUS_NONE
        desc = record.props.get("Description", "")
        return cls(
            reference=record.reference,
            lcsc=pn.lcsc or "",
            digikey_pn=pn.digikey_pn or "",
            mpn=pn.mpn or "",
            status=status,
            description=desc if isinstance(desc, str) else "",
            source_file=record.source_file,
            symbol_uuid=record.symbol_uuid,
            had_mpn=bool(pn.mpn),
        )


class LcscTableModel(QAbstractTableModel):
    """Table of Accept · Ref · LCSC · DigiKey · MPN · Status, one row per BOM
    part. Rows whose resolution discovered a value the schematic doesn't already
    carry become checkable; accepting them queues that value for write-back."""

    def __init__(
        self,
        records: Optional[Sequence[PartRecord]] = None,
        store: Optional[PartRotationStore] = None,
    ):
        super().__init__()
        self._rows: List[LcscRow] = [LcscRow.from_record(r) for r in records or []]
        self._checked: Set[int] = set()
        self._accepted: Set[int] = set()
        # Per-LCSC rotation overrides for the CPL export. Ephemeral by default so
        # headless/test models need no file; the app injects a disk-backed store.
        self._store = store if store is not None else PartRotationStore(":memory:")

    # ---- loading -----------------------------------------------------
    def load(self, records: Sequence[PartRecord]) -> None:
        self.beginResetModel()
        self._rows = [LcscRow.from_record(r) for r in records]
        self._checked.clear()
        self._accepted.clear()
        self.endResetModel()

    # ---- checkability ------------------------------------------------
    def _checkable(self, row: int) -> bool:
        """A row is checkable once resolution has found a new value for it and it
        hasn't been accepted yet."""
        return row not in self._accepted and bool(self._rows[row].pending)

    # ---- Qt dimensions ----------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_LCSC_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _LCSC_COLUMNS[section]
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0 and self._checkable(index.row()):
            f |= Qt.ItemIsUserCheckable
        if index.column() == _COL_ROTATION and self._rotatable(index.row()):
            f |= Qt.ItemIsEditable
        return f

    def _rotatable(self, row: int) -> bool:
        """A row carries an editable rotation override once it has an LCSC part
        number (the override is keyed on that part)."""
        return bool(self._rows[row].lcsc)

    def _rotation(self, row: int) -> float:
        """The stored extra rotation for *row*'s LCSC part, or 0.0."""
        return self._store.get(self._rows[row].lcsc) or 0.0

    def _status_text(self, row: int) -> str:
        return STATUS_ACCEPTED if row in self._accepted else self._rows[row].status

    def _link_url(self, row: "LcscRow", col: int) -> Optional[str]:
        """The supplier URL a cell links to (LCSC/DigiKey with a value), else
        ``None``."""
        if col == _COL_LCSC:
            return lcsc_url(row.lcsc)
        if col == _COL_DIGIKEY:
            return digikey_url(row.digikey_pn)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, col = index.row(), index.column()
        row = self._rows[r]

        if role == Qt.CheckStateRole and col == 0:
            if not self._checkable(r):
                return None
            return Qt.Checked if r in self._checked else Qt.Unchecked

        if col == _COL_ROTATION:
            if not self._rotatable(r):
                # The override is keyed on the LCSC part, so a row with no LCSC
                # number yet can't carry one — say so instead of looking broken.
                if role == Qt.DisplayRole:
                    return "—"
                if role == Qt.ToolTipRole:
                    return "Run Resolve LCSC first to set a rotation for this part."
                return None  # not editable, no EditRole value
            deg = self._rotation(r)
            if role == Qt.EditRole:
                return deg  # the delegate wants the raw degrees
            if role == Qt.DisplayRole:
                return f"{int(deg)}°"
            if role == Qt.ToolTipRole:
                return (
                    "Extra JLCPCB rotation for this LCSC part, added on top of the "
                    "footprint-family offset. Double-click to change; cached and "
                    "reused on every board."
                )
            return None

        # LCSC / DigiKey cells with a value are clickable links: expose the URL
        # (LINK_ROLE) and render them blue + underlined.
        url = self._link_url(row, col)
        if role == LINK_ROLE:
            return url
        if url is not None:
            if role == Qt.ForegroundRole:
                return _link_color()
            if role == Qt.FontRole:
                return _link_font()
            if role == Qt.ToolTipRole:
                return url

        if role == Qt.ToolTipRole:
            # The description is often long and gets elided in its column; show
            # the full text on hover. Other columns fall back to the row reason.
            if col == _COL_DESCRIPTION:
                return row.description or None
            return row.reason or None

        if role == Qt.DisplayRole:
            return (
                "",  # Accept column carries a checkbox, no text
                row.reference,
                row.lcsc,
                row.digikey_pn,
                row.mpn,
                self._status_text(r),
                "",  # Rotation handled above (needs the store)
                row.description,
            )[col]
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if (
            role == Qt.EditRole
            and index.column() == _COL_ROTATION
            and self._rotatable(index.row())
        ):
            try:
                degrees = float(value)
            except (TypeError, ValueError):
                return False
            lcsc = self._rows[index.row()].lcsc
            self._store.set(lcsc, degrees)
            # The override is keyed on the LCSC part, so every row sharing that
            # part now reads the new value — refresh them all.
            for r in range(len(self._rows)):
                if self._rows[r].lcsc == lcsc:
                    idx = self.index(r, _COL_ROTATION)
                    self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])
            return True
        if role == Qt.CheckStateRole and index.column() == 0 and self._checkable(index.row()):
            row = index.row()
            # The view passes an int; tests may pass a Qt.CheckState. Normalise.
            state = value if isinstance(value, Qt.CheckState) else Qt.CheckState(value)
            if state == Qt.Checked:
                self._checked.add(row)
            else:
                self._checked.discard(row)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            return True
        return False

    # ---- results -----------------------------------------------------
    def apply_resolution(self, row: int, result: Resolution) -> None:
        """Fold a :class:`Resolution` (from the worker) into *row*."""
        if not 0 <= row < len(self._rows):
            return
        r = self._rows[row]
        if result.lcsc:
            r.lcsc = result.lcsc
        if result.resolved_mpn:
            r.mpn = result.resolved_mpn
        r.status = _RESULT_STATUS[result.status]
        r.reason = result.reason
        # Queue whatever resolution newly discovered for write-back: a resolved
        # LCSC (the symbol had none, else it'd be Status.OK) and, if DigiKey
        # surfaced an MPN the symbol lacked, that MPN too.
        pending: Dict[str, str] = {}
        if result.status == Status.RESOLVED:
            if result.lcsc:
                pending[CANONICAL_KEYS["lcsc"]] = result.lcsc
            if result.resolved_mpn and not r.had_mpn:
                pending[CANONICAL_KEYS["mpn"]] = result.resolved_mpn
        r.pending = pending
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, self.columnCount() - 1)
        )

    # ---- accept ------------------------------------------------------
    def _checkable_rows(self) -> List[int]:
        return [r for r in range(len(self._rows)) if self._checkable(r)]

    def accept_checked(self) -> None:
        self._accept({r for r in self._checked if self._checkable(r)})

    def accept_all(self) -> None:
        self._accept(set(self._checkable_rows()))

    def _accept(self, rows: Set[int]) -> None:
        if not rows:
            return
        self._accepted |= rows
        self._checked -= rows
        top = self.index(min(rows), 0)
        bottom = self.index(max(rows), self.columnCount() - 1)
        self.dataChanged.emit(top, bottom)

    def accepted_edits(self) -> Dict[Path, Dict[str, Dict[str, str]]]:
        """Group accepted rows into ``{source_file: {symbol_uuid: {key: value}}}``
        for :func:`kicadtools.writer.write_properties`."""
        edits: Dict[Path, Dict[str, Dict[str, str]]] = defaultdict(dict)
        for row in self._accepted:
            r = self._rows[row]
            if r.source_file and r.symbol_uuid and r.pending:
                edits[Path(r.source_file)].setdefault(r.symbol_uuid, {}).update(
                    r.pending
                )
        return dict(edits)

    def rows(self) -> List[LcscRow]:
        return self._rows

    def rotation_overrides(self) -> Dict[str, float]:
        """``{reference: degrees}`` for every row whose LCSC part has a non-zero
        rotation override — ready to feed ``cpl_from_schematic``'s
        ``manual_rotation`` so the CPL export honours the dropdown choices."""
        out: Dict[str, float] = {}
        for row in self._rows:
            deg = self._store.get(row.lcsc) if row.lcsc else None
            if deg:
                out[row.reference] = deg
        return out


class RotationDelegate(QStyledItemDelegate):
    """A dropdown editor for the LCSC tab's Rotation column, offering the
    quarter-turn overrides in :data:`ROTATION_CHOICES`."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for deg in ROTATION_CHOICES:
            combo.addItem(f"{deg}°", deg)
        return combo

    def setEditorData(self, editor, index):
        current = index.data(Qt.EditRole)
        try:
            pos = ROTATION_CHOICES.index(int(current or 0))
        except (TypeError, ValueError):
            pos = 0
        editor.setCurrentIndex(pos)

    def setModelData(self, editor, model, index):
        model.setData(index, float(editor.currentData()), Qt.EditRole)


class ResolveWorker(QObject):
    """Runs the resolution pipeline off the GUI thread.

    Lives in a :class:`QThread`; :meth:`run` is invoked by the thread's
    ``started`` signal. Emits one ``row_resolved`` per part (queued back to the
    main thread) and a final ``finished`` with the ✅/➕/⚑ tallies.
    """

    progress = Signal(str)
    row_resolved = Signal(int, object)   # (source row index, Resolution)
    finished = Signal(int, int, int)     # (had_lcsc, resolved, flagged)

    def __init__(
        self,
        records: Sequence[PartRecord],
        cache_path: Optional[Path] = None,
        delay: float = 0.5,
    ):
        super().__init__()
        self._records = list(records)
        self._cache_path = cache_path
        self._delay = delay

    def run(self) -> None:
        # The SQLite cache is created and used entirely within this thread.
        cache = Cache(self._cache_path) if self._cache_path else None
        resolver = Resolver(
            cache=cache,
            delay=self._delay,
            progress=lambda ref, msg: self.progress.emit(f"{ref}: {msg}"),
        )
        counts = {Status.OK: 0, Status.RESOLVED: 0, Status.FLAGGED: 0}
        for i, record in enumerate(self._records):
            result = resolver.resolve(record)
            counts[result.status] += 1
            self.row_resolved.emit(i, result)
        if cache:
            cache.close()
        self.finished.emit(
            counts[Status.OK], counts[Status.RESOLVED], counts[Status.FLAGGED]
        )
