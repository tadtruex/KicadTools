"""
LED colour, wavelength and kind (``LED_PLAN.md``, step 1).

A colour name on a schematic (``"LED_Green"``, ``"G"``) is matched to a part
by dominant wavelength, not by the supplier's colour label: JLCPCB lists
``Green`` parts from 486 to 573 nm. Each colour has a band in nm
(:data:`COLORS`); white, warm white, cool white and pink have no dominant
wavelength and match by label. Infrared matches on the peak wavelength,
since IR parts list no dominant one.

- :func:`parse_colors` / :func:`parse_color` read colour names from schematic
  text; single letters (``G``) only when the caller says the footprint is an
  LED's.
- :func:`led_kind` sorts a symbol into plain (one colour), multicolour
  (bicolour, RGB) or smart (a driver IC built in).
- :func:`parse_nm`, :func:`parse_mcd`, :func:`parse_vf` and
  :func:`parse_labels` read supplier values, one entry per die
  (``"569nm、621nm"``).
- :func:`in_band` says whether a one-colour part has the colour asked for.

Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple


PLAIN = "plain"
MULTICOLOR = "multicolor"
SMART = "smart"

# A stated wavelength ("525nm") replaces the colour's band with this many nm
# either side of it.
STATED_WAVELENGTH_NM = 10.0


@dataclass(frozen=True)
class Color:
    """One colour: its name, band in nm (``None`` for a label-only colour)
    and whether the band applies to the peak rather than the dominant
    wavelength."""
    name: str
    band: Optional[Tuple[float, float]]
    peak: bool = False

    @property
    def label_only(self) -> bool:
        return self.band is None

    def band_text(self) -> str:
        """``"510–540 nm"``, or ``"by label"`` for white and pink."""
        if self.band is None:
            return "by label"
        lo, hi = self.band
        kind = " peak" if self.peak else ""
        return f"{_fmt_nm(lo)}–{_fmt_nm(hi)} nm{kind}"


# The bands overlap where suppliers' parts do (yellow and amber).
COLORS: Mapping[str, Color] = {c.name: c for c in (
    Color("ultraviolet", (380.0, 435.0)),
    Color("blue", (455.0, 480.0)),
    Color("cyan", (480.0, 510.0)),
    Color("green", (510.0, 540.0)),
    Color("yellow green", (555.0, 575.0)),
    Color("yellow", (580.0, 595.0)),
    Color("amber", (588.0, 600.0)),
    Color("orange", (598.0, 615.0)),
    Color("red", (615.0, 645.0)),
    Color("deep red", (645.0, 670.0)),
    Color("infrared", (700.0, 1000.0), peak=True),
    Color("white", None),
    Color("warm white", None),
    Color("cool white", None),
    Color("pink", None),
)}

# Spelling (lower case, words separated by one space) -> colour name.
SPELLINGS: Mapping[str, str] = {
    **{name: name for name in COLORS},
    "uv": "ultraviolet", "violet": "ultraviolet", "purple": "ultraviolet",
    "blue green": "cyan", "ice blue": "cyan", "aqua": "cyan",
    "emerald green": "green", "true green": "green", "pure green": "green",
    "lime": "yellow green", "yellowgreen": "yellow green",
    "ir": "infrared",
    "warmwhite": "warm white", "coolwhite": "cool white",
    "cold white": "cool white", "natural white": "white",
}

# Longest spelling first, so "yellow green" is one colour and not two.
_SPELLING_RES = [
    (re.compile(r"\b" + r"\s+".join(map(re.escape, s.split())) + r"\b"), name)
    for s, name in sorted(SPELLINGS.items(), key=lambda kv: -len(kv[0]))
]

# Single letters read as colours on an LED footprint (AtTinyDemo D3, Value G).
LETTERS: Mapping[str, str] = {
    "R": "red", "G": "green", "B": "blue", "Y": "yellow", "W": "white",
    "O": "orange", "A": "amber",
}

_SMART_RE = re.compile(
    r"\b(ws28\d\d\w*|sk68\d\d\w*|sk9822\w*|apa10[24]\w*|apa106\w*|neopixels?|"
    r"dotstars?|addressable|built\s*-?\s*in\s+ic|integrated\s+ic|"
    r"smart\s+led|intelligent)\b",
    re.IGNORECASE,
)
_MULTI_WORD_RE = re.compile(
    r"\b(bi|tri|multi|dual|two|three)\s*-?\s*colou?r(ed)?\b|\brgbw?\b",
    re.IGNORECASE,
)
_LED_WORD_RE = re.compile(r"\bleds?\b", re.IGNORECASE)


def _fmt_nm(v: float) -> str:
    return f"{v:g}"


def _words(text: object) -> str:
    """Lower case, every run of non-alphanumerics one space (``"LED_Green"``
    -> ``"led green"``)."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _tokens(text: object) -> Tuple[str, ...]:
    return tuple(t for t in re.split(r"[^A-Za-z0-9]+", str(text or "")) if t)


