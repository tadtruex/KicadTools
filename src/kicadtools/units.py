"""
Parse and format the electrical values that appear on schematics and in
supplier catalogs.

A schematic holds terse opaque strings (``"10k"``, ``"100nF"``, ``"1%"``);
DigiKey and JLCPCB render the same values their own way (``"10 kOhms"``,
``"0.1 µF"``, ``"±1%"``, ``"0.063W, 1/16W"``). The parsers here reduce every
rendering to a number in SI base units, or ``None`` when the text can't be
parsed. Comparisons are left to the caller, which applies its own relative
tolerance.

The formatters produce the short form used for display and as JLCPCB search
text (``"100nF"``, ``"25V"``). :func:`package_code` extracts the imperial chip
size from a footprint or package string, and :func:`norm_dielectric` /
:func:`dielectric_tokens` normalize ceramic dielectric codes.

:func:`parse_cap_value` reads a capacitor Value in plain (``"100nF"``) or
library style (``"C_4p7U"``, ``"C_10U_805"``); :func:`parse_cap_description`
reads the facts a free-text Description states (``"10uF, 25V, X5R, 805
package"``) into a :class:`~kicadtools.evidence.DescriptionEvidence`.

Pure: no I/O.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from kicadtools.evidence import DescriptionEvidence


# --------------------------------------------------------------------------- #
# Parsers — rendering -> number in SI base units, or None.
# --------------------------------------------------------------------------- #

# Resistance multipliers. Case matters for 'M' (mega) vs 'm' (milli); a letter
# may also stand in for the decimal point (R-notation: "4k7" == 4.7k, "4R7" == 4.7).
_RES_MULT = {"R": 1.0, "r": 1.0, "K": 1e3, "k": 1e3, "M": 1e6, "m": 1e-3, "G": 1e9}


def parse_resistance(text: object) -> Optional[float]:
    """Ohms from a resistance rendering (``"10 kOhms"``, ``"10k"``, ``"4R7"``,
    ``"1M"``, ``"0"``), or ``None`` when it can't be parsed."""
    if text is None:
        return None
    s = re.sub(r"(?i)ohms?", "", str(text))          # drop 'Ohm(s)'
    s = s.replace("Ω", "").replace("±", "")  # drop 'Ω' and '±'
    s = "".join(s.split())                            # drop all whitespace
    if not s:
        return None
    m = re.fullmatch(r"([0-9.]*)([RrKkMmG])?([0-9.]*)", s)
    if not m:
        return None
    head, letter, tail = m.group(1), m.group(2) or "", m.group(3)
    num = f"{head}.{tail}" if tail else head          # letter as decimal point
    try:
        return float(num) * _RES_MULT.get(letter, 1.0)
    except ValueError:
        return None


def parse_percent(text: object) -> Optional[float]:
    """Percent from a tolerance rendering (``"±1%"``, ``"1 %"``, ``"0.1%"``)."""
    if text is None:
        return None
    s = "".join(str(text).replace("±", "").replace("%", "").split())
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_watts(text: object) -> Optional[float]:
    """Watts from a power rendering, taking the first parseable token so a
    compound ``"0.063W, 1/16W"`` (two spellings of one value) parses to ``0.063``,
    a fraction ``"1/4W"`` parses to ``0.25`` and JLCPCB's ``"100mW"`` to ``0.1``."""
    if text is None:
        return None
    for token in str(text).split(","):
        t = re.sub(r"(?i)watts?", "", token)
        t = "".join(t.replace("W", "").replace("w", "").split())
        if not t:
            continue
        scale = 1.0
        if t.endswith("m"):  # "100mW" -> "100m" once the W is gone
            t, scale = t[:-1], 1e-3
        if "/" in t:
            num, _, den = t.partition("/")
            try:
                return float(num) / float(den) * scale
            except (ValueError, ZeroDivisionError):
                continue
        try:
            return float(t) * scale
        except ValueError:
            continue
    return None


# Capacitance multipliers; "u" and "µ" (micro sign) and "μ" (Greek mu) all mean micro.
_CAP_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}


def parse_capacitance(text: object) -> Optional[float]:
    """Farads from a capacitance rendering (``"100nF"``, ``"0.1 µF"``, ``"4.7uF"``,
    ``"4u7"``, ``"22pF"``), or ``None`` when it can't be parsed."""
    if text is None:
        return None
    s = "".join(str(text).split()).replace("F", "").replace("f", "")
    if not s:
        return None
    m = re.fullmatch(r"([0-9.]*)([pnuµμm])?([0-9.]*)", s)
    if not m or not (m.group(1) or m.group(3)):
        return None
    head, letter, tail = m.group(1), m.group(2) or "", m.group(3)
    num = f"{head}.{tail}" if tail else head  # letter as decimal point: "4u7"
    try:
        return float(num) * _CAP_MULT.get(letter, 1.0)
    except ValueError:
        return None


