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

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QComboBox, QStyledItemDelegate

from kicadtools.intent import ElectricalIntent
from kicadtools.partnum import CANONICAL_KEYS, extract_part_numbers
from kicadtools.partrotation import PartRotationStore
from kicadtools.resolve import (
    Cache,
    PartRecord,
    Resolution,
    Resolver,
    Status,
    cached_jlcpcb_part,
)
from kicadtools.substitute import (
    ALTERNATES_KEY,
    DEFAULT_BUILD_QTY,
    STOCK_LOW,
    STOCK_OUT,
    merge_alternates,
    search_substitutes,
    stock_level,
)
from kicadtools.suppliers import (
    DigiKeyClient,
    JlcPart,
    digikey_url,
    jlcpcb_search,
    lcsc_url,
)

# An "Accept" checkbox leads the row (mirroring the Intents tab), then the
# read-only part-number columns, JLCPCB stock, an (editable) JLCPCB rotation
# override, with the (often long) description trailing last.
_LCSC_COLUMNS = ["Accept", "Ref", "LCSC", "DigiKey", "MPN", "Status", "Stock",
                 "Rotation", "Description"]
_COL_DESCRIPTION = _LCSC_COLUMNS.index("Description")
_COL_LCSC = _LCSC_COLUMNS.index("LCSC")
_COL_DIGIKEY = _LCSC_COLUMNS.index("DigiKey")
_COL_STOCK = _LCSC_COLUMNS.index("Stock")
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
STATUS_SUBSTITUTE = "⇄ substitute"

# Translucent row tints for stock problems, readable on light and dark themes.
_STOCK_TINT = {
    STOCK_OUT: QColor(220, 50, 47, 70),   # red: none in stock / not sold
    STOCK_LOW: QColor(230, 160, 0, 70),   # amber: fewer than the build needs
}

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
    footprint: str = ""
    alternates: str = ""  # the symbol's Alternates property, as on disk
    intent: Optional[ElectricalIntent] = None
    replaced_lcsc: str = ""  # the LCSC a chosen substitute replaced

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
            footprint=str(record.props.get("Footprint", "") or ""),
            alternates=str(record.props.get(ALTERNATES_KEY, "") or ""),
            intent=record.intent,
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
        # JLCPCB catalog entries by LCSC code, from Check Stock. Kept across
        # load() so a save-and-reload doesn't drop the stock colouring.
        self._stock: Dict[str, JlcPart] = {}
        self.build_qty = DEFAULT_BUILD_QTY

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
        status = self._rows[row].status
        if row in self._accepted and status != STATUS_SUBSTITUTE:
            return STATUS_ACCEPTED
        return status

    # ---- stock -------------------------------------------------------
    def set_build_qty(self, qty: int) -> None:
        """Set how many boards the build places; re-tints every row."""
        self.build_qty = max(1, int(qty))
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, self.columnCount() - 1)
            )

    def lcsc_codes(self) -> List[str]:
        """The distinct LCSC codes on the board, in row order."""
        return list(dict.fromkeys(r.lcsc for r in self._rows if r.lcsc))

    def apply_stock(self, lcsc: str, part: Optional[JlcPart]) -> None:
        """Record *lcsc*'s catalog entry (``None``: not found on JLCPCB)."""
        if part is None:
            # Not in the catalog at all: as unorderable as zero stock.
            part = JlcPart(lcsc=lcsc, no_buy_reason="not found on JLCPCB")
        self._stock[lcsc] = part
        for r, row in enumerate(self._rows):
            if row.lcsc == lcsc:
                self.dataChanged.emit(self.index(r, 0), self.index(r, self.columnCount() - 1))

    def part_for(self, lcsc: str) -> Optional[JlcPart]:
        """The checked catalog entry for LCSC code *lcsc*, if any."""
        return self._stock.get(lcsc) if lcsc else None

    def stock_part(self, row: int) -> Optional[JlcPart]:
        return self.part_for(self._rows[row].lcsc)

    def per_board(self, row: int) -> int:
        """How many places on one board use *row*'s LCSC part (1 without one)."""
        lcsc = self._rows[row].lcsc
        if not lcsc:
            return 1
        return sum(1 for r in self._rows if r.lcsc == lcsc)

    def needed(self, row: int) -> int:
        """Units of *row*'s part the whole build needs."""
        return self.per_board(row) * self.build_qty

    def stock_level(self, row: int) -> Optional[str]:
        return stock_level(self.stock_part(row), self.needed(row))

    def _stock_cell(self, r: int, role):
        row = self._rows[r]
        part = self.stock_part(r)
        if role == Qt.DisplayRole:
            if not row.lcsc:
                return "—"
            return "" if part is None else f"{part.stock:,}"
        if role == Qt.ToolTipRole:
            if not row.lcsc:
                return "No LCSC number to check."
            if part is None:
                return "Run Check Stock to read JLCPCB's stock."
            lines = [
                f"Need {self.needed(r):,} ({self.per_board(r)} per board × "
                f"{self.build_qty} boards); JLCPCB has {part.stock:,}."
            ]
            if part.no_buy_reason:
                lines.append(f"JLCPCB: {part.no_buy_reason}")
            lines.append(f"{'Basic' if part.is_basic else 'Extended'} part.")
            return "\n".join(lines)
        return None

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

        if role == Qt.BackgroundRole:
            return _STOCK_TINT.get(self.stock_level(r))

        if col == _COL_STOCK:
            return self._stock_cell(r, role)

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
                "",  # Stock handled above
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
        if r.status == STATUS_SUBSTITUTE:
            return  # a chosen substitute outranks a re-resolution of the old part
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

    # ---- substitutes -------------------------------------------------
    def rows_like(self, row: int) -> List[int]:
        """*row* and every other row placing the same part (same LCSC and
        footprint) — the set one substitute choice replaces. A row without an
        LCSC number stands alone."""
        base = self._rows[row]
        if not base.lcsc:
            return [row]
        return [
            i for i, r in enumerate(self._rows)
            if r.lcsc == base.lcsc and r.footprint == base.footprint
        ]

    def apply_substitute(
        self, rows: Sequence[int], part: JlcPart, digikey_pn: Optional[str]
    ) -> None:
        """Replace *rows*' LCSC part with *part* and accept the change.

        Queues ``LCSC`` = the substitute's code and, when *digikey_pn* is known,
        appends it to the symbol's ``Alternates`` list. The symbol's original
        ``MPN`` / ``DigiKeyPN`` / ``Manufacturer`` are left as they are. Any
        part numbers resolution had already queued for the row are kept.
        """
        self._stock[part.lcsc] = part
        for i in rows:
            r = self._rows[i]
            r.replaced_lcsc = r.replaced_lcsc or r.lcsc
            r.lcsc = part.lcsc
            r.status = STATUS_SUBSTITUTE
            r.pending = dict(r.pending)
            r.pending[CANONICAL_KEYS["lcsc"]] = part.lcsc
            if digikey_pn:
                r.pending[ALTERNATES_KEY] = merge_alternates(
                    r.pending.get(ALTERNATES_KEY, r.alternates), digikey_pn
                )
            was = f" for {r.replaced_lcsc}" if r.replaced_lcsc else ""
            r.reason = f"substitute{was}: {part.mpn} ({part.manufacturer})"
            self._accepted.add(i)
            self._checked.discard(i)
        for i in rows:
            self.dataChanged.emit(self.index(i, 0), self.index(i, self.columnCount() - 1))

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
            if QThread.currentThread().isInterruptionRequested():
                break  # the window is closing
            result = resolver.resolve(record)
            counts[result.status] += 1
            self.row_resolved.emit(i, result)
        if cache:
            cache.close()
        self.finished.emit(
            counts[Status.OK], counts[Status.RESOLVED], counts[Status.FLAGGED]
        )


