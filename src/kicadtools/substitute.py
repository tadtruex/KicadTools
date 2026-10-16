"""
Find an in-stock JLCPCB part that is parametrically equivalent to a BOM part.

Used when the part a symbol names is out of stock, short for the build, or no
longer made. Resistors and capacitors only for now — the types whose
interchangeability is fully described by a handful of parameters.

The search spec is assembled per parameter, from the first source that has it:

1. **The current part** — its JLCPCB catalog attributes, or, when JLCPCB has
   none (a ``C9900…`` placeholder, or a part with no LCSC number), the DigiKey
   parameters of the symbol's MPN / DigiKey PN. The part already chosen is the
   most complete statement of what the design needs.
2. **The intent** — ``Value``, ``Voltage:Min``, ``Tolerance:Preferred`` … on the
   symbol, for whatever the current part didn't supply.

The package always comes from the KiCad footprint (``C_0603_1608Metric`` →
``0603``), since a substitute that doesn't fit the pads is useless.

Each parameter has a rule: resistance and capacitance must be **equal**; voltage
and power are **floors** (the substitute may be rated higher); tolerance is a
**ceiling** (the substitute may be tighter); dielectric must be **one of** an
allowed set (the current one, or one strictly better — X5R → X7R). JLCPCB's
keyword search ranks by text relevance, so every hit is re-checked here against
its parsed attributes, and one that lacks a constrained attribute is rejected.

Pure apart from :func:`search_substitutes`, whose network calls are injected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from kicadtools.confirm import (
    parse_capacitance,
    parse_percent,
    parse_resistance,
    parse_volts,
    parse_watts,
)
from kicadtools.intent import ElectricalIntent
from kicadtools.suppliers import JlcPart

SUPPORTED_TYPES = ("resistor", "capacitor")

# Schematic property holding acceptable alternate DigiKey part numbers,
# comma-separated. The original part-number fields are never rewritten.
ALTERNATES_KEY = "Alternates"

# Default number of boards a build places, for "is there enough stock".
DEFAULT_BUILD_QTY = 25

# Stock verdicts for a part against the quantity a build needs.
STOCK_OUT = "out"  # zero stock, or JLCPCB won't sell it
STOCK_LOW = "low"  # some stock, less than the build needs
STOCK_OK = "ok"


def stock_level(part: Optional[JlcPart], needed: int) -> Optional[str]:
    """:data:`STOCK_OUT` / :data:`STOCK_LOW` / :data:`STOCK_OK` for *part*
    against *needed* units, or ``None`` when the part hasn't been looked up."""
    if part is None:
        return None
    if not part.orderable:
        return STOCK_OUT
    if part.stock < needed:
        return STOCK_LOW
    return STOCK_OK


# --------------------------------------------------------------------------- #
# Formatting — how a wanted value is shown, and how it is typed into the search.
# --------------------------------------------------------------------------- #

def _fmt_ohms(v: float) -> str:
    for scale, suffix in ((1e6, "M"), (1e3, "k")):
        if v >= scale:
            return f"{round(v / scale, 6):g}{suffix}"
    return f"{round(v, 6):g}"


def _fmt_farads(v: float) -> str:
    for scale, suffix in ((1e-6, "uF"), (1e-9, "nF")):
        if v >= scale:
            return f"{round(v / scale, 6):g}{suffix}"
    return f"{round(v / 1e-12, 6):g}pF"


def _fmt_volts(v: float) -> str:
    return f"{v:g}V"


def _fmt_percent(v: float) -> str:
    return f"{v:g}%"


def _fmt_watts(v: float) -> str:
    return f"{round(v * 1000, 6):g}mW" if v < 1 else f"{v:g}W"


# --------------------------------------------------------------------------- #
# Package and dielectric normalization.
# --------------------------------------------------------------------------- #

_IMPERIAL_RE = re.compile(
    r"(?<![0-9])(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)(?![0-9])"
)


def package_code(text: object) -> Optional[str]:
    """The imperial chip-size code in *text* (a KiCad footprint like
    ``Capacitor_SMD:C_0603_1608Metric``, JLCPCB's ``0603``, DigiKey's
    ``0603 (1608 Metric)``), or ``None``. Only the name after a ``Library:``
    prefix is searched, so the library name can't contribute a false match."""
    s = str(text or "")
    s = s.split(":", 1)[1] if ":" in s else s
    m = _IMPERIAL_RE.search(s)
    return m.group(1) if m else None


def _norm_dielectric(text: object) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    return "C0G" if s == "COG" else s


