"""SMD / through-hole mounting per part, from its footprint (``THT_PLAN.md``
step 1).

Sources, in the order ``footprints.FootprintPads`` uses for pad counts:

1. the footprint placed on the board under the part's reference. It is used
   even when its library id differs from the schematic's Footprint field (the
   result carries a note): the board is what is built, and boards updated
   from KiCad 5 libraries often hold the renamed footprint
   (``LED_SMD:LED_0603_1608Metric``) while the schematic still names the old
   one (``LEDs:LED_0603``). This differs from the pad-count rule in
   ``footprints``, which skips such a board copy;
2. the library ``.kicad_mod`` for the schematic's Footprint field, when the
   part is not placed on the board.

Each source gives the footprint's ``(attr …)`` and its pads. The decision:

- Plated pads all one type (``thru_hole`` only, or ``smd`` only): that type.
  When the ``attr`` names the other type the pads win and the result carries a
  note (Breakout U6, Rohm ``BD33FC0WEFJ-E2``: ``attr through_hole``, nine SMD
  pads).
- Both types (USB connectors: SMD signal pads, plated shield tabs): the
  ``attr``. With no ``attr`` the result is ``unknown`` with a note.
- No ``smd`` or ``thru_hole`` pads (mounting hole, logo): the ``attr``.

KiCad's standard through-hole footprints carry no ``attr`` in the library
file, only in the board copy, so the pads are what classify them before the
board is laid out. ``np_thru_hole`` pads (unplated holes, alignment pegs) and
``connect`` pads are not counted.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from kicadtools.footprints import FootprintPads
from kicadtools.pcb import SMD, THT, UNKNOWN, Pad, sibling_pcb

BOARD = "board"
LIBRARY = "library"

_NAMES = {SMD: "SMD", THT: "through-hole"}


@dataclass(frozen=True)
class Mounting:
    """The mounting of one part and how it was decided."""

    kind: str  # SMD, THT or UNKNOWN
    source: str  # BOARD, LIBRARY, or "" when no footprint was found
    attr: str  # the footprint's (attr …) verdict: SMD, THT or UNKNOWN
    thru_hole_pads: int = 0
    smd_pads: int = 0
    note: str = ""


NOT_FOUND = Mounting(kind=UNKNOWN, source="", attr=UNKNOWN, note="footprint not found")


def decide(attr: str, pads: Iterable[Pad], source: str) -> Mounting:
    """The :class:`Mounting` for a footprint with *attr* and *pads*, read from
    *source*. Pure."""
    pads = tuple(pads)
    tht = sum(1 for p in pads if p.kind == "thru_hole")
    smd = sum(1 for p in pads if p.kind == "smd")

    def result(kind: str, note: str = "") -> Mounting:
        return Mounting(kind, source, attr, tht, smd, note)

    if tht and not smd:
        by_pads = THT
    elif smd and not tht:
        by_pads = SMD
    elif tht and smd:
        if attr in (SMD, THT):
            return result(attr)
        return result(UNKNOWN, f"no attr, {smd} SMD and {tht} through-hole pads")
    else:
        return result(attr)

    if attr in (SMD, THT) and attr != by_pads:
        return result(
            by_pads,
            f"footprint attr is {_NAMES[attr]}, pads are all {_NAMES[by_pads]}",
        )
    return result(by_pads)


class BoardMounting:
    """Mounting for the parts of one board. *pcb_path* is the board file (may be
    missing or ``None``); *project_dir* and *standard_dir* are as for
    :class:`~kicadtools.footprints.FootprintPads`, whose board and library
    lookups this uses."""

    def __init__(
        self,
        pcb_path: Optional[Path] = None,
        *,
        project_dir: Optional[Path] = None,
        standard_dir: Optional[Path] = None,
        footprints: Optional[FootprintPads] = None,
    ):
        self.footprints = footprints or FootprintPads(
            pcb_path, project_dir=project_dir, standard_dir=standard_dir
        )

    @classmethod
    def for_schematic(cls, schematic: Path, **kwargs) -> "BoardMounting":
        """For the board beside *schematic* (``<name>.kicad_pcb``, which need not
        exist), with the schematic's folder as the project folder."""
        schematic = Path(schematic)
        return cls(sibling_pcb(schematic), project_dir=schematic.parent, **kwargs)

    def mounting(self, reference: str, lib_id: Optional[str]) -> Mounting:
        """Mounting of *reference*, whose schematic Footprint field is *lib_id*."""
        placed = self.footprints.placed(reference)
        if placed is not None:
            result = decide(placed.attr, placed.pads, BOARD)
            if lib_id and placed.footprint != lib_id:
                other = f"board footprint {placed.footprint} differs from schematic {lib_id}"
                note = f"{result.note}; {other}" if result.note else other
                result = replace(result, note=note)
            return result
        if lib_id:
            footprint = self.footprints.library_footprint(lib_id)
            if footprint is not None:
                return decide(footprint.attr, footprint.pads, LIBRARY)
        return NOT_FOUND

    def mounting_for(self, record) -> Mounting:
        """Mounting of a part record (anything with ``reference`` and a
        ``props`` dict holding the schematic's ``Footprint``)."""
        lib_id = str(record.props.get("Footprint") or "") or None
        return self.mounting(record.reference, lib_id)


def mounting_for_records(records, schematic: Path) -> Tuple[Dict[str, Mounting], Optional[Path]]:
    """``({refdes: Mounting}, pcb_path)`` for *records* of *schematic*;
    *pcb_path* is ``None`` when there is no ``.kicad_pcb`` beside it (the
    library footprints are still read)."""
    board = BoardMounting.for_schematic(schematic)
    return {r.reference: board.mounting_for(r) for r in records}, board.footprints.pcb_path


def mounting_kinds(records, schematic: Path) -> Tuple[Dict[str, str], Optional[Path]]:
    """``({refdes: "smd" | "tht" | "unknown"}, pcb_path)``: the shape the BOM and
    CPL exports take (``jlcpcb.build_jlcpcb_bom``, ``digikey_bom``)."""
    mountings, pcb = mounting_for_records(records, schematic)
    return {ref: m.kind for ref, m in mountings.items()}, pcb
