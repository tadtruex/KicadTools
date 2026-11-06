"""Parts-tab rows for the BOM parts no assignment path takes.

Capacitors, resistors, inductors, plain LEDs, ICs (and multicolour or smart
LEDs) and through-hole parts each have a grouping of their own. Every other
BOM part is listed too, so a gap on the board is visible on the Parts tab:
:func:`group_others` collects the records none of those groupings took into
:class:`OtherGroup` s keyed on intent type, Value, Footprint and part
numbers. Each row's :class:`OtherRequirement` gives the reason it has no
search (:attr:`OtherRequirement.reason`):

- :data:`NO_PART`: nothing to buy. A mounting hole, fiducial, bare pad or
  solder jumper, read from the footprint
  (:func:`kicadtools.thtfootprint.parse_signature`) or, with no footprint,
  from the symbol's footprint filters (``ki_fp_filters`` =
  ``SolderJumper*Open*``);
- :data:`NO_FOOTPRINT`: a type with an assignment path whose path needs a
  footprint (a connector, searched as a through-hole part);
- :data:`FOOTPRINT_NOT_FOUND`: a connector whose footprint is in neither the
  board nor the libraries, so its mounting is unknown;
- :data:`NOT_SUPPORTED`: a type with no assignment path yet (diodes,
  transistors, switches, potentiometers, jumpers, test points, types the
  classifier does not know), or a connector on a surface-mount footprint.

Pure: no schematic I/O and no network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from kicadtools.evidence import ExistingPart
from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.partsdb import norm_mpn
from kicadtools.pcb import UNKNOWN
from kicadtools.requirement import Member, _prop
from kicadtools.thtfootprint import NO_PART as _NO_PART_FAMILY
from kicadtools.thtfootprint import parse_signature

OTHER_KIND = "other"

NO_PART = "no part"
NO_FOOTPRINT = "no footprint"
NOT_SUPPORTED = "type not supported yet"
FOOTPRINT_NOT_FOUND = "footprint not found"

# Footprint filters (``ki_fp_filters``) that name footprints with nothing to
# buy. A symbol whose every filter is one of these needs no part.
_NO_PART_FILTERS = {"solderjumper": "solder jumper", "mountinghole": "mounting hole",
                    "fiducial": "fiducial", "nettie": "net tie"}

# Intent types whose path searches from the footprint: with none, the row
# says "no footprint" rather than "not supported".
_FOOTPRINT_TYPES = frozenset({"connector"})


def _type_name(intent: Any) -> str:
    name = str(getattr(intent, "intent_type", "") or "").strip()
    return "unknown" if not name or name.lower() == "unknown" else name.lower()


def _no_part_what(props: Mapping[str, object]) -> str:
    """What the part is when there is nothing to buy (``"mounting hole"``,
    ``"solder jumper"``); ``""`` when there is a part."""
    footprint = _prop(props, "Footprint")
    if footprint:
        sig = parse_signature(footprint)
        return (sig.what or "no part") if sig.family == _NO_PART_FAMILY else ""
    filters = _prop(props, "ki_fp_filters").lower().split()
    labels = [next((label for prefix, label in _NO_PART_FILTERS.items()
                    if f.startswith(prefix)), None) for f in filters]
    if labels and all(labels):
        return labels[0]
    return ""


@dataclass(frozen=True)
class OtherRequirement:
    """Why a row has no search, and what the symbol states."""

    type_name: str                 # the intent type: "diode", "switch", "unknown"
    reason: str                    # NO_PART, NO_FOOTPRINT or NOT_SUPPORTED
    value_text: str = ""
    footprint: str = ""
    what: str = ""                 # for NO_PART: "mounting hole", "solder jumper"
    mounting: str = ""             # "smd", "tht", "unknown"; "" when not read
    description: str = ""
    kind: str = OTHER_KIND
    props: Mapping[str, object] = field(default_factory=dict)

    @property
    def no_part(self) -> bool:
        return self.reason == NO_PART

    @property
    def searchable(self) -> bool:
        return False

    def problem(self) -> str:
        """The reason as a line for a tooltip or the status bar."""
        if self.reason == NO_PART:
            return f"nothing to buy ({self.what})" if self.what else "nothing to buy"
        if self.reason == NO_FOOTPRINT:
            return f"{self.type_name}: no footprint; its search reads the footprint"
        if self.reason == FOOTPRINT_NOT_FOUND:
            return (f"{self.type_name}: footprint {self.footprint} is not on the board "
                    "or in the libraries, so its mounting is unknown")
        if self.footprint and self.type_name in _FOOTPRINT_TYPES:
            return f"{self.type_name} on a surface-mount footprint: not supported yet"
        return f"{self.type_name}: no assignment path yet"


def other_requirement(intent: Any, props: Mapping[str, object],
                      mounting: str = "") -> OtherRequirement:
    """The requirement of one record no other grouping took. *mounting* is
    the footprint's mounting kind (:mod:`kicadtools.mounting`: ``"smd"``,
    ``"tht"``, ``"unknown"``), ``""`` when not read."""
    type_name = _type_name(intent)
    footprint = _prop(props, "Footprint")
    what = _no_part_what(props)
    if what:
        reason = NO_PART
    elif not footprint and type_name in _FOOTPRINT_TYPES:
        reason = NO_FOOTPRINT
    elif footprint and type_name in _FOOTPRINT_TYPES and mounting == UNKNOWN:
        reason = FOOTPRINT_NOT_FOUND
    else:
        reason = NOT_SUPPORTED
    return OtherRequirement(
        type_name=type_name, reason=reason, value_text=_prop(props, "Value"),
        footprint=footprint, what=what, mounting=mounting,
        description=_prop(props, "Description"), props=dict(props))


@dataclass
class OtherGroup:
    """BOM parts with no assignment path that share an intent type, Value,
    Footprint and part numbers."""

    requirement: OtherRequirement
    members: List[Member]

    @property
    def kind(self) -> str:
        return OTHER_KIND

    @property
    def existing(self) -> ExistingPart:
        return self.members[0].existing

    @property
    def references(self) -> List[str]:
        return sorted({m.reference for m in self.members}, key=natural_key)

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def assigned(self) -> bool:
        """Every member has an MPN, Manufacturer and DigiKey PN. Nothing on
        the Cockpit writes them for this row; the test lets a part named on
        the schematic read as assigned."""
        for m in self.members:
            pn = extract_part_numbers(m.props)
            if not (pn.mpn and pn.manufacturer and pn.digikey_pn):
                return False
        return True


def _member_key(m: Any) -> Tuple[str, Optional[str]]:
    return (m.reference, m.symbol_uuid)


def claimed(groups: Iterable[Any]) -> Set[Tuple[str, Optional[str]]]:
    """``(reference, symbol_uuid)`` of every member of *groups*."""
    return {_member_key(m) for g in groups for m in g.members}


def group_others(records: Sequence[Any], taken: Set[Tuple[str, Optional[str]]],
                 board: Any = None) -> List[OtherGroup]:
    """Group the *records* whose ``(reference, symbol_uuid)`` is not in
    *taken* (see :func:`claimed`). With *board*
    (:class:`kicadtools.mounting.BoardMounting`), a footprint's mounting is
    read, so a connector whose footprint is not found says so. Ordered by
    first reference."""
    by_key: Dict[Tuple[Any, ...], OtherGroup] = {}
    for rec in records:
        if (rec.reference, rec.symbol_uuid) in taken:
            continue
        pn = extract_part_numbers(rec.props)
        key = (_type_name(rec.intent), _prop(rec.props, "Value").upper(),
               _prop(rec.props, "Footprint"), norm_mpn(pn.mpn), norm_mpn(pn.digikey_pn))
        member = Member(reference=rec.reference, source_file=rec.source_file,
                        symbol_uuid=rec.symbol_uuid, props=rec.props, intent=rec.intent)
        group = by_key.get(key)
        if group is None:
            footprint = _prop(rec.props, "Footprint")
            mounting = (board.mounting(rec.reference, footprint).kind
                        if board is not None and footprint else "")
            by_key[key] = OtherGroup(
                other_requirement(rec.intent, rec.props, mounting), [member])
        else:
            group.members.append(member)
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))
