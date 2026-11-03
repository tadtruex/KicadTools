"""
The part types the Parts tab assigns, and what differs between them.

A :class:`PartType` holds everything the requirement, search, database, write
plan and Cockpit code need to know about one type: how its value and rating
are parsed and shown, the schematic property that states the rating, the
JLCPCB category and attribute names, the DigiKey parameter names, the
footprints written for each chip size, and its settings keys. The code in
:mod:`kicadtools.requirement`, :mod:`kicadtools.partsearch`,
:mod:`kicadtools.partsdb` and :mod:`kicadtools.assign` reads these fields and
holds no per-type branches beyond the flags on :class:`PartType`.

* **Capacitor** — value in farads, rating ``Voltage:Min`` in volts, a
  dielectric (``Class:Preferred``), tolerance. The voltage floor is a design
  decision: with no stated or remembered value it is unset and the dialog
  asks for one.
* **Resistor** — value in ohms, rating ``Power:Min`` in watts, tolerance, no
  dielectric. With no stated or remembered power floor the search applies no
  limit, and the chosen part's own rating is written.
* **Inductor** — value in henries, rating ``Current:Min`` in amps (compared
  with the saturation current, or the rated current where a supplier lists
  no saturation current), shielding (``Shielding:Preferred``) in the place
  of the dielectric, tolerance. The current floor works as a resistor's power
  floor: "any" when none is stated or remembered. The package is a chip size
  (``0603``) or, for a power inductor, a body size in millimetres
  (``4x4mm``); a body size is never read from the footprint name, and no
  footprint is written for one.

The class field (``Class:Preferred`` for a capacitor's dielectric,
``Shielding:Preferred`` for an inductor's shielding) is held in the
requirement's ``dielectrics`` field for either type.

Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Type, Union

from kicadtools.evidence import DescriptionEvidence
from kicadtools.intent import (
    CapacitorIntent,
    ElectricalIntent,
    InductorIntent,
    ResistorIntent,
)
from kicadtools.units import (
    body_dims,
    body_size,
    chip_size,
    dielectric_tokens,
    dielectric_upgrades,
    fmt_amps,
    fmt_farads,
    fmt_henries,
    fmt_ohms,
    fmt_volts,
    fmt_watts,
    package_code,
    parse_amps,
    parse_cap_description,
    parse_cap_value,
    parse_capacitance,
    parse_ind_description,
    parse_ind_value,
    parse_inductance,
    parse_percent,
    parse_res_description,
    parse_res_value,
    parse_resistance,
    parse_volts,
    parse_watts,
    same_body,
    shielding_tokens,
    shielding_upgrades,
)


# DigiKey's package parameter, the same for every chip part, and the body
# size of a part with no chip size.
DK_PACKAGE = "Package / Case"
DK_SIZE = "Size / Dimension"


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


# A supplier parameter name, or several tried in order (the first that holds
# a value is used).
Names = Union[str, Tuple[str, ...]]


@dataclass(frozen=True)
class SupplierNames:
    """A supplier's parameter names for the value, rating, class (dielectric
    or shielding) and tolerance of one part type. ``dielectric`` is ``""`` for
    a type with no class field. A field given as a tuple names alternatives,
    tried in order (:func:`supplier_value`)."""
    nominal: Names
    rating: Names
    dielectric: Names
    tolerance: Names


def name_list(names: Names) -> Tuple[str, ...]:
    """*names* as a tuple; ``()`` for ``""``."""
    if isinstance(names, str):
        return (names,) if names else ()
    return tuple(names)


def name_title(names: Names) -> str:
    """The first of *names*, for messages."""
    listed = name_list(names)
    return listed[0] if listed else ""


def supplier_value(get: Callable[[str], Optional[str]], names: Names) -> Optional[str]:
    """The first value read through *get* under *names* that is not empty
    and not ``"-"`` (JLCPCB's "not listed"); ``None`` when there is none."""
    for name in name_list(names):
        raw = get(name)
        if raw is not None and str(raw).strip() not in ("", "-"):
            return raw
    return None


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
    # True: the type has a class field (dielectric, shielding).
    has_class: bool
    parse_tolerance: Callable[[Optional[str], Optional[float]], Optional[float]]
    # JLCPCB (firstSortName, secondSortName) pairs searched for a chip size.
    jlc_categories: Tuple[Tuple[str, str], ...]
    jlc: SupplierNames
    digikey: SupplierNames
    footprints: Dict[str, str] = field(default_factory=dict)
    # The class field: its schematic property, the intent attribute that
    # holds it, its title, the parser of supplier and typed text into codes,
    # and the codes accepted in place of one.
    class_property: str = "Class:Preferred"
    class_attr: str = "class_preferred"
    class_title: str = "Dielectric"
    class_tokens: Callable[[object], Tuple[str, ...]] = dielectric_tokens
    class_upgrades: Callable[[object], Tuple[str, ...]] = dielectric_upgrades
    # The settings key suffix of the class default; "" for a capacitor,
    # whose default depends on the capacitance band.
    class_setting: str = ""
    # True: a JLCPCB hit that lists no class is not rejected for it; the
    # DigiKey check decides. JLCPCB omits the shielding of most chip inductors.
    jlc_class_optional: bool = False
    # True: the default tolerance depends on the class (capacitor dielectric
    # classes 1 and 2).
    tolerance_by_class: bool = False
    # True: a package may be a body size in millimetres (power inductors) as
    # well as a chip size.
    body_sizes: bool = False
    # JLCPCB categories searched, without a package filter, for a body size.
    jlc_body_categories: Tuple[Tuple[str, str], ...] = ()

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

    def class_text(self, intent: object) -> List[str]:
        """The class entries stated on *intent* (``Class:Preferred`` /
        ``Shielding:Preferred``); ``[]`` when none."""
        return list(getattr(intent, self.class_attr, None) or [])

    # ---- packages -------------------------------------------------------
    def package_of(self, text: object) -> Optional[str]:
        """The package a supplier string, ``Package:Preferred`` or typed text
        names: a chip size (``"0603"``, from ``"0603 (1608 Metric)"`` or the
        shorthand ``"603"``), else, for a type with body sizes, a body size
        (``"4x4mm"`` from ``"SMD,4x4mm"``); ``None`` when it names neither."""
        code = package_code(text) or chip_size(text)
        if code or not self.body_sizes:
            return code
        return body_size(text)

    def digikey_package(self, get: Callable[[str], Optional[str]]) -> Optional[str]:
        """The package of a DigiKey product read through *get*: the chip size
        in ``Package / Case``, else, for a type with body sizes, the body size
        in ``Size / Dimension``."""
        code = package_code(get(DK_PACKAGE))
        if code or not self.body_sizes:
            return code
        return body_size(get(DK_SIZE))

    def same_package(self, a: Optional[str], b: Optional[str]) -> bool:
        """True when packages *a* and *b* are the same: equal chip sizes, or
        body sizes within :data:`kicadtools.units.BODY_MATCH_MM` per side."""
        if not a or not b:
            return False
        if a == b:
            return True
        return self.body_sizes and same_body(a, b)

    def is_body_size(self, pkg: Optional[str]) -> bool:
        """True when *pkg* is a body size of a type that has them."""
        return self.body_sizes and body_dims(pkg) is not None

    def package_problem(self, pkg: str) -> str:
        """``""`` when *pkg* can be searched, else the reason."""
        if package_code(pkg) == pkg or self.is_body_size(pkg):
            return ""
        if self.body_sizes:
            return f"package '{pkg}' is not a chip size or a body size (4x4mm)"
        return f"package '{pkg}' is not a known chip size"

    def jlc_searches(self, pkg: str) -> List[Tuple[Tuple[str, str], str]]:
        """The filtered JLCPCB searches for *pkg*, as (category, package
        filter): each chip-size category with the size as the filter, or each
        body-size category with no package filter (JLCPCB writes body sizes in
        several forms, so they are compared after the search)."""
        if self.is_body_size(pkg):
            return [(c, "") for c in self.jlc_body_categories]
        return [(c, pkg) for c in self.jlc_categories]


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
    has_class=True,
    parse_tolerance=_cap_tolerance,
    jlc_categories=(("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT"),),
    jlc=SupplierNames("Capacitance", "Voltage Rating", "Temperature Coefficient",
                      "Tolerance"),
    digikey=SupplierNames("Capacitance", "Voltage - Rated", "Temperature Coefficient",
                          "Tolerance"),
    footprints=_chip_footprints("Capacitor_SMD", "C"),
    tolerance_by_class=True,
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
    has_class=False,
    parse_tolerance=_percent_tolerance,
    jlc_categories=(("Resistors", "Chip Resistor - Surface Mount"),),
    jlc=SupplierNames("Resistance", "Power(Watts)", "", "Tolerance"),
    digikey=SupplierNames("Resistance", "Power (Watts)", "", "Tolerance"),
    footprints=_chip_footprints("Resistor_SMD", "R"),
)

