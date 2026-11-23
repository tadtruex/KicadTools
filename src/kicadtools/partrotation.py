"""Persistent per-part rotation overrides for the JLCPCB CPL export.

KiCad and the LCSC/JLCPCB assembly libraries build some footprints to different
pin-1 conventions, so a part placed at 0° in KiCad can land rotated on JLCPCB's
line. The built-in :mod:`kicadtools.rotations` table corrects the *families* both
community tables agree on, but deliberately leaves the disputed ones (notably
SOT-23 and QFN) at 0° — which is exactly where a real board bites: e.g. every
SOT-23 transistor on the ECG board needs a manual +180°.

This module remembers those corrections **per LCSC part number**, so a rotation
dialed in once (in the BOM Cockpit's LCSC tab) is reused on every board that
places the same part. The value is an *additive* offset in degrees CCW, applied
on top of the family offset — the same semantics as ``build_cpl``'s
``manual_rotation`` argument, which is how the offset ultimately reaches the CPL.

The store shares the SQLite file with :class:`kicadtools.resolve.Cache` but owns
its own ``part_rotation`` table, so bumping the LCSC-selection version (which
wipes ``mpn_to_lcsc``) never touches these user-chosen rotations.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Mapping, Optional


class PartRotationStore:
    """SQLite-backed map of LCSC part number -> extra rotation (degrees CCW).

    Pass ``":memory:"`` for an ephemeral store (tests). Only non-zero offsets are
    kept: setting a part's rotation to 0 removes its row, since 0 is the default.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS part_rotation "
            "(lcsc TEXT PRIMARY KEY, degrees REAL NOT NULL)"
        )
        self.conn.commit()

    def get(self, lcsc: str) -> Optional[float]:
        """The stored extra rotation for *lcsc*, or ``None`` if none is set."""
        if not lcsc:
            return None
        row = self.conn.execute(
            "SELECT degrees FROM part_rotation WHERE lcsc = ?", (lcsc,)
        ).fetchone()
        return float(row[0]) if row else None

    def set(self, lcsc: str, degrees: float) -> None:
        """Record *degrees* of extra rotation for *lcsc*. A zero (mod 360) offset
        clears the entry, since it matches the no-override default."""
        if not lcsc:
            return
        normalized = float(degrees) % 360.0
        if normalized == 0.0:
            self.clear(lcsc)
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO part_rotation(lcsc, degrees) VALUES (?, ?)",
            (lcsc, normalized),
        )
        self.conn.commit()

    def clear(self, lcsc: str) -> None:
        """Remove any override for *lcsc* (back to the default 0°)."""
        self.conn.execute("DELETE FROM part_rotation WHERE lcsc = ?", (lcsc,))
        self.conn.commit()

    def all(self) -> Dict[str, float]:
        """Every stored override as ``{lcsc: degrees}``."""
        return {
            lcsc: float(deg)
            for lcsc, deg in self.conn.execute(
                "SELECT lcsc, degrees FROM part_rotation"
            )
        }

    def close(self) -> None:
        self.conn.close()


class DbRotationStore:
    """The :class:`PartRotationStore` interface over the parts database
    (:meth:`kicadtools.partsdb.PartsDb.rotation` and the rest), so the
    overrides are on the server with the parts (``DB_PLAN.md`` step 6).

    *db* returns the open database, so the store follows the Cockpit when it
    reconnects or goes offline. With no database, or a read-only one, reads
    give nothing or the copy's values and :meth:`set` calls *refused* and
    stores nothing."""

    def __init__(self, db, refused=None) -> None:
        self._db = db
        self._refused = refused

    def get(self, lcsc: str) -> Optional[float]:
        db = self._db()
        return db.rotation(lcsc) if db is not None else None

    def set(self, lcsc: str, degrees: float) -> None:
        db = self._db()
        if db is None or db.read_only:
            if self._refused is not None:
                self._refused()
            return
        db.set_rotation(lcsc, degrees)

    def clear(self, lcsc: str) -> None:
        self.set(lcsc, 0.0)

    def all(self) -> Dict[str, float]:
        db = self._db()
        return db.rotations() if db is not None else {}

    def close(self) -> None:
        """The database belongs to the Cockpit; nothing to close here."""


def manual_rotation_for_refs(
    ref_to_lcsc: Mapping[str, str], store: PartRotationStore
) -> Dict[str, float]:
    """Turn a ``{refdes: lcsc}`` map into the ``{refdes: degrees}`` dict that
    :func:`kicadtools.cpl.build_cpl` takes as ``manual_rotation``.

    References whose LCSC part has no stored override are omitted (they fall back
    to the family offset). This is the bridge that carries per-*part* overrides
    onto the per-*reference* placements in the CPL.
    """
    out: Dict[str, float] = {}
    for ref, lcsc in ref_to_lcsc.items():
        deg = store.get(lcsc) if lcsc else None
        if deg:
            out[ref] = deg
    return out
