"""
Capacitor requirements and grouping.

A :class:`CapacitorRequirement` is what any acceptable part for a capacitor
symbol must meet: capacitance, package, minimum voltage, allowed dielectrics and
maximum tolerance. Each field except capacitance is a :class:`Sourced` value
that records where it came from:

* ``schematic`` — stated on the symbol (``Footprint``, ``Package:Preferred``,
  ``Voltage:Min``, ``Class:Preferred``, ``Tolerance:Preferred``);
* ``default``   — taken from the remembered settings or the built-in defaults;
* ``edited``    — changed in the Cockpit's assign dialog;
* ``relaxed``   — a default tolerance limit dropped because no part met it;
  the field then holds the chosen part's own tolerance (see
  :func:`kicadtools.capsearch.find_candidates`);
* ``part``      — taken from the symbol's existing part (a supplier lookup,
  :class:`kicadtools.evidence.PartEvidence`);
* ``description`` — stated in the symbol's free-text ``Description``
  (:class:`kicadtools.evidence.DescriptionEvidence`); used only for fields no
  other source states;
* ``unset``     — no value from any source (the dialog prompts for one);
* ``any``       — cleared in the Cockpit's assign dialog: no limit on the
  field; the chosen part's own value is written.

Each field takes the first source that states it; the order per field is in
:func:`capacitor_requirement`. When two sources state a capacitance or a
package and disagree, the requirement records a conflict line and is not
searchable. A Description that disagrees records a warning line instead.

A value stated on the symbol that does not parse (``Voltage:Min`` = ``"tbd"``,
``Tolerance:Preferred`` = ``"±0.25pF"``) keeps the ``schematic`` tag with a
value of ``None``. The write plan fills only fields tagged ``default``,
``edited``, ``relaxed``, ``part`` or ``description``, so such a stated value is
never overwritten. Only ``default`` and ``edited`` values update the
remembered settings.

:func:`group_capacitors` collects the BOM's capacitors into
:class:`RequirementGroup` s keyed on the values stated on the schematic, so a
symbol with a stated 50 V floor is never merged with one that states none.

The defaults mapping is passed in (built by :func:`capacitor_defaults` from the
parts database's settings table), so every function here is pure: no database
and no network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.evidence import DescriptionEvidence, ExistingPart, PartEvidence
from kicadtools.intent import CapacitorIntent
from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.units import (
    chip_size,
    dielectric_tokens,
    dielectric_upgrades,
    fmt_farads,
    package_code,
    parse_cap_description,
    parse_cap_value,
    parse_percent,
    parse_volts,
)

# Source tags for a Sourced value.
SCHEMATIC = "schematic"
DEFAULT = "default"
EDITED = "edited"
RELAXED = "relaxed"
PART = "part"
DESCRIPTION = "description"
UNSET = "unset"
# Cleared in the assign dialog: the search applies no limit on the field, and
# the chosen part's own value is written (see capsearch.requirement_for).
ANY = "any"

# Two capacitances within this relative difference are the same value.
_CAP_MATCH_REL = 1e-3

# Built-in defaults, overlaid by the settings table. "cap.voltage_min" has no
# built-in value: a voltage floor is a design decision, so it is unset until
# one has been used.
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
        "Murata, Samsung, Yageo, TDK, Taiyo Yuden, KEMET, Walsin, Samwha, "
        "Vishay, Wurth, Kyocera, AVX, Holy Stone, Knowles"
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
    "Class:Preferred",
    "Tolerance:Preferred",
    "Package:Preferred",
)


def capacitor_defaults(settings: Mapping[str, str]) -> Dict[str, str]:
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
    codes = _codes(dielectrics)
    if codes and all(c in _CLASS1 for c in codes):
        return "class1"
    return "class2"


@dataclass(frozen=True)
class Sourced:
    """A requirement value and the source tag it came from."""
    value: Any
    source: str


@dataclass(frozen=True)
class CapacitorRequirement:
    """The parameters any acceptable part for one capacitor must meet."""
    value_text: str                      # the symbol's Value, as written
    capacitance_f: Optional[float]       # None when Value doesn't parse
    package: Sourced                     # Optional[str], e.g. "0603"
    voltage_min_v: Sourced               # Optional[float]
    dielectrics: Sourced                 # Tuple[str, ...], normalized
    tolerance_max_pct: Sourced           # Optional[float]
    footprint: str = ""                  # the symbol's Footprint, "" when empty
    capacitance_source: str = SCHEMATIC  # SCHEMATIC | DESCRIPTION | PART | UNSET
    conflicts: Tuple[str, ...] = ()      # one line per disagreement between sources
    warnings: Tuple[str, ...] = ()       # Description disagreements; do not block

    @property
    def searchable(self) -> bool:
        """No conflicts; capacitance, package and voltage floor are all known,
        and the package is an imperial chip size."""
        return not self.problem()

    def problem(self) -> str:
        """``""`` when searchable, else a one-line reason. The first conflict
        comes before every other reason."""
        if self.conflicts:
            return self.conflicts[0]
        if self.capacitance_f is None or self.capacitance_f <= 0:
            if not self.value_text:
                return "no Value"
            return f"Value '{self.value_text}' is not a capacitance"
        pkg = self.package.value
        if not pkg:
            if self.footprint:
                return (
                    f"footprint '{self.footprint}' is not a chip-capacitor size"
                )
            return "no package"
        if package_code(pkg) != pkg:
            return f"package '{pkg}' is not a known chip size"
        if self.voltage_min_v.value is None and self.voltage_min_v.source != ANY:
            if self.voltage_min_v.source == SCHEMATIC:
                return "Voltage:Min on the schematic is not a voltage"
            return "no minimum voltage (Voltage:Min)"
        return ""

    def edited(
        self,
        *,
        package: Optional[str] = None,
        voltage_min_v: Optional[float] = None,
        dielectrics: Optional[Sequence[str]] = None,
        tolerance_max_pct: Optional[float] = None,
        clear: Sequence[str] = (),
    ) -> "CapacitorRequirement":
        """A copy in which each argument that is not ``None`` replaces its field,
        tagged :data:`EDITED`, and each field named in *clear*
        (``"voltage_min_v"``, ``"dielectrics"``, ``"tolerance_max_pct"``) has
        no value, tagged :data:`ANY`. The package of a symbol with a footprint
        is fixed by that footprint, so editing it raises ``ValueError``."""
        changes: Dict[str, Sourced] = {}
        for name in clear:
            if name not in _CLEARABLE:
                raise ValueError(f"{name} cannot be cleared")
            changes[name] = Sourced(_CLEARABLE[name], ANY)
        if package is not None:
            if self.footprint:
                raise ValueError(
                    f"package is set by the footprint '{self.footprint}' "
                    "and cannot be edited"
                )
            text = str(package).strip()
            changes["package"] = Sourced(_chip(text) or text, EDITED)
        if voltage_min_v is not None:
            changes["voltage_min_v"] = Sourced(float(voltage_min_v), EDITED)
        if dielectrics is not None:
            changes["dielectrics"] = Sourced(_codes(dielectrics), EDITED)
        if tolerance_max_pct is not None:
            changes["tolerance_max_pct"] = Sourced(
                float(tolerance_max_pct), EDITED
            )
        return replace(self, **changes)


# Fields the assign dialog can clear, and the value a cleared field holds.
_CLEARABLE: Dict[str, Any] = {
    "voltage_min_v": None,
    "dielectrics": (),
    "tolerance_max_pct": None,
}


def _codes(entries: Sequence[str] | str) -> Tuple[str, ...]:
    """Normalized dielectric codes from *entries*, in order, without repeats.
    Each entry is split with :func:`dielectric_tokens`, so ``"C0G/NP0"`` as one
    entry gives two codes rather than ``"C0GNP0"``."""
    if isinstance(entries, str):
        entries = [entries]
    out: List[str] = []
    for entry in entries:
        for code in dielectric_tokens(entry):
            if code not in out:
                out.append(code)
    return tuple(out)


def _chip(text: object) -> Optional[str]:
    """The chip-size code in *text*: a four- or five-digit code anywhere in a
    footprint or supplier string (:func:`kicadtools.units.package_code`), else
    the whole text as a bare token, which accepts the three-digit shorthand
    (``"402"`` -> ``"0402"``, :func:`kicadtools.units.chip_size`)."""
    return package_code(text) or chip_size(text)


def _default(defaults: Mapping[str, str], key: str) -> str:
    """The *defaults* entry for *key*, stripped; the built-in value when the
    entry is missing or blank."""
    value = str(defaults.get(key) or "").strip()
    return value or BUILTIN_DEFAULTS.get(key, "")


def _package(intent: CapacitorIntent, footprint: str,
             defaults: Mapping[str, str], value_package: Optional[str],
             description: DescriptionEvidence,
             part: Optional[PartEvidence]) -> Sourced:
    """Package from the footprint name, else ``Package:Preferred``, else the
    Value's size suffix (``C_10U_805``), else the existing part, else the
    description, else the ``cap.package`` default. A footprint with no
    chip-size code (a radial electrolytic, say) gives ``None``: the footprint
    fixes the package, so the other sources do not apply."""
    if footprint:
        return Sourced(package_code(footprint), SCHEMATIC)
    if intent.package_preferred:
        pref = intent.package_preferred[0]
        return Sourced(_chip(pref) or pref, SCHEMATIC)
    if value_package:
        return Sourced(value_package, SCHEMATIC)
    if part is not None and part.package:
        return Sourced(_chip(part.package) or part.package, PART)
    if description.package:
        return Sourced(description.package, DESCRIPTION)
    pkg = _default(defaults, "cap.package")
    return Sourced(_chip(pkg) or pkg, DEFAULT)


def _voltage(intent: CapacitorIntent, defaults: Mapping[str, str],
             description: DescriptionEvidence,
             part: Optional[PartEvidence]) -> Sourced:
    """``Voltage:Min``, else the existing part's rating, else the description's
    (lowest) voltage, else the ``cap.voltage_min`` setting, else unset. A
    stated value that does not parse is ``None`` tagged ``schematic``. The
    voltage floor is never derived from net or rail names."""
    text = str(intent.voltage_min or "").strip()
    if text:
        return Sourced(parse_volts(text), SCHEMATIC)
    if part is not None and part.voltage_v is not None:
        return Sourced(float(part.voltage_v), PART)
    if description.voltage_v is not None:
        return Sourced(float(description.voltage_v), DESCRIPTION)
    remembered = parse_volts(defaults.get("cap.voltage_min"))
    if remembered is not None:
        return Sourced(remembered, DEFAULT)
    return Sourced(None, UNSET)


def _upgraded(codes: Sequence[str]) -> Tuple[str, ...]:
    """Each normalized code in *codes* followed by its accepted upgrades
    (:func:`kicadtools.units.dielectric_upgrades`), in order, without repeats."""
    out: List[str] = []
    for code in _codes(codes):
        for c in dielectric_upgrades(code):
            if c not in out:
                out.append(c)
    return tuple(out)


def _dielectrics(intent: CapacitorIntent, capacitance_f: Optional[float],
                 defaults: Mapping[str, str],
                 description: DescriptionEvidence,
                 part: Optional[PartEvidence]) -> Sourced:
    """``Class:Preferred``, else the existing part's dielectric, else the
    description's (each with its upgrades, X5R -> X5R, X7R), else the
    ``cap.dielectric.<band>`` default for the capacitance; unset when none is
    available. A stated value is tagged ``schematic`` even when it holds no
    dielectric code, so it is never replaced by a default."""
    if intent.class_preferred:
        return Sourced(_codes(intent.class_preferred), SCHEMATIC)
    if part is not None and part.dielectrics:
        codes = _upgraded(part.dielectrics)
        if codes:
            return Sourced(codes, PART)
    if description.dielectrics:
        codes = _upgraded(description.dielectrics)
        if codes:
            return Sourced(codes, DESCRIPTION)
    if capacitance_f is None or capacitance_f <= 0:
        return Sourced((), UNSET)
    key = "cap.dielectric." + dielectric_band(capacitance_f)
    codes = _codes(_default(defaults, key))
    return Sourced(codes, DEFAULT) if codes else Sourced((), UNSET)


def _tolerance(intent: CapacitorIntent, dielectrics: Tuple[str, ...],
               defaults: Mapping[str, str],
               part: Optional[PartEvidence]) -> Sourced:
    """The loosest ``Tolerance:Preferred`` entry, else the existing part's
    tolerance, else the ``cap.tolerance.<class>`` default for the dielectrics.
    A stated value with no percentage in it (``"±0.25pF"``) is ``None`` tagged
    ``schematic``: the search applies no tolerance limit and the stated text
    is kept."""
    if intent.tolerance_preferred:
        stated = [
            p for p in (parse_percent(t) for t in intent.tolerance_preferred)
            if p is not None
        ]
        return Sourced(max(stated) if stated else None, SCHEMATIC)
    if part is not None and part.tolerance_pct is not None:
        return Sourced(float(part.tolerance_pct), PART)
    key = "cap.tolerance." + tolerance_class(dielectrics)
    pct = parse_percent(_default(defaults, key))
    return Sourced(pct, DEFAULT) if pct is not None else Sourced(None, UNSET)


def _cap_conflicts(stated: Sequence[Tuple[str, Optional[float]]]) -> List[str]:
    """A line for every pair of ``(source name, farads)`` entries that both
    state a capacitance and differ by more than 0.1 %."""
    known = [(name, f) for name, f in stated if f is not None and f > 0]
    out: List[str] = []
    for i, (name_a, a) in enumerate(known):
        for name_b, b in known[i + 1:]:
            if abs(a - b) > _CAP_MATCH_REL * max(a, b):
                out.append(
                    f"{name_a} says {fmt_farads(a)}; {name_b} says {fmt_farads(b)}"
                )
    return out


def _package_conflicts(stated: Sequence[Tuple[str, Optional[str]]]) -> List[str]:
    """A line for every pair of ``(source name, package)`` entries that both
    state a chip size and differ. An entry with no chip-size code (a radial
    footprint, an unparseable ``Package:Preferred``) states none."""
    known = [(name, _chip(p)) for name, p in stated if p]
    known = [(name, p) for name, p in known if p]
    out: List[str] = []
    for i, (name_a, a) in enumerate(known):
        for name_b, b in known[i + 1:]:
            if a != b:
                out.append(f"{name_a} says {a}; {name_b} says {b}")
    return out


def _description_gaps(intent: CapacitorIntent, footprint: str, value_text: str,
                      value_package: Optional[str],
                      description: DescriptionEvidence,
                      part: Optional[PartEvidence]) -> DescriptionEvidence:
    """The fields of *description* that no other source states: the
    capacitance when there is no Value and no part capacitance, the package
    when there is no footprint, ``Package:Preferred``, Value size suffix or
    part package, the voltage when there is no ``Voltage:Min`` and no part
    rating, the dielectrics when there is no ``Class:Preferred`` and no part
    dielectric. A symbol described completely by its other properties and its
    part gives an empty result."""
    has_part = part is not None
    return DescriptionEvidence(
        capacitance_f=None if value_text or (has_part and part.capacitance_f is not None)
        else description.capacitance_f,
        package=None if (footprint or intent.package_preferred or value_package
                         or (has_part and part.package))
        else description.package,
        voltage_v=None if (str(intent.voltage_min or "").strip()
                           or (has_part and part.voltage_v is not None))
        else description.voltage_v,
        dielectrics=() if (intent.class_preferred or (has_part and part.dielectrics))
        else description.dielectrics,
    )


def description_key(intent: CapacitorIntent, footprint: str,
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
    value_text = str(intent.value or "").strip()
    value_pkg = parse_cap_value(value_text)[1] if value_text else None
    gaps = _description_gaps(intent, (footprint or "").strip(), value_text,
                             value_pkg, description, None)
    return (gaps.capacitance_f, gaps.package, gaps.voltage_v, tuple(gaps.dielectrics))


def capacitor_requirement(
    intent: CapacitorIntent,
    footprint: str,
    defaults: Mapping[str, str],
    *,
    description: Optional[DescriptionEvidence] = None,
    part: Optional[PartEvidence] = None,
) -> CapacitorRequirement:
    """Build the requirement for one capacitor from its *intent*, its
    ``Footprint`` text, the *defaults* mapping (see :func:`capacitor_defaults`),
    and the evidence of its *description* and existing *part*.

    Each field takes the first source that states it:

    * capacitance — Value (plain or library style, ``C_4p7U``) [schematic],
      existing part [part], description [description];
    * package — footprint, ``Package:Preferred``, the Value's size suffix
      [schematic], existing part [part], description [description], the
      ``cap.package`` setting [default];
    * voltage floor — ``Voltage:Min`` [schematic], existing part [part],
      description [description], the ``cap.voltage_min`` setting [default];
    * dielectrics — ``Class:Preferred`` [schematic], existing part [part],
      description [description] (each with its upgrades), band default
      [default];
    * tolerance — ``Tolerance:Preferred`` [schematic], existing part [part],
      class default [default].

    A Value that is stated but does not parse keeps capacitance ``None``
    tagged ``schematic``, and is not compared with the other sources. Every
    pair of sources other than the description stating different capacitances
    (more than 0.1 % apart) or different packages adds a line to
    ``conflicts``.

    The description fills only the fields no other source states
    (:func:`_description_gaps`). A description capacitance or package that
    disagrees with the value used adds a line to ``warnings`` instead of
    ``conflicts``: library boilerplate and descriptions copied from another
    symbol do not block a search."""
    stated = description or DescriptionEvidence()
    footprint = (footprint or "").strip()
    value_text = str(intent.value or "").strip()
    value_f, value_pkg = parse_cap_value(value_text) if value_text else (None, None)
    description = _description_gaps(intent, footprint, value_text, value_pkg,
                                    stated, part)

    if value_text:
        capacitance, cap_source = value_f, SCHEMATIC
    elif part is not None and part.capacitance_f is not None:
        capacitance, cap_source = part.capacitance_f, PART
    elif description.capacitance_f is not None:
        capacitance, cap_source = description.capacitance_f, DESCRIPTION
    else:
        capacitance, cap_source = None, UNSET

    part_label = part.label if part is not None else ""
    conflicts = _cap_conflicts([
        ("Value", value_f),
        (part_label, part.capacitance_f if part is not None else None),
    ])
    conflicts += _package_conflicts([
        ("Footprint", footprint),
        ("Package:Preferred",
         intent.package_preferred[0] if intent.package_preferred else None),
        ("Value", value_pkg),
        (part_label, part.package if part is not None else None),
    ])

    package = _package(intent, footprint, defaults, value_pkg, description, part)
    warnings = [
        f"{line} (Description not used)"
        for line in _cap_conflicts([("Description", stated.capacitance_f),
                                    (_used_label(cap_source, part_label), capacitance)])
    ]
    if package.source != DESCRIPTION:
        warnings += [
            f"{line} (Description not used)"
            for line in _package_conflicts([
                ("Description", stated.package),
                (_used_label(package.source, part_label, footprint), package.value),
            ])
        ]

    dielectrics = _dielectrics(intent, capacitance, defaults, description, part)
    return CapacitorRequirement(
        value_text=value_text,
        capacitance_f=capacitance,
        package=package,
        voltage_min_v=_voltage(intent, defaults, description, part),
        dielectrics=dielectrics,
        tolerance_max_pct=_tolerance(intent, dielectrics.value, defaults, part),
        footprint=footprint,
        capacitance_source=cap_source,
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
    )


def _used_label(source: str, part_label: str, footprint: str = "") -> str:
    """The name of the source a used capacitance or package came from, for a
    warning line."""
    if source == PART:
        return part_label
    if source == SCHEMATIC:
        return "Footprint" if footprint else "Value"
    return "the " + source


@dataclass
class Member:
    """One placed capacitor in a :class:`RequirementGroup`. A subsheet symbol
    instantiated twice gives two members that share a ``symbol_uuid``."""
    reference: str
    source_file: Optional[Path]
    symbol_uuid: Optional[str]
    props: Dict[str, object]
    intent: Any  # the CapacitorIntent

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
        return parse_cap_description(_prop(self.props, "Description"))


def _prop(props: Mapping[str, object], key: str) -> str:
    """A string property, stripped; ``""`` when missing or not a string."""
    value = props.get(key)
    return value.strip() if isinstance(value, str) else ""


@dataclass
class RequirementGroup:
    """Capacitors that state the same values on the schematic, the same
    open Description fields and the same existing part numbers, and so share one
    requirement and one part choice."""
    requirement: CapacitorRequirement
    members: List[Member]

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
                              defaults: Mapping[str, str]) -> CapacitorRequirement:
        """The requirement recomputed from the first member's intent, footprint
        and description, with *part* as the existing part's evidence and
        *defaults* as the settings. Further conflicts can be appended with
        :func:`dataclasses.replace`."""
        first = self.members[0]
        return capacitor_requirement(
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


def group_capacitors(records: Sequence[Any],
                     defaults: Mapping[str, str]) -> List[RequirementGroup]:
    """Group the capacitor records (from
    :func:`kicadtools.resolve.iter_bom_part_records`) by the values stated on
    the schematic, the Description fields those values leave open
    (:func:`description_key`; none for a symbol with a part number) and the
    existing MPN, DigiKey PN and
    LCSC number (compared upper-cased), so symbols with different existing
    parts are separate groups. Each group's requirement includes its
    description evidence and no part evidence. Records whose intent is not a
    ``CapacitorIntent`` are skipped. Groups are ordered by the natural key of
    their first reference."""
    by_key: Dict[Tuple[str, ...], RequirementGroup] = {}
    for rec in records:
        intent = rec.intent
        if not isinstance(intent, CapacitorIntent):
            continue
        pn = extract_part_numbers(rec.props)
        has_pn = bool(pn.mpn or pn.digikey_pn or pn.lcsc)
        key = tuple(_prop(rec.props, k) for k in _GROUP_PROPS) + (
            description_key(intent, _prop(rec.props, "Footprint"),
                            parse_cap_description(_prop(rec.props, "Description")),
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
            req = capacitor_requirement(
                intent, _prop(rec.props, "Footprint"), defaults,
                description=member.description,
            )
            by_key[key] = RequirementGroup(req, [member])
        else:
            group.members.append(member)
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))
