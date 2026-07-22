"""
Build a BOM in the CSV format DigiKey's cart / BOM uploader ingests.

DigiKey's uploader auto-detects four columns — ``Quantity``, ``Digi-Key Part
Number``, ``Reference Designator`` (all the reference designators for one part,
comma-joined in a single cell) and ``Manufacturer Part Number``. DigiKey matches
a line on its Digi-Key part number when present, otherwise on the manufacturer
part number; the reference designators are carried through onto the order for
your own cross-referencing. A line with neither part number can't be ordered —
it still exports, but it's reported.

Identical parts collapse to one line. Grouping is by ``(Value, Footprint,
DigiKeyPN, MPN)`` — the part numbers are part of the key so two otherwise
identical parts with different (or one-blank) numbers stay on separate lines,
and Value/Footprint keep genuinely different parts that happen to share a blank
part number apart (so the quantities stay meaningful) even though those two
columns are not themselves emitted.

Parts can be filtered by mounting — surface-mount only, non-surface-mount only
(through-hole *and* undetermined), or both — read from the sibling
``.kicad_pcb`` (see :data:`MOUNTING_FILTERS`). Unlike the JLCPCB export there is
no per-line designator cap: DigiKey imposes none.

Pure and I/O-free apart from the two thin wrappers at the bottom
(:func:`digikey_bom_from_schematic` reads a schematic; :func:`write_digikey_csv`
writes to an open file), so the grouping logic is easy to test and shared by the
CLI (and, later, the GUI).
"""
from __future__ import annotations

import csv
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.pcb import SMD, UNKNOWN
from kicadtools.resolve import PartRecord, iter_bom_part_records

# The exact header row DigiKey's uploader auto-detects. "Quantity" +
# "Digi-Key Part Number" alone are enough to add to a cart; the MPN is a
# fallback match key and the customer reference is carried onto the order.
DK_FIELDS = [
    "Quantity",
    "Digi-Key Part Number",
    "Reference Designator",
    "Manufacturer Part Number",
]

# Mounting-filter modes for :func:`build_digikey_bom`. "non-smd" keeps every
# part that isn't surface-mount — through-hole *and* undetermined (so a board
# with no PCB yet, where every part reads "unknown", still exports in full).
SMD_ONLY = "smd"
NON_SMD = "non-smd"
BOTH = "both"
MOUNTING_FILTERS = (SMD_ONLY, NON_SMD, BOTH)


def _passes(mounting_value: str, mode: str) -> bool:
    """Whether a part with *mounting_value* survives the *mode* filter."""
    if mode == SMD_ONLY:
        return mounting_value == SMD
    if mode == NON_SMD:
        return mounting_value != SMD  # through-hole and undetermined both kept
    return True  # BOTH


@dataclass(frozen=True)
class DigiKeyBomLine:
    """One line of a DigiKey BOM: a group of identical parts."""

    digikey_pn: str  # "" when the part carries no DigiKey number
    mpn: str  # "" when the part carries no manufacturer number
    references: Tuple[str, ...]  # natural-sorted refdes in this group
    mounting: str = UNKNOWN  # smd / tht / unknown (from the PCB, when available)

    @property
    def customer_reference(self) -> str:
        return ",".join(self.references)

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def orderable(self) -> bool:
        """A line DigiKey can match — it carries a DigiKey or manufacturer number."""
        return bool(self.digikey_pn or self.mpn)


def build_digikey_bom(
    records: Sequence[PartRecord],
    mounting: Optional[Mapping[str, str]] = None,
    mounting_filter: str = BOTH,
) -> List[DigiKeyBomLine]:
    """Group *records* into DigiKey BOM lines. Pure — no I/O.

    Parts are grouped by ``(Value, Footprint, DigiKeyPN, MPN)``; their refdes are
    collected, natural-sorted (``C2`` before ``C10``) and comma-joined into one
    Customer Reference cell. Lines are ordered by the natural key of their first
    designator, so the output is deterministic regardless of traversal order.

    *mounting* maps a refdes to ``"smd"``/``"tht"``/``"unknown"`` (from the board
    ``.kicad_pcb``); each line records its group's mounting. *mounting_filter*
    (one of :data:`MOUNTING_FILTERS`) selects which parts are kept: ``"smd"``
    keeps only surface-mount parts, ``"non-smd"`` keeps everything else
    (through-hole *and* undetermined — including every part when *mounting* is
    empty because no PCB was found), ``"both"`` keeps them all.
    """
    mount = mounting or {}
    # Preserve first-seen order per group so the line's mounting is stable.
    groups: "OrderedDict[Tuple[str, str, str, str], List[str]]" = OrderedDict()

    for rec in records:
        if not _passes(mount.get(rec.reference, UNKNOWN), mounting_filter):
            continue
        value = str(rec.props.get("Value", "") or "")
        footprint = str(rec.props.get("Footprint", "") or "")
        pn = extract_part_numbers(rec.props)
        digikey_pn = (pn.digikey_pn or "").strip()
        mpn = (pn.mpn or "").strip()
        groups.setdefault((value, footprint, digikey_pn, mpn), []).append(rec.reference)

    lines: List[DigiKeyBomLine] = []
    for (_value, _footprint, digikey_pn, mpn), refs in groups.items():
        ordered = sorted(refs, key=natural_key)
        lines.append(
            DigiKeyBomLine(
                digikey_pn=digikey_pn,
                mpn=mpn,
                references=tuple(ordered),
                # A group shares one footprint, so its parts share one mounting.
                mounting=mount.get(ordered[0], UNKNOWN),
            )
        )
    lines.sort(key=lambda ln: natural_key(ln.references[0]))
    return lines


def unorderable_lines(lines: Sequence[DigiKeyBomLine]) -> List[DigiKeyBomLine]:
    """The lines DigiKey can't match — no DigiKey number and no MPN."""
    return [ln for ln in lines if not ln.orderable]


def write_digikey_csv(lines: Sequence[DigiKeyBomLine], fileobj) -> None:
    """Write *lines* as a DigiKey BOM CSV to the open text *fileobj*.

    Multi-designator cells contain commas and are quoted automatically
    (``QUOTE_MINIMAL``). Open the file with ``newline=""`` (as the caller does)
    so no blank lines appear between rows on Windows.
    """
    writer = csv.DictWriter(fileobj, fieldnames=DK_FIELDS)
    writer.writeheader()
    for ln in lines:
        writer.writerow(
            {
                "Quantity": ln.quantity,
                "Digi-Key Part Number": ln.digikey_pn,
                "Reference Designator": ln.customer_reference,
                "Manufacturer Part Number": ln.mpn,
            }
        )


def digikey_bom_from_schematic(
    path: Path, mounting_filter: str = BOTH
) -> List[DigiKeyBomLine]:
    """Read the schematic at *path* and build its DigiKey BOM lines.

    Note: this convenience wrapper never reads a PCB, so *mounting_filter* only
    has an effect other than ``"both"`` when passed alongside a caller-supplied
    mounting map — which this wrapper doesn't do. The CLI reads the mounting map
    itself and calls :func:`build_digikey_bom` directly.
    """
    return build_digikey_bom(iter_bom_part_records(Path(path)), mounting_filter=mounting_filter)
