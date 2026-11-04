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
:func:`parse_res_value` and :func:`parse_res_description` do the same for
resistors (``"R_49p9K"``, ``"10K, 603, 1%"``).

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
# Each code and the codes that dominate it: the same or a wider temperature
# range with the same or a tighter change (R ±15 % beats S ±22 %). X6S and
# X7S are not upgrades of X5R: their change is looser.
_DIELECTRIC_UPGRADES: Dict[str, Tuple[str, ...]] = {
    "X5R": ("X5R", "X7R", "X8R"),
    "X6S": ("X6S", "X7R", "X8R"),
    "X7S": ("X7S", "X7R", "X8R"),
    "X7R": ("X7R", "X8R"),
    "X8R": ("X8R",),
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
        nominal=capacitance,
        rating=min(volts) if volts else None,
        dielectrics=dielectrics,
        package=package,
    )


# --------------------------------------------------------------------------- #
# Resistor Values and Descriptions as written on the user's boards.
# --------------------------------------------------------------------------- #

# "49p9K": a 'p' between digits followed by a multiplier letter is a decimal
# point. Groups: integer digits, fraction digits, multiplier letter.
_RES_P_DECIMAL_RE = re.compile(r"([0-9]*)[pP]([0-9]+)([RrKkMmG]?)")


def _res_value_text(text: str) -> str:
    """*text* rewritten for :func:`parse_resistance`: the 'p' decimal point
    replaced by '.'."""
    m = _RES_P_DECIMAL_RE.fullmatch(text)
    if m:
        return f"{m.group(1) or '0'}.{m.group(2)}{m.group(3)}"
    return text


def parse_res_value(text: object) -> Tuple[Optional[float], Optional[str]]:
    """``(ohms, package)`` from a resistor Value. A plain Value (``"10K"``,
    ``"4k7"``, ``"1"``, ``"10M"`` = 10 MΩ) has no package. Library style takes
    an optional ``"R_"`` / ``"R-"`` prefix and ``_``-separated parts: the first
    is the value, with ``p`` as a decimal point (``"R_49p9K"`` = 49.9 kΩ), a
    later part accepted by :func:`chip_size` is the package (``"R_10K_805"`` ->
    ``(10e3, "0805")``). A size code standing alone (``"R_0805"``) is the
    package, with no resistance. Either element is ``None`` when it is not
    stated or does not parse."""
    if text is None:
        return None, None
    s = str(text).strip()
    ohms = parse_resistance(s)
    if ohms is not None:
        return ohms, None
    if s[:2] in ("R_", "R-", "r_", "r-"):
        s = s[2:]
    parts = [p for p in s.split("_") if p]
    if not parts:
        return None, None
    if chip_size(parts[0]):
        return None, chip_size(parts[0])
    package = next((chip_size(p) for p in parts[1:] if chip_size(p)), None)
    return parse_resistance(_res_value_text(parts[0])), package


# A resistance written with its multiplier or unit apart ("10 kOhms",
# "4.7 k"): the space is removed before the text is split into tokens.
_DESC_RES_SPACE_RE = re.compile(r"([0-9])\s+(?=(?:[kKMG]|[Oo]hm|Ω))")
_DESC_POWER_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?(?:/[0-9]+)?)\s?(m?)W(?:atts?)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DESC_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9.])±?([0-9]+(?:\.[0-9]+)?)\s?%")


def parse_res_description(text: object) -> DescriptionEvidence:
    """The facts a resistor Description states: tolerance from the first
    percentage (``"1%"``), power from the first wattage (``"1/4W"``,
    ``"100mW"``), package from the first token :func:`chip_size` accepts
    (``"603"`` -> ``"0603"``), and resistance from the first other token that
    :func:`parse_res_value` reads (``"10K"``, ``"49.9K"``, ``"330"``). Text
    stating none of these (``"Resistor, small US symbol"``) gives empty
    evidence."""
    s = str(text or "")

    tolerance: Optional[float] = None
    m = _DESC_PERCENT_RE.search(s)
    if m:
        tolerance = float(m.group(1))

    power: Optional[float] = None
    m = _DESC_POWER_RE.search(s)
    if m:
        power = parse_watts(m.group(1) + m.group(2) + "W")

    masked = _DESC_POWER_RE.sub(" ", _DESC_PERCENT_RE.sub(" ", s))
    masked = _DESC_METRIC_RE.sub(" ", masked)
    masked = _DESC_RES_SPACE_RE.sub(r"\1", masked)
    package: Optional[str] = None
    ohms: Optional[float] = None
    for token in re.split(r"[\s,;]+", masked):
        token = token.strip("()[]")
        if not token:
            continue
        if package is None and chip_size(token):
            package = chip_size(token)
            continue
        if ohms is None and re.search(r"[0-9]", token):
            ohms = parse_res_value(token)[0]

    return DescriptionEvidence(
        nominal=ohms,
        rating=power,
        package=package,
        tolerance_pct=tolerance,
    )


# --------------------------------------------------------------------------- #
# Inductors: inductance, current, body size and shielding.
# --------------------------------------------------------------------------- #