def parse_volts(text: object) -> Optional[float]:
    """Volts from a voltage rendering (``"25V"``, ``"6.3 V"``, ``"1kV"``)."""
    if text is None:
        return None
    s = "".join(str(text).split()).upper().replace("V", "")
    scale = 1.0
    if s.endswith("K"):
        s, scale = s[:-1], 1e3
    try:
        return float(s) * scale
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Formatters — number -> the short form shown and typed into a search.
# --------------------------------------------------------------------------- #

def fmt_ohms(v: float) -> str:
    """``"4.7k"``, ``"1M"``, ``"220"``."""
    for scale, suffix in ((1e6, "M"), (1e3, "k")):
        if v >= scale:
            return f"{round(v / scale, 6):g}{suffix}"
    return f"{round(v, 6):g}"


def fmt_farads(v: float) -> str:
    """``"4.7uF"``, ``"100nF"``, ``"22pF"``."""
    for scale, suffix in ((1e-6, "uF"), (1e-9, "nF")):
        if v >= scale:
            return f"{round(v / scale, 6):g}{suffix}"
    return f"{round(v / 1e-12, 6):g}pF"


def fmt_volts(v: float) -> str:
    """``"25V"``, ``"6.3V"``."""
    return f"{v:g}V"


def fmt_percent(v: float) -> str:
    """``"1%"``, ``"0.1%"``."""
    return f"{v:g}%"


def fmt_watts(v: float) -> str:
    """``"100mW"`` below one watt, ``"2W"`` at or above."""
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


def norm_dielectric(text: object) -> str:
    """A dielectric code upper-cased with everything but letters and digits
    removed (``"x7r"`` -> ``"X7R"``, ``"NP0"`` -> ``"NP0"``). ``"COG"`` and
    ``"NPO"`` (letter O) are the common misspellings of ``"C0G"`` and ``"NP0"``
    (zero) and map to them."""
    s = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    return {"COG": "C0G", "NPO": "NP0"}.get(s, s)


def dielectric_tokens(text: object) -> Tuple[str, ...]:
    """The dielectric codes in a supplier string, each normalized with
    :func:`norm_dielectric`: ``"C0G, NP0"`` -> ``("C0G", "NP0")``,
    ``"X7R"`` -> ``("X7R",)``. Codes are alphanumeric, so any other character
    separates them: JLCPCB writes ``"C0G、NP0"`` (ideographic comma) and
    ``"NP0(C0G)"``. Empty tokens are dropped."""
    out = (norm_dielectric(t) for t in re.split(r"[^A-Za-z0-9]+", str(text or "")))
    return tuple(t for t in out if t)


# The dielectrics acceptable in place of a stated one: the same, or one with a
# tighter capacitance change over a wider temperature range. X7R (±15 %,
# −55…125 °C) dominates X5R (±15 %, −55…85 °C), X6S (±22 %, −55…105 °C) and
# X7S (±22 %, −55…125 °C). C0G and NP0 are two names for one class.
_DIELECTRIC_UPGRADES: Dict[str, Tuple[str, ...]] = {
    "X5R": ("X5R", "X7R"),
    "X6S": ("X6S", "X7R"),
    "X7S": ("X7S", "X7R"),
    "X7R": ("X7R",),
    "C0G": ("C0G", "NP0"),
    "NP0": ("NP0", "C0G"),
}


def dielectric_upgrades(code: object) -> Tuple[str, ...]:
    """*code* (normalized with :func:`norm_dielectric`) followed by the
    dielectrics that dominate it: ``"X5R"`` -> ``("X5R", "X7R")``,
    ``"C0G"`` -> ``("C0G", "NP0")``. A code outside the table maps to itself
    alone; an empty code maps to ``()``."""
    n = norm_dielectric(code)
    if not n:
        return ()
    return _DIELECTRIC_UPGRADES.get(n, (n,))


# --------------------------------------------------------------------------- #
# Capacitor Values and Descriptions as written on the user's boards.
# --------------------------------------------------------------------------- #

# Chip-size codes in the four- or five-digit form, and the three-digit
# shorthand written with the leading zero dropped ("805" for 0805).
_CHIP_SIZES = ("01005", "0201", "0402", "0603", "0805", "1206", "1210",
               "1812", "2010", "2220", "2512")
_CHIP_SHORT = {c[1:]: c for c in _CHIP_SIZES if len(c) == 4 and c[0] == "0"}


def chip_size(token: object) -> Optional[str]:
    """The chip-size code a bare token names: ``"805"`` or ``"0805"`` ->
    ``"0805"``, ``"603"`` -> ``"0603"``, ``"1210"`` -> ``"1210"``; ``None`` for
    any other token."""
    t = str(token if token is not None else "").strip()
    if t in _CHIP_SIZES:
        return t
    return _CHIP_SHORT.get(t)


# "4p7U" / "P1U": a 'p' between digits, or leading, followed by a unit letter is
# a decimal point. Groups: integer digits, fraction digits, unit letter.
_P_DECIMAL_RE = re.compile(r"([0-9]*)[pP]([0-9]+)([nNuUµμ])[fF]?")


