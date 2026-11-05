"""
Write plan for assigning a chosen LED to a plain-LED group (``LED_PLAN.md``,
step 5), in the ``{source_file: {symbol_uuid: {key: value}}}`` shape of
:func:`kicadtools.writer.write_properties` (``None`` removes a property).

Per symbol:

* ``Intent:Type`` = ``led`` when the type was guessed;
* ``Color:Preferred`` when the colour was read from the Value or
  Description, taken from the existing part, or edited (a stated one is
  left as written);
* ``Wavelength``, ``Intensity:Min``, ``Vf:Max`` and ``Lens:Preferred`` when
  edited in the dialog;
* ``MPN``, ``Manufacturer``, ``DigiKeyPN`` and ``LCSC`` from the candidate;
* ``Footprint`` (``LED_SMD:LED_0603_1608Metric``) when the symbol has none;
* ``None`` for every other spelling of the four part-number fields.

``Value`` is never written.

Pure: no schematic I/O and no network I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from kicadtools.assign import _alias_removals, _text
from kicadtools.ledrequirement import TEXT, LedGroup, LedRequirement
from kicadtools.partnum import CANONICAL_KEYS
from kicadtools.requirement import EDITED, PART

_METRIC = {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216",
           "1210": "3225"}

LED_FOOTPRINTS: Dict[str, str] = {
    size: f"LED_SMD:LED_{size}_{m}Metric" for size, m in _METRIC.items()}


def led_assignment_plan(group: LedGroup, req: LedRequirement, cand
                        ) -> Dict[Path, Dict[str, Dict[str, Optional[str]]]]:
    """Plan the writes that assign *cand* (an
    :class:`~kicadtools.ledsearch.LedCandidate`) to every member of *group*,
    with *req* as accepted. Raises ``ValueError`` when *req* has conflicts,
    a part number is missing on *cand*, or a member without a footprint has
    a package with no entry in :data:`LED_FOOTPRINTS`."""
    if req.conflicts:
        raise ValueError(f"conflicting sources: {'; '.join(req.conflicts)}; "
                         "fix the schematic first")
    part_props = {
        CANONICAL_KEYS["mpn"]: _text(cand.mpn),
        CANONICAL_KEYS["manufacturer"]: _text(cand.manufacturer),
        CANONICAL_KEYS["digikey_pn"]: _text(cand.digikey_pn),
        CANONICAL_KEYS["lcsc"]: _text(cand.lcsc),
    }
    missing = [k for k, v in part_props.items() if not v]
    if missing:
        raise ValueError(f"candidate {_text(cand.mpn) or '?'} has no "
                         f"{', '.join(missing)}; all four part numbers are required")

    req_props: Dict[str, str] = {}
    if req.colors.source in (TEXT, PART, EDITED) and req.colors.value:
        req_props["Color:Preferred"] = ", ".join(req.colors.value)
    if req.wavelength.source == EDITED and req.wavelength.value is not None:
        req_props["Wavelength"] = f"{req.wavelength.value:g}nm"
    if req.intensity_min.source == EDITED and req.intensity_min.value is not None:
        req_props["Intensity:Min"] = f"{req.intensity_min.value:g}mcd"
    if req.vf_max.source == EDITED and req.vf_max.value is not None:
        req_props["Vf:Max"] = f"{req.vf_max.value:g}V"
    if req.lenses.source == EDITED and req.lenses.value:
        req_props["Lens:Preferred"] = ", ".join(req.lenses.value)

    plan: Dict[Path, Dict[str, Dict[str, Optional[str]]]] = {}
    for m in group.members:
        if m.source_file is None or not m.symbol_uuid:
            continue
        props: Dict[str, Optional[str]] = {}
        if getattr(m.intent, "intent_guessed", False):
            props["Intent:Type"] = "led"
        props.update(req_props)
        props.update(part_props)
        if not _text(m.props.get("Footprint")):
            pkg = req.package.value
            if pkg not in LED_FOOTPRINTS:
                raise ValueError(f"no LED footprint for package {pkg!r}")
            props["Footprint"] = LED_FOOTPRINTS[pkg]
        props.update(_alias_removals(m.props))
        plan.setdefault(Path(m.source_file), {})[m.symbol_uuid] = props
    return plan
