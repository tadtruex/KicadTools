"""Read the authoritative SMD/THT mounting type from a KiCad ``.kicad_pcb``.

The schematic doesn't record whether a part is surface-mount or through-hole —
that belongs to the footprint. KiCad stamps every placed footprint in the board
file with ``(attr smd)`` or ``(attr through_hole)`` (optionally followed by flags
like ``exclude_from_bom`` / ``dnp``). This module reads that per reference
designator, so the BOM can be filtered to SMD-only from the real board data
rather than guessing from the footprint name.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


# --- footprint placement (for the JLCPCB CPL / pick-and-place export) -----
#
# Coordinates in a ``.kicad_pcb`` are already in millimetres (KiCad 6+), with the
# Y axis pointing DOWN. A footprint carries its origin + orientation in
# ``(at x y angle)`` and its side in the direct ``(layer "F.Cu"|"B.Cu")`` child;
# each ``(pad …)`` gives a ``(at …)`` local to that origin and a ``(size w h)``.
# The CPL builder (``kicadtools.cpl``) turns these into JLCPCB centroids.


@dataclass(frozen=True)
class Pad:
    """One pad, in the footprint's local frame (mm), before the footprint's own
    rotation/placement is applied."""

    x: float
    y: float
    w: float
    h: float
    angle: float  # pad's own rotation, degrees
    # The pad number as text ("1", "A3", "EP"); "" for an unnumbered pad
    # (paste aperture, mechanical hole). Last and defaulted so positional
    # construction ``Pad(x, y, w, h, angle)`` keeps working.
    number: str = ""
    # The pad type token: "smd", "thru_hole", "np_thru_hole" or "connect";
    # "" when the pad node has none.
    kind: str = ""


@dataclass(frozen=True)
class Placement:
    """A placed footprint's raw geometry, straight from the ``.kicad_pcb`` (mm,
    Y-down, angle CCW-positive) — no JLCPCB transforms applied yet."""

    reference: str
    footprint: str  # library id, e.g. "Resistor_SMD:R_0603_1608Metric"
    x: float
    y: float
    angle: float
    side: str  # "top" | "bottom"
    pads: Tuple[Pad, ...]
    # The footprint's (attr …) verdict: SMD, THT or UNKNOWN.
    attr: str = UNKNOWN


def _direct_child(node, head: str):
    """The first direct child list of *node* headed by ``Symbol(head)``, or None.
    Direct-only, so a nested ``(layer …)`` inside a property/pad is not matched."""
    for item in node:
        if isinstance(item, list) and item and item[0] == Symbol(head):
            return item
    return None


def _numbers(node) -> List[float]:
    """The numeric tail of an s-expr like ``(at 1.0 2.0 90)`` -> ``[1.0, 2.0, 90.0]``."""
    if not node:
        return []
    return [float(v) for v in node[1:] if isinstance(v, (int, float))]


def _footprint_side(node) -> str:
    layer = _direct_child(node, "layer")
    name = str(layer[1]) if layer and len(layer) >= 2 else ""
    return "bottom" if name.startswith("B.") else "top"


def _pad_number(pad) -> str:
    """The number of a ``(pad <number> <type> <shape> …)`` node as text. KiCad 6+
    quotes it (``"1"``); older files leave it bare, which the s-expression reader
    returns as an int or a Symbol."""
    if len(pad) < 2:
        return ""
    value = pad[1]
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, Symbol)):
        return str(value)
    return ""


def _pad_kind(pad) -> str:
    """The type token of a ``(pad <number> <type> <shape> …)`` node."""
    if len(pad) < 3 or not isinstance(pad[2], Symbol):
        return ""
    return str(pad[2])


def _parse_pads(node) -> Tuple[Pad, ...]:
    pads: List[Pad] = []
    for item in node:
        if not (isinstance(item, list) and item and item[0] == Symbol("pad")):
            continue
        at = _numbers(_direct_child(item, "at"))
        size = _numbers(_direct_child(item, "size"))
        if len(at) < 2 or len(size) < 2:
            continue
        pads.append(
            Pad(
                x=at[0],
                y=at[1],
                w=size[0],
                h=size[1],
                angle=at[2] if len(at) > 2 else 0.0,
                number=_pad_number(item),
                kind=_pad_kind(item),
            )
        )
    return tuple(pads)


def parse_footprint_placements(text: str) -> Dict[str, Placement]:
    """Map each placed footprint's reference to its :class:`Placement` from
    ``.kicad_pcb`` *text*. Pure — no I/O. Footprints without a resolvable
    reference or ``(at …)`` are skipped."""
    tree = loads(text)
    out: Dict[str, Placement] = {}
    if not isinstance(tree, list):
        return out
    for node in tree:
        if not (isinstance(node, list) and node and node[0] == Symbol("footprint")):
            continue
        ref = _footprint_reference(node)
        at = _numbers(_direct_child(node, "at"))
        if not ref or len(at) < 2:
            continue
        lib = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        out[ref] = Placement(
            reference=ref,
            footprint=lib,
            x=at[0],
            y=at[1],
            angle=at[2] if len(at) > 2 else 0.0,
            side=_footprint_side(node),
            pads=_parse_pads(node),
            attr=_footprint_mounting(node),
        )
    return out


def read_aux_origin(text: str) -> Tuple[float, float]:
    """The board's drill/place (auxiliary) origin in mm, or ``(0.0, 0.0)`` when
    the board doesn't set one. JLCPCB only requires the CPL and the Gerbers to
    share this origin; KiCad exports both relative to it."""
    tree = loads(text)
    if not isinstance(tree, list):
        return (0.0, 0.0)
    setup = _direct_child(tree, "setup")
    if setup is None:
        return (0.0, 0.0)
    origin = _numbers(_direct_child(setup, "aux_axis_origin"))
    return (origin[0], origin[1]) if len(origin) >= 2 else (0.0, 0.0)


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
