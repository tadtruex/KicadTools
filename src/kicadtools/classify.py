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
# LED detection  (shared by the footprint and description strategies)
# ----------------------------------------------------------------------
# LEDs are diodes, so a diode symbol/footprint alone can't tell them apart —
# the give-away is in the text: the word "LED", a wavelength, or a colour.
# A colour word ("red", "green", …) is common in non-LED contexts ("white
# ceramic capacitor"), so it's only trusted where the part is already known to
# be diode-ish; hence the ``allow_color`` gate.
_LED_COLORS: List[str] = [
    "warm white", "cool white", "infrared", "ultraviolet",
    "red", "green", "blue", "yellow", "amber", "white", "orange", "rgb",
]
_LED_WAVELENGTH_RE = re.compile(r"\b\d{3,4}\s*nm\b")


def _led_signal(props: Dict[str, object], *, allow_color: bool) -> Optional[str]:
    """Return a human-readable LED signal from the Description/Value text, else
    ``None``. Colour words count only when *allow_color* is set."""
    blob = " ".join(str(props.get(k, "")) for k in ("Description", "Value"))
    text = re.sub(r"[^a-z0-9]+", " ", blob.lower()).strip()
    if not text:
        return None
    if re.search(r"\bled\b", text) or "light emitting diode" in text:
        return "text mentioning 'LED'"
    m = _LED_WAVELENGTH_RE.search(text)
    if m:
        return f"an LED wavelength {m.group(0).strip()!r} in the text"
    if allow_color:
        for color in _LED_COLORS:
            if re.search(r"\b" + re.escape(color) + r"\b", text):
                return f"the LED colour {color!r} in the text"
    return None


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
            if intent_type == "diode":
                # An LED drawn with a plain diode footprint: let the text override.
                led = _led_signal(props, allow_color=True)
                if led is not None:
                    return IntentGuess(
                        "led", f"diode-style footprint {library!r} but {led}", "footprint"
                    )
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
    "transistor": [
        "transistor", "transistor array", "mosfet", "fet", "jfet", "igbt",
        "bjt", "npn", "pnp", "bipolar", "darlington",
        "n-channel", "p-channel", "nmos", "pmos",
    ],
}


def _by_description(props: Dict[str, object]) -> Optional[IntentGuess]:
    matched: Dict[str, str] = {}

    description = props.get("Description")
    if isinstance(description, str) and description.strip():
        text = description.lower()
        for intent_type, keywords in _DESC_KEYWORDS.items():
            for kw in keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", text):
                    matched[intent_type] = f"description mentions {kw!r}"
                    break

    # LED evidence can also live in the Value ("LED_Green", "1515 rgb led") and
    # covers wavelengths the keyword table doesn't list. Colours are excluded
    # here (too common elsewhere); they're only trusted in the diode-footprint
    # case in ``_by_footprint``.
    led = _led_signal(props, allow_color=False)
    if led is not None:
        matched["led"] = led

    # Only trust the text when it points to exactly one type.
    if len(matched) != 1:
        return None
    intent_type, basis = next(iter(matched.items()))
    return IntentGuess(intent_type, basis, "description")


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
