"""
Confirm a typed intent against its exact DigiKey product, and propose fills.

When a symbol names a specific supplier part number, we fetch that part's exact
parametric set from DigiKey (the parametric source of truth) and check it against
the minimum spec the schematic author wrote on the symbol. Each intent field is:

* **confirmed** — the schematic value agrees with DigiKey's,
* a **conflict** — they disagree (the named part isn't what the symbol asked
  for), or
* a **fill** — the symbol left the field blank and DigiKey supplies it.

Values are compared through small per-parameter normalizers because DigiKey
renders them its own way (``"10 kOhms"``, ``"±1%"``, ``"0.063W, 1/16W"``) while a
schematic holds terse opaque strings (``"10k"``, ``"1%"``). The parsers are in
:mod:`kicadtools.units`; the intent model keeps values opaque on purpose.

Resistors only for now; other types are added by writing their :class:`_FieldSpec`
list. This module is GUI-agnostic and does no network I/O: :func:`confirm_resistor`
takes an already-fetched :class:`~kicadtools.suppliers.DigiKeyProduct`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Tuple

from kicadtools.intent import ResistorIntent
from kicadtools.suppliers import DigiKeyProduct
# parse_capacitance and parse_volts are unused here; they stay importable from
# kicadtools.confirm for existing callers.
from kicadtools.units import (  # noqa: F401
    parse_capacitance,
    parse_percent,
    parse_resistance,
    parse_volts,
    parse_watts,
)


# --------------------------------------------------------------------------- #
# Value normalizers — DigiKey rendering  <->  opaque intent string.
#
# The parsers are defined in kicadtools.units and re-exported here. Each returns
# a comparable number (SI base units) or ``None`` when the text can't be parsed.
# Comparisons downstream use a small relative epsilon so "10 kOhms" and "10k"
# agree despite rounding in DigiKey's rendering.
# --------------------------------------------------------------------------- #



def _close(a: float, b: float, rel: float = 0.01) -> bool:
    """True when *a* and *b* agree within a small relative tolerance."""
    if a == 0.0 and b == 0.0:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b))


# --------------------------------------------------------------------------- #
# Per-field match strategies. Each returns True (agree), False (conflict), or
# None ("can't tell" — an unparseable value on either side, treated as UNKNOWN
# rather than a false conflict).
# --------------------------------------------------------------------------- #

def _match_resistance(intent_raw: object, dk_raw: object) -> Optional[bool]:
    a, b = parse_resistance(intent_raw), parse_resistance(dk_raw)
    if a is None or b is None:
        return None
    return _close(a, b)


def _match_tolerance(preferred: List[str], dk_raw: object) -> Optional[bool]:
    """A part is acceptable when its tolerance is at least as tight as the
    loosest one the author would accept (the largest listed percentage)."""
    have = parse_percent(dk_raw)
    prefs = [p for p in (parse_percent(x) for x in preferred) if p is not None]
    if have is None or not prefs:
        return None
    return have <= max(prefs) * 1.0001


def _match_power(power_min: object, dk_raw: object) -> Optional[bool]:
    """``power_min`` is a floor: the part must be rated at least that high."""
    need, have = parse_watts(power_min), parse_watts(dk_raw)
    if need is None or have is None:
        return None
    return have >= need * 0.9999


def _match_package(footprint: object, dk_raw: object) -> Optional[bool]:
    """DigiKey's terse package code (``"0402"``) must appear as a standalone
    digit run in the KiCad footprint name (``"…:R_0402_1005Metric"``)."""
    code = str(dk_raw).strip().lower()
    if not code:
        return None
    return re.search(rf"(?<!\d){re.escape(code)}(?!\d)", str(footprint).lower()) is not None


# --------------------------------------------------------------------------- #
# Field specifications: which intent attribute maps to which DigiKey parameter,
# how to compare them, and (for a blank field) how to canonicalize DigiKey's
# rendering into the terse value we'd write back onto the symbol.
# --------------------------------------------------------------------------- #

def _canon_identity(dk_raw: str) -> str:
    return dk_raw


def _canon_tolerance(dk_raw: str) -> str:
    return dk_raw.replace("±", "").strip()      # "±1%" -> "1%"


def _canon_power(dk_raw: str) -> str:
    return dk_raw.split(",")[0].strip()              # "0.063W, 1/16W" -> "0.063W"


@dataclass(frozen=True)
class _FieldSpec:
    attr: str                              # ResistorIntent attribute name
    prop_key: str                          # schematic property a fill writes to
    label: str                             # human-readable label
    dk_params: Tuple[str, ...]             # DigiKey ParameterText, in preference order
    match: Callable[[object, object], Optional[bool]]
    canonical: Callable[[str], str] = _canon_identity
    fillable: bool = True                  # may a blank field be filled from DigiKey?
    is_list: bool = False                  # the intent attribute is a list


_RESISTOR_FIELDS: Tuple[_FieldSpec, ...] = (
    # ``value`` is required on a ResistorIntent, so it is confirm-only (never filled).
    _FieldSpec("value", "Value", "resistance", ("Resistance",),
               _match_resistance, fillable=False),
    _FieldSpec("tolerance_preferred", "Tolerance:Preferred", "tolerance",
               ("Tolerance",), _match_tolerance,
               canonical=_canon_tolerance, is_list=True),
    _FieldSpec("power_min", "Power:Min", "power rating", ("Power (Watts)",),
               _match_power, canonical=_canon_power),
    # The KiCad footprint is a full ``lib:name`` we won't overwrite with DigiKey's
    # bare package code, so package is confirm-only.
    _FieldSpec("footprint", "Footprint", "package",
               ("Supplier Device Package", "Package / Case"),
               _match_package, fillable=False),
)


class Outcome(str, Enum):
    CONFIRMED = "confirmed"  # symbol value agrees with DigiKey
    CONFLICT = "conflict"    # symbol value disagrees with DigiKey
    FILL = "fill"            # symbol left it blank; DigiKey supplies it
    UNKNOWN = "unknown"      # nothing to compare, or unparseable on either side


@dataclass
class FieldCheck:
    """The verdict for one intent field against its DigiKey parameter."""
    attr: str
    prop_key: str                 # schematic property key a FILL writes to
    label: str
    intent_value: Optional[str]   # raw, as on the symbol (a list is joined)
    digikey_value: Optional[str]  # raw ValueText from DigiKey
    outcome: Outcome
    proposed: Optional[str] = None  # canonical value to write back — FILL only


@dataclass
class ConfirmResult:
    """The full confirm/fill verdict for one part."""
    reference: str
    product: DigiKeyProduct
    checks: List[FieldCheck] = field(default_factory=list)

    @property
    def conflicts(self) -> List[FieldCheck]:
        return [c for c in self.checks if c.outcome is Outcome.CONFLICT]

    @property
    def fills(self) -> List[FieldCheck]:
        return [c for c in self.checks if c.outcome is Outcome.FILL]

    @property
    def confirmed(self) -> List[FieldCheck]:
        return [c for c in self.checks if c.outcome is Outcome.CONFIRMED]

    @property
    def ok(self) -> bool:
        """True when no field conflicts (fills are fine — they're improvements)."""
        return not self.conflicts

    def summary(self) -> str:
        """One-line human summary of what agreed, clashed, and can be filled."""
        parts: List[str] = []
        if self.confirmed:
            parts.append("confirmed " + ", ".join(c.label for c in self.confirmed))
        for c in self.conflicts:
            parts.append(
                f"CONFLICT {c.label} (symbol {c.intent_value!r} vs DigiKey {c.digikey_value!r})"
            )
        for c in self.fills:
            parts.append(f"fill {c.label} ← {c.proposed!r}")
        body = "; ".join(parts) if parts else "nothing to check"
        return f"{self.reference}: {body}"


def confirm_resistor(intent: ResistorIntent, product: DigiKeyProduct) -> ConfirmResult:
    """Check a resistor's intent fields against its resolved DigiKey product.

    Pure and offline: the caller fetches *product* (see
    :meth:`kicadtools.resolve.Resolver.confirm`). Every field yields a
    :class:`FieldCheck`; blanks that DigiKey can fill carry a canonical
    ``proposed`` value ready for the change-set (task #6).
    """
    checks: List[FieldCheck] = []
    for spec in _RESISTOR_FIELDS:
        raw = getattr(intent, spec.attr)
        intent_present = bool(raw)
        dk_raw = next(
            (v for v in (product.param(p) for p in spec.dk_params) if v), None
        )

        proposed: Optional[str] = None
        if intent_present and dk_raw is not None:
            verdict = spec.match(raw, dk_raw)
            outcome = (
                Outcome.UNKNOWN if verdict is None
                else Outcome.CONFIRMED if verdict
                else Outcome.CONFLICT
            )
        elif not intent_present and dk_raw is not None and spec.fillable:
            outcome = Outcome.FILL
            proposed = spec.canonical(dk_raw)
        else:
            outcome = Outcome.UNKNOWN

        intent_str = ", ".join(raw) if spec.is_list else (raw if raw else None)
        checks.append(
            FieldCheck(
                spec.attr, spec.prop_key, spec.label,
                intent_str, dk_raw, outcome, proposed,
            )
        )

    return ConfirmResult(intent.reference, product, checks)