class StockWorker(QObject):
    """Reads JLCPCB's catalog entry (stock included) for each LCSC code, off
    the GUI thread. Emits ``part_checked`` per code and ``finished`` with the
    number found, then ends its thread's event loop itself so the window can
    ``wait()`` on the thread while closing."""

    progress = Signal(str)
    part_checked = Signal(str, object)   # (lcsc, JlcPart or None)
    finished = Signal(int, int)          # (found, not found)

    def __init__(self, codes: Sequence[str], cache_path: Optional[Path] = None):
        super().__init__()
        self._codes = list(codes)
        self._cache_path = cache_path

    def run(self) -> None:
        cache = Cache(self._cache_path) if self._cache_path else None
        found = missing = 0
        for i, code in enumerate(self._codes, 1):
            if QThread.currentThread().isInterruptionRequested():
                break
            self.progress.emit(f"Checking JLCPCB stock {i}/{len(self._codes)}: {code}…")
            part = cached_jlcpcb_part(code, cache)
            found += part is not None
            missing += part is None
            self.part_checked.emit(code, part)
        if cache:
            cache.close()
        self.finished.emit(found, missing)
        QThread.currentThread().quit()


class SubstituteWorker(QObject):
    """Runs :func:`kicadtools.substitute.search_substitutes` for one part off
    the GUI thread; emits ``found`` (a ``SubstituteSearch``) or ``failed``."""

    found = Signal(object)
    failed = Signal(str)

    def __init__(self, *, row: LcscRow, needed: int, cache_path: Optional[Path] = None):
        super().__init__()
        self._row = row
        self._needed = needed
        self._cache_path = cache_path

    def run(self) -> None:
        cache = Cache(self._cache_path) if self._cache_path else None
        try:
            # The current part comes from the board's own numbers, not a
            # substitute already chosen this session.
            lcsc = self._row.replaced_lcsc or self._row.lcsc
            result = search_substitutes(
                intent=self._row.intent,
                footprint=self._row.footprint,
                lcsc=lcsc,
                mpn=self._row.mpn,
                digikey_pn=self._row.digikey_pn,
                needed=self._needed,
                part_lookup=lambda code: cached_jlcpcb_part(code, cache),
                search=jlcpcb_search,
                digikey=DigiKeyClient(),
            )
            if cache:
                for cand in result.candidates:
                    cache.put_part(cand.part)
            self.found.emit(result)
        except Exception as exc:  # noqa: BLE001 — report any failure in the dialog
            self.failed.emit(str(exc))
        finally:
            if cache:
                cache.close()
            QThread.currentThread().quit()
