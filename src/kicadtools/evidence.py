"""
What a capacitor's existing part numbers and description say about it.

Shared by :mod:`kicadtools.requirement` (which merges evidence into a
requirement, pure) and :mod:`kicadtools.capsearch` (which builds
:class:`PartEvidence` from DigiKey / JLCPCB lookups). No I/O here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ExistingPart:
    """The part numbers a symbol already holds, as read from its properties
    (any spelling, via :func:`kicadtools.partnum.extract_part_numbers`).
    Fields may be swapped on real boards (a DigiKey PN in the MPN field), so
    they are treated as lookup keys, not as typed facts."""
    mpn: Optional[str] = None
    digikey_pn: Optional[str] = None
    lcsc: Optional[str] = None
    manufacturer: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not (self.mpn or self.digikey_pn or self.lcsc)


@dataclass(frozen=True)
class PartEvidence:
    """Parameters of the symbol's existing part, from a supplier lookup.
    ``label`` names where they came from, for display
    (``"current part 445-9118-1-ND (DigiKey)"``)."""
    label: str
    capacitance_f: Optional[float] = None
    voltage_v: Optional[float] = None
    dielectrics: Tuple[str, ...] = ()
    tolerance_pct: Optional[float] = None
    package: Optional[str] = None


@dataclass(frozen=True)
class DescriptionEvidence:
    """What a free-text Description states (``"10uF, 25V, X5R, 805 package"``).
    Every field is ``None`` / empty when the text does not state it."""
    capacitance_f: Optional[float] = None
    voltage_v: Optional[float] = None
    dielectrics: Tuple[str, ...] = ()
    package: Optional[str] = None