# The dielectrics a substitute may use in place of the current one: the same, or
# one with a tighter capacitance change over a wider temperature range. X7R
# (±15 %, −55…125 °C) dominates X5R (±15 %, −55…85 °C), X6S (±22 %, −55…105 °C)
# and X7S (±22 %, −55…125 °C). C0G and NP0 are two names for one class.
_DIELECTRIC_UPGRADES: Dict[str, Tuple[str, ...]] = {
    "X5R": ("X5R", "X7R"),
    "X6S": ("X6S", "X7R"),
    "X7S": ("X7S", "X7R"),
    "X7R": ("X7R",),
    "C0G": ("C0G", "NP0"),
    "NP0": ("NP0", "C0G"),
}


# --------------------------------------------------------------------------- #
# Parameter table.
# --------------------------------------------------------------------------- #

EQ = "eq"          # must equal (within 0.1 %)
MIN = "min"        # substitute's rating must be at least this
MAX = "max"        # substitute's value must be at most this
ONE_OF = "one_of"  # substitute's value must be in an allowed set

_RULE_PREFIX = {EQ: "", MIN: "≥ ", MAX: "≤ ", ONE_OF: ""}


@dataclass(frozen=True)
class _Param:
    name: str
    label: str
    rule: str
    jlc_attr: str                                  # JLCPCB attribute_name_en
    dk_attr: str                                   # DigiKey ParameterText
    required: bool
    parse: Optional[Callable[[object], Optional[float]]] = None  # None for ONE_OF
    fmt: Optional[Callable[[float], str]] = None


_PARAMS: Dict[str, Tuple[_Param, ...]] = {
    "resistor": (
        _Param("resistance", "Resistance", EQ, "Resistance", "Resistance", True,
               parse_resistance, _fmt_ohms),
        _Param("tolerance", "Tolerance", MAX, "Tolerance", "Tolerance", False,
               parse_percent, _fmt_percent),
        _Param("power", "Power", MIN, "Power(Watts)", "Power (Watts)", False,
               parse_watts, _fmt_watts),
    ),
    "capacitor": (
        _Param("capacitance", "Capacitance", EQ, "Capacitance", "Capacitance", True,
               parse_capacitance, _fmt_farads),
        # A capacitor substitute with an unknown voltage floor could be any
        # rating at all, so the voltage is required.
        _Param("voltage", "Voltage", MIN, "Voltage Rating", "Voltage - Rated", True,
               parse_volts, _fmt_volts),
        _Param("dielectric", "Dielectric", ONE_OF, "Temperature Coefficient",
               "Temperature Coefficient", False),
        _Param("tolerance", "Tolerance", MAX, "Tolerance", "Tolerance", False,
               parse_percent, _fmt_percent),
    ),
}


def _intent_raw(intent: Optional[ElectricalIntent], name: str):
    """The intent's value for parameter *name*: a string, a list of strings
    (``Tolerance:Preferred``, ``Class:Preferred``), or ``None``."""
    if intent is None:
        return None
    attr = {
        "resistance": "value",
        "capacitance": "value",
        "tolerance": "tolerance_preferred",
        "power": "power_min",
        "voltage": "voltage_min",
        "dielectric": "class_preferred",
    }[name]
    return getattr(intent, attr, None) or None


# --------------------------------------------------------------------------- #
# The search spec.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Constraint:
    """One parameter a substitute must satisfy, and where the value came from."""
    name: str
    label: str
    rule: str
    value: Optional[float] = None       # numeric rules
    choices: Tuple[str, ...] = ()       # ONE_OF
    text: str = ""                      # the value as typed into a search: "100nF"
    source: str = ""                    # "current part (JLCPCB)", "intent", …

    @property
    def shown(self) -> str:
        """The requirement for display: ``"≥ 25V"``, ``"X7R or X5R"``."""
        if self.rule == ONE_OF:
            return " or ".join(self.choices)
        return f"{_RULE_PREFIX[self.rule]}{self.text}"