def color(name: object) -> Optional[Color]:
    """The :class:`Color` for a name or spelling (``"Emerald Green"`` ->
    green), else ``None``."""
    key = _words(name)
    canon = SPELLINGS.get(key)
    if canon is None:
        found = _scan(key)
        canon = found[0] if len(found) == 1 else None
    return COLORS.get(canon) if canon else None


def _scan(words: str) -> Tuple[str, ...]:
    """Colour names in *words* (already :func:`_words`-normalized), in text
    order, longest spelling first, without overlaps; a repeated colour is
    listed once."""
    hits = []
    taken = [False] * len(words)
    for rx, name in _SPELLING_RES:
        for m in rx.finditer(words):
            if any(taken[m.start():m.end()]):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            hits.append((m.start(), name))
    out = []
    for _, name in sorted(hits):
        if name not in out:
            out.append(name)
    return tuple(out)


def _letter_colors(token: str) -> Tuple[str, ...]:
    """``"G"`` -> green; ``"RGB"`` -> red, green, blue. Upper-case letters,
    each a colour letter, none repeated; anything else gives ``()``."""
    if not token.isupper() or len(token) > 4 or len(set(token)) != len(token):
        return ()
    if not all(ch in LETTERS for ch in token):
        return ()
    return tuple(LETTERS[ch] for ch in token)


def parse_colors(text: object, *, letters: bool = False) -> Tuple[str, ...]:
    """Colour names in schematic text, in order (``"LED_Green"`` -> green).

    With *letters* (the footprint is an LED's, or the text says LED), a token
    of colour letters counts too: ``"G"`` -> green, ``"LED_RG_..."`` -> red,
    green."""
    found = list(_scan(_words(text)))
    if letters:
        for tok in _tokens(text):
            for name in _letter_colors(tok):
                if name not in found:
                    found.append(name)
    return tuple(found)


def parse_color(text: object, *, letters: bool = False) -> Optional[str]:
    """The one colour in *text*, else ``None`` (none, or more than one)."""
    found = parse_colors(text, letters=letters)
    return found[0] if len(found) == 1 else None


def is_led_footprint(footprint: object) -> bool:
    """The footprint id names an LED (``LED_SMD:LED_0603_1608Metric``,
    ``LEDs:LED_0603``)."""
    return any(_LED_WORD_RE.search(_words(part))
               for part in str(footprint or "").split(":"))


def _led_text(props: Mapping[str, object]) -> str:
    return " ".join(str(props.get(k) or "") for k in ("Value", "Description"))


def _allow_letters(props: Mapping[str, object]) -> bool:
    return (is_led_footprint(props.get("Footprint"))
            or bool(_LED_WORD_RE.search(_words(_led_text(props)))))


def colors_of(props: Mapping[str, object]) -> Tuple[str, ...]:
    """Colours a symbol's Value and Description name; letters count on an
    LED footprint or when the text says LED."""
    return parse_colors(_led_text(props), letters=_allow_letters(props))


def led_kind(props: Mapping[str, object]) -> str:
    """:data:`PLAIN`, :data:`MULTICOLOR` or :data:`SMART` for an LED symbol.

    Smart: a driver-IC name or word in the Value or Description (``WS2812``,
    ``SK6812``, ``APA102``, ``NeoPixel``, ``addressable``, ``built-in IC``).
    Multicolour: two or more colours, or ``bicolor``, ``RGB`` and the like.
    Otherwise plain, with one colour or none. The reference designator is not
    used: its letter is the designer's choice."""
    text = _led_text(props)
    if _SMART_RE.search(text):
        return SMART
    if _MULTI_WORD_RE.search(text) or len(colors_of(props)) > 1:
        return MULTICOLOR
    return PLAIN


# --------------------------------------------------------------------------- #
# Supplier values — one entry per die.
# --------------------------------------------------------------------------- #

_CHANNEL_SPLIT_RE = re.compile(r"[、,;/]|\s+and\s+")
_NUM_RE = r"[0-9]+(?:\.[0-9]+)?"
_RANGE_RE = re.compile(rf"({_NUM_RE})\s*([a-zµ]*)\s*[~\-–]\s*({_NUM_RE})\s*([a-zµ]*)",
                       re.IGNORECASE)
_VALUE_RE = re.compile(rf"({_NUM_RE})\s*([a-zµ]*)", re.IGNORECASE)


def _channels(text: object) -> Tuple[str, ...]:
    return tuple(p.strip() for p in _CHANNEL_SPLIT_RE.split(str(text or ""))
                 if p.strip())


