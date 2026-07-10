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
from typing import Dict, List, Mapping, Optional, Tuple


def _norm(key: str) -> str:
    """Normalize a property key for alias matching: lower-case, alphanumerics
    only (so ``"Digi-Key_PN "`` and ``"DigiKeyPN"`` both become ``digikeypn``)."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


# Canonical field -> the single property-key spelling we normalize toward when
# writing back into a schematic. Reading tolerates every alias below; writing
# collapses them onto exactly one of these.
CANONICAL_KEYS: Dict[str, str] = {
    "lcsc": "LCSC",
    "mpn": "MPN",
    "digikey_pn": "DigiKeyPN",
    "manufacturer": "Manufacturer",
}


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


@dataclass(frozen=True)
class PropEdit:
    """One property edit to apply to a symbol when normalizing its keys.

    * ``rename`` — change property ``old_key``'s key to the canonical ``key``,
      leaving its value untouched.
    * ``set``    — set the existing canonical ``key``'s value to ``value``.
    * ``remove`` — delete the whole ``key`` property node.
    """
    op: str                         # "rename" | "set" | "remove"
    key: str                        # canonical key (rename/set) or key to drop (remove)
    old_key: Optional[str] = None   # source key, for "rename"
    value: Optional[str] = None     # the value that ends up under `key` (rename/set)

    def describe(self) -> str:
        """One-line, human-readable summary for dry-run output."""
        if self.op == "rename":
            return f'rename "{self.old_key}" → "{self.key}"  ({self.value})'
        if self.op == "set":
            return f'set    "{self.key}" = "{self.value}"'
        return f'remove "{self.key}"'


def normalization_edits(props: Mapping[str, object]) -> List[PropEdit]:
    """Plan the edits that collapse a symbol's part-number keys onto their
    canonical spellings (see :data:`CANONICAL_KEYS`).

    For each canonical field that carries a **non-empty** value, the value is
    moved onto the canonical key and every other variant spelling of that field
    is dropped. Fields with only empty variants are left untouched (there is no
    value worth relocating), so the planner never churns blank properties.

    Returns an empty list when the symbol is already canonical — pure analysis,
    no schematic I/O.
    """
    pn = extract_part_numbers(props)
    values = {
        "lcsc": pn.lcsc,
        "mpn": pn.mpn,
        "digikey_pn": pn.digikey_pn,
        "manufacturer": pn.manufacturer,
    }
    raw_keys = [k for k in props if isinstance(k, str)]

    edits: List[PropEdit] = []
    for canonical_field, aliases in _ALIASES.items():
        value = values[canonical_field]
        if not value:
            continue  # nothing to preserve — leave any empty variants in place
        canonical = CANONICAL_KEYS[canonical_field]
        source = pn.source_key[canonical_field]  # raw key holding the value
        variants = [k for k in raw_keys if _norm(k) in aliases]
        others: Tuple[str, ...] = tuple(
            k for k in variants if k != canonical and k != source
        )

        if source == canonical:
            # Already under the canonical key; just clear stray duplicate spellings.
            edits.extend(PropEdit("remove", k) for k in others)
        elif canonical in variants:
            # A (usually empty) canonical property already exists — fill it and
            # drop the variant the value came from, plus any other spellings.
            edits.append(PropEdit("set", canonical, value=value))
            edits.append(PropEdit("remove", source))
            edits.extend(PropEdit("remove", k) for k in others)
        else:
            # Rename the value's key onto the canonical spelling in place.
            edits.append(PropEdit("rename", canonical, old_key=source, value=value))
            edits.extend(PropEdit("remove", k) for k in others)

    return edits