@dataclass
class SearchSpec:
    """What a substitute must be: type, package, and per-parameter constraints."""
    intent_type: str
    package: Optional[str]
    package_source: str = ""
    constraints: List[Constraint] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)  # required, no source had it

    @property
    def searchable(self) -> bool:
        return (
            self.intent_type in SUPPORTED_TYPES
            and bool(self.package)
            and not self.missing
        )

    def problem(self) -> str:
        """Why the spec can't be searched, or ``""``."""
        if self.intent_type not in SUPPORTED_TYPES:
            return (
                f"substitutes can only be found for resistors and capacitors "
                f"(this part's intent type is {self.intent_type or 'unknown'!r})"
            )
        if not self.package:
            return "no chip-size package (0603, 0805, …) found in the footprint"
        if self.missing:
            return (
                "no value for " + ", ".join(self.missing)
                + " from the current part or the intent"
            )
        return ""

    def keywords(self) -> List[str]:
        """The JLCPCB search texts: the defining value and the package, once per
        allowed dielectric (relevance ranking buries an X7R part under a search
        for X6S, so each allowed one gets its own search). Floors and ceilings
        are left out of the text (a higher-rated part is a valid substitute) and
        checked on the results."""
        words: List[str] = [c.text for c in self.constraints if c.rule == EQ]
        if self.package:
            words.append(self.package)
        base = " ".join(words)
        choices = next(
            (c.choices for c in self.constraints if c.rule == ONE_OF and c.choices), ()
        )
        return [f"{base} {d}" for d in choices] if choices else [base]


def _constraint_from(p: _Param, raw, source: str, *, from_part: bool) -> Optional[Constraint]:
    if raw is None or raw == "" or raw == []:
        return None
    if p.rule == ONE_OF:
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        normed = [_norm_dielectric(v) for v in values if _norm_dielectric(v)]
        if not normed:
            return None
        if from_part and len(normed) == 1:
            # The current part's dielectric admits the upgrades.
            choices = _DIELECTRIC_UPGRADES.get(normed[0], (normed[0],))
        else:
            choices = tuple(dict.fromkeys(normed))  # the intent's list, as given
        return Constraint(p.name, p.label, p.rule, choices=choices, source=source)

    if isinstance(raw, (list, tuple)):
        # A preferred-tolerance list: the loosest one listed is the ceiling.
        parsed = [v for v in (p.parse(x) for x in raw) if v is not None]
        value = max(parsed) if parsed else None
    else:
        value = p.parse(raw)
    if value is None:
        return None
    return Constraint(p.name, p.label, p.rule, value=value, text=p.fmt(value), source=source)


def build_spec(
    intent_type: str,
    footprint: str,
    *,
    part_attrs: Optional[Mapping[str, str]] = None,
    part_source: Optional[str] = None,
    part_package: str = "",
    intent: Optional[ElectricalIntent] = None,
) -> SearchSpec:
    """Assemble the :class:`SearchSpec` for one part.

    *part_attrs* are the current part's attributes, from JLCPCB
    (``part_source="jlcpcb"``) or DigiKey (``"digikey"``) — each names its
    parameters differently. For each parameter the current part's value is used
    when present and parseable; otherwise the *intent*'s. *part_package* is the
    current part's package text, the fallback when *footprint* names no size.
    """
    intent_type = (intent_type or "").lower()
    source_label = {"jlcpcb": "current part (JLCPCB)", "digikey": "current part (DigiKey)"}
    package = package_code(footprint)
    package_source = "footprint"
    if package is None and package_code(part_package):
        package, package_source = package_code(part_package), source_label.get(part_source, "")

    spec = SearchSpec(intent_type, package, package_source if package else "")
    for p in _PARAMS.get(intent_type, ()):
        c = None
        if part_attrs:
            attr = p.jlc_attr if part_source == "jlcpcb" else p.dk_attr
            c = _constraint_from(
                p, part_attrs.get(attr), source_label.get(part_source, ""), from_part=True
            )
        if c is None:
            c = _constraint_from(p, _intent_raw(intent, p.name), "intent", from_part=False)
        if c is not None:
            spec.constraints.append(c)
        elif p.required:
            spec.missing.append(p.label)
    return spec


# --------------------------------------------------------------------------- #
# Matching and ranking.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Check:
    """How a candidate met one requirement, for display."""
    label: str
    wanted: str
    have: str


@dataclass
class Candidate:
    part: JlcPart
    checks: List[Check]


def _param(intent_type: str, name: str) -> _Param:
    return next(p for p in _PARAMS[intent_type] if p.name == name)


