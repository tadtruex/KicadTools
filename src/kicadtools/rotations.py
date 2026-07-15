"""Per-footprint-family rotation offsets for the JLCPCB CPL export.

KiCad and the LCSC/JLCPCB assembly libraries build some footprints to different
IPC pin-1 conventions, so a part placed at 0° in KiCad lands rotated on JLCPCB's
line. The fix community tools use is an additive, per-family rotation offset
looked up by a regex over the footprint's library name (positive = CCW).

Two things to know:

* Offsets are **optional for a first-pass file** — JLCPCB's assembly previewer
  lets you rotate any part by hand — so an empty table still yields a usable CPL;
  the table just cuts down the manual corrections.
* The two canonical community tables (JLCKicadTools' ``cpl_rotations_db.csv`` and
  bennymeg Fabrication-Toolkit's ``transformations.csv``) **disagree** on some
  families (notably ``SOT-23`` and ``QFN``, off by 90–180°). We ship **only the
  offsets both tables agree on**, so every default here is low-risk; the disputed
  families are deliberately omitted (left at 0°, nudge them in the previewer).
  Verify against JLCPCB's previewer before trusting any addition.

See ``JLCPCB_SPEC.md`` §3.3 for the provenance of each entry.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Optional, Tuple

# (regex over the footprint library name, offset degrees CCW). First match wins,
# so list more specific patterns before their general fallback. Only families
# agreed by BOTH community tables are included.
_DEFAULT_RULES: Tuple[Tuple[str, float], ...] = (
    # SO-family and flat-pack ICs.
    (r"^SOIC-", 270.0),
    (r"^SSOP-", 270.0),
    (r"^TSSOP-", 270.0),
    (r"^DFN-", 270.0),
    (r"^SOP-4_", 0.0),   # opto/relay 4-pin SOP: no offset (special-cased in both)
    (r"^SOP-18_", 0.0),
    (r"^SOP-", 270.0),
    (r"^LQFP-", 270.0),
    (r"^TQFP-", 270.0),
    # Polarized electrolytic caps.
    (r"^CP_EIA-", 180.0),
    (r"^CP_Elec_", 180.0),
    # Connectors / misc agreed families.
    (r"^JST_GH_SM", 180.0),
    (r"^JST_PH_S", 180.0),
    (r"^TO-277", 90.0),
    (r"^LED_WS2812B-2020_PLCC4", 90.0),
)


def _footprint_body(footprint: str) -> str:
    """Drop the ``Library:`` prefix — patterns match on the footprint name."""
    return footprint.split(":", 1)[1] if ":" in footprint else footprint


class RotationDb:
    """Compiled family-offset lookup. Construct with :func:`default_rotation_db`
    or pass your own ``(pattern, degrees)`` rules (e.g. loaded from a CSV)."""

    def __init__(self, rules: Iterable[Tuple[str, float]]) -> None:
        self._rules: List[Tuple[re.Pattern, float]] = [
            (re.compile(pat), float(deg)) for pat, deg in rules
        ]

    def offset(self, footprint: str) -> float:
        """The rotation offset (degrees CCW) for *footprint*'s library name; the
        first matching rule wins, else ``0.0``."""
        body = _footprint_body(str(footprint or ""))
        for pattern, deg in self._rules:
            if pattern.search(body):
                return deg
        return 0.0


def default_rotation_db() -> RotationDb:
    """The built-in low-risk (both-tables-agree) family-offset table."""
    return RotationDb(_DEFAULT_RULES)