# Inductance multipliers. A bare number is not an inductance (it could be
# henries or microhenries), so a letter or an "H" is required.
_IND_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
             "R": 1e-6, "r": 1e-6, "": 1.0}


def parse_inductance(text: object) -> Optional[float]:
    """Henries from an inductance rendering (``"10uH"``, ``"150 nH"``,
    ``"4.7µH"``, ``"4u7"``, ``"1u"``, ``"1mH"``, ``"4R7"`` = 4.7 µH), or
    ``None``. A bare number without a unit letter or ``H`` gives ``None``."""
    if text is None:
        return None
    s = "".join(str(text).split())
    has_h = s[-1:] in ("H", "h")
    if has_h:
        s = s[:-1]
    m = re.fullmatch(r"([0-9.]*)([pnuµμmRr])?([0-9.]*)", s)
    if not m or not (m.group(1) or m.group(3)):
        return None
    head, letter, tail = m.group(1), m.group(2) or "", m.group(3)
    if not letter and not has_h:
        return None
    if letter in ("R", "r") and has_h:
        return None  # "4R7H" is not a rendering any supplier uses
    num = f"{head or '0'}.{tail}" if tail else head
    try:
        return float(num) * _IND_MULT[letter]
    except ValueError:
        return None


def fmt_henries(v: float) -> str:
    """``"10uH"``, ``"150nH"``, ``"1mH"``."""
    for scale, suffix in ((1e-3, "mH"), (1e-6, "uH")):
        if v >= scale * (1 - 1e-9):
            return f"{round(v / scale, 6):g}{suffix}"
    return f"{round(v / 1e-9, 6):g}nH"


def parse_amps(text: object) -> Optional[float]:
    """Amps from a current rendering (``"1.3A"``, ``"840mA"``, ``"22.3 A"``,
    ``"13p5A"``), or ``None``. JLCPCB's ``"-"`` (not listed) gives ``None``."""
    if text is None:
        return None
    s = "".join(str(text).split())
    s = re.sub(r"(?i)amps?$", "", s)
    if s[-1:] in ("A", "a"):
        s = s[:-1]
    scale = 1.0
    if s.endswith("m"):
        s, scale = s[:-1], 1e-3
    m = re.fullmatch(r"([0-9]*)[pP]([0-9]+)", s)
    if m:
        s = f"{m.group(1) or '0'}.{m.group(2)}"
    try:
        return float(s) * scale
    except ValueError:
        return None


def fmt_amps(v: float) -> str:
    """``"840mA"`` below one amp, ``"1.3A"`` at or above."""
    return f"{round(v * 1000, 6):g}mA" if v < 1 else f"{round(v, 6):g}A"


# A body size in millimetres: "SMD,4x4mm", "4.00mm x 4.00mm", "5.8x5.2mm",
# "4 x 4 mm". The unit is required after the second number, so DigiKey's
# inch rendering ('0.157" L x 0.157" W (4.00mm x 4.00mm)') matches only at
# its millimetre part.
_BODY_RE = re.compile(
    r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(?:mm)?\s*[xX×*]\s*([0-9]+(?:\.[0-9]+)?)\s*mm",
)
# The same without the unit, for a size typed in the dialog ("4x4").
_BODY_BARE_RE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[xX×*]\s*([0-9]+(?:\.[0-9]+)?)\s*")

# Two body sizes within this much per side (mm) are the same size: suppliers
# round the same part differently ("4x4mm", "4.10mm x 4.10mm").
BODY_MATCH_MM = 0.25


def _fmt_body(a: float, b: float) -> str:
    big, small = max(a, b), min(a, b)
    return f"{round(big, 1):g}x{round(small, 1):g}mm"


def body_size(text: object) -> Optional[str]:
    """A body size in normalized form, longer side first (``"4x4mm"``,
    ``"5.8x5.2mm"``), from a supplier package (``"SMD,4x4mm"``), DigiKey's
    ``Size / Dimension`` or a typed size (``"4x4"``, ``"4 x 4 mm"``); ``None``
    when *text* states none."""
    s = str(text or "")
    m = _BODY_RE.search(s) or _BODY_BARE_RE.fullmatch(s)
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if a <= 0 or b <= 0:
        return None
    return _fmt_body(a, b)


def body_dims(size: object) -> Optional[Tuple[float, float]]:
    """The two sides of a normalized body size, longer first."""
    m = _BODY_RE.fullmatch(str(size or ""))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return max(a, b), min(a, b)


def same_body(a: object, b: object) -> bool:
    """True when two body sizes are within :data:`BODY_MATCH_MM` per side."""
    da, db = body_dims(a), body_dims(b)
    if da is None or db is None:
        return False
    return all(abs(x - y) <= BODY_MATCH_MM + 1e-9 for x, y in zip(da, db))


SHIELDED = "Shielded"
SEMI_SHIELDED = "Semi-Shielded"
UNSHIELDED = "Unshielded"


