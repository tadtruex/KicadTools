"""Build a JLCPCB CPL (component placement / pick-and-place) file from a KiCad
``.kicad_pcb``.

JLCPCB's assembly uploader wants five columns — ``Designator``, ``Mid X``,
``Mid Y`` (the component **centroid** in mm), ``Layer`` (``Top``/``Bottom``) and
``Rotation`` (degrees, CCW-positive). We compute them directly from the board's
footprint geometry (``kicadtools.pcb.parse_footprint_placements``), applying the
transforms confirmed in ``JLCPCB_SPEC.md`` §3:

* **Centroid** = center of the merged bounding box of the footprint's pads (the
  ``(at …)`` anchor is not always the body center), in board coordinates.
* **Mid X / Mid Y** = centroid minus the board's aux (drill/place) origin, with
  **Y inverted** (KiCad's file Y points down; JLCPCB wants Y up) — the same
  frame KiCad's own position export uses.
* **Rotation** = the footprint orientation, plus ``180 − angle`` for bottom-side
  parts (KiCad mirrors bottom footprints, JLCPCB doesn't), plus an optional
  per-family library offset and per-part manual override, all mod 360.

Pure and I/O-free apart from :func:`cpl_from_schematic`, so the math is easy to
test and shared by the CLI and the GUI.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from kicadtools.naturalsort import natural_key
from kicadtools.pcb import (
    Placement,
    parse_footprint_placements,
    read_aux_origin,
)
from kicadtools.rotations import RotationDb, default_rotation_db

CPL_FIELDS = ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]


@dataclass(frozen=True)
class CplLine:
    """One placement row: a part's centroid, side and rotation for JLCPCB."""

    designator: str
    mid_x: float  # mm, relative to the aux origin, Y-up
    mid_y: float
    layer: str  # "Top" | "Bottom"
    rotation: float  # degrees CCW, in [0, 360)


def _centroid(p: Placement) -> Tuple[float, float]:
    """The footprint's centroid in board coordinates (mm, Y-down): the center of
    the merged axis-aligned bounding box of its pads, each pad placed by the
    footprint's rotation. Falls back to the ``(at …)`` anchor when there are no
    pads (e.g. a mechanical/fiducial footprint)."""
    if not p.pads:
        return (p.x, p.y)
    theta = math.radians(p.angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    xs: List[float] = []
    ys: List[float] = []
    for pad in p.pads:
        # Pad center in board coordinates (rotate the local anchor by the
        # footprint orientation, then translate to the footprint origin).
        cx = p.x + pad.x * cos_t - pad.y * sin_t
        cy = p.y + pad.x * sin_t + pad.y * cos_t
        # AABB half-extents of the pad rectangle at its total rotation
        # (footprint + the pad's own angle).
        pth = math.radians(p.angle + pad.angle)
        hx = abs(pad.w / 2 * math.cos(pth)) + abs(pad.h / 2 * math.sin(pth))
        hy = abs(pad.w / 2 * math.sin(pth)) + abs(pad.h / 2 * math.cos(pth))
        xs += [cx - hx, cx + hx]
        ys += [cy - hy, cy + hy]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def build_cpl(
    placements: Iterable[Placement],
    aux_origin: Tuple[float, float] = (0.0, 0.0),
    include: Optional[Set[str]] = None,
    rotation_db: Optional[RotationDb] = None,
    manual_rotation: Optional[Mapping[str, float]] = None,
) -> List[CplLine]:
    """Turn *placements* into JLCPCB CPL lines. Pure — no I/O.

    *aux_origin* is subtracted from every centroid (use the board's drill/place
    origin, the same one the Gerbers are exported against). *include*, when
    given, keeps only those reference designators — pass the BOM's ref set so the
    CPL and BOM designators match exactly (a JLCPCB requirement). *rotation_db*
    supplies per-family offsets (default: the built-in low-risk table);
    *manual_rotation* maps a refdes to an extra offset in degrees.
    """
    db = rotation_db if rotation_db is not None else default_rotation_db()
    manual = manual_rotation or {}
    ax, ay = aux_origin
    lines: List[CplLine] = []
    for p in placements:
        if include is not None and p.reference not in include:
            continue
        cx, cy = _centroid(p)
        mid_x = cx - ax
        mid_y = -(cy - ay)  # KiCad Y-down -> JLCPCB Y-up

        rot = p.angle
        if p.side == "bottom":
            rot = 180.0 - rot  # KiCad mirrors bottom parts; JLCPCB doesn't
            layer = "Bottom"
        else:
            layer = "Top"
        rot = (rot + db.offset(p.footprint) + float(manual.get(p.reference, 0.0))) % 360.0

        lines.append(CplLine(p.reference, mid_x, mid_y, layer, rot))

    lines.sort(key=lambda ln: natural_key(ln.designator))
    return lines


def _fmt_coord(value: float) -> str:
    """Coordinate as a bare mm number (no unit suffix), 4 decimals like KiCad."""
    return f"{value:.4f}"


def _fmt_rotation(value: float) -> str:
    """Rotation as a compact number: an integer when whole, else 2 decimals."""
    value = round(value % 360.0, 2)
    return str(int(value)) if value == int(value) else f"{value:g}"


def write_cpl_csv(lines: Sequence[CplLine], fileobj) -> None:
    """Write *lines* as a JLCPCB CPL CSV to the open text *fileobj*.

    Open the file with ``newline=""`` and ``encoding="utf-8"`` (no BOM), as the
    caller does."""
    writer = csv.DictWriter(fileobj, fieldnames=CPL_FIELDS)
    writer.writeheader()
    for ln in lines:
        writer.writerow(
            {
                "Designator": ln.designator,
                "Mid X": _fmt_coord(ln.mid_x),
                "Mid Y": _fmt_coord(ln.mid_y),
                "Layer": ln.layer,
                "Rotation": _fmt_rotation(ln.rotation),
            }
        )


def cpl_from_schematic(
    pcb_path: Path,
    include: Optional[Set[str]] = None,
    manual_rotation: Optional[Mapping[str, float]] = None,
) -> List[CplLine]:
    """Read the board at *pcb_path* and build its JLCPCB CPL lines."""
    text = Path(pcb_path).read_text(encoding="utf-8")
    placements = parse_footprint_placements(text).values()
    return build_cpl(
        placements,
        aux_origin=read_aux_origin(text),
        include=include,
        manual_rotation=manual_rotation,
    )
