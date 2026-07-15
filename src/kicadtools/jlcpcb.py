"""
Build a BOM in the CSV format JLCPCB's SMT-assembly uploader expects.

JLCPCB auto-detects four columns — ``Comment`` (the part value), ``Designator``
(all refdes for one part, comma-joined in a single cell), ``Footprint``, and
``LCSC Part #`` (the JLCPCB/LCSC C-number). Only the first three are strictly
required; the LCSC number is optional at upload but strongly recommended — a
blank cell means JLCPCB matches (or asks you to pick) the part by hand.

Identical parts collapse to one line. Grouping is by ``(Value, Footprint,
LCSC)`` — LCSC is part of the key on purpose: JLCPCB wants exactly one LCSC per
line, so two otherwise-identical parts with different (or one-blank) LCSC values
stay on separate lines.

Pure and I/O-free apart from the two thin wrappers at the bottom
(:func:`jlcpcb_bom_from_schematic` reads a schematic; :func:`write_jlcpcb_csv`
writes to an open file), so the grouping logic is easy to test and shared by the
CLI and the GUI.
"""
from __future__ import annotations

import csv
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.pcb import SMD, THT, UNKNOWN
from kicadtools.resolve import PartRecord, iter_bom_part_records

# The exact header row JLCPCB auto-detects. "LCSC Part #" is the canonical
# spelling (what KiCad's Fabrication Toolkit emits ready-to-upload); JLCPCB also
# accepts "JLCPCB Part #" / "Value" etc., but this set needs no manual mapping.
JLC_FIELDS = ["Comment", "Designator", "Footprint", "LCSC Part #"]

# JLCPCB rejects a BOM line carrying more than 200 reference designators; groups
# larger than this are split across multiple lines (same part, same LCSC).
MAX_DESIGNATORS_PER_LINE = 200


def _valid_lcsc(value: object) -> str:
    """Return *value* as a clean LCSC code (``C`` + digits, upper-cased), or ``""``.

    JLCPCB's part-number cell must hold **only** an LCSC ``C\\d+`` code — a stray
    MPN there is a documented cause of rejection. Anything that isn't a C-code is
    blanked, so the part still exports (JLCPCB then asks you to pick it) rather
    than poisoning the upload."""
    v = str(value or "").strip().upper()
    return v if re.fullmatch(r"C\d+", v) else ""


@dataclass(frozen=True)
class JlcBomLine:
    """One line of a JLCPCB BOM: a group of identical parts."""

    comment: str
    footprint: str
    lcsc: str  # "" when the part carries no LCSC number
    references: Tuple[str, ...]  # natural-sorted refdes in this group
    mounting: str = UNKNOWN  # smd / tht / unknown (from the PCB, when available)

    @property
    def designator(self) -> str:
        return ",".join(self.references)

    @property
    def quantity(self) -> int:
        return len(self.references)


def build_jlcpcb_bom(
    records: Sequence[PartRecord],
    lcsc_override: Optional[Mapping[str, str]] = None,
    mounting: Optional[Mapping[str, str]] = None,
    smd_only: bool = False,
) -> List[JlcBomLine]:
    """Group *records* into JLCPCB BOM lines. Pure — no I/O.

    Parts are grouped by ``(Value, Footprint, LCSC)``; their refdes are collected,
    natural-sorted (``C2`` before ``C10``) and comma-joined into one Designator
    cell. Lines are ordered by the natural key of their first designator, so the
    output is deterministic regardless of schematic traversal order.

    *lcsc_override* maps a reference designator to an LCSC number to use in place
    of the one on the record (only when non-empty) — the hook the GUI uses to
    inject LCSC numbers resolved in the session but not yet saved to the file.

    *mounting* maps a refdes to ``"smd"``/``"tht"``/``"unknown"`` (from the board
    ``.kicad_pcb``); each line records its group's mounting. When *smd_only* is
    set, only parts known to be SMD are kept — a part with ``tht`` or
    ``unknown`` mounting (including every part when *mounting* is empty because no
    PCB was found) is dropped (see :func:`undetermined_references` to report the
    excluded-but-unclassified ones).
    """
    override = lcsc_override or {}
    mount = mounting or {}
    # Preserve first-seen order per group so the line's mounting is stable.
    groups: "OrderedDict[Tuple[str, str, str], List[str]]" = OrderedDict()

    for rec in records:
        if smd_only and mount.get(rec.reference, UNKNOWN) != SMD:
            continue
        value = str(rec.props.get("Value", "") or "")
        footprint = str(rec.props.get("Footprint", "") or "")
        lcsc = _valid_lcsc(override.get(rec.reference) or extract_part_numbers(rec.props).lcsc)
        groups.setdefault((value, footprint, lcsc), []).append(rec.reference)

    lines: List[JlcBomLine] = []
    for (value, footprint, lcsc), refs in groups.items():
        ordered = sorted(refs, key=natural_key)
        # Split oversized groups into <=200-designator lines (JLCPCB's cap).
        for start in range(0, len(ordered), MAX_DESIGNATORS_PER_LINE):
            chunk = ordered[start : start + MAX_DESIGNATORS_PER_LINE]
            lines.append(
                JlcBomLine(
                    comment=value,
                    footprint=footprint,
                    lcsc=lcsc,
                    references=tuple(chunk),
                    # A group shares one footprint, so its parts share one mounting.
                    mounting=mount.get(chunk[0], UNKNOWN),
                )
            )
    lines.sort(key=lambda ln: natural_key(ln.references[0]))
    return lines


def undetermined_references(
    records: Sequence[PartRecord], mounting: Optional[Mapping[str, str]]
) -> List[str]:
    """Refdes whose mounting is neither ``smd`` nor ``tht`` — the parts an
    SMD-only export drops but can't vouch for (unknown footprint, or no PCB at
    all). Confirmed through-hole parts are *not* listed: they're correctly
    excluded, not uncertain."""
    mount = mounting or {}
    refs = [r.reference for r in records if mount.get(r.reference, UNKNOWN) not in (SMD, THT)]
    return sorted(refs, key=natural_key)


def missing_lcsc(lines: Sequence[JlcBomLine]) -> List[JlcBomLine]:
    """The lines with no LCSC number (a blank ``LCSC Part #`` cell)."""
    return [ln for ln in lines if not ln.lcsc]


def write_jlcpcb_csv(lines: Sequence[JlcBomLine], fileobj) -> None:
    """Write *lines* as a JLCPCB BOM CSV to the open text *fileobj*.

    Multi-designator cells contain commas and are quoted automatically
    (``QUOTE_MINIMAL``). Open the file with ``newline=""`` (as the caller does)
    so no blank lines appear between rows on Windows.
    """
    writer = csv.DictWriter(fileobj, fieldnames=JLC_FIELDS)
    writer.writeheader()
    for ln in lines:
        writer.writerow(
            {
                "Comment": ln.comment,
                "Designator": ln.designator,
                "Footprint": ln.footprint,
                "LCSC Part #": ln.lcsc,
            }
        )


def jlcpcb_bom_from_schematic(path: Path) -> List[JlcBomLine]:
    """Read the schematic at *path* and build its JLCPCB BOM lines."""
    return build_jlcpcb_bom(iter_bom_part_records(Path(path)))
