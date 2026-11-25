"""The one-time import of the master component list into the lab inventory
(``INVENTORY_PLAN.md`` step 2).

:func:`read_table` reads the spreadsheet (``.numbers`` through
``numbers-parser``, or a ``.csv`` export) into rows keyed by column header;
:func:`convert_row` turns one row into an :class:`ImportItem`;
:func:`import_items` writes them to an empty inventory in one transaction.

Columns (matched by header, ignoring case and spacing; others are ignored
and listed in the report):

- **Comp Number** — the bin: a location named after the number inside the
  location given (``17.0`` with ``school`` → ``"school/17"``), with nothing
  else set; its description and notes are set later on the location
  itself. Blank: the item goes to the top-level location ``unknown``, with
  its quantity (as :mod:`kicadtools.inventorymigrate` does).
- **Component Type** — the category: ``R`` Resistor, ``C`` Capacitor, ``L``
  Inductor, ``D`` Diode, ``Q`` Transistor, ``U`` IC, ``VR`` Regulator, ``P``
  Processor, ``Y`` Crystal; any other code is kept as written.
- **Value** and **Package** — the item's name, ``"4.7k 0603"``. A package
  stored as a number is a chip size and gets its leading zero back
  (``603.0`` → ``0603``). A resistor value stored as a number gets ``Ω``
  (``330.0`` → ``330Ω``).
- **Tolerance** and **Description** — the description, joined with ``", "``.
  A tolerance stored as a fraction is a percentage (``0.01`` → ``1%``); text
  (a capacitor's dielectric, ``256 Steps``) is kept. A number in
  Description is a capacitor's voltage (``50.0`` → ``50V``) or an
  inductor's current (``3.5`` → ``3.5A``).
- **Approx Stock** — the quantity.
- **Manufacturer PN**, **DigiKey PN** — kept as written.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kicadtools.inventorydb import SEP, InventoryDb, InventoryError, location_name
from kicadtools.inventorymigrate import UNPLACED

CATEGORIES = {
    "R": "Resistor", "C": "Capacitor", "L": "Inductor", "D": "Diode",
    "Q": "Transistor", "U": "IC", "VR": "Regulator", "P": "Processor",
    "Y": "Crystal",
}


# Column header (normalised) -> field.
_COLUMNS = {
    "componenttype": "type",
    "compnumber": "bin",
    "value": "value",
    "tolerance": "tolerance",
    "package": "package",
    "description": "description",
    "approxstock": "quantity",
    "manufacturerpn": "mpn",
    "digikeypn": "digikey_pn",
}


class InventoryImportError(RuntimeError):
    """The import was refused or failed; nothing was written."""


@dataclass(frozen=True)
class ImportItem:
    """One row of the list, ready to add. *line* is its spreadsheet row
    (the header is row 1)."""
    line: int
    name: str
    bin: Optional[str]
    quantity: Optional[int]
    category: Optional[str]
    description: Optional[str]
    mpn: Optional[str]
    digikey_pn: Optional[str]


@dataclass
class ImportReport:
    items: int = 0
    bins: int = 0                    # locations made for Comp Numbers
    unbinned: List[str] = field(default_factory=list)   # put in UNPLACED
    skipped: List[str] = field(default_factory=list)      # rows with no value
    ignored_columns: List[str] = field(default_factory=list)
    dry_run: bool = False

    def lines(self) -> List[str]:
        out = [f"{self.items} items, {self.bins} bins"
               + (" (dry run: rolled back)" if self.dry_run else "")]
        if self.unbinned:
            out.append(f"in no bin, put in {UNPLACED!r} ({len(self.unbinned)}): "
                       + ", ".join(self.unbinned))
        if self.skipped:
            out.append(f"skipped ({len(self.skipped)}): " + "; ".join(self.skipped))
        if self.ignored_columns:
            out.append("columns ignored: " + ", ".join(self.ignored_columns))
        return out


def _norm_header(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _number(value: Any) -> Optional[float]:
    """*value* as a number when it is one (a cell's float, or text such as
    ``"603"`` from a CSV); ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    try:
        return float(text) if re.fullmatch(r"-?\d+(\.\d+)?", text) else None
    except ValueError:
        return None


def _text(value: Any) -> Optional[str]:
    """A cell as text: a whole number without ``.0``; ``None`` when blank."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A whole number in full (a part number stored as a number keeps
        # every digit); otherwise the shortest form that reads back the same.
        return str(int(value)) if float(value).is_integer() else repr(float(value))
    text = " ".join(str(value).split())
    return text or None


def _package(value: Any) -> Optional[str]:
    n = _number(value)
    if n is not None and n == int(n) and 0 < n < 10000:
        return f"{int(n):04d}"
    return _text(value)


def _tolerance(value: Any) -> Optional[str]:
    n = _number(value)
    if n is not None and 0 < n < 1:
        return f"{_text(round(n * 100, 6))}%"
    return _text(value)


def _rating(code: Optional[str], value: Any) -> Optional[str]:
    """The Description cell: a number is a capacitor's voltage or an
    inductor's current."""
    n = _number(value)
    if n is not None:
        unit = {"C": "V", "L": "A"}.get(code or "", "")
        return f"{_text(n)}{unit}"
    return _text(value)