def _cap_value_text(text: str) -> str:
    """*text* rewritten for :func:`parse_capacitance`: the 'p' decimal point
    replaced by '.', and the unit letters p/n/u lower-cased ('m' keeps its case
    and its milli meaning)."""
    m = _P_DECIMAL_RE.fullmatch(text)
    if m:
        text = f"{m.group(1) or '0'}.{m.group(2)}{m.group(3)}"
    return re.sub(r"[PNU]", lambda c: c.group(0).lower(), text)


def parse_cap_value(text: object) -> Tuple[Optional[float], Optional[str]]:
    """``(farads, package)`` from a capacitor Value. A plain Value
    (``"100nF"``, ``"0.1uF"``, ``"4p7"`` = 4.7 pF) has no package. Library
    style takes an optional ``"C_"`` / ``"C-"`` prefix and ``_``-separated parts:
    the first is the value, a later part accepted by :func:`chip_size` is the
    package (``"C_10U_805"`` -> ``(10e-6, "0805")``, ``"C_P1U"`` ->
    ``(1e-7, None)``). A size code standing alone (``"0805"``, ``"C_805"``)
    is the package, with no capacitance. Either element is ``None`` when it is
    not stated or does not parse."""
    if text is None:
        return None, None
    s = str(text).strip()
    if chip_size(s):
        return None, chip_size(s)
    farads = parse_capacitance(s)
    if farads is not None:
        return farads, None
    if s[:2] in ("C_", "C-", "c_", "c-"):
        s = s[2:]
    parts = [p for p in s.split("_") if p]
    if not parts:
        return None, None
    if chip_size(parts[0]):
        # "C_0805": a size code alone states the package, not 805 farads.
        return None, chip_size(parts[0])
    package = next((chip_size(p) for p in parts[1:] if chip_size(p)), None)
    return parse_capacitance(_cap_value_text(parts[0])), package


_DESC_CAP_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s?([pPnNuUµμ])(?:[fF](?:arads?)?)?(?![A-Za-z0-9])"
)
_DESC_VOLT_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s?([kK]?)[vV](?:DC|dc)?(?![A-Za-z0-9])"
)
# "25-50V": a range written with one unit; both ends are voltage tokens.
_DESC_VOLT_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?)\s?[-–~]\s?([0-9]+(?:\.[0-9]+)?)\s?([kK]?)[vV](?:DC|dc)?(?![A-Za-z0-9])"
)
# "0603 Metric", "1608Metric": a metric size, whose digits are not an imperial code.
_DESC_METRIC_RE = re.compile(r"(?<![A-Za-z0-9])[0-9]+\s?metric", re.IGNORECASE)
_DESC_DIELECTRIC_RE = re.compile(
    r"(?<![A-Za-z0-9])(X5R|X6S|X7R|X7S|X8R|C0G|NP0|Y5V|Z5U|COG|NPO)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def parse_cap_description(text: object) -> DescriptionEvidence:
    """The facts a capacitor Description states. Capacitance is the first
    capacitance token (``"10uF"``, ``"4.7 uF"``, ``"100nF"``); voltage is the
    lowest voltage token (``"25V - 50V"`` and ``"25-50V"`` -> 25.0); dielectrics are every
    dielectric code present, normalized, de-duplicated and in order; package is
    the first standalone token :func:`chip_size` accepts outside the value,
    voltage and metric-size tokens (``"805 package"`` -> ``"0805"``;
    ``"0603 Metric"`` is not read as 0603). Text stating none of these
    (``"Unpolarized capacitor, small symbol"``) gives empty evidence."""
    s = str(text or "")

    capacitance: Optional[float] = None
    m = _DESC_CAP_RE.search(s)
    if m:
        capacitance = parse_capacitance(m.group(1) + m.group(2).lower())

    volts: List[float] = []
    for r in _DESC_VOLT_RANGE_RE.finditer(s):
        scale = 1e3 if r.group(3) else 1.0
        volts += [float(r.group(1)) * scale, float(r.group(2)) * scale]
    for v in _DESC_VOLT_RE.finditer(s):
        volts.append(float(v.group(1)) * (1e3 if v.group(2) else 1.0))

    dielectrics = tuple(dict.fromkeys(
        norm_dielectric(d.group(1)) for d in _DESC_DIELECTRIC_RE.finditer(s)))

    # Blank out value, voltage and metric-size tokens so their digits are not
    # read as an imperial size.
    masked = _DESC_VOLT_RANGE_RE.sub(" ", s)
    masked = _DESC_METRIC_RE.sub(" ", masked)
    masked = _DESC_VOLT_RE.sub(" ", _DESC_CAP_RE.sub(" ", masked))
    package = next((chip_size(t) for t in re.split(r"[^A-Za-z0-9]+", masked)
                    if chip_size(t)), None)

    return DescriptionEvidence(
        capacitance_f=capacitance,
        voltage_v=min(volts) if volts else None,
        dielectrics=dielectrics,
        package=package,
    )