def _with_unit(text: str, units: Mapping[str, float],
               pick) -> Optional[float]:
    """A number in *text* scaled by its unit; a range ``a~b`` gives
    ``pick(a, b)``. A unit not in *units* gives ``None``; no unit takes the
    first (base) unit."""
    base = next(iter(units.values()))

    def scale(unit: str) -> Optional[float]:
        if not unit:
            return base
        return units.get(unit.lower())

    m = _RANGE_RE.search(text)
    if m:
        u1 = m.group(2) or m.group(4)
        s1, s2 = scale(u1), scale(m.group(4) or u1)
        if s1 is None or s2 is None:
            return None
        return pick(float(m.group(1)) * s1, float(m.group(3)) * s2)
    m = _VALUE_RE.search(text)
    if not m:
        return None
    s = scale(m.group(2))
    return None if s is None else float(m.group(1)) * s


def parse_nm(text: object) -> Tuple[float, ...]:
    """Wavelengths in nm (``"625nm"`` -> (625,), ``"569nm、621nm"`` ->
    (569, 621)); a range gives its midpoint."""
    out = []
    for ch in _channels(text):
        v = _with_unit(ch, {"nm": 1.0}, lambda a, b: (a + b) / 2)
        if v is not None:
            out.append(v)
    return tuple(out)


def parse_mcd(text: object) -> Tuple[float, ...]:
    """Luminous intensity in mcd (``"250mcd"``, ``"1.2cd"``); a range gives
    its lower end, the figure a minimum can rely on."""
    out = []
    for ch in _channels(text):
        v = _with_unit(ch, {"mcd": 1.0, "cd": 1000.0, "ucd": 1e-3, "µcd": 1e-3},
                       min)
        if v is not None:
            out.append(v)
    return tuple(out)


def parse_vf(text: object) -> Tuple[float, ...]:
    """Forward voltages in V (``"2V"``, ``"2V、2.1V"``); a range gives its
    upper end, the figure a maximum must allow for."""
    out = []
    for ch in _channels(text):
        v = _with_unit(ch, {"v": 1.0, "mv": 1e-3}, max)
        if v is not None:
            out.append(v)
    return tuple(out)


def parse_labels(text: object) -> Tuple[Optional[str], ...]:
    """Colour labels per die (``"Orange Red, Yellow Green"`` -> one entry
    each). An entry is the colour name its label spells, the first when it
    spells more than one (``"Orange Red"`` -> orange), else ``None``."""
    out = []
    for ch in _channels(text):
        found = _scan(_words(ch))
        out.append(found[0] if found else None)
    return tuple(out)


def band_for(name: object, wavelength: object = None
             ) -> Optional[Tuple[float, float]]:
    """The band a part must fall in: ±:data:`STATED_WAVELENGTH_NM` around a
    stated *wavelength*, else the colour's band; ``None`` for a label-only
    colour or an unknown name."""
    stated = parse_nm(wavelength) if wavelength else ()
    if stated:
        w = stated[0]
        return (w - STATED_WAVELENGTH_NM, w + STATED_WAVELENGTH_NM)
    c = color(name)
    return c.band if c else None


def band_text(name: object, wavelength: object = None) -> str:
    """The band as the dialog shows it (``"510–540 nm"``, ``"515–535 nm"``
    for a stated 525 nm, ``"by label"``), or ``""`` for an unknown name."""
    stated = parse_nm(wavelength) if wavelength else ()
    if stated:
        lo, hi = band_for(name, wavelength)
        return f"{_fmt_nm(lo)}–{_fmt_nm(hi)} nm"
    c = color(name)
    return c.band_text() if c else ""


def in_band(name: object, *, dominant: Sequence[float] = (),
            peak: Sequence[float] = (), labels: Sequence[Optional[str]] = (),
            wavelength: object = None) -> Optional[bool]:
    """Whether a one-colour part has the colour *name*.

    The part's dominant wavelength is in the band (:func:`band_for`); for
    infrared the peak, else the dominant. A label-only colour (white, pink)
    matches when the part's label is that colour; ``warm white`` and ``cool
    white`` accept a plain ``white`` label (colour temperature is TBD 3).
    ``None`` when the part gives nothing to decide by, or the colour is
    unknown. A part with more than one die is never in band."""
    c = color(name)
    stated = band_for(name, wavelength) if wavelength else None
    if c is None and stated is None:
        return None
    if c is not None and c.label_only and stated is None:
        if len(labels) > 1:
            return False
        got = labels[0] if labels else None
        if got is None:
            return None
        if got == c.name:
            return True
        return c.name in ("warm white", "cool white") and got == "white"
    lo, hi = stated or c.band
    use_peak = c is not None and c.peak and stated is None
    values = (tuple(peak) or tuple(dominant)) if use_peak else tuple(dominant)
    if not values:
        return None
    if len(values) > 1:
        return False
    return lo <= values[0] <= hi