# JLCPCB's inductor categories.
_JLC_CHIP_INDUCTORS = ("Inductors, Coils, Chokes", "Inductors (SMD)")
_JLC_POWER_INDUCTORS = ("Inductors, Coils, Chokes", "Power Inductors")

INDUCTOR = PartType(
    name="inductor",
    prefix="ind",
    intent_class=InductorIntent,
    nominal_title="Inductance",
    parse_nominal=parse_inductance,
    fmt_nominal=fmt_henries,
    search_text=fmt_henries,
    parse_value=parse_ind_value,
    parse_description=parse_ind_description,
    library_prefixes=("L_", "L-"),
    rating_property="Current:Min",
    rating_setting="current_min",
    rating_attr="current_min",
    rating_title="Current",
    parse_rating=parse_amps,
    fmt_rating=fmt_amps,
    rating_required=False,
    has_class=True,
    parse_tolerance=_percent_tolerance,
    jlc_categories=(_JLC_CHIP_INDUCTORS, _JLC_POWER_INDUCTORS),
    # JLCPCB spells the saturation current two ways; a chip inductor lists
    # "-" there, and its rated current is used.
    jlc=SupplierNames("Inductance",
                      ("Current - Saturation(Isat)", "Current - Saturation (Isat)",
                       "Current Rating"),
                      "Type", "Tolerance"),
    digikey=SupplierNames("Inductance",
                          ("Current - Saturation (Isat)", "Current Rating (Amps)"),
                          "Shielding", "Tolerance"),
    footprints=_chip_footprints("Inductor_SMD", "L"),
    class_property="Shielding:Preferred",
    class_attr="shielding_preferred",
    class_title="Shielding",
    class_tokens=shielding_tokens,
    class_upgrades=shielding_upgrades,
    class_setting="shielding",
    jlc_class_optional=True,
    body_sizes=True,
    jlc_body_categories=(_JLC_POWER_INDUCTORS,),
)

PART_TYPES: Tuple[PartType, ...] = (CAPACITOR, RESISTOR, INDUCTOR)


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
