"""
LCSC tab: a table of each BOM part's LCSC / DigiKey / MPN numbers, plus a
background worker that fills the gaps via :class:`kicadtools.resolve.Resolver`.

The table populates instantly from the part numbers already in the schematic;
pressing *Resolve LCSC* runs the (networked, serial) resolution pipeline on a
worker thread and streams each result back into the model as it lands.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal

from kicadtools.partnum import extract_part_numbers
from kicadtools.resolve import Cache, PartRecord, Resolution, Resolver, Status

_LCSC_COLUMNS = ["Ref", "LCSC", "DigiKey", "MPN", "Status"]

# Row status text. The pre-resolution states describe what the schematic already
# has; the post-resolution states mirror the CLI's ✅ / ➕ / ⚑.
STATUS_HAS = "✅ has LCSC"
STATUS_NEEDS = "… needs lookup"
STATUS_NONE = "⚑ no part #"
STATUS_RESOLVED = "➕ resolved"
STATUS_FLAGGED = "⚑ flagged"

_RESULT_STATUS = {
    Status.OK: STATUS_HAS,
    Status.RESOLVED: STATUS_RESOLVED,
    Status.FLAGGED: STATUS_FLAGGED,
}


@dataclass
class LcscRow:
    reference: str
    lcsc: str
    digikey_pn: str
    mpn: str
    status: str
    reason: str = ""

    @classmethod
    def from_record(cls, record: PartRecord) -> "LcscRow":
        pn = extract_part_numbers(record.props)
        if pn.lcsc:
            status = STATUS_HAS
        elif pn.mpn or pn.digikey_pn:
            status = STATUS_NEEDS
        else:
            status = STATUS_NONE
        return cls(
            reference=record.reference,
            lcsc=pn.lcsc or "",
            digikey_pn=pn.digikey_pn or "",
            mpn=pn.mpn or "",
            status=status,
        )


class LcscTableModel(QAbstractTableModel):
    """Table of Ref · LCSC · DigiKey · MPN · Status, one row per BOM part."""

    def __init__(self, records: Optional[Sequence[PartRecord]] = None):
        super().__init__()
        self._rows: List[LcscRow] = [LcscRow.from_record(r) for r in records or []]

    # ---- loading -----------------------------------------------------
    def load(self, records: Sequence[PartRecord]) -> None:
        self.beginResetModel()
        self._rows = [LcscRow.from_record(r) for r in records]
        self.endResetModel()

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
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ToolTipRole:
            return row.reason or None
        if role == Qt.DisplayRole:
            return (
                row.reference,
                row.lcsc,
                row.digikey_pn,
                row.mpn,
                row.status,
            )[index.column()]
        return None

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
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, self.columnCount() - 1)
        )

    def rows(self) -> List[LcscRow]:
        return self._rows


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
