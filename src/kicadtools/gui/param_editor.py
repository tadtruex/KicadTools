"""A quick-entry dialog for a component's electrical-intent parameters.

Reached from the Intents tab's right-click menu ("Edit Electrical Parameters").
The point is to fill in the intent fields a parametric part search will need —
value, footprint, tolerance/power floors, preferred manufacturers — as fast as
possible, so a symbol that only carries a bare ``Value`` can be turned into a
fully-specified search query.

The field layout and the "assume this if the user left it blank" defaults live in
pure module-level helpers (:func:`fields_for`, :func:`prefilled_values`,
:func:`edits_from`) so they can be unit-tested without a running ``QApplication``;
:class:`ElectricalParamsDialog` is just the Qt rendering over them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from kicadtools.intent import ElectricalIntent


@dataclass(frozen=True)
class Field:
    """One editable parameter: a form label, the intent attribute it reflects,
    and the schematic ``(property …)`` key it is written back to.

    Every ``prop_key`` here is an intent-owned custom field (``Intent:Type``,
    ``<Param>:<Qualifier>``) — never one of KiCad's own fields (``Value``,
    ``Footprint``, ``Datasheet``…), which have KiCad-specific meaning and syntax
    and are left untouched.

    ``is_list`` fields are comma-separated in the UI (``Tolerance:Preferred`` and
    friends) and round-trip through the intent model's ``_parse_preferred``.
    """

    label: str
    attr: str
    prop_key: str
    is_list: bool = False


# KiCad-owned fields we show for context but NEVER write — ``(label, attr)``.
# ``value`` only exists on typed subclasses, so a field is rendered only when the
# intent actually has that attribute.
CONTEXT_ATTRS: List[tuple] = [
    ("KiCad Value", "value"),
    ("KiCad Footprint", "footprint"),
]

# Base-class fields — every component has these. ``Package:Preferred`` is the
# intent's package constraint (e.g. ``0603``), deliberately distinct from KiCad's
# ``Footprint`` (a full land-pattern path).
BASE_FIELDS: List[Field] = [
    Field("Intent Type", "intent_type", "Intent:Type"),
    Field("Package", "package_preferred", "Package:Preferred", is_list=True),
    Field("Preferred Mfrs", "mfr_preferred", "Mfr:Preferred", is_list=True),
]

# Type-specific fields, keyed by (lower-cased) Intent:Type. Extend as more intent
# subclasses gain a parametric editor. A resistor's resistance lives in KiCad's
# ``Value`` field, so it is shown as read-only context, not edited here.
TYPE_FIELDS: Dict[str, List[Field]] = {
    "resistor": [
        Field("Tolerance", "tolerance_preferred", "Tolerance:Preferred", is_list=True),
        Field("Power (min)", "power_min", "Power:Min"),
    ],
}

# "Assume this unless the symbol already specifies it" defaults, per intent type,
# keyed by the property they fill. Applied only where the current value is blank.
TYPE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "resistor": {
        "Package:Preferred": "0603",
        "Mfr:Preferred": "Yageo, Bourns, Samsung",
        "Tolerance:Preferred": "1%",
    },
}


def context_rows(intent: ElectricalIntent) -> List[tuple]:
    """``(label, value)`` pairs of KiCad-owned fields to display read-only, for
    the attributes this intent actually carries. A blank field shows as ``—``."""
    rows = []
    for label, attr in CONTEXT_ATTRS:
        if hasattr(intent, attr):
            rows.append((label, str(getattr(intent, attr) or "") or "—"))
    return rows


def _type_key(intent: ElectricalIntent) -> str:
    return (intent.intent_type or "").lower()


def fields_for(intent: ElectricalIntent) -> List[Field]:
    """The base fields plus any specific to *intent*'s type, in display order."""
    return list(BASE_FIELDS) + list(TYPE_FIELDS.get(_type_key(intent), []))


def _current(intent: ElectricalIntent, field: Field) -> str:
    """*intent*'s current value for *field*, as the string the UI shows (a list
    field joins on ``", "``; a missing/None attribute is empty)."""
    val = getattr(intent, field.attr, None)
    if field.is_list:
        return ", ".join(val) if val else ""
    return "" if val is None else str(val)


def prefilled_values(intent: ElectricalIntent) -> Dict[str, str]:
    """The value to seed each field with, keyed by property.

    Uses the symbol's current value; where that is blank, falls back to the
    per-type assumed default (e.g. a resistor with no footprint pre-fills
    ``0603``). A field the symbol already specifies is never overwritten by a
    default.
    """
    defaults = TYPE_DEFAULTS.get(_type_key(intent), {})
    out: Dict[str, str] = {}
    for field in fields_for(intent):
        current = _current(intent, field)
        out[field.prop_key] = current or defaults.get(field.prop_key, "")
    return out


def edits_from(values: Dict[str, str]) -> Dict[str, str]:
    """The write plan for one symbol: the entered *values* with blanks dropped
    and surrounding whitespace trimmed. Never writes an empty property."""
    return {k: v.strip() for k, v in values.items() if v and v.strip()}


class ElectricalParamsDialog(QDialog):
    """Edit one component's intent parameters. On accept, :meth:`edits` returns
    the ``{property_key: value}`` map to write back to the symbol."""

    def __init__(self, intent: ElectricalIntent, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit Electrical Parameters — {intent.reference}")

        prefill = prefilled_values(intent)
        form = QFormLayout()
        form.addRow("Reference", QLabel(intent.reference))
        # KiCad-owned fields, shown read-only for context (never written).
        for label, value in context_rows(intent):
            form.addRow(label, QLabel(value))
        self._editors: Dict[str, QLineEdit] = {}
        for field in fields_for(intent):
            editor = QLineEdit(prefill.get(field.prop_key, ""))
            self._editors[field.prop_key] = editor
            form.addRow(field.label, editor)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def edits(self) -> Dict[str, str]:
        """The non-empty ``{property_key: value}`` edits the user entered."""
        return edits_from({k: e.text() for k, e in self._editors.items()})
