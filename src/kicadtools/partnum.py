"""
Pull supplier / manufacturer part numbers out of a symbol's raw properties.

KiCad schematics in this repo spell the same field a dozen different ways —
``DigiKey PN``, ``Digikey``, ``DigikeyPartNumber``, ``Digi-Key_PN``,
``DigiKeyPN`` … — and a single symbol frequently carries several of those keys
at once, all but one of them empty. This module collapses that mess onto four
canonical fields by matching a normalized form of each key (lower-cased, with
every non-alphanumeric character removed) against an alias table, always
preferring a non-empty value.

No network, no schematic I/O — just dict-in, :class:`PartNumbers`-out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional


def _norm(key: str) -> str:
    """Normalize a property key for alias matching: lower-case, alphanumerics
    only (so ``"Digi-Key_PN "`` and ``"DigiKeyPN"`` both become ``digikeypn``)."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


# Canonical field -> ordered aliases (already normalized), most-specific first.
# Order only decides ties when two *different* normalized keys are both populated
# with conflicting values; empties never win regardless of order.
_ALIASES: Dict[str, List[str]] = {
    # JLCPCB / LCSC part number (e.g. "C541465")
    "lcsc": ["lcsc", "lcscpn", "lcscpartnumber", "lcscpart", "jlcpcb", "jlc"],
    # Manufacturer part number
    "mpn": [
        "mpn",
        "manufacturerpn",
        "manufacturerpartnumber",
        "mfrpn",
        "mfrpartnumber",
        "mfgpn",
    ],
    # DigiKey part number, in any of its spellings
    "digikey_pn": ["digikeypn", "digikeypartnumber", "digikeypart", "digikey"],
    # Manufacturer *name* (not a part number)
    "manufacturer": ["manufacturer", "mfr", "mfg"],
}


@dataclass(frozen=True)
class PartNumbers:
    """Canonical part-number fields resolved from a symbol's properties.

    Every field is either a non-empty string or ``None``. ``source_key`` records
    the raw property key each value came from, for reporting.
    """
    lcsc: Optional[str] = None
    mpn: Optional[str] = None
    digikey_pn: Optional[str] = None
    manufacturer: Optional[str] = None
    source_key: Dict[str, str] = None  # canonical field -> raw key it came from

    def __post_init__(self) -> None:
        if self.source_key is None:
            object.__setattr__(self, "source_key", {})


def extract_part_numbers(props: Mapping[str, object]) -> PartNumbers:
    """Resolve the canonical part-number fields from a symbol's raw *props*.

    Non-string values (the ``dnp``/``in_bom`` bools folded in by the parser) and
    empty/whitespace values are ignored. When several raw keys collapse to the
    same normalized key (e.g. an empty ``DigikeyPN`` alongside a populated
    ``DigiKey PN``), the non-empty value wins.
    """
    # normalized key -> (value, original key); prefer the first non-empty value.
    norm: Dict[str, tuple[str, str]] = {}
    for raw_key, raw_val in props.items():
        if not isinstance(raw_key, str) or not isinstance(raw_val, str):
            continue
        value = raw_val.strip()
        nk = _norm(raw_key)
        if nk not in norm or (not norm[nk][0] and value):
            norm[nk] = (value, raw_key)

    resolved: Dict[str, str] = {}
    source_key: Dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            entry = norm.get(alias)
            if entry and entry[0]:
                resolved[canonical] = entry[0]
                source_key[canonical] = entry[1]
                break

    return PartNumbers(
        lcsc=resolved.get("lcsc"),
        mpn=resolved.get("mpn"),
        digikey_pn=resolved.get("digikey_pn"),
        manufacturer=resolved.get("manufacturer"),
        source_key=source_key,
    )