def _bin_label(value: Any) -> Optional[str]:
    """The bin name of a Comp Number cell (``17.0`` → ``"17"``)."""
    n = _number(value)
    if n is not None and n == int(n):
        return str(int(n))
    return _text(value)


def read_table(path: Path) -> List[Dict[str, Any]]:
    """The rows of the first table in *path* (``.numbers`` or ``.csv``),
    each a dict from header to cell; blank rows dropped. The header row is
    the first row. Each dict also holds ``"__line__"``, its row number."""
    path = Path(path)
    if not path.exists():
        raise InventoryImportError(f"{path}: no such file")
    suffix = path.suffix.lower()
    if suffix == ".numbers":
        try:
            from numbers_parser import Document
        except ImportError:
            raise InventoryImportError(
                "reading a .numbers file needs numbers-parser "
                "(pip install 'kicadtools[inventory]'), or export the sheet as CSV") from None
        table = Document(str(path)).sheets[0].tables[0]
        grid = table.rows(values_only=True)
    elif suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            grid = [[c if c != "" else None for c in row] for row in csv.reader(f)]
    else:
        raise InventoryImportError(f"{path}: not a .numbers or .csv file")
    if not grid:
        raise InventoryImportError(f"{path}: empty")
    header = [h for h in grid[0]]
    rows = []
    for number, cells in enumerate(grid[1:], start=2):
        if not any(c is not None and str(c).strip() for c in cells):
            continue
        row = {str(h): c for h, c in zip(header, cells) if h is not None}
        row["__line__"] = number
        rows.append(row)
    return rows


def columns(rows: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, str], List[str]]:
    """({field: header}, [headers not used]) of the rows' headers."""
    found: Dict[str, str] = {}
    ignored: List[str] = []
    for header in (rows[0].keys() if rows else ()):
        if header == "__line__":
            continue
        name = _COLUMNS.get(_norm_header(header))
        if name and name not in found:
            found[name] = header
        else:
            ignored.append(header)
    return found, ignored


def convert_row(row: Dict[str, Any], cols: Dict[str, str]) -> Optional[ImportItem]:
    """The item of one row; ``None`` when the row has no Value."""
    def cell(name: str) -> Any:
        return row.get(cols[name]) if name in cols else None

    code = _text(cell("type"))
    value = _text(cell("value"))
    if value is None:
        return None
    if code == "R" and _number(cell("value")) is not None:
        value += "Ω"
    package = _package(cell("package"))
    name = f"{value} {package}" if package else value
    description = ", ".join(t for t in (_tolerance(cell("tolerance")),
                                         _rating(code, cell("description"))) if t) or None
    quantity = _number(cell("quantity"))
    bin_name = _bin_label(cell("bin"))
    return ImportItem(
        line=int(row.get("__line__", 0)), name=name, bin=bin_name,
        quantity=int(round(quantity)) if quantity is not None and quantity >= 0 else None,
        category=CATEGORIES.get(code or "", code), description=description,
        mpn=_text(cell("mpn")), digikey_pn=_text(cell("digikey_pn")))


def convert(rows: Sequence[Dict[str, Any]]) -> Tuple[List[ImportItem], ImportReport]:
    """Every row's item, and a report with the rows skipped and the columns
    ignored. Refuses a table without a Value column."""
    cols, ignored = columns(rows)
    if "value" not in cols:
        raise InventoryImportError("the table has no Value column")
    report = ImportReport(ignored_columns=ignored)
    items = []
    for row in rows:
        item = convert_row(row, cols)
        if item is None:
            report.skipped.append(f"row {row.get('__line__')}: no value")
        else:
            items.append(item)
    return items, report


class _DryRun(Exception):
    pass


def import_items(inv: InventoryDb, items: Sequence[ImportItem], *, location: str,
                 report: Optional[ImportReport] = None,
                 dry_run: bool = False) -> ImportReport:
    """Add *items* to the empty inventory *inv* in one transaction, each
    with one stock row: in *location*/Comp Number (``school/17``), or in
    ``unknown`` for a row with no Comp Number. With *dry_run* everything is
    done and rolled back. Refuses an inventory that already holds locations
    or items."""
    report = report or ImportReport()
    report.dry_run = dry_run
    try:
        top = location_name(location or "")
    except InventoryError as exc:
        raise InventoryImportError(f"a location is needed for the imported bins: {exc}") \
            from exc
    if inv.locations() or inv.items():
        raise InventoryImportError(
            f"{inv.db.label}: the inventory already holds locations or items; "
            "the import only fills an empty one")
    try:
        with inv._tx():
            for item in items:
                where = f"{top}{SEP}{item.bin}" if item.bin is not None else UNPLACED
                if item.bin is not None and inv.location(where) is None:
                    report.bins += 1
                try:
                    inv.add_item(item.name, location=where, quantity=item.quantity,
                                 category=item.category, description=item.description,
                                 mpn=item.mpn, digikey_pn=item.digikey_pn)
                except InventoryError as exc:
                    raise InventoryImportError(f"row {item.line} ({item.name}): {exc}") from exc
                report.items += 1
                if item.bin is None:
                    report.unbinned.append(item.name)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report
