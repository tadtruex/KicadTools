"""
Guess a component's intent type from its schematic properties.

Used when a symbol carries no explicit ``Intent:Type`` property. Strategies are
tried in priority order and the first confident guess wins:

  1. footprint library – a footprint drawn from a type-named library
     (``Resistor_SMD``, ``Capacitor_SMD``, ``Diode_SMD``, ``Connector_*`` …) is
     a strong, unambiguous signal.
  2. description        – an unambiguous type word in the ``Description`` text.
  3. reference prefix   – the designator letter(s): ``R``, ``C``, ``D``, ``L`` …

``guess_intent_type`` returns an :class:`IntentGuess` (a lower-cased intent-type
string matching the intent registry keys, plus how it was reached) or ``None``
when nothing is confident enough — leaving the part unclassified for now.

Adding a new signal is just another ``_by_*`` function appended to ``_STRATEGIES``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class IntentGuess:
    """A guessed intent type and a record of how it was reached."""

    intent_type: str          # e.g. "resistor" (matches the intent registry keys)
    basis: str                # human-readable justification, for -v output
    strategy: str             # "footprint" | "description" | "refdes"


# ----------------------------------------------------------------------
# 1. Footprint library  (highest confidence)
# ----------------------------------------------------------------------
# A KiCad footprint id is "Library:Name"; the library half is frequently named
# after the part type. Matched against the library token by prefix. Order
# matters — put more specific prefixes first.
_FOOTPRINT_LIB_TYPES: List[Tuple[str, str]] = [
    ("Resistor", "resistor"),
    ("Capacitor", "capacitor"),
    ("Diode", "diode"),
    ("LED", "led"),
    ("Inductor", "inductor"),
    ("Crystal", "crystal"),
    ("Oscillator", "crystal"),
    ("Connector", "connector"),
    ("Button_Switch", "switch"),
    ("Switch", "switch"),
    ("Fuse", "fuse"),
    ("Ferrite", "ferrite"),
    ("Transformer", "transformer"),
    ("Relay", "relay"),
    ("Battery", "battery"),
]


def _by_footprint(props: Dict[str, object]) -> Optional[IntentGuess]:
    footprint = props.get("Footprint")
    if not isinstance(footprint, str) or ":" not in footprint:
        return None
    library = footprint.split(":", 1)[0]
    lib_lower = library.lower()
    for prefix, intent_type in _FOOTPRINT_LIB_TYPES:
        if lib_lower.startswith(prefix.lower()):
            return IntentGuess(intent_type, f"footprint library {library!r}", "footprint")
    return None


# ----------------------------------------------------------------------
# 2. Description keywords  (used only when unambiguous)
# ----------------------------------------------------------------------
_DESC_KEYWORDS: Dict[str, List[str]] = {
    "resistor": ["resistor"],
    "capacitor": ["capacitor"],
    "diode": ["diode"],
    "led": ["led", "light emitting diode"],
    "inductor": ["inductor", "choke"],
    "crystal": ["crystal", "resonator", "oscillator"],
    "connector": ["connector", "header"],
    "switch": ["switch", "push button", "pushbutton"],
    "battery": ["battery", "coin cell"],
    "fuse": ["fuse"],
    "ferrite": ["ferrite"],
    "transistor": ["transistor", "mosfet"],
}


def _by_description(props: Dict[str, object]) -> Optional[IntentGuess]:
    description = props.get("Description")
    if not isinstance(description, str) or not description.strip():
        return None
    text = description.lower()

    matched: Dict[str, str] = {}
    for intent_type, keywords in _DESC_KEYWORDS.items():
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                matched[intent_type] = kw
                break

    # Only trust the description when it points to exactly one type.
    if len(matched) != 1:
        return None
    intent_type, kw = next(iter(matched.items()))
    return IntentGuess(intent_type, f"description mentions {kw!r}", "description")


# ----------------------------------------------------------------------
# 3. Reference-designator prefix  (last resort)
# ----------------------------------------------------------------------
_REFDES_TYPES: Dict[str, str] = {
    "R": "resistor",
    "RN": "resistor",
    "C": "capacitor",
    "D": "diode",
    "LED": "led",
    "L": "inductor",
    "FB": "ferrite",
    "J": "connector",
    "P": "connector",
    "CN": "connector",
    "Q": "transistor",
    "U": "ic",
    "BT": "battery",
    "SW": "switch",
    "S": "switch",
    "Y": "crystal",
    "X": "crystal",
    "F": "fuse",
    "T": "transformer",
    "K": "relay",
}


def _by_refdes(props: Dict[str, object]) -> Optional[IntentGuess]:
    reference = props.get("Reference")
    if not isinstance(reference, str):
        return None
    m = re.match(r"^([A-Za-z]+)", reference)
    if not m:
        return None
    prefix = m.group(1).upper()
    intent_type = _REFDES_TYPES.get(prefix)
    if intent_type is None:
        return None
    return IntentGuess(intent_type, f"reference prefix {prefix!r}", "refdes")


# ----------------------------------------------------------------------
# Strategy chain – tried in priority order.
# ----------------------------------------------------------------------
_STRATEGIES: List[Callable[[Dict[str, object]], Optional[IntentGuess]]] = [
    _by_footprint,
    _by_description,
    _by_refdes,
]


def guess_intent_type(props: Dict[str, object]) -> Optional[IntentGuess]:
    """Return the first confident :class:`IntentGuess`, or ``None``."""
    for strategy in _STRATEGIES:
        guess = strategy(props)
        if guess is not None:
            return guess
    return None
