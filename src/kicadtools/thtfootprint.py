"""Footprint signatures from KiCad footprint names (``THT_PLAN.md`` step 3).

A signature is what the through-hole searches (plan steps 5–7) filter on,
read from the footprint's library id alone:

- pin headers and sockets (``Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical``):
  rows, positions per row, pitch, orientation, and whether it is the SMD
  variant;
- terminal blocks (``TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal``):
  maker, positions, pitch, orientation. A name without a ``1xNN`` token
  (``digikey-footprints:Terminal_Block_D1.3mm_P5mm``) takes its positions from
  the pad count, when the caller passes one;
- through-hole capacitors and resistors (``Capacitor_THT:CP_Radial_D6.3mm_P2.50mm``,
  ``Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal``): body
  style, polarized, diameter, length, width, height, lead spacing;
- footprints that name a part (``Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal``,
  ``digikey-footprints:CUI_PJ-002B``): the maker and the part-number token;
- footprints with no part to buy (solder-wire pads, mounting holes,
  fiducials, bare test pads, solder jumpers, net ties): ``NO_PART`` and what
  they are;
- loop test points (``TestPoint_Loop_…``): a real part with no number in the
  name.

Anything else is ``OTHER``. Geometry tokens (``1x04``, ``P2.54mm``,
``D6.3mm``, ``Horizontal``) are read for every family. Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

HEADER = "header"
SOCKET = "socket"
TERMINAL_BLOCK = "terminal block"
CAPACITOR = "capacitor"
RESISTOR = "resistor"
NAMED_PART = "named part"
TEST_POINT = "test point"
NO_PART = "no part"
OTHER = "other"

VERTICAL = "vertical"
HORIZONTAL = "horizontal"
ANGLED = "45 degree"


@dataclass(frozen=True)
class FootprintSignature:
    """What a footprint name says about the part that goes on it. Fields that
    the name does not give are ``None`` (numbers) or empty."""

    family: str
    lib_id: str
    rows: Optional[int] = None
    per_row: Optional[int] = None
    pitch_mm: Optional[float] = None
    orientation: str = ""  # VERTICAL, HORIZONTAL, ANGLED or ""
    smd: bool = False  # the SMD variant of a header or socket footprint
    style: str = ""  # passives: radial, axial, disc, rect, box, sip, ...
    polarized: bool = False  # CP_* capacitors
    diameter_mm: Optional[float] = None
    length_mm: Optional[float] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    lead_spacing_mm: Tuple[float, ...] = ()
    maker: str = ""
    mpn: str = ""  # the part-number token of a NAMED_PART footprint
    what: str = ""  # NO_PART: solder wire pad, mounting hole, ...

    @property
    def positions(self) -> Optional[int]:
        """Total pin positions (rows × per row), or ``None``."""
        if self.per_row is None:
            return None
        return (self.rows or 1) * self.per_row


# --- tokens -------------------------------------------------------------------

_GRID = re.compile(r"^(\d+)x(\d+)(?:-\d+MP)?$")
_PITCH = re.compile(r"^P(\d+(?:\.\d+)?)(?:mm)?$")
_SPACING_XY = re.compile(r"^P[xy](\d+(?:\.\d+)?)mm$")
_DIM = re.compile(r"^([DLWH])(\d+(?:\.\d+)?)mm$")
_BARE_SIZE = re.compile(r"^\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?mm$")
_SIP = re.compile(r"^SIP(\d+)$")
# Package and size tokens that the short-name rule must not read as part
# numbers (see _named_part).
_PACKAGE_TOKEN = re.compile(
    r"^(?:TO|DO|SOD|SOT|SC|DIP|PDIP|SDIP|eDIP|SIP|eSIP|SIL|PLCC|LCC|DPAK|D2PAK|HC|PG)\d*-", re.I
)
_SIZE_TOKEN = re.compile(r"^(?:[DLWH]\d+(?:\.\d+)?mm|\d+(?:\.\d+)?x\d)", re.I)
_MM_END = re.compile(r"\dmm$", re.I)
_ORIENTATIONS = {
    "vertical": VERTICAL,
    "horizontal": HORIZONTAL,
    "45degree": ANGLED,
    "45-degree": ANGLED,
}

# Maker tokens that come before a part number in a footprint name.
_MAKERS = {
    m.lower(): m
    for m in (
        "JST", "CUI", "Samtec", "Harwin", "Keystone", "Molex", "TE", "Amphenol",
        "Hirose", "Wuerth", "Wurth", "Bourns", "CalTest", "MPD", "Kycon",
        "Switchcraft", "Littelfuse", "Omron", "E-Switch", "CK", "Alps", "Phoenix",
        "Rohm", "Vishay", "Murata", "Kemet", "Panasonic", "Nichicon", "Eaton",
    )
}

# Footprints with no part to buy, by name prefix, and what they are.
_NO_PART: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^SolderWirePad", re.I), "solder wire pad"),
    (re.compile(r"^SolderWire-", re.I), "solder wire pad"),
    (re.compile(r"^MountingHole_\d", re.I), "mounting hole"),
    (re.compile(r"^Fiducial", re.I), "fiducial"),
    (re.compile(r"^TestPoint_(?:THTPad|Pad|Plated_Hole|Bridge|\d+Pads)", re.I), "test pad"),
    (re.compile(r"^SolderJumper", re.I), "solder jumper"),
    (re.compile(r"^NetTie", re.I), "net tie"),
]

_CAP_STYLES = {"Radial": "radial", "Axial": "axial", "Disc": "disc", "Rect": "rect"}
_RES_STYLES = {
    ("Axial", "Power"): "axial power",
    ("Axial", "Shunt"): "axial shunt",
    ("Radial", "Power"): "radial power",
    ("Axial",): "axial",
    ("Box",): "box",
    ("Bare",): "bare element",
    ("Array",): "sip",
}


def _is_geometry(token: str) -> bool:
    return bool(
        _GRID.match(token)
        or _PITCH.match(token)
        or _SPACING_XY.match(token)
        or _DIM.match(token)
        or _BARE_SIZE.match(token)
        or token.lower() in _ORIENTATIONS
        or token.lower().startswith("drill")
    )


def _looks_like_mpn(token: str) -> bool:
    """A token that can be a part number: has a digit or a hyphen, and is not
    a geometry token."""
    return bool(token) and not _is_geometry(token) and (
        any(ch.isdigit() for ch in token) or "-" in token
    )


def _geometry(tokens: List[str]) -> Dict[str, object]:
    """Rows, positions, pitch or lead spacing, body dimensions and orientation
    from the name's tokens. The first ``P…`` is the pitch; every ``P…`` and
    ``Px…``/``Py…`` is a lead spacing."""
    out: Dict[str, object] = {}
    spacing: List[float] = []
    for token in tokens:
        grid = _GRID.match(token)
        if grid and "rows" not in out:
            out["rows"], out["per_row"] = int(grid.group(1)), int(grid.group(2))
            continue
        pitch = _PITCH.match(token) or _SPACING_XY.match(token)
        if pitch:
            spacing.append(float(pitch.group(1)))
            continue
        dim = _DIM.match(token)
        if dim:
            key = {"D": "diameter_mm", "L": "length_mm", "W": "width_mm", "H": "height_mm"}[dim.group(1)]
            out.setdefault(key, float(dim.group(2)))
            continue
        orientation = _ORIENTATIONS.get(token.lower())
        if orientation:
            out["orientation"] = orientation
    if spacing:
        out["pitch_mm"] = spacing[0]
        out["lead_spacing_mm"] = tuple(spacing)
    return out


def _package_name(token: str) -> bool:
    """A token that names a package or a size, not a part: ``TO-92``,
    ``TO56-3``, ``SDIP-10C``, ``PLCC-20``, ``HC35-U``, ``PG-LLGA-5-1``,
    ``D5.0mm-3``, ``15x7.5RM7.6``, ``PUSH-12mm``."""
    return bool(_PACKAGE_TOKEN.match(token) or _SIZE_TOKEN.match(token)
                or _MM_END.search(token))


def looks_like_part_number(token: str) -> bool:
    """A token that can be a part number on its own: at least 5 characters,
    a digit or a hyphen, and not a geometry, package or size token
    (``S3B-PH-K``, ``USB-A-S-F-B-TH``; not ``01x04``, ``TO-92``,
    ``P2.54mm``)."""
    return len(token) >= 5 and _looks_like_mpn(token) and not _package_name(token)


def _named_part(lib: str, tokens: List[str]) -> Optional[Tuple[str, str]]:
    """``(maker, mpn)`` when the name names a part: a maker token followed by
    a part-number token, or a short name whose last token is a part number
    (``691137710002``, ``SW_SLW-118562-4A-N-D``).

    The short-name case has no maker to go by, so it also needs a digit in
    the token, and it does not apply to package names (:func:`_package_name`)
    or to KiCad's ``Package_*`` libraries, whose names are packages
    (``Package_TO_SOT_THT:TO-8-2``, ``Package_DIP:Toshiba_11-7A9``)."""
    for i, token in enumerate(tokens):
        maker = _MAKERS.get(token.lower())
        if maker is None:
            continue
        for later in tokens[i + 1:]:
            if _looks_like_mpn(later):
                return maker, later
    if len(tokens) <= 2 and not lib.startswith("Package_"):
        last = tokens[-1]
        if (_looks_like_mpn(last) and len(last) >= 5
                and any(ch.isdigit() for ch in last) and not _package_name(last)):
            return "", last
    return None


def _split(lib_id: str) -> Tuple[str, str]:
    lib, sep, name = (lib_id or "").partition(":")
    return (lib, name) if sep else ("", lib)


# --- families -----------------------------------------------------------------


def _header(lib_id, name, tokens, geometry):
    family = HEADER if name.startswith("PinHeader_") else SOCKET
    return FootprintSignature(
        family=family,
        lib_id=lib_id,
        smd="SMD" in tokens,
        **geometry,
    )


def _terminal_block(lib_id, lib, tokens, geometry, pad_count):
    maker = ""
    if tokens[0] == "TerminalBlock":
        rest = tokens[1:]
    elif tokens[:2] == ["Terminal", "Block"]:
        rest = tokens[2:]
    else:
        rest = tokens
    if lib.startswith("TerminalBlock_"):
        maker = lib[len("TerminalBlock_"):]
    elif rest and rest[0].isalpha():
        maker = rest[0]
    if "per_row" not in geometry and pad_count:
        geometry = {**geometry, "rows": 1, "per_row": pad_count}
    return FootprintSignature(family=TERMINAL_BLOCK, lib_id=lib_id, maker=maker, **geometry)


def _capacitor(lib_id, tokens, geometry):
    style = _CAP_STYLES.get(tokens[1], "") if len(tokens) > 1 else ""
    if "Tantal" in tokens:
        style = f"{style} tantalum".strip()
    if "SnapIn" in tokens:
        style = f"{style} snap-in".strip()
    return FootprintSignature(
        family=CAPACITOR,
        lib_id=lib_id,
        style=style,
        polarized=tokens[0] == "CP",
        **geometry,
    )


def _resistor(lib_id, tokens, geometry):
    style = ""
    for key, value in _RES_STYLES.items():
        if tuple(tokens[1:1 + len(key)]) == key:
            style = value
            break
    if style == "sip":
        sip = next((_SIP.match(t) for t in tokens if _SIP.match(t)), None)
        if sip:
            geometry = {**geometry, "rows": 1, "per_row": int(sip.group(1))}
    return FootprintSignature(family=RESISTOR, lib_id=lib_id, style=style, **geometry)


def parse_signature(lib_id: str, pad_count: Optional[int] = None) -> FootprintSignature:
    """The :class:`FootprintSignature` of footprint *lib_id* (``Lib:Name``, or a
    bare name). *pad_count* fills the positions of a terminal block whose name
    gives none."""
    lib, name = _split(lib_id)
    tokens = [t for t in name.split("_") if t]
    if not tokens:
        return FootprintSignature(family=OTHER, lib_id=lib_id)
    geometry = _geometry(tokens)

    for pattern, what in _NO_PART:
        if pattern.match(name):
            return FootprintSignature(family=NO_PART, lib_id=lib_id, what=what, **geometry)
    if name.startswith("TestPoint_Loop"):
        return FootprintSignature(family=TEST_POINT, lib_id=lib_id, **geometry)
    if name.startswith(("PinHeader_", "PinSocket_")):
        return _header(lib_id, name, tokens, geometry)
    if lib.startswith("TerminalBlock") or name.startswith(("TerminalBlock_", "Terminal_Block_")):
        return _terminal_block(lib_id, lib, tokens, geometry, pad_count)
    if lib == "Capacitor_THT" or (tokens[0] in ("C", "CP") and len(tokens) > 1 and tokens[1] in _CAP_STYLES):
        return _capacitor(lib_id, tokens, geometry)
    if lib == "Resistor_THT" or (tokens[0] == "R" and len(tokens) > 1 and tokens[1] in ("Axial", "Array", "Box", "Bare", "Radial")):
        return _resistor(lib_id, tokens, geometry)

    named = _named_part(lib, tokens)
    if named:
        maker, mpn = named
        return FootprintSignature(family=NAMED_PART, lib_id=lib_id, maker=maker, mpn=mpn, **geometry)
    return FootprintSignature(family=OTHER, lib_id=lib_id, **geometry)