def shielding_tokens(text: object) -> Tuple[str, ...]:
    """The shielding classes a string names, in order, without repeats:
    ``"Shielded"``, ``"Semi-Shielded"`` or ``"Unshielded"``. Reads DigiKey's
    ``Shielding`` (``"Semi-Shielded"``), JLCPCB's ``Type`` (``"Magnetic
    Shielded Inductor"``, ``"Unshielded Inductor"``) and a typed list
    (``"shielded, semi"``). Text naming none (JLCPCB's ``"Multilayer
    inductor"``) gives ``()``."""
    out: List[str] = []
    for piece in re.split(r"[,;/]", str(text or "")):
        p = piece.strip().lower()
        if not p:
            continue
        if "semi" in p:
            code = SEMI_SHIELDED
        elif re.search(r"\b(un|non)[\s-]?shield", p):
            code = UNSHIELDED
        elif "shield" in p:
            code = SHIELDED
        else:
            continue
        if code not in out:
            out.append(code)
    return tuple(out)


# The shielding classes acceptable in place of a stated one: the same or
# better.
_SHIELDING_UPGRADES: Dict[str, Tuple[str, ...]] = {
    UNSHIELDED: (UNSHIELDED, SEMI_SHIELDED, SHIELDED),
    SEMI_SHIELDED: (SEMI_SHIELDED, SHIELDED),
    SHIELDED: (SHIELDED,),
}


def shielding_upgrades(code: object) -> Tuple[str, ...]:
    """*code* followed by the better-shielded classes: ``"Semi-Shielded"`` ->
    ``("Semi-Shielded", "Shielded")``. Text naming no class gives ``()``."""
    codes = shielding_tokens(code)
    return _SHIELDING_UPGRADES[codes[0]] if codes else ()


_IND_P_DECIMAL_RE = re.compile(r"([0-9]*)[pP]([0-9]+)([nNuUµμmM])[hH]?")


def _ind_value_text(text: str) -> str:
    """*text* rewritten for :func:`parse_inductance`: the 'p' decimal point
    replaced by '.', and an upper-case unit letter lower-cased (``"4p7U"`` ->
    ``"4.7u"``)."""
    m = _IND_P_DECIMAL_RE.fullmatch(text)
    if m:
        return f"{m.group(1) or '0'}.{m.group(2)}{m.group(3).lower()}"
    if re.fullmatch(r"[0-9.]+[NU]H?", text):
        return text[:-1].lower() + text[-1] if text.endswith(("H", "h")) else text.lower()
    return text


def parse_ind_value(text: object) -> Tuple[Optional[float], Optional[str]]:
    """``(henries, package)`` from an inductor Value. A plain Value
    (``"22uH"``, ``"1u"``) has no package. Library style takes an optional
    ``"L_"`` / ``"L-"`` prefix and ``_``-separated parts: the first is the
    value, with ``p`` as a decimal point (``"L_4p7U_13p5A"`` = 4.7 µH), a later
    part accepted by :func:`chip_size` is the package (``"L_10U_0805"``). A
    current part (``"1A"``) is read by :func:`parse_ind_description`, not
    here. Either element is ``None`` when it is not stated or does not
    parse."""
    if text is None:
        return None, None
    s = str(text).strip()
    henries = parse_inductance(s)
    if henries is not None:
        return henries, None
    if s[:2] in ("L_", "L-", "l_", "l-"):
        s = s[2:]
    parts = [p for p in s.split("_") if p]
    if not parts:
        return None, None
    if chip_size(parts[0]):
        return None, chip_size(parts[0])
    package = next((chip_size(p) for p in parts[1:] if chip_size(p)), None)
    return parse_inductance(_ind_value_text(parts[0])), package


_DESC_IND_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?)\s?([nuµμm])\s?H(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DESC_AMPS_RE = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]+(?:\.[0-9]+)?)\s?(m?)A(?:mps?)?(?![A-Za-z0-9])",
)


def parse_ind_description(text: object) -> DescriptionEvidence:
    """The facts an inductor Description states (``"Inductor 10 uH, 1A"``):
    inductance from the first value with a henry unit, current from the first
    amps value (taken as the saturation-current floor), tolerance from the
    first percentage, package from the first chip-size token or millimetre
    body size, and shielding from a shielding word. ``dielectrics`` holds the
    shielding classes. Text stating none of these (``"Inductor, small
    symbol"``) gives empty evidence."""
    s = str(text or "")

    henries: Optional[float] = None
    m = _DESC_IND_RE.search(s)
    if m:
        henries = parse_inductance(m.group(1) + m.group(2).lower() + "H")

    amps: Optional[float] = None
    m = _DESC_AMPS_RE.search(s)
    if m:
        amps = parse_amps(m.group(1) + m.group(2) + "A")

    tolerance: Optional[float] = None
    m = _DESC_PERCENT_RE.search(s)
    if m:
        tolerance = float(m.group(1))

    package = body_size(s)
    if package is None:
        masked = _DESC_METRIC_RE.sub(" ", s)
        for token in re.split(r"[\s,;]+", masked):
            if chip_size(token.strip("()[]")):
                package = chip_size(token.strip("()[]"))
                break

    return DescriptionEvidence(
        nominal=henries,
        rating=amps,
        dielectrics=shielding_tokens(s),
        package=package,
        tolerance_pct=tolerance,
    )
