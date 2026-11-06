"""The KiCad footprint a connector symbol with no footprint implies, read
from its Value, Description and footprint filters.

A schematic drawn from generic KiCad symbols often has connectors with no
footprint yet. Their text still says a good deal: KiCad's generic connector
symbols state rows and positions in the Description (``"Generic connector,
single row, 01x03"``), and the Value often names a series (``"TMP235 (JST PH
3)"``, ``"Stemma QT (LSM303AGR)"``) or a part (``"9V DC in"`` with
Description ``"DC Barrel Jack"``). :func:`implied_footprint` turns that into
the KiCad standard footprint the through-hole search reads
(:func:`kicadtools.thtfootprint.parse_signature`) and the write plan writes
when the chosen part fits it.

Rules, first match wins:

- **Stemma QT / Qwiic**: JST SH, 4 positions, side entry, surface mount
  (``SM04B-SRSS-TB``) [value];
- **JST SH n**: ``SM0nB-SRSS-TB``, side entry [value];
- **JST PH n**: ``BnB-PH-K``, top entry [value];
- **JST XH n**: ``BnB-XH-A``, top entry [value];
- **barrel jack** (``barrel jack``, ``DC jack``, ``DC in``, or a
  ``BarrelJack*`` footprint filter): CUI ``PJ-102AH``, 2.1 mm, through-hole
  [default];
- **generic connector** (``single row, 01xNN`` / ``double row, 02xNN`` in
  the Description): a 2.54 mm vertical pin header with those rows and
  positions [default].

``n`` is the number after the series name, else the Description's
positions. A series named with no position count gives nothing.

Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

# Where the implication came from: the Value names the series, or the
# Cockpit's default for what the text leaves open.
VALUE = "value"
DEFAULT = "default"

_GRID_RE = re.compile(r"\b(single|double)\s+row,\s*(\d{2})x(\d{2})\b", re.I)
_STEMMA_RE = re.compile(r"\b(stemma\s*qt|qwiic)\b", re.I)
_BARREL_RE = re.compile(r"\b(barrel\s*jack|dc\s*jack|dc\s+in)\b", re.I)


def _series_re(series: str) -> re.Pattern:
    return re.compile(rf"\bjst[\s_-]*{series}\b(?:[\s_-]*(\d{{1,2}}))?", re.I)


_JST = {
    "SH": (_series_re("sh"),
           "Connector_JST:JST_SH_SM{n:02d}B-SRSS-TB_1x{n:02d}-1MP_P1.00mm_Horizontal"),
    "PH": (_series_re("ph"), "Connector_JST:JST_PH_B{n}B-PH-K_1x{n:02d}_P2.00mm_Vertical"),
    "XH": (_series_re("xh"), "Connector_JST:JST_XH_B{n}B-XH-A_1x{n:02d}_P2.50mm_Vertical"),
}

BARREL_JACK = "Connector_BarrelJack:BarrelJack_CUI_PJ-102AH_Horizontal"
_HEADER = "Connector_PinHeader_2.54mm:PinHeader_{rows}x{n:02d}_P2.54mm_Vertical"


@dataclass(frozen=True)
class ImpliedFootprint:
    """A footprint implied by a symbol's text: the KiCad library id, where
    it came from (:data:`VALUE` or :data:`DEFAULT`) and why, as a line for a
    tooltip."""

    lib_id: str
    source: str
    basis: str


def _text(props: Mapping[str, object], key: str) -> str:
    v = props.get(key)
    return v.strip() if isinstance(v, str) else ""


def implied_footprint(props: Mapping[str, object]) -> Optional[ImpliedFootprint]:
    """The footprint a connector symbol with no footprint implies, else
    ``None``. Reads ``Value``, ``Description`` and ``ki_fp_filters``."""
    value = _text(props, "Value")
    description = _text(props, "Description")
    text = f"{value} {description}"
    grid = _GRID_RE.search(description)
    rows = 1 if grid is None else (1 if grid.group(1).lower() == "single" else 2)
    per_row = int(grid.group(3)) if grid is not None else None

    if _STEMMA_RE.search(text):
        return ImpliedFootprint(_JST["SH"][1].format(n=4), VALUE,
                                "Stemma QT / Qwiic is JST SH, 4 positions, side entry")
    for name, (pattern, template) in _JST.items():
        m = pattern.search(text)
        if m is None:
            continue
        n = int(m.group(1)) if m.group(1) else per_row
        if not n:
            return None
        entry = "side entry" if name == "SH" else "top entry"
        return ImpliedFootprint(template.format(n=n), VALUE,
                                f"JST {name}, {n} positions, {entry}")
    filters = _text(props, "ki_fp_filters").lower()
    if _BARREL_RE.search(text) or "barreljack" in filters:
        return ImpliedFootprint(BARREL_JACK, DEFAULT,
                                "barrel jack: CUI PJ-102AH (2.1 mm, through-hole) by default")
    if grid is not None and per_row:
        lib_id = _HEADER.format(rows=rows, n=per_row)
        return ImpliedFootprint(lib_id, DEFAULT,
                                f"{rows}x{per_row:02d} connector: 2.54 mm vertical pin "
                                "header by default")
    return None
