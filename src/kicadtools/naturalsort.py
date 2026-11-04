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


# A leading number: an optional currency sign, digits with optional thousands
# commas and a decimal part, then an optional SI prefix (``10µF``, ``4.7k``).
_NUMBER = re.compile(
    r"^\s*[$€£]?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)([pnuµμmkKMG](?![a-z]))?"
)
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def display_key(value) -> tuple:
    """A sort key for a table cell's text. A cell that starts with a number
    sorts by that number's value, ahead of text: ``1,234`` after ``987``,
    ``$0.12`` before ``$0.5``, ``100nF`` before ``10µF``, ``6.3V`` before
    ``25V``. The rest of the text, and every other cell, sorts by
    :func:`natural_key`."""
    s = "" if value is None else str(value).strip()
    m = _NUMBER.match(s)
    if m is None:
        return ((1, 0.0), natural_key(s))
    number = float(m.group(1).replace(",", "")) * _SI.get(m.group(2) or "", 1.0)
    return ((0, number), natural_key(s[m.end():]))
