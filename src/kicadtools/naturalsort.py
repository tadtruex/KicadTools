"""A small, dependency-free natural-sort key.

Lives outside :mod:`kicadtools.gui` (which pulls in PySide6) so non-GUI code —
the CLI, the JLCPCB BOM builder — can order reference designators the same way
the GUI tables do.
"""
from __future__ import annotations

import re


def natural_key(value) -> tuple:
    """A sort key that orders embedded numbers numerically, so ``C2`` sorts
    before ``C10``. Digit runs compare as ints (and ahead of text), so mixed
    chunks never raise."""
    s = "" if value is None else str(value)
    key = []
    for chunk in re.split(r"(\d+)", s.lower()):
        if chunk.isdigit():
            key.append((0, int(chunk)))
        elif chunk:
            key.append((1, chunk))
    return tuple(key)
