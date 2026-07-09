"""
Resolve every placed BOM part to an LCSC (JLCPCB) part number.

The pipeline, per part:

1. Has an LCSC number already  -> trusted, no lookup (``OK``).
2. Has an MPN                   -> JLCPCB search -> LCSC (``RESOLVED``).
3. Has a DigiKey PN             -> DigiKey -> MPN -> JLCPCB -> LCSC.
4. Has none of the above        -> ``FLAGGED`` for manual work.

Any lookup miss or error at steps 2-3 degrades the part to ``FLAGGED`` with a
recorded reason. A small SQLite cache memoizes the two network hops so reruns
are cheap and kinder to DigiKey's rate limit.

This module is deliberately GUI-agnostic: :class:`Resolver` takes injected
supplier callables (so tests run offline) and the CLI / a future Cockpit panel
both drive :meth:`Resolver.resolve`.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kicadtools.partnum import PartNumbers, extract_part_numbers
from kicadtools.sexpr_parser import (
    extract_properties,
    iter_symbol_instances,
    iter_symbols_from_sexpr,
    load_schematic_tree,
    symbol_uuid,
)
from kicadtools.suppliers import DigiKeyClient, jlcpcb_lcsc_for


class Status(str, Enum):
    OK = "ok"              # already carried an LCSC number
    RESOLVED = "resolved"  # LCSC found via lookup
    FLAGGED = "flagged"    # needs manual attention


@dataclass
class PartRecord:
    """A placed BOM part with its raw properties and write-back provenance."""
    reference: str
    props: Dict[str, object]
    source_file: Path
    symbol_uuid: Optional[str]


def iter_bom_part_records(root_path: Path) -> List[PartRecord]:
    """Yield one :class:`PartRecord` per placed BOM part across the hierarchy.

    Applies the same filter as ``components.bom_components`` — drops power/flag
    symbols (``#…`` refs), do-not-place parts, and parts excluded from the BOM —
    but keeps the *raw* property dict (which the typed intent model discards).
    """
    records: List[PartRecord] = []
    for sch_path, sexpr in load_schematic_tree(Path(root_path)):
        for symbol in iter_symbols_from_sexpr(sexpr):
            base_props = extract_properties(symbol)
            uuid = symbol_uuid(symbol)
            for ref, _sheet_path in iter_symbol_instances(symbol):
                if ref.startswith("#"):
                    continue
                if base_props.get("dnp", False):
                    continue
                if not base_props.get("in_bom", True):
                    continue
                props = dict(base_props)
                props["Reference"] = ref
                records.append(PartRecord(ref, props, sch_path, uuid))
    return records


@dataclass
class Resolution:
    """Outcome of resolving one part to an LCSC number."""
    reference: str
    part: PartNumbers
    status: Status
    entry: str                       # "lcsc" | "mpn" | "digikey" | "none"
    reason: str
    source_file: Path
    symbol_uuid: Optional[str]
    lcsc: Optional[str] = None
    resolved_mpn: Optional[str] = None  # MPN discovered via DigiKey (step 3)


class Cache:
    """SQLite memo of the two network hops: DigiKey PN -> MPN, MPN -> LCSC.

    Only positive results are stored, so a transient network failure never
    poisons a later run. Pass ``":memory:"`` for an ephemeral cache.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS dk_to_mpn "
            "(dk_pn TEXT PRIMARY KEY, mpn TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS mpn_to_lcsc "
            "(mpn TEXT PRIMARY KEY, lcsc TEXT NOT NULL)"
        )
        self.conn.commit()

    def get_mpn(self, dk_pn: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT mpn FROM dk_to_mpn WHERE dk_pn = ?", (dk_pn,)
        ).fetchone()
        return row[0] if row else None

    def put_mpn(self, dk_pn: str, mpn: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO dk_to_mpn(dk_pn, mpn) VALUES (?, ?)",
            (dk_pn, mpn),
        )
        self.conn.commit()

    def get_lcsc(self, mpn: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT lcsc FROM mpn_to_lcsc WHERE mpn = ?", (mpn,)
        ).fetchone()
        return row[0] if row else None

    def put_lcsc(self, mpn: str, lcsc: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO mpn_to_lcsc(mpn, lcsc) VALUES (?, ?)",
            (mpn, lcsc),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class Resolver:
    """Drive the LCSC resolution pipeline for individual parts.

    *digikey* is anything with an ``mpn_for(dk_pn) -> Optional[str]`` method;
    *jlcpcb* is a ``mpn -> Optional[str]`` callable. Both default to the live
    clients but are injectable so tests stay offline. *delay* seconds are slept
    after each **live** network call (cache hits don't sleep).
    """

    def __init__(
        self,
        digikey: Optional[DigiKeyClient] = None,
        jlcpcb: Callable[[str], Optional[str]] = jlcpcb_lcsc_for,
        cache: Optional[Cache] = None,
        delay: float = 0.0,
        progress: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.digikey = digikey if digikey is not None else DigiKeyClient()
        self.jlcpcb = jlcpcb
        self.cache = cache
        self.delay = delay
        # progress(reference, message) — fired only for real network calls (not
        # cache hits), so a caller can show which lookup is in flight.
        self.progress = progress
        self._ref: str = ""

    def _emit(self, message: str) -> None:
        if self.progress:
            self.progress(self._ref, message)

    def _mpn_for(self, dk_pn: str) -> Optional[str]:
        if self.cache and (hit := self.cache.get_mpn(dk_pn)):
            return hit
        self._emit(f"DigiKey lookup for {dk_pn} → MPN…")
        mpn = self.digikey.mpn_for(dk_pn)
        if self.delay:
            time.sleep(self.delay)
        if mpn and self.cache:
            self.cache.put_mpn(dk_pn, mpn)
        return mpn

    def _lcsc_for(self, mpn: str) -> Optional[str]:
        if self.cache and (hit := self.cache.get_lcsc(mpn)):
            return hit
        self._emit(f"JLCPCB lookup for MPN {mpn} → LCSC…")
        lcsc = self.jlcpcb(mpn)
        if self.delay:
            time.sleep(self.delay)
        if lcsc and self.cache:
            self.cache.put_lcsc(mpn, lcsc)
        return lcsc

    def resolve(self, record: PartRecord) -> Resolution:
        self._ref = record.reference
        pn = extract_part_numbers(record.props)

        def make(status: Status, entry: str, reason: str, **extra) -> Resolution:
            return Resolution(
                reference=record.reference,
                part=pn,
                status=status,
                entry=entry,
                reason=reason,
                source_file=record.source_file,
                symbol_uuid=record.symbol_uuid,
                **extra,
            )

        # 1) Already has an LCSC number — trusted.
        if pn.lcsc:
            return make(Status.OK, "lcsc", "already has LCSC", lcsc=pn.lcsc)

        # Determine the manufacturer part number and how we got there.
        resolved_mpn: Optional[str] = None
        if pn.mpn:
            entry, mpn = "mpn", pn.mpn
        elif pn.digikey_pn:
            entry = "digikey"
            mpn = self._mpn_for(pn.digikey_pn)
            if not mpn:
                return make(
                    Status.FLAGGED,
                    entry,
                    f"DigiKey PN {pn.digikey_pn!r} did not resolve to an MPN",
                )
            resolved_mpn = mpn
        else:
            # 4) Nothing to go on.
            return make(
                Status.FLAGGED,
                "none",
                "no LCSC, MPN, or DigiKey PN on the part",
            )

        # 2/3) We have an MPN — search JLCPCB for the LCSC number.
        lcsc = self._lcsc_for(mpn)
        if lcsc:
            return make(
                Status.RESOLVED,
                entry,
                f"LCSC {lcsc} ← MPN {mpn}",
                lcsc=lcsc,
                resolved_mpn=resolved_mpn,
            )
        return make(
            Status.FLAGGED,
            entry,
            f"no LCSC match for MPN {mpn}",
            resolved_mpn=resolved_mpn,
        )

    def resolve_all(self, records: List[PartRecord]) -> List[Resolution]:
        return [self.resolve(r) for r in records]
