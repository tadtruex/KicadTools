"""
IC package facts read from a KiCad footprint name or from DigiKey's package
parameters, for the IC footprint check (``IC_PLAN.md`` steps 3 and 6).

Both readers return a :class:`PackageInfo` with five optional fields:

* ``family``   — the package family under one canonical name, so that
  equivalent spellings compare equal: ``VFQFN``, ``WQFN``, ``HVQFN`` and
  ``LFCSP`` are all ``"QFN"``; ``SC-74A`` and ``SOT-753`` are ``"SOT-23"``
  with 5 pins; ``SOT-363`` is ``"SC-70"`` with 6 pins. The canonical names
  are the values of :data:`FAMILIES`;
* ``pins``     — the number of signal pins, **not** counting an exposed pad
  (``QFN-24-1EP`` and DigiKey's ``24-VFQFN Exposed Pad`` both give 24);
* ``body_mm``  — the body size as ``(smaller, larger)`` in millimetres; sorted
  because KiCad writes ``3x2mm`` where DigiKey writes ``(2x3)`` for the same
  part;
* ``pitch_mm`` — the lead pitch in millimetres. KiCad names state it
  (``P0.5mm``); DigiKey's package strings do not, so for DigiKey it is only
  the pitch the package standard fixes (:func:`standard_pitch`: SOIC, DIP and
  the JEDEC LQFP/TQFP sizes) and ``None`` for every other package. A QFN or
  DFN of one body size is made in more than one pitch (a 6-DFN (2x2) is made
  in 0.5 mm and 0.65 mm, and DigiKey's string for the LDL212 does not say
  which);
* ``ep``       — whether the package has an exposed pad. ``True`` when the
  text says so; ``False`` when the text names a family and would have said so
  (a KiCad name, DigiKey's ``Package / Case``); ``None`` when unknown;
* ``also``     — other families the same source names for the part (DigiKey's
  ``8-TSSOP, 8-MSOP`` is MSOP also TSSOP). Compare families with
  :meth:`PackageInfo.same_family`, which counts any of them as a match.

SOT-223 and SOT-89 pins count the leads, not the tab: KiCad's
``SOT-223-3_TabPin2`` has 3 distinct pad numbers, and DigiKey's ``SOT-223-4``
and ``TO-261-4`` (which count the tab) are read as 3. A footprint's pad count
for these families may or may not include the tab (:data:`TAB_FAMILIES`).

A field is ``None`` when the text does not state it. A custom footprint name
(``TexasInstruments:RHH0036C``, ``ECG_Footprints:CP_20_10_ADI_NOTH``) gives
all ``None``; one that states only some facts (``STMicro:LDL212_2x2mm_
Pitch0.5mm``) gives those. Families not in the alias table are not guessed:
an unrecognized family gives ``family`` and ``pins`` of ``None``.

The chip-size parsing for capacitors and resistors
(:func:`kicadtools.units.package_code`) covers imperial codes only and is not
used here.

Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Pattern, Tuple

from kicadtools.parttypes import DK_PACKAGE


@dataclass(frozen=True)
class PackageInfo:
    """Package facts from one source; each field is ``None`` when unstated
    (see the module docstring for what each field means)."""
    family: Optional[str] = None
    pins: Optional[int] = None
    body_mm: Optional[Tuple[float, float]] = None
    pitch_mm: Optional[float] = None
    ep: Optional[bool] = None
    also: Tuple[str, ...] = ()

    @property
    def families(self) -> Tuple[str, ...]:
        """``family`` followed by ``also``; empty when no family is known."""
        return ((self.family,) + self.also) if self.family else ()

    def same_family(self, other: "PackageInfo") -> bool:
        """True when the two sources name a common family."""
        return bool(set(self.families) & set(other.families))

    @property
    def known(self) -> bool:
        """True when at least one field is stated."""
        return any(v is not None for v in
                   (self.family, self.pins, self.body_mm, self.pitch_mm, self.ep))


# --------------------------------------------------------------------------- #
# Family aliases.
# --------------------------------------------------------------------------- #

# Each row: (pattern, canonical family, fixed pin count or None, exposed pad
# implied). The pattern matches at the start of a token, case-insensitively,
# and must end at a word boundary that is not a digit (so "SOT-23" does not
# match "SOT-223"). A fixed pin count is for names that state the package
# without a pin suffix (SOT-753 is a 5-pin SOT-23). Rows are tried in order;
# a more specific spelling comes before a shorter one that is its prefix.
_ALIASES: Tuple[Tuple[str, str, Optional[int], bool], ...] = (
    # SOT-23 and its thin variant. TO-236 is the JEDEC name of SOT-23-3;
    # SC-74A / SOT-753 / SOT-25 are SOT-23-5; SC-74 / SOT-457 / SOT-26 are
    # SOT-23-6. "ThinSOT" is Linear Technology's name for TSOT-23.
    (r"TSOT-?23", "TSOT-23", None, False),
    (r"ThinSOT(?:-?23)?", "TSOT-23", None, False),
    (r"SOT-?23", "SOT-23", None, False),
    (r"TO-?236", "SOT-23", None, False),
    (r"SC-?74A", "SOT-23", 5, False),
    (r"SOT-?753", "SOT-23", 5, False),
    (r"SOT-?25", "SOT-23", 5, False),
    (r"SC-?74", "SOT-23", 6, False),
    (r"SOT-?457", "SOT-23", 6, False),
    (r"SOT-?26", "SOT-23", 6, False),
    # SC-70: SOT-323 is the 3-pin, SOT-353 / SC-88A the 5-pin, SOT-363 /
    # SC-88 the 6-pin.
    (r"SOT-?323", "SC-70", 3, False),
    (r"SOT-?353", "SC-70", 5, False),
    (r"SC-?88A", "SC-70", 5, False),
    (r"SOT-?363", "SC-70", 6, False),
    (r"SC-?88", "SC-70", 6, False),
    (r"SC-?70", "SC-70", None, False),
    (r"SOT-?563", "SOT-563", 6, False),
    # TO-261 is the JEDEC name of SOT-223, TO-243 that of SOT-89.
    (r"SOT-?223", "SOT-223", None, False),
    (r"TO-?261(?:AA)?", "SOT-223", None, False),
    (r"SOT-?89", "SOT-89", None, False),
    (r"TO-?243(?:AA)?", "SOT-89", None, False),
    # Small-outline leaded. VSSOP is TI's name for the 3x3 mm MSOP; µMAX is
    # Maxim's. HTSSOP / PowerTSSOP are TSSOP with an exposed pad. QSOP is the
    # 0.635 mm SSOP.
    (r"SOIC", "SOIC", None, False),
    (r"SO", "SOIC", None, False),
    (r"(?:MSOP|VSSOP|[uµμ]MAX)", "MSOP", None, False),
    (r"HVSSOP", "MSOP", None, True),
    (r"TSSOP", "TSSOP", None, False),
    (r"(?:HTSSOP|PowerTSSOP)", "TSSOP", None, True),
    (r"(?:SSOP|QSOP)", "SSOP", None, False),
    # No-lead. Every [H][V|W|U|X|T][F]QFN thickness/pitch variant is QFN, as
    # are ADI's LFCSP (with its -WQ / -VQ / -UQ suffix) and MLPQ; DFN and SON
    # (TI's name) with the same prefixes are DFN. DRQFN (dual-row) is not.
    (r"[HVWUXT]{0,2}F?QFN", "QFN", None, False),
    (r"LFCSP(?:-[A-Z]{2})?", "QFN", None, False),
    (r"MLPQ", "QFN", None, False),
    (r"[HVWUXT2]{0,3}F?(?:DFN|SON)", "DFN", None, False),
    (r"MLPD", "DFN", None, False),
    # Quad flat pack. LQFP (1.4 mm) and TQFP (1.0 mm) differ in height only;
    # they stay separate families as IC_PLAN.md lists them.
    (r"LQFP", "LQFP", None, False),
    (r"TQFP", "TQFP", None, False),
    # Through-hole DIP; DigiKey writes PDIP for the plastic one.
    (r"[PC]?DIP", "DIP", None, False),
)

_ALIAS_RES: Tuple[Tuple[Pattern[str], str, Optional[int], bool], ...] = tuple(
    (re.compile(rf"({pat})(?![A-Za-z0-9])", re.IGNORECASE), fam, pins, ep)
    for pat, fam, pins, ep in _ALIASES
)

# The default pin count of a family named without a pin suffix ("SOT-23",
# "TSOT-23", "SC-70", "SOT-223", "SOT-89" alone are the 3-lead packages).
_DEFAULT_PINS = {"SOT-23": 3, "TSOT-23": 3, "SC-70": 3, "SOT-223": 3, "SOT-89": 3}

# DigiKey pin counts that include the tab, mapped to the lead count KiCad's
# name gives: SOT-223-4 / TO-261-4 is KiCad's SOT-223-3_TabPin2.
_DK_TAB_COUNTED = {("SOT-223", 4): 3, ("SOT-89", 4): 3}

#: Families with a tab that a footprint may number as its own pad, give a
#: lead's number, or count in its name: KiCad's ``SOT-223`` (3 leads, tab pad
#: 4: 4 pads), ``SOT-223-3_TabPin2`` (3 pads), ``SOT-223-6`` (5 leads and the
#: tab, named 6: 6 pads) and ``SOT-223-6_TabPin3`` (named 6: 5 pads).
TAB_FAMILIES = frozenset(fam for fam, _ in _DK_TAB_COUNTED)

# DigiKey's Package / Case lists a generic "N-TSSOP" ahead of the specific
# package for SC-70 ("5-TSSOP, SC-70-5, SOT-353") and MSOP ("8-TSSOP,
# 8-MSOP"); the specific one is preferred when both are listed.
_DK_GENERIC = frozenset({"TSSOP"})

#: Every canonical family name either reader can return.
FAMILIES: Tuple[str, ...] = tuple(dict.fromkeys(fam for _, fam, _, _ in _ALIASES))


def _match_family(token: str) -> Optional[Tuple[str, Optional[int], bool, str]]:
    """``(family, fixed pins, ep implied, rest)`` for the alias at the start of
    *token*, or ``None``. ``rest`` is the text after the alias."""
    for rx, fam, pins, ep in _ALIAS_RES:
        m = rx.match(token)
        if m:
            return fam, pins, ep, token[m.end():]
    return None


def normalize_family(text: object) -> Optional[str]:
    """The canonical family a package name starts with (``"VFQFN"`` ->
    ``"QFN"``, ``"SC-74A"`` -> ``"SOT-23"``, ``"PDIP"`` -> ``"DIP"``), or
    ``None`` when it is not one of :data:`FAMILIES`."""
    hit = _match_family(str(text or "").strip())
    return hit[0] if hit else None


# --------------------------------------------------------------------------- #
# KiCad footprint names.
# --------------------------------------------------------------------------- #

# After the family: "-24" pins (KiCad's "SOIC-20W" wide and ST's "UQFN-6L"
# letter allowed), an optional "-16" body pin count ("SOIC-14-16" is 14 pins in
# a 16-pin body), then "-1EP" / "-EP".
_KICAD_SUFFIX_RE = re.compile(r"-(\d+)[WL]?(?:-\d+)?(?:-(1?EP))?(?![A-Za-z0-9.])", re.IGNORECASE)
_KICAD_EP_SUFFIX_RE = re.compile(r"-1?EP(?![A-Za-z0-9])", re.IGNORECASE)
# A "_"-separated field that is the body: "4x4mm", "3.9x4.9mm".
_BODY_FIELD_RE = re.compile(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm", re.IGNORECASE)
# A pitch field: KiCad's "P0.5mm", ST's "Pitch0.5mm".
_PITCH_FIELD_RE = re.compile(r"(?:P|Pitch)(\d+(?:\.\d+)?)mm", re.IGNORECASE)
# An exposed-pad size field: "EP2.7x2.7mm", "EP1x2.65".
_EP_FIELD_RE = re.compile(r"EP\d", re.IGNORECASE)
# A vendor prefix KiCad puts before a standard name: "Linear_", "AMS_", "ST_".
_VENDOR_RE = re.compile(r"[A-Za-z0-9]+_")


def _body(a: str, b: str) -> Tuple[float, float]:
    x, y = float(a), float(b)
    return (x, y) if x <= y else (y, x)


def _kicad_family(name: str) -> Tuple[Optional[str], Optional[int], Optional[bool]]:
    """``(family, pins, ep)`` from the first ``_`` field of *name* (after one
    optional vendor prefix), or all ``None``."""
    candidates = [name]
    v = _VENDOR_RE.match(name)
    if v:
        candidates.append(name[v.end():])
    for text in candidates:
        head = text.split("_", 1)[0]
        hit = _match_family(head)
        if hit is None:
            continue
        fam, fixed, ep_implied, rest = hit
        m = _KICAD_SUFFIX_RE.match(rest)
        if m:
            pins: Optional[int] = int(m.group(1))
            ep = ep_implied or bool(m.group(2))
        elif rest == "" or _KICAD_EP_SUFFIX_RE.fullmatch(rest):
            pins = fixed if fixed is not None else _DEFAULT_PINS.get(fam)
            ep = ep_implied or bool(rest)
        else:
            continue
        if fixed is not None and pins != fixed:
            # "SOT-323-5" names two pin counts; trust neither.
            pins = None
        return fam, pins, ep
    return None, None, None


def parse_footprint(name: object) -> PackageInfo:
    """Package facts from a KiCad footprint name, with or without its
    ``Library:`` prefix: ``"QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm"`` ->
    ``PackageInfo("QFN", 24, (4.0, 4.0), 0.5, True)``; ``"SOT-23-5"`` ->
    ``PackageInfo("SOT-23", 5, None, None, False)``. Only the name after the
    library is read, so the library name cannot contribute a match."""
    s = str(name or "").strip()
    s = s.split(":", 1)[1] if ":" in s else s
    if not s:
        return PackageInfo()
    family, pins, ep = _kicad_family(s)
    fields = s.split("_")[1:] if family is not None else s.split("_")
    body = pitch = None
    for f in fields:
        if body is None:
            m = _BODY_FIELD_RE.fullmatch(f)
            if m:
                body = _body(m.group(1), m.group(2))
                continue
        if pitch is None:
            m = _PITCH_FIELD_RE.fullmatch(f)
            if m:
                pitch = float(m.group(1))
                continue
        if _EP_FIELD_RE.match(f):
            ep = True
    return PackageInfo(family, pins, body, pitch, ep)


# --------------------------------------------------------------------------- #
# Pitch fixed by the package standard.
# --------------------------------------------------------------------------- #

# Families made in one pitch only: SOIC (JEDEC MS-012 / MS-013) and DIP.
_FAMILY_PITCH_MM = {"SOIC": 1.27, "DIP": 2.54}

# JEDEC MS-026 quad flat packs, (pins, body side mm) -> pitch mm. LQFP and
# TQFP differ in height only, so one table serves both.
_QFP_PITCH_MM = {
    (32, 7.0): 0.8, (44, 10.0): 0.8, (48, 7.0): 0.5, (64, 7.0): 0.4,
    (64, 10.0): 0.5, (100, 14.0): 0.5, (144, 20.0): 0.5,
}


def standard_pitch(family: Optional[str], pins: Optional[int],
                   body_mm: Optional[Tuple[float, float]]) -> Optional[float]:
    """The lead pitch the package standard fixes for *family* (with *pins*
    and *body_mm* for a quad flat pack), or ``None`` when the standard allows
    more than one: ``("SOIC", 8, None)`` -> 1.27, ``("LQFP", 48, (7, 7))`` ->
    0.5, ``("DFN", 6, (2, 2))`` -> ``None``."""
    if family in _FAMILY_PITCH_MM:
        return _FAMILY_PITCH_MM[family]
    if family in ("LQFP", "TQFP") and pins is not None and body_mm is not None \
            and body_mm[0] == body_mm[1]:
        return _QFP_PITCH_MM.get((pins, body_mm[0]))
    return None


# --------------------------------------------------------------------------- #
# DigiKey package parameters.
# --------------------------------------------------------------------------- #

DK_SUPPLIER_PACKAGE = "Supplier Device Package"
# Parameter names DigiKey uses for a pin count, on the categories that have
# one. Most IC categories have none; the count then comes from the package.
DK_PIN_PARAMS: Tuple[str, ...] = (
    "Number of Pins", "Number of Terminations", "Number of Leads", "Pin Count",
)

# "(4x4)", "(1.6x1.6)", "(3x3mm)": a body size in a DigiKey package string.
_DK_BODY_RE = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*\)", re.IGNORECASE)
# "24-VFQFN ...": a leading pin count.
_DK_PINS_RE = re.compile(r"(\d+)-(.*)", re.DOTALL)
_DK_EP_RE = re.compile(r"Exposed\s+Pad|PowerPAD|(?<![A-Za-z])EP(?![A-Za-z])", re.IGNORECASE)
_DK_THIN_RE = re.compile(r"(?<![A-Za-z])Thin(?![A-Za-z])", re.IGNORECASE)


def _split_top(text: str) -> Tuple[str, ...]:
    """*text* split on commas outside parentheses (DigiKey's
    ``8-SOIC (0.154", 3.90mm Width)`` is one piece)."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return tuple(p.strip() for p in out if p.strip())


# "(SC70-6)" in "DCK (SC70-6)": a parenthesized package name.
_DK_PAREN_RE = re.compile(r"\(([^()]*)\)")

# One recognized DigiKey package name: (family, pins, pins stated, ep).
_Piece = Tuple[str, Optional[int], bool, bool]


def _dk_piece(piece: str) -> Optional[_Piece]:
    """``(family, pins, pins stated, ep)`` from one DigiKey package name
    (``"24-VFQFN Exposed Pad"``, ``"SOT-23-6 Thin"``, ``"SC-74A"``, ``"DCK
    (SC70-6)"``), or ``None`` when no family is recognized. ``pins stated`` is
    False when ``pins`` is only the family's default (``"SOT-23"`` alone)."""
    pins: Optional[int] = None
    rest = piece
    m = _DK_PINS_RE.fullmatch(piece)
    if m and _match_family(m.group(2)):
        pins, rest = int(m.group(1)), m.group(2)
    hit = _match_family(rest)
    if hit is None:
        # A vendor code with the package in parentheses: "DCK (SC70-6)".
        for p in _DK_PAREN_RE.findall(piece):
            sub = _dk_piece(p.strip())
            if sub is not None:
                return sub
        return None
    fam, fixed, ep_implied, tail = hit
    stated = True
    if pins is None:
        s = _KICAD_SUFFIX_RE.match(tail)
        if s:
            pins = int(s.group(1))
        elif fixed is not None:
            pins = fixed
        else:
            pins, stated = _DEFAULT_PINS.get(fam), False
    elif fixed is not None and pins != fixed:
        pins = None
    if pins is not None and stated:
        pins = _DK_TAB_COUNTED.get((fam, pins), pins)
    if fam == "SOT-23" and _DK_THIN_RE.search(piece):
        fam = "TSOT-23"
    return fam, pins, stated, ep_implied or bool(_DK_EP_RE.search(tail))


def _dk_string(text: str) -> Tuple[List[_Piece], Optional[Tuple[float, float]], bool]:
    """``(recognized pieces, body, ep stated)`` from one DigiKey package
    string; the pieces in the order DigiKey lists them."""
    pieces: List[_Piece] = []
    body = None
    for piece in _split_top(text):
        hit = _dk_piece(piece)
        if hit is not None:
            pieces.append(hit)
        if body is None:
            b = _DK_BODY_RE.search(piece)
            if b:
                body = _body(b.group(1), b.group(2))
    return pieces, body, bool(_DK_EP_RE.search(text))


def _choose(pieces: List[_Piece]) -> Optional[_Piece]:
    """The first piece, or the first specific one when the first is a generic
    family (:data:`_DK_GENERIC`)."""
    for p in pieces:
        if p[0] not in _DK_GENERIC:
            return p
    return pieces[0] if pieces else None


def _thin_pair(fam: str) -> str:
    """SOT-23 and TSOT-23 as one key, for agreeing on a pin count."""
    return "SOT-23" if fam == "TSOT-23" else fam


def _pin_param(params: Mapping[str, str]) -> Optional[int]:
    for key in DK_PIN_PARAMS:
        m = re.match(r"\s*(\d+)\s*$", str(params.get(key) or ""))
        if m:
            return int(m.group(1))
    return None


def parse_digikey_package(params: Optional[Mapping[str, str]]) -> PackageInfo:
    """Package facts from a DigiKey product's parameters
    (:attr:`kicadtools.suppliers.DigiKeyProduct.parameters`).

    The family comes from ``Supplier Device Package`` first (it names one
    package where ``Package / Case`` may list several: ``8-TSSOP, 8-MSOP``),
    else from ``Package / Case``, where a specific family is preferred over the
    generic ``N-TSSOP`` DigiKey lists first for SC-70 and MSOP parts. Every
    other family either string names goes in ``also``. SOT-23 becomes TSOT-23
    when ``Package / Case`` says the part is thin.

    The pins are the chosen piece's stated count, else a count another piece
    of the same family states (``Supplier Device Package`` ``"SOT-23"`` with
    ``Package / Case`` ``"SC-74A, SOT-753"`` is 5), else the family's default;
    a pin-count parameter (:data:`DK_PIN_PARAMS`) overrides them. The body
    comes from either string's ``(WxH)``. ``ep`` is True when either string
    states an exposed pad, False when ``Package / Case`` names a family without
    one (DigiKey states it there), else ``None``. ``pitch_mm`` is the
    :func:`standard_pitch` of the result, ``None`` when the standard does not
    fix it.

    ``{"Package / Case": "24-VFQFN Exposed Pad", "Supplier Device Package":
    "24-QFN (4x4)"}`` -> ``PackageInfo("QFN", 24, (4.0, 4.0), None, True)``."""
    params = params or {}
    case_pieces, case_body, case_ep = _dk_string(str(params.get(DK_PACKAGE) or ""))
    sdp_pieces, sdp_body, sdp_ep = _dk_string(str(params.get(DK_SUPPLIER_PACKAGE) or ""))
    piece = _choose(sdp_pieces) or _choose(case_pieces)
    family: Optional[str] = None
    pins: Optional[int] = None
    if piece is not None:
        family, pins = piece[0], piece[1]
        if family == "SOT-23" and any(p[0] == "TSOT-23" for p in case_pieces):
            family = "TSOT-23"
        if not piece[2]:
            key = _thin_pair(family)
            for p in sdp_pieces + case_pieces:
                if p[2] and p[1] is not None and _thin_pair(p[0]) == key:
                    pins = p[1]
                    break
    pins = _pin_param(params) or pins
    also = tuple(dict.fromkeys(p[0] for p in sdp_pieces + case_pieces
                               if p[0] != family))
    ep_stated = case_ep or sdp_ep or bool(piece and piece[3])
    if ep_stated:
        ep: Optional[bool] = True
    elif case_pieces:
        ep = False
    else:
        ep = None
    body = sdp_body or case_body
    return PackageInfo(family, pins, body, standard_pitch(family, pins, body), ep, also)
