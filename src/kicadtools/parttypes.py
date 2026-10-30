"""
The part types the Parts tab assigns, and what differs between them.

A :class:`PartType` holds everything the requirement, search, database, write
plan and Cockpit code need to know about one type: how its value and rating
are parsed and shown, the schematic property that states the rating, the
JLCPCB category and attribute names, the DigiKey parameter names, the
footprints written for each chip size, and its settings keys. The code in
:mod:`kicadtools.requirement`, :mod:`kicadtools.partsearch`,
:mod:`kicadtools.partsdb` and :mod:`kicadtools.assign` reads these fields and
holds no per-type branches beyond :attr:`PartType.has_dielectric`.

* **Capacitor** — value in farads, rating ``Voltage:Min`` in volts, a
  dielectric (``Class:Preferred``), tolerance. The voltage floor is a design
  decision: with no stated or remembered value it is unset and the dialog
  asks for one.
* **Resistor** — value in ohms, rating ``Power:Min`` in watts, tolerance, no
  dielectric. With no stated or remembered power floor the search applies no
  limit, and the chosen part's own rating is written.

Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple, Type

from kicadtools.evidence import DescriptionEvidence
from kicadtools.intent import CapacitorIntent, ElectricalIntent, ResistorIntent
from kicadtools.units import (
    fmt_farads,
    fmt_ohms,
    fmt_volts,
    fmt_watts,
    parse_cap_description,
    parse_cap_value,
    parse_capacitance,
    parse_percent,
    parse_res_description,
    parse_res_value,
    parse_resistance,
    parse_volts,
    parse_watts,
)


def _cap_tolerance(raw: Optional[str], nominal: Optional[float]) -> Optional[float]:
    """Percent from a capacitor's supplier tolerance. Small capacitors are
    specified with an absolute tolerance (``"±0.25pF"``); that is converted to
    percent of *nominal*, so ±0.25 pF on 10 pF is 2.5 %. ``None`` when *raw*
    does not parse."""
    tol = parse_percent(raw)
    if tol is not None or raw is None or "%" in raw:
        return tol
    absolute = parse_capacitance(str(raw).replace("±", "").replace("+/-", ""))
    if absolute is None or not nominal or nominal <= 0:
        return None
    return round(absolute / nominal * 100.0, 6)


def _percent_tolerance(raw: Optional[str], _nominal: Optional[float]) -> Optional[float]:
    """Percent from a supplier tolerance (``"±1%"``)."""
    return parse_percent(raw)


@dataclass(frozen=True)
class SupplierNames:
    """A supplier's parameter names for the value, rating, dielectric and
    tolerance of one part type. ``dielectric`` is ``""`` for a type with
    none."""
    nominal: str
    rating: str
    dielectric: str
    tolerance: str


@dataclass(frozen=True)
class PartType:
    """What one part type needs from the shared assign code."""
    name: str                              # Intent:Type, part.type in the database
    prefix: str                            # settings key prefix: "cap", "res"
    intent_class: Type[ElectricalIntent]
    nominal_title: str                     # "Capacitance", "Resistance"
    parse_nominal: Callable[[object], Optional[float]]
    fmt_nominal: Callable[[float], str]
    # Keyword for the filtered JLCPCB search: "4.7uF", "10kΩ".
    search_text: Callable[[float], str]
    # A Value as written -> (nominal, package from a size suffix).
    parse_value: Callable[[object], Tuple[Optional[float], Optional[str]]]
    parse_description: Callable[[object], DescriptionEvidence]
    library_prefixes: Tuple[str, ...]      # library-style Value prefixes
    rating_property: str                   # "Voltage:Min", "Power:Min"
    rating_setting: str                    # settings key suffix: "voltage_min"
    rating_attr: str                       # the intent attribute that holds it
    rating_title: str                      # "Voltage", "Power"
    parse_rating: Callable[[object], Optional[float]]
    fmt_rating: Callable[[float], str]
    # True: a missing rating floor is unset and must be entered before a
    # search. False: it is "any", no limit.
    rating_required: bool
    has_dielectric: bool
    parse_tolerance: Callable[[Optional[str], Optional[float]], Optional[float]]
    jlc_category: Tuple[str, str]          # (firstSortName, secondSortName)
    jlc: SupplierNames
    digikey: SupplierNames
    footprints: Dict[str, str] = field(default_factory=dict)

    def key(self, suffix: str) -> str:
        """The settings key ``"<prefix>.<suffix>"`` (``"res.package"``)."""
        return f"{self.prefix}.{suffix}"

    def rating_text(self, intent: object) -> str:
        """The rating floor stated on *intent* (``Voltage:Min`` /
        ``Power:Min``), stripped; ``""`` when none."""
        return str(getattr(intent, self.rating_attr, None) or "").strip()

    def is_library_value(self, text: str) -> bool:
        """True when *text* is written in library style (``C_4p7U``,
        ``R_49p9K``)."""
        return text.strip().upper().startswith(self.library_prefixes)


def _chip_footprints(library: str, letter: str) -> Dict[str, str]:
    """KiCad standard-library footprints for the common chip sizes."""
    metric = {"0402": "1005", "0603": "1608", "0805": "2012",
              "1206": "3216", "1210": "3225"}
    return {size: f"{library}:{letter}_{size}_{m}Metric" for size, m in metric.items()}


CAPACITOR = PartType(
    name="capacitor",
    prefix="cap",
    intent_class=CapacitorIntent,
    nominal_title="Capacitance",
    parse_nominal=parse_capacitance,
    fmt_nominal=fmt_farads,
    search_text=fmt_farads,
    parse_value=parse_cap_value,
    parse_description=parse_cap_description,
    library_prefixes=("C_", "C-"),
    rating_property="Voltage:Min",
    rating_setting="voltage_min",
    rating_attr="voltage_min",
    rating_title="Voltage",
    parse_rating=parse_volts,
    fmt_rating=fmt_volts,
    rating_required=True,
    has_dielectric=True,
    parse_tolerance=_cap_tolerance,
    jlc_category=("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT"),
    jlc=SupplierNames("Capacitance", "Voltage Rating", "Temperature Coefficient",
                      "Tolerance"),
    digikey=SupplierNames("Capacitance", "Voltage - Rated", "Temperature Coefficient",
                          "Tolerance"),
    footprints=_chip_footprints("Capacitor_SMD", "C"),
)

RESISTOR = PartType(
    name="resistor",
    prefix="res",
    intent_class=ResistorIntent,
    nominal_title="Resistance",
    parse_nominal=parse_resistance,
    fmt_nominal=fmt_ohms,
    search_text=lambda ohms: fmt_ohms(ohms) + "Ω",
    parse_value=parse_res_value,
    parse_description=parse_res_description,
    library_prefixes=("R_", "R-"),
    rating_property="Power:Min",
    rating_setting="power_min",
    rating_attr="power_min",
    rating_title="Power",
    parse_rating=parse_watts,
    fmt_rating=fmt_watts,
    rating_required=False,
    has_dielectric=False,
    parse_tolerance=_percent_tolerance,
    jlc_category=("Resistors", "Chip Resistor - Surface Mount"),
    jlc=SupplierNames("Resistance", "Power(Watts)", "", "Tolerance"),
    digikey=SupplierNames("Resistance", "Power (Watts)", "", "Tolerance"),
    footprints=_chip_footprints("Resistor_SMD", "R"),
)

PART_TYPES: Tuple[PartType, ...] = (CAPACITOR, RESISTOR)

# DigiKey's package parameter, the same for every chip part.
DK_PACKAGE = "Package / Case"


def part_type(name: str) -> PartType:
    """The :class:`PartType` named *name* (``"capacitor"``, ``"resistor"``)."""
    for t in PART_TYPES:
        if t.name == name:
            return t
    raise KeyError(f"unknown part type {name!r}")


def type_of_intent(intent: object) -> Optional[PartType]:
    """The :class:`PartType` whose intent class *intent* is, else ``None``."""
    for t in PART_TYPES:
        if isinstance(intent, t.intent_class):
            return t
    return None
