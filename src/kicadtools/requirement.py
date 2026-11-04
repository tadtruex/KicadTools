"""
Part requirements and grouping, for capacitors, resistors and inductors.

A :class:`PartRequirement` is what any acceptable part for a symbol must meet:
its value (``nominal``, in farads, ohms or henries), package, minimum rating
(volts for a capacitor, watts for a resistor, saturation amps for an
inductor), allowed classes (``dielectrics``: a capacitor's dielectrics, an
inductor's shielding classes; none for a resistor) and maximum tolerance. What differs between the types is in
:mod:`kicadtools.parttypes`; ``kind`` names the type. Each field except the
value is a :class:`Sourced` value that records where it came from:

* ``schematic`` — stated on the symbol (``Footprint``, ``Package:Preferred``,
  ``Voltage:Min`` / ``Power:Min`` / ``Current:Min``, ``Class:Preferred`` /
  ``Shielding:Preferred``, ``Tolerance:Preferred``);
* ``default``   — taken from the remembered settings or the built-in defaults;
* ``edited``    — changed in the Cockpit's assign dialog;
* ``relaxed``   — a default tolerance limit dropped because no part met it;
  the field then holds the chosen part's own tolerance (see
  :func:`kicadtools.partsearch.find_candidates`);
* ``part``      — taken from the symbol's existing part (a supplier lookup,
  :class:`kicadtools.evidence.PartEvidence`);
* ``description`` — stated in the symbol's free-text ``Description``
  (:class:`kicadtools.evidence.DescriptionEvidence`); used only for fields no
  other source states;
* ``unset``     — no value from any source (the dialog prompts for one);
* ``any``       — no limit on the field: cleared in the Cockpit's assign
  dialog, or a resistor's power floor or an inductor's current floor with no
  stated or remembered value; the chosen part's own value is written.

Each field takes the first source that states it; the order per field is in
:func:`part_requirement`. When two sources state a value or a package and
disagree, the requirement records a conflict line and is not searchable. A
Description that disagrees records a warning line instead.

A value stated on the symbol that does not parse (``Voltage:Min`` = ``"tbd"``,
``Tolerance:Preferred`` = ``"±0.25pF"``) keeps the ``schematic`` tag with a
value of ``None``. The write plan fills only fields tagged ``default``,
``edited``, ``relaxed``, ``part`` or ``description``, so such a stated value is
never overwritten. Only ``default`` and ``edited`` values update the
remembered settings.

:func:`group_parts` collects the BOM's capacitors, resistors and inductors into
:class:`RequirementGroup` s keyed on the values stated on the schematic, so a
symbol with a stated 50 V floor is never merged with one that states none.

The defaults mapping is passed in (built by :func:`part_defaults` from the
parts database's settings table), so every function here is pure: no database
and no network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.evidence import DescriptionEvidence, ExistingPart, PartEvidence
from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.parttypes import CAPACITOR, PartType, part_type, type_of_intent
from kicadtools.units import package_code, parse_percent

# Source tags for a Sourced value.
SCHEMATIC = "schematic"
DEFAULT = "default"
EDITED = "edited"
RELAXED = "relaxed"
PART = "part"
DESCRIPTION = "description"
UNSET = "unset"
# No limit on the field: the search applies none, and the chosen part's own
# value is written (see partsearch.requirement_for).
ANY = "any"

# Two values within this relative difference are the same value.
_VALUE_MATCH_REL = 1e-3

# Built-in defaults, overlaid by the settings table. "cap.voltage_min",
# "res.power_min" and "ind.current_min" have no built-in value: a capacitor's
# voltage floor is a design decision, so it is unset until one has been used;
# a resistor with no power floor and an inductor with no current floor are
# searched with none. "ind.package" has no built-in value either: most
# inductors are power inductors, whose body size is not a default.
BUILTIN_DEFAULTS: Dict[str, str] = {
    "cap.package": "0603",
    "cap.dielectric.small": "C0G, NP0",
    "cap.dielectric.mid": "X7R",
    "cap.dielectric.bulk": "X7R, X5R",
    "cap.tolerance.class1": "5%",
    "cap.tolerance.class2": "10%",
    # Manufacturers whose parts are searched, matched as text within the
    # supplier's manufacturer name (case and accents ignored). Each is stocked
    # by both DigiKey and JLCPCB.
    "cap.manufacturers": (
        "Murata, Samsung, Yageo, TDK, Taiyo Yuden, KEMET, Vishay, Wurth, "
        "Kyocera, AVX"
    ),
    "res.package": "0603",
    "res.tolerance": "1%",
    "res.manufacturers": (
        "Yageo, Vishay, Panasonic, Stackpole, Rohm, TE Connectivity, Bourns"
    ),
    "ind.shielding": "Shielded",
    "ind.tolerance": "20%",
    "ind.manufacturers": (
        "Coilcraft, Murata, TDK, Taiyo Yuden, Bourns, Wurth, Vishay, Sunlord, "
        "Samsung, Eaton, Pulse, Abracon"
    ),
}

# Dielectric band boundaries (farads): small at or below 1 nF, bulk at or
# above 10 uF. A relative margin absorbs float error in parsed values such as
# "0.001uF".
_SMALL_MAX_F = 1e-9
_BULK_MIN_F = 10e-6
_BAND_EPS = 1e-6

# Class 1 (temperature-stable) dielectric codes, normalized.
_CLASS1 = frozenset({"C0G", "NP0"})

# The schematic properties that form a group key, in key order.
_GROUP_PROPS = (
    "Value",
    "Footprint",
    "Voltage:Min",
    "Power:Min",
    "Current:Min",
    "Class:Preferred",
    "Shielding:Preferred",
    "Tolerance:Preferred",
    "Package:Preferred",
)


def part_defaults(settings: Mapping[str, str]) -> Dict[str, str]:
    """:data:`BUILTIN_DEFAULTS` with every key in *settings* overlaid on it."""
    out = dict(BUILTIN_DEFAULTS)
    out.update(settings)
    return out


def dielectric_band(capacitance_f: float) -> str:
    """``"small"`` at or below 1 nF, ``"bulk"`` at or above 10 uF, else ``"mid"``."""
    if capacitance_f <= _SMALL_MAX_F * (1 + _BAND_EPS):
        return "small"
    if capacitance_f >= _BULK_MIN_F * (1 - _BAND_EPS):
        return "bulk"
    return "mid"


def tolerance_class(dielectrics: Sequence[str]) -> str:
    """``"class1"`` when every entry is C0G/NP0, else ``"class2"``. An empty
    sequence is ``"class2"``: the looser tolerance applies until a dielectric
    is known."""
    codes = _codes(CAPACITOR, dielectrics)
    if codes and all(c in _CLASS1 for c in codes):
        return "class1"
    return "class2"


def tolerance_key(ptype: PartType, dielectrics: Sequence[str]) -> str:
    """The settings key of the default tolerance: per dielectric class for a
    capacitor (``"cap.tolerance.class2"``), else one per type
    (``"res.tolerance"``, ``"ind.tolerance"``)."""
    if ptype.tolerance_by_class:
        return ptype.key("tolerance." + tolerance_class(dielectrics))
    return ptype.key("tolerance")


@dataclass(frozen=True)
class Sourced:
    """A requirement value and the source tag it came from."""
    value: Any
    source: str


@dataclass(frozen=True)
class PartRequirement:
    """The parameters any acceptable part for one symbol must meet."""
    kind: str                            # PartType name: "capacitor", "resistor", "inductor"
    value_text: str                      # the symbol's Value, as written
    nominal: Optional[float]             # farads / ohms / henries; None when Value doesn't parse
    package: Sourced                     # Optional[str], e.g. "0603", "4x4mm"
    rating_min: Sourced                  # Optional[float], volts / watts / amps
    dielectrics: Sourced                 # Tuple[str, ...], the class codes; () for a resistor
    tolerance_max_pct: Sourced           # Optional[float]
    footprint: str = ""                  # the symbol's Footprint, "" when empty
    nominal_source: str = SCHEMATIC      # SCHEMATIC | DESCRIPTION | PART | UNSET
    conflicts: Tuple[str, ...] = ()      # one line per disagreement between sources
    warnings: Tuple[str, ...] = ()       # Description disagreements; do not block

    @property
    def ptype(self) -> PartType:
        """The :class:`~kicadtools.parttypes.PartType` named by ``kind``."""
        return part_type(self.kind)

    @property
    def searchable(self) -> bool:
        """No conflicts; value, package and (for a capacitor) rating floor
        are all known, and the package is a chip size or, for an inductor, a
        body size."""
        return not self.problem()

    def problem(self) -> str:
        """``""`` when searchable, else a one-line reason. The first conflict
        comes before every other reason."""
        if self.conflicts:
            return self.conflicts[0]
        t = self.ptype
        if self.nominal is None or self.nominal <= 0:
            if not self.value_text:
                return "no Value"
            return f"Value '{self.value_text}' is not a {t.nominal_title.lower()}"
        pkg = self.package.value
        if not pkg:
            if self.footprint and t.body_sizes:
                return (f"footprint '{self.footprint}' names no chip size; enter "
                        "the body size (4x4mm)")
            if self.footprint:
                return f"footprint '{self.footprint}' is not a chip size"
            return "no package"
        why = t.package_problem(pkg)
        if why:
            return why
        if self.rating_min.value is None and self.rating_min.source != ANY:
            noun = t.rating_title.lower()
            if self.rating_min.source == SCHEMATIC:
                return f"{t.rating_property} on the schematic is not a {noun}"
            return f"no minimum {noun} ({t.rating_property})"
        return ""

    def edited(
        self,
        *,
        package: Optional[str] = None,
        rating_min: Optional[float] = None,
        dielectrics: Optional[Sequence[str]] = None,
        tolerance_max_pct: Optional[float] = None,
        clear: Sequence[str] = (),
    ) -> "PartRequirement":
        """A copy in which each argument that is not ``None`` replaces its field,
        tagged :data:`EDITED`, and each field named in *clear*
        (``"rating_min"``, ``"dielectrics"``, ``"tolerance_max_pct"``) has
        no value, tagged :data:`ANY`. The package of a symbol with a footprint
        is fixed by that footprint, so editing it raises ``ValueError``; a
        footprint with no chip size on a type with body sizes (a power
        inductor) fixes none, and its body size can be edited."""
        changes: Dict[str, Sourced] = {}
        for name in clear:
            if name not in _CLEARABLE:
                raise ValueError(f"{name} cannot be cleared")
            changes[name] = Sourced(_CLEARABLE[name], ANY)
        t = self.ptype
        if package is not None:
            if not package_editable(t, self.footprint):
                raise ValueError(
                    f"package is set by the footprint '{self.footprint}' "
                    "and cannot be edited"
                )
            text = str(package).strip()
            changes["package"] = Sourced(t.package_of(text) or text, EDITED)
        if rating_min is not None:
            changes["rating_min"] = Sourced(float(rating_min), EDITED)
        if dielectrics is not None:
            changes["dielectrics"] = Sourced(_codes(t, dielectrics), EDITED)
        if tolerance_max_pct is not None:
            changes["tolerance_max_pct"] = Sourced(
                float(tolerance_max_pct), EDITED
            )
        return replace(self, **changes)


# Fields the assign dialog can clear, and the value a cleared field holds.
_CLEARABLE: Dict[str, Any] = {
    "rating_min": None,
    "dielectrics": (),
    "tolerance_max_pct": None,
}


def package_editable(ptype: PartType, footprint: str) -> bool:
    """True when the package of a symbol with *footprint* can be edited: it
    has no footprint, or the type has body sizes and the footprint names no
    chip size (a power inductor's footprint does not give its body size)."""
    if not footprint:
        return True
    return ptype.body_sizes and package_code(footprint) is None


def _codes(ptype: PartType, entries: Sequence[str] | str) -> Tuple[str, ...]:
    """Normalized class codes of *ptype* from *entries*, in order, without
    repeats. Each entry is split with the type's tokenizer
    (:attr:`~kicadtools.parttypes.PartType.class_tokens`), so ``"C0G/NP0"``
    as one entry gives two codes rather than ``"C0GNP0"``."""
    if isinstance(entries, str):
        entries = [entries]
    out: List[str] = []
    for entry in entries:
        for code in ptype.class_tokens(entry):
            if code not in out:
                out.append(code)
    return tuple(out)


def _default(defaults: Mapping[str, str], key: str) -> str:
    """The *defaults* entry for *key*, stripped; the built-in value when the
    entry is missing or blank."""
    value = str(defaults.get(key) or "").strip()
    return value or BUILTIN_DEFAULTS.get(key, "")


def _package(ptype: PartType, intent: Any, footprint: str,
             defaults: Mapping[str, str], value_package: Optional[str],
             description: DescriptionEvidence,
             part: Optional[PartEvidence]) -> Sourced:
    """Package from the footprint name, else ``Package:Preferred``, else the
    Value's size suffix (``C_10U_805``), else the existing part, else the
    description, else the ``<prefix>.package`` default. A footprint with no
    chip-size code (a radial electrolytic, say) gives ``None``: the footprint
    fixes the package, so the other sources do not apply. For a type with
    body sizes such a footprint fixes nothing that can be read: the body size
    comes from ``Package:Preferred``, the existing part or the description,
    and no default applies (the default is a chip size, which the footprint
    rules out)."""
    code = package_code(footprint) if footprint else None
    if code:
        return Sourced(code, SCHEMATIC)
    if footprint and not ptype.body_sizes:
        return Sourced(None, SCHEMATIC)
    if intent.package_preferred:
        pref = intent.package_preferred[0]
        return Sourced(ptype.package_of(pref) or pref, SCHEMATIC)
    if value_package:
        return Sourced(value_package, SCHEMATIC)
    if part is not None and part.package:
        return Sourced(ptype.package_of(part.package) or part.package, PART)
    if description.package:
        return Sourced(description.package, DESCRIPTION)
    if footprint:
        return Sourced(None, UNSET)
    pkg = _default(defaults, ptype.key("package"))
    if not pkg:
        return Sourced(None, UNSET)
    return Sourced(ptype.package_of(pkg) or pkg, DEFAULT)


def _rating(ptype: PartType, intent: Any, defaults: Mapping[str, str],
            description: DescriptionEvidence,
            part: Optional[PartEvidence]) -> Sourced:
    """The stated rating floor (``Voltage:Min`` / ``Power:Min``), else the
    existing part's rating, else the description's (lowest) rating, else the
    remembered setting; else unset for a type whose floor is required, and
    ``any`` (no limit) otherwise. A stated value that does not parse is
    ``None`` tagged ``schematic``. A voltage floor is never derived from net
    or rail names."""
    text = ptype.rating_text(intent)
    if text:
        return Sourced(ptype.parse_rating(text), SCHEMATIC)
    if part is not None and part.rating is not None:
        return Sourced(float(part.rating), PART)
    if description.rating is not None:
        return Sourced(float(description.rating), DESCRIPTION)
    remembered = ptype.parse_rating(defaults.get(ptype.key(ptype.rating_setting)))
    if remembered is not None:
        return Sourced(remembered, DEFAULT)
    return Sourced(None, UNSET if ptype.rating_required else ANY)


def allowed_classes(ptype: PartType, codes: Sequence[str]) -> Tuple[str, ...]:
    """The classes a part may have to meet a requirement whose class field
    holds *codes*: each code and the classes that dominate it
    (:attr:`~kicadtools.parttypes.PartType.class_upgrades`; X5R -> X5R, X7R,
    X8R; Semi-Shielded -> Semi-Shielded, Shielded), whatever the field's
    source (stated on the schematic, a default, the part or the
    Description). The database lookup and the supplier checks use this; the
    field itself keeps the codes as stated."""
    return _upgraded(ptype, codes)


def _upgraded(ptype: PartType, codes: Sequence[str]) -> Tuple[str, ...]:
    """Each normalized code in *codes* followed by its accepted upgrades
    (:attr:`~kicadtools.parttypes.PartType.class_upgrades`: X5R -> X5R, X7R;
    Semi-Shielded -> Semi-Shielded, Shielded), in order, without repeats."""
    out: List[str] = []
    for code in _codes(ptype, codes):
        for c in ptype.class_upgrades(code):
            if c not in out:
                out.append(c)
    return tuple(out)


def _dielectrics(ptype: PartType, intent: Any, nominal: Optional[float],
                 defaults: Mapping[str, str],
                 description: DescriptionEvidence,
                 part: Optional[PartEvidence]) -> Sourced:
    """The class field: ``Class:Preferred`` / ``Shielding:Preferred``, else
    the existing part's class, else the description's, else the default: ``cap.dielectric.<band>``
    for the capacitance, ``ind.shielding`` for an inductor; unset when none is
    available, and always for a type without a class. A stated value is
    tagged ``schematic`` even when it holds no code, so it is never replaced
    by a default. The codes are kept as read: a part of a better class
    (X7R for X5R) meets the field when matching (:func:`allowed_classes`)."""
    if not ptype.has_class:
        return Sourced((), UNSET)
    stated = ptype.class_text(intent)
    if stated:
        return Sourced(_codes(ptype, stated), SCHEMATIC)
    # The codes as read; the better classes are allowed when matching
    # (allowed_classes), whatever the source.
    if part is not None and part.dielectrics:
        codes = _codes(ptype, part.dielectrics)
        if codes:
            return Sourced(codes, PART)
    if description.dielectrics:
        codes = _codes(ptype, description.dielectrics)
        if codes:
            return Sourced(codes, DESCRIPTION)
    if ptype.class_setting:
        key = ptype.key(ptype.class_setting)
    elif nominal is None or nominal <= 0:
        return Sourced((), UNSET)
    else:
        key = ptype.key("dielectric." + dielectric_band(nominal))
    codes = _codes(ptype, _default(defaults, key))
    return Sourced(codes, DEFAULT) if codes else Sourced((), UNSET)


def _tolerance(ptype: PartType, intent: Any, dielectrics: Tuple[str, ...],
               defaults: Mapping[str, str],
               description: DescriptionEvidence,
               part: Optional[PartEvidence]) -> Sourced:
    """The loosest ``Tolerance:Preferred`` entry, else the existing part's
    tolerance, else the description's, else the default
    (:func:`tolerance_key`). A stated value with no percentage in it
    (``"±0.25pF"``) is ``None`` tagged ``schematic``: the search applies no
    tolerance limit and the stated text is kept."""
    if intent.tolerance_preferred:
        stated = [
            p for p in (parse_percent(t) for t in intent.tolerance_preferred)
            if p is not None
        ]
        return Sourced(max(stated) if stated else None, SCHEMATIC)
    if part is not None and part.tolerance_pct is not None:
        return Sourced(float(part.tolerance_pct), PART)
    if description.tolerance_pct is not None:
        return Sourced(float(description.tolerance_pct), DESCRIPTION)
    pct = parse_percent(_default(defaults, tolerance_key(ptype, dielectrics)))
    return Sourced(pct, DEFAULT) if pct is not None else Sourced(None, UNSET)


def _nominal_conflicts(ptype: PartType,
                       stated: Sequence[Tuple[str, Optional[float]]]) -> List[str]:
    """A line for every pair of ``(source name, value)`` entries that both
    state a value and differ by more than 0.1 %."""
    known = [(name, v) for name, v in stated if v is not None and v > 0]
    out: List[str] = []
    for i, (name_a, a) in enumerate(known):
        for name_b, b in known[i + 1:]:
            if abs(a - b) > _VALUE_MATCH_REL * max(a, b):
                out.append(
                    f"{name_a} says {ptype.fmt_nominal(a)}; "
                    f"{name_b} says {ptype.fmt_nominal(b)}"
                )
    return out


def _package_conflicts(ptype: PartType,
                       stated: Sequence[Tuple[str, Optional[str]]]) -> List[str]:
    """A line for every pair of ``(source name, package)`` entries that both
    state a package (:meth:`~kicadtools.parttypes.PartType.package_of`) and
    differ (:meth:`~kicadtools.parttypes.PartType.same_package`). An entry
    with no package in it (a radial footprint, an unparseable
    ``Package:Preferred``) states none."""
    known = [(name, ptype.package_of(p)) for name, p in stated if p]
    known = [(name, p) for name, p in known if p]
    out: List[str] = []
    for i, (name_a, a) in enumerate(known):
        for name_b, b in known[i + 1:]:
            if not ptype.same_package(a, b):
                out.append(f"{name_a} says {a}; {name_b} says {b}")
    return out


def _description_gaps(ptype: PartType, intent: Any, footprint: str,
                      value_text: str, value_package: Optional[str],
                      description: DescriptionEvidence,
                      part: Optional[PartEvidence]) -> DescriptionEvidence:
    """The fields of *description* that no other source states: the value
    when there is no Value and no part value, the package when there is no
    footprint, ``Package:Preferred``, Value size suffix or part package, the
    rating when there is no stated floor and no part rating, the classes
    when there is no stated class and no part class, the tolerance
    when there is no ``Tolerance:Preferred`` and no part tolerance. A symbol
    described completely by its other properties and its part gives an empty
    result."""
    has_part = part is not None
    return DescriptionEvidence(
        nominal=None if value_text or (has_part and part.nominal is not None)
        else description.nominal,
        package=None if ((footprint and not ptype.body_sizes)
                         or package_code(footprint)
                         or intent.package_preferred or value_package
                         or (has_part and part.package))
        else description.package,
        rating=None if (ptype.rating_text(intent)
                        or (has_part and part.rating is not None))
        else description.rating,
        dielectrics=() if (ptype.class_text(intent)
                           or (has_part and part.dielectrics))
        else description.dielectrics,
        tolerance_pct=None if (intent.tolerance_preferred
                               or (has_part and part.tolerance_pct is not None))
        else description.tolerance_pct,
    )


def description_key(intent: Any, footprint: str,
                    description: DescriptionEvidence,
                    has_part_number: bool = False) -> Tuple[Any, ...]:
    """The group-key entry for a symbol's description: the fields that the
    symbol's other properties leave open (:func:`_description_gaps` with no
    part). Two symbols whose descriptions differ only in text, or only in
    fields stated elsewhere on the symbol, give the same key. A symbol with an
    existing part number gives ``()``: the part numbers are in the key, and
    the part states every field."""
    if has_part_number:
        return ()
    ptype = type_of_intent(intent) or CAPACITOR
    value_text = str(intent.value or "").strip()
    value_pkg = ptype.parse_value(value_text)[1] if value_text else None
    gaps = _description_gaps(ptype, intent, (footprint or "").strip(), value_text,
                             value_pkg, description, None)
    return (gaps.nominal, gaps.package, gaps.rating, tuple(gaps.dielectrics),
            gaps.tolerance_pct)


def part_requirement(
    intent: Any,
    footprint: str,
    defaults: Mapping[str, str],
    *,
    description: Optional[DescriptionEvidence] = None,
    part: Optional[PartEvidence] = None,
) -> PartRequirement:
    """Build the requirement for one capacitor, resistor or inductor from its *intent*
    (whose class gives the part type), its ``Footprint`` text, the *defaults*
    mapping (see :func:`part_defaults`), and the evidence of its
    *description* and existing *part*.

    Each field takes the first source that states it:

    * value — Value (plain or library style, ``C_4p7U``, ``R_49p9K``)
      [schematic], existing part [part], description [description];
    * package — footprint, ``Package:Preferred``, the Value's size suffix
      [schematic], existing part [part], description [description], the
      ``<prefix>.package`` setting [default];
    * rating floor — ``Voltage:Min`` / ``Power:Min`` / ``Current:Min``
      [schematic], existing part [part], description [description], the
      remembered setting [default]; then unset for a capacitor, ``any`` for a
      resistor or an inductor;
    * class (capacitors, inductors) — ``Class:Preferred`` /
      ``Shielding:Preferred`` [schematic], existing part [part], description
      [description] (each with its upgrades), the default [default];
    * tolerance — ``Tolerance:Preferred`` [schematic], existing part [part],
      description [description], default [default].

    A Value that is stated but does not parse keeps the value ``None`` tagged
    ``schematic``, and is not compared with the other sources. Every pair of
    sources other than the description stating different values (more than
    0.1 % apart) or different packages adds a line to ``conflicts``.

    The description fills only the fields no other source states
    (:func:`_description_gaps`). A description value or package that
    disagrees with the value used adds a line to ``warnings`` instead of
    ``conflicts``: library boilerplate and descriptions copied from another
    symbol do not block a search."""
    ptype = type_of_intent(intent)
    if ptype is None:
        raise TypeError(f"{type(intent).__name__} is not a capacitor, resistor or inductor intent")
    stated = description or DescriptionEvidence()
    footprint = (footprint or "").strip()
    value_text = str(intent.value or "").strip()
    value_n, value_pkg = ptype.parse_value(value_text) if value_text else (None, None)
    description = _description_gaps(ptype, intent, footprint, value_text, value_pkg,
                                    stated, part)

    if value_text:
        nominal, nominal_source = value_n, SCHEMATIC
    elif part is not None and part.nominal is not None:
        nominal, nominal_source = part.nominal, PART
    elif description.nominal is not None:
        nominal, nominal_source = description.nominal, DESCRIPTION
    else:
        nominal, nominal_source = None, UNSET

    part_label = part.label if part is not None else ""
    conflicts = _nominal_conflicts(ptype, [
        ("Value", value_n),
        (part_label, part.nominal if part is not None else None),
    ])
    conflicts += _package_conflicts(ptype, [
        ("Footprint", footprint),
        ("Package:Preferred",
         intent.package_preferred[0] if intent.package_preferred else None),
        ("Value", value_pkg),
        (part_label, part.package if part is not None else None),
    ])

    package = _package(ptype, intent, footprint, defaults, value_pkg, description, part)
    warnings = [
        f"{line} (Description not used)"
        for line in _nominal_conflicts(ptype, [
            ("Description", stated.nominal),
            (_used_label(nominal_source, part_label), nominal)])
    ]
    if package.source != DESCRIPTION:
        warnings += [
            f"{line} (Description not used)"
            for line in _package_conflicts(ptype, [
                ("Description", stated.package),
                (_used_label(package.source, part_label, footprint), package.value),
            ])
        ]

    dielectrics = _dielectrics(ptype, intent, nominal, defaults, description, part)
    return PartRequirement(
        kind=ptype.name,
        value_text=value_text,
        nominal=nominal,
        package=package,
        rating_min=_rating(ptype, intent, defaults, description, part),
        dielectrics=dielectrics,
        tolerance_max_pct=_tolerance(ptype, intent, dielectrics.value, defaults,
                                     description, part),
        footprint=footprint,
        nominal_source=nominal_source,
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
    )


def _used_label(source: str, part_label: str, footprint: str = "") -> str:
    """The name of the source a used value or package came from, for a
    warning line."""
    if source == PART:
        return part_label
    if source == SCHEMATIC:
        return "Footprint" if footprint else "Value"
    return "the " + source


@dataclass
class Member:
    """One placed part in a :class:`RequirementGroup`. A subsheet symbol
    instantiated twice gives two members that share a ``symbol_uuid``."""
    reference: str
    source_file: Optional[Path]
    symbol_uuid: Optional[str]
    props: Dict[str, object]
    intent: Any  # a CapacitorIntent, ResistorIntent or InductorIntent

    @property
    def existing(self) -> ExistingPart:
        """The part numbers in the symbol's properties, under any alias
        spelling. Each field holds the text as written: a DigiKey PN in the
        MPN field stays in ``mpn``."""
        pn = extract_part_numbers(self.props)
        return ExistingPart(
            mpn=pn.mpn or None,
            digikey_pn=pn.digikey_pn or None,
            lcsc=pn.lcsc or None,
            manufacturer=pn.manufacturer or None,
        )

    @property
    def description(self) -> DescriptionEvidence:
        """What the symbol's ``Description`` property states."""
        ptype = type_of_intent(self.intent) or CAPACITOR
        return ptype.parse_description(_prop(self.props, "Description"))


def _prop(props: Mapping[str, object], key: str) -> str:
    """A string property, stripped; ``""`` when missing or not a string."""
    value = props.get(key)
    return value.strip() if isinstance(value, str) else ""


@dataclass
class RequirementGroup:
    """Parts of one type that state the same values on the schematic, the
    same open Description fields and the same existing part numbers, and so
    share one requirement and one part choice."""
    requirement: PartRequirement
    members: List[Member]

    @property
    def ptype(self) -> PartType:
        """The group's part type."""
        return self.requirement.ptype

    @property
    def existing(self) -> ExistingPart:
        """The first member's existing part numbers (the group key includes
        them, so every member holds the same ones)."""
        return self.members[0].existing

    @property
    def description(self) -> DescriptionEvidence:
        """The first member's description evidence (the group key includes
        the fields of it that can be used)."""
        return self.members[0].description

    def requirement_with_part(self, part: Optional[PartEvidence],
                              defaults: Mapping[str, str]) -> PartRequirement:
        """The requirement recomputed from the first member's intent, footprint
        and description, with *part* as the existing part's evidence and
        *defaults* as the settings. Further conflicts can be appended with
        :func:`dataclasses.replace`."""
        first = self.members[0]
        return part_requirement(
            first.intent, _prop(first.props, "Footprint"), defaults,
            description=self.description, part=part,
        )

    @property
    def references(self) -> List[str]:
        """Member references in natural order (``C2`` before ``C10``)."""
        return sorted((m.reference for m in self.members), key=natural_key)

    @property
    def quantity(self) -> int:
        return len(self.members)

    @property
    def assigned(self) -> bool:
        """Every member has an MPN, Manufacturer, DigiKey PN, LCSC number and a
        Footprint."""
        for m in self.members:
            pn = extract_part_numbers(m.props)
            if not (pn.mpn and pn.manufacturer and pn.digikey_pn and pn.lcsc):
                return False
            if not _prop(m.props, "Footprint"):
                return False
        return True


def group_parts(records: Sequence[Any],
                defaults: Mapping[str, str]) -> List[RequirementGroup]:
    """Group the capacitor, resistor and inductor records (from
    :func:`kicadtools.resolve.iter_bom_part_records`) by part type, the values
    stated on the schematic, the Description fields those values leave open
    (:func:`description_key`; none for a symbol with a part number) and the
    existing MPN, DigiKey PN and LCSC number (compared upper-cased), so
    symbols with different existing parts are separate groups. Each group's
    requirement includes its description evidence and no part evidence.
    Records of other types are skipped. Groups are ordered by the natural key
    of their first reference."""
    by_key: Dict[Tuple[Any, ...], RequirementGroup] = {}
    for rec in records:
        intent = rec.intent
        ptype = type_of_intent(intent)
        if ptype is None:
            continue
        pn = extract_part_numbers(rec.props)
        has_pn = bool(pn.mpn or pn.digikey_pn or pn.lcsc)
        key = (ptype.name,) + tuple(_prop(rec.props, k) for k in _GROUP_PROPS) + (
            description_key(intent, _prop(rec.props, "Footprint"),
                            ptype.parse_description(_prop(rec.props, "Description")),
                            has_pn),
            (pn.mpn or "").upper(),
            (pn.digikey_pn or "").upper(),
            (pn.lcsc or "").upper(),
        )
        member = Member(
            reference=rec.reference,
            source_file=rec.source_file,
            symbol_uuid=rec.symbol_uuid,
            props=rec.props,
            intent=intent,
        )
        group = by_key.get(key)
        if group is None:
            req = part_requirement(
                intent, _prop(rec.props, "Footprint"), defaults,
                description=member.description,
            )
            by_key[key] = RequirementGroup(req, [member])
        else:
            group.members.append(member)
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))
