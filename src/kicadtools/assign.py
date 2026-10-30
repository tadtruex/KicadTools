"""
Write plan for assigning a chosen capacitor or resistor to a requirement group.

:func:`assignment_plan` turns one :class:`~kicadtools.requirement.RequirementGroup`,
its (possibly edited) :class:`~kicadtools.requirement.PartRequirement` and
the chosen candidate into the ``{source_file: {symbol_uuid: {key: value}}}``
shape that :func:`kicadtools.writer.write_properties` applies. A value of
``None`` in the plan means "remove this property".

Per symbol the plan holds:

* ``Intent:Type`` = the part type (``capacitor``, ``resistor``) when the type
  was guessed;
* ``Value`` when the symbol's Value is empty and the value came from the
  existing part or the Description (a stated Value is never replaced);
* the rating floor (``Voltage:Min`` / ``Power:Min``), ``Class:Preferred``
  (capacitors) and ``Tolerance:Preferred`` when that field's value came from
  a default, an edit, a relaxed limit, the existing part or the Description
  (a value stated on the schematic is left as written);
* ``Package:Preferred`` when the package was edited and the symbol has no
  footprint;
* ``MPN``, ``Manufacturer``, ``DigiKeyPN`` and ``LCSC`` from the candidate,
  under the canonical keys of :data:`kicadtools.partnum.CANONICAL_KEYS`;
* ``Footprint`` from the part type's footprint table
  (:attr:`kicadtools.parttypes.PartType.footprints`) when the symbol's
  footprint is empty;
* ``None`` for every other spelling of the four part-number fields present on
  the symbol, so the symbol ends with the canonical keys only.

Values are written in the terse form: ``16V``, ``100mW``, ``10%``,
``X7R, X5R``.

Pure: no schematic I/O and no network I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from kicadtools.partnum import _ALIASES, _norm, CANONICAL_KEYS
from kicadtools.requirement import (
    DEFAULT,
    DESCRIPTION,
    EDITED,
    PART,
    RELAXED,
    PartRequirement,
    RequirementGroup,
    Sourced,
)
from kicadtools.units import dielectric_tokens, fmt_percent

if TYPE_CHECKING:  # partsearch imports partsdb; the plan reads four attributes only
    from kicadtools.partsearch import Candidate

# Sources whose value the plan writes to the symbol, so the requirement is
# stated on the schematic after an accept.
_WRITTEN_SOURCES = (DEFAULT, EDITED, RELAXED, PART, DESCRIPTION)

# Canonical field -> the candidate attribute that holds its value.
_CANDIDATE_ATTRS: Dict[str, str] = {
    "mpn": "mpn",
    "manufacturer": "manufacturer",
    "digikey_pn": "digikey_pn",
    "lcsc": "lcsc",
}


def _written(field: Sourced) -> bool:
    """True when *field*'s source is in :data:`_WRITTEN_SOURCES` and it has a
    value."""
    return field.source in _WRITTEN_SOURCES and field.value not in (None, "", ())


def _text(value: object) -> str:
    """A property value as a stripped string; ``""`` for anything else."""
    return value.strip() if isinstance(value, str) else ""


def _dielectric_text(codes) -> str:
    """``"X7R, X5R"``: the codes normalized, in order, without repeats."""
    out = []
    for entry in codes:
        for code in dielectric_tokens(str(entry)):
            if code not in out:
                out.append(code)
    return ", ".join(out)


def _footprint_for(req: PartRequirement) -> str:
    """The footprint for the requirement's package, from its part type's
    table."""
    pkg = req.package.value
    footprints = req.ptype.footprints
    try:
        return footprints[pkg]
    except KeyError:
        known = ", ".join(sorted(footprints))
        raise ValueError(
            f"no footprint for package {pkg!r}; known packages: {known}"
        ) from None


def _alias_removals(props: Dict[str, object]) -> Dict[str, None]:
    """``{key: None}`` for every property on the symbol that is a spelling of
    one of the four part-number fields other than its canonical key."""
    canonical = set(CANONICAL_KEYS.values())
    aliases = {a for names in _ALIASES.values() for a in names}
    return {
        key: None
        for key in props
        if isinstance(key, str) and key not in canonical and _norm(key) in aliases
    }


def assignment_plan(
    group: RequirementGroup, req: PartRequirement, cand: "Candidate"
) -> Dict[Path, Dict[str, Dict[str, Optional[str]]]]:
    """Plan the property writes that assign *cand* to every member of *group*.

    *req* is the requirement as accepted in the dialog (``group.requirement``
    with any edits applied). *cand* is read for ``mpn``, ``manufacturer``,
    ``digikey_pn`` and ``lcsc`` only. Members without a source
    file or symbol uuid are skipped. Raises ``ValueError`` when any of the four
    part-number values is empty on *cand* (a partial write would remove alias
    spellings and leave the group unassigned), when *req* lists conflicts
    between its sources, or when a member has no footprint and the package
    has no footprint in the part type's table.
    """
    if req.conflicts:
        raise ValueError(
            f"conflicting sources: {'; '.join(req.conflicts)}; "
            "fix the schematic first"
        )
    ptype = req.ptype
    value_written = (
        req.nominal_source in (PART, DESCRIPTION)
        and req.nominal is not None
        and req.nominal > 0
    )

    # Requirement fields, identical for every member.
    req_props: Dict[str, str] = {}
    if _written(req.rating_min):
        req_props[ptype.rating_property] = ptype.fmt_rating(req.rating_min.value)
    if ptype.has_dielectric and _written(req.dielectrics):
        req_props["Class:Preferred"] = _dielectric_text(req.dielectrics.value)
    if _written(req.tolerance_max_pct):
        req_props["Tolerance:Preferred"] = fmt_percent(req.tolerance_max_pct.value)

    part_props: Dict[str, str] = {
        CANONICAL_KEYS[field]: _text(getattr(cand, attr, None))
        for field, attr in _CANDIDATE_ATTRS.items()
    }
    missing = [key for key, value in part_props.items() if not value]
    if missing:
        raise ValueError(
            f"candidate {_text(getattr(cand, 'mpn', None)) or '?'} has no "
            f"{', '.join(missing)}; all four part numbers are required"
        )

    plan: Dict[Path, Dict[str, Dict[str, Optional[str]]]] = {}
    for m in group.members:
        if m.source_file is None or not m.symbol_uuid:
            continue
        has_footprint = bool(_text(m.props.get("Footprint")))

        props: Dict[str, Optional[str]] = {}
        if getattr(m.intent, "intent_guessed", False):
            props["Intent:Type"] = ptype.name
        if value_written and not _text(m.props.get("Value")):
            props["Value"] = ptype.fmt_nominal(req.nominal)
        props.update(req_props)
        if req.package.source == EDITED and not has_footprint:
            props["Package:Preferred"] = req.package.value
        props.update(part_props)
        if not has_footprint:
            props["Footprint"] = _footprint_for(req)
        props.update(_alias_removals(m.props))

        # A subsheet symbol instantiated twice appears as two members with
        # one uuid; both produce the same entry.
        plan.setdefault(Path(m.source_file), {})[m.symbol_uuid] = props
    return plan