def evaluate(spec: SearchSpec, part: JlcPart) -> Optional[Candidate]:
    """*part* as a :class:`Candidate` when it meets every requirement in *spec*,
    else ``None``. A part missing an attribute the spec constrains is rejected —
    an unknown rating is not an acceptable one."""
    if package_code(part.package) != spec.package:
        return None
    checks = [Check("Package", spec.package or "", part.package)]
    for c in spec.constraints:
        p = _param(spec.intent_type, c.name)
        raw = part.attributes.get(p.jlc_attr)
        if raw is None:
            return None
        if c.rule == ONE_OF:
            if _norm_dielectric(raw) not in c.choices:
                return None
        else:
            v = p.parse(raw)
            if v is None or c.value is None:
                return None
            if c.rule == EQ and abs(v - c.value) > 1e-3 * max(abs(v), abs(c.value)):
                return None
            if c.rule == MIN and v < c.value * (1 - 1e-6):
                return None
            if c.rule == MAX and v > c.value * (1 + 1e-6):
                return None
        checks.append(Check(c.label, c.shown, raw))
    return Candidate(part, checks)


def _rank(c: Candidate, needed: int) -> tuple:
    """Lower is better: enough stock for the build, then Basic (no per-part
    setup fee) over Extended, then JLCPCB-preferred, then cheaper, then more
    stock."""
    p = c.part
    return (
        0 if p.stock >= needed else 1,
        0 if p.is_basic else 1,
        0 if p.preferred else 1,
        p.unit_price,
        -p.stock,
    )


def find_substitutes(
    spec: SearchSpec,
    parts: Iterable[JlcPart],
    needed: int,
    exclude: Iterable[str] = (),
) -> List[Candidate]:
    """The orderable *parts* that satisfy *spec*, best first (see :func:`_rank`).
    Codes in *exclude* (the current part) and repeats are dropped."""
    skip = {e.upper() for e in exclude if e}
    seen = set()
    out: List[Candidate] = []
    for part in parts:
        code = part.lcsc.upper()
        if not code or code in skip or code in seen or not part.orderable:
            continue
        seen.add(code)
        cand = evaluate(spec, part)
        if cand is not None:
            out.append(cand)
    out.sort(key=lambda c: _rank(c, needed))
    return out


# --------------------------------------------------------------------------- #
# Orchestration (network calls injected).
# --------------------------------------------------------------------------- #

@dataclass
class SubstituteSearch:
    """The result of one substitute search: the spec it used and what matched."""
    spec: SearchSpec
    candidates: List[Candidate] = field(default_factory=list)
    examined: int = 0                       # search hits checked against the spec
    current: Optional[JlcPart] = None       # the current part's catalog entry


def search_substitutes(
    *,
    intent: Optional[ElectricalIntent],
    footprint: str,
    lcsc: str,
    mpn: str,
    digikey_pn: str,
    needed: int,
    part_lookup: Callable[[str], Optional[JlcPart]],
    search: Callable[[str], Sequence[JlcPart]],
    digikey=None,
) -> SubstituteSearch:
    """Build the spec for one BOM part and search JLCPCB for substitutes.

    *part_lookup* maps an LCSC code to its :class:`JlcPart`; *search* maps a
    keyword to catalog hits; *digikey* is anything with ``configured`` and
    ``product_for(pn)`` (a :class:`~kicadtools.suppliers.DigiKeyClient`), used
    only when JLCPCB has no attributes for the current part.
    """
    current = part_lookup(lcsc) if lcsc else None
    attrs: Optional[Mapping[str, str]] = None
    source: Optional[str] = None
    part_package = ""
    if current is not None and current.attributes:
        attrs, source, part_package = current.attributes, "jlcpcb", current.package
    elif digikey is not None and getattr(digikey, "configured", False) and (mpn or digikey_pn):
        product = digikey.product_for(mpn or digikey_pn)
        if product is not None and product.parameters:
            attrs, source = product.parameters, "digikey"
            part_package = (
                product.param("Supplier Device Package")
                or product.param("Package / Case")
                or ""
            )

    intent_type = intent.intent_type if intent is not None else ""
    spec = build_spec(
        intent_type,
        footprint,
        part_attrs=attrs,
        part_source=source,
        part_package=part_package,
        intent=intent,
    )
    if not spec.searchable:
        return SubstituteSearch(spec, current=current)
    hits: List[JlcPart] = []
    for kw in spec.keywords():
        hits.extend(search(kw))
    return SubstituteSearch(
        spec,
        find_substitutes(spec, hits, needed, exclude=[lcsc]),
        examined=len(hits),
        current=current,
    )


def merge_alternates(existing: object, digikey_pn: str) -> str:
    """*existing* ``Alternates`` text with *digikey_pn* appended, comma-separated.
    Already-listed numbers (compared case-insensitively) aren't repeated."""
    items = [s.strip() for s in str(existing or "").split(",") if s.strip()]
    if digikey_pn and digikey_pn.strip().upper() not in {i.upper() for i in items}:
        items.append(digikey_pn.strip())
    return ", ".join(items)
