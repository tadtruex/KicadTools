"""Read the authoritative SMD/THT mounting type from a KiCad ``.kicad_pcb``.

The schematic doesn't record whether a part is surface-mount or through-hole —
that belongs to the footprint. KiCad stamps every placed footprint in the board
file with ``(attr smd)`` or ``(attr through_hole)`` (optionally followed by flags
like ``exclude_from_bom`` / ``dnp``). This module reads that per reference
designator, so the BOM can be filtered to SMD-only from the real board data
rather than guessing from the footprint name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from sexpdata import Symbol, loads

# Mounting verdicts.
SMD = "smd"
THT = "tht"
UNKNOWN = "unknown"  # board_only, an attr we don't recognize, or no attr at all


def _footprint_reference(node) -> Optional[str]:
    for item in node:
        if (
            isinstance(item, list)
            and len(item) >= 3
            and item[0] == Symbol("property")
            and item[1] == "Reference"
        ):
            return item[2] if isinstance(item[2], str) else None
    return None


def _footprint_mounting(node) -> str:
    """Classify one ``(footprint …)`` node from its ``(attr …)`` tokens."""
    for item in node:
        if isinstance(item, list) and item and item[0] == Symbol("attr"):
            tokens = {str(t) for t in item[1:]}
            if "smd" in tokens:
                return SMD
            if "through_hole" in tokens:
                return THT
            return UNKNOWN  # e.g. board_only
    return UNKNOWN  # no attr node


def parse_footprint_mounting(text: str) -> Dict[str, str]:
    """Map each placed footprint's reference designator to ``smd``/``tht``/
    ``unknown`` from ``.kicad_pcb`` *text*. Pure — no I/O."""
    tree = loads(text)
    mounting: Dict[str, str] = {}
    if not isinstance(tree, list):
        return mounting
    for node in tree:
        if isinstance(node, list) and node and node[0] == Symbol("footprint"):
            ref = _footprint_reference(node)
            if ref:
                mounting[ref] = _footprint_mounting(node)
    return mounting


def sibling_pcb(schematic: Path) -> Optional[Path]:
    """The ``<name>.kicad_pcb`` next to *schematic*, if it exists."""
    pcb = Path(schematic).with_suffix(".kicad_pcb")
    return pcb if pcb.exists() else None


def read_mounting_for_schematic(
    schematic: Path,
) -> Tuple[Dict[str, str], Optional[Path]]:
    """Return ``({refdes: mounting}, pcb_path)`` for *schematic*'s sibling PCB,
    or ``({}, None)`` when there is no ``.kicad_pcb`` beside it."""
    pcb = sibling_pcb(schematic)
    if pcb is None:
        return {}, None
    return parse_footprint_mounting(pcb.read_text(encoding="utf-8")), pcb
