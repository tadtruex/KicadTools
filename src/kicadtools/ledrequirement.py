"""
LED requirements and grouping (``LED_PLAN.md``, step 2).

Surface-mount LEDs come in two kinds, sorted by
:func:`kicadtools.ledcolor.led_kind`:

* **Plain** (one colour on a chip footprint) get the passive treatment. An
  :class:`LedRequirement` holds the package, the colour (and so the
  wavelength band a part must fall in), a stated wavelength, a minimum
  intensity, a maximum forward voltage and the lens; each is a
  :class:`~kicadtools.requirement.Sourced` value. Grouped into
  :class:`LedGroup` s.
* **Multicolour and smart** LEDs are found from what the symbol names, as ICs
  are: an :class:`NamedLedGroup` holds an
  :class:`~kicadtools.icrequirement.IcRequirement` (kind ``"led"``) whose
  ``value_mpns`` are the part-number tokens of the footprint name and the
  Value.

Field sources (tags from :mod:`kicadtools.requirement`, plus
:data:`TEXT`):

* package — the footprint's chip size, ``Package:Preferred`` [schematic],
  the existing part [part], the ``led.package`` setting [default];
* colour — ``Color:Preferred`` [schematic], a colour named in the Value or
  Description [text], the existing part's colour [part]; else unset, and
  the row is not searchable. There is no default colour;
* wavelength — ``Wavelength`` [schematic], a wavelength in the Value or
  Description (``Red 630nm``) [text]; else any. A wavelength narrows the
  band to ±10 nm around it;
* intensity minimum (``Intensity:Min``, mcd), forward-voltage maximum
  (``Vf:Max``) and lens (``Lens:Preferred``: clear, diffused) — stated
  [schematic], else any. There is no default brightness.

A stated colour or wavelength whose band does not hold the existing part's
wavelength is a conflict line, as is a stated colour the Value contradicts
and a package two sources disagree on; a requirement with a conflict is not
searchable.

Through-hole LEDs are left to the through-hole rows
(:mod:`kicadtools.thtassign`, ``THT_PLAN.md`` step 7).

Pure: no schematic I/O and no network I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools import ledcolor
from kicadtools.evidence import ExistingPart
from kicadtools.icrequirement import (
    IcGroup,
    IcRequirement,
    Identifiers,
    ic_identifiers,
    normalize_identifier,
)
from kicadtools.intent import LedIntent
from kicadtools.mounting import THT
from kicadtools.naturalsort import natural_key
from kicadtools.partnum import extract_part_numbers
from kicadtools.requirement import (
    ANY,
    DEFAULT,
    EDITED,
    PART,
    SCHEMATIC,
    UNSET,
    Member,
    Sourced,
    _prop,
)
from kicadtools.thtfootprint import looks_like_part_number
from kicadtools.units import chip_size, package_code, parse_volts

LED_KIND = "led"

# Source tag: read from the colour or wavelength named in the Value or
# Description. The write plan writes Color:Preferred for this source.
TEXT = "text"

# Lens kinds, and the supplier and schematic words for each.
CLEAR = "clear"
DIFFUSED = "diffused"
_LENS_WORDS = (
    (DIFFUSED, ("diffus", "milky", "frosted", "translucent")),
    (CLEAR, ("clear", "transparent", "colorless", "colourless")),
)

_WAVELENGTH_RE = re.compile(r"(?<![0-9.])([0-9]{3,4}(?:\.[0-9]+)?)\s*nm\b",
                            re.IGNORECASE)

# Problem lines.
NO_COLOR = "no colour (Color:Preferred)"


def lens_kind(text: object) -> Optional[str]:
    """:data:`CLEAR` or :data:`DIFFUSED` from a lens text (``"Water
    Clear"``, ``"Diffused"``, DigiKey's ``"Colorless"``), else ``None``."""
    low = str(text or "").lower()
    for kind, words in _LENS_WORDS:
        if any(w in low for w in words):
            return kind
    return None


@dataclass(frozen=True)
class LedEvidence:
    """What a supplier lookup of the symbol's existing part says about it,
    one entry per die. ``label`` names the source, for display."""
    label: str
    package: Optional[str] = None
    dominant: Tuple[float, ...] = ()
    peak: Tuple[float, ...] = ()
    labels: Tuple[Optional[str], ...] = ()
    intensity_mcd: Tuple[float, ...] = ()
    vf: Tuple[float, ...] = ()
    lens: Optional[str] = None

    @property
    def dies(self) -> int:
        """The number of dies the part lists (the longest per-die field)."""
        return max(len(self.dominant), len(self.peak), len(self.labels), 0)

    @property
    def color(self) -> Optional[str]:
        """The colour of a one-die part: the colour whose band holds its
        dominant wavelength (the narrowest band when bands overlap), else the
        label of a label-only colour; ``None`` otherwise."""
        if self.dies > 1:
            return None
        if self.dominant:
            return color_at(self.dominant[0])
        if self.peak:
            hit = color_at(self.peak[0])
            if hit == "infrared":
                return hit
        if self.labels and self.labels[0]:
            c = ledcolor.color(self.labels[0])
            if c is not None and c.label_only:
                return c.name
        return None

    def wavelength_text(self) -> str:
        """``"525 nm"`` / ``"569, 621 nm"`` for messages; ``"no wavelength"``
        when none is listed."""
        values = self.dominant or self.peak
        if not values:
            return "no wavelength"
        return ", ".join(f"{v:g}" for v in values) + " nm"


def color_at(nm: float) -> Optional[str]:
    """The colour whose band holds *nm*: the narrowest when two do (590 nm is
    amber, 12 nm wide, rather than yellow, 15 nm), else ``None``."""
    hits = [c for c in ledcolor.COLORS.values()
            if c.band is not None and c.band[0] <= nm <= c.band[1]]
    if not hits:
        return None
    return min(hits, key=lambda c: c.band[1] - c.band[0]).name


@dataclass(frozen=True)
class LedRequirement:
    """What any acceptable part for one plain LED must meet."""
    value_text: str                  # the symbol's Value, as written
    footprint: str                   # the symbol's Footprint, "" when empty
    package: Sourced                 # Optional[str], a chip size
    colors: Sourced                  # Tuple[str, ...], colour names; any one will do
    wavelength: Sourced              # Optional[float] nm, narrows the band
    intensity_min: Sourced           # Optional[float] mcd
    vf_max: Sourced                  # Optional[float] V
    lenses: Sourced                  # Tuple[str, ...]: CLEAR, DIFFUSED
    conflicts: Tuple[str, ...] = ()
    kind: str = LED_KIND

    @property
    def searchable(self) -> bool:
        return not self.problem()

    def problem(self) -> str:
        """``""`` when searchable, else a one-line reason; the first conflict
        comes first."""
        if self.conflicts:
            return self.conflicts[0]
        pkg = self.package.value
        if not pkg:
            if self.footprint:
                return f"footprint '{self.footprint}' is not a chip size"
            return "no package"
        if package_code(pkg) != pkg:
            return f"package '{pkg}' is not a known chip size"
        if not self.colors.value:
            if self.colors.source == SCHEMATIC:
                return "Color:Preferred on the schematic is not a colour"
            return NO_COLOR
        return ""

    def bands(self) -> Tuple[Tuple[str, Optional[Tuple[float, float]]], ...]:
        """(colour, band) per colour: the colour's band, or ±10 nm around a
        stated wavelength; ``None`` for a label-only colour."""
        wl = self.wavelength.value
        stated = f"{wl}nm" if wl is not None else None
        return tuple((c, ledcolor.band_for(c, stated)) for c in self.colors.value)

    def band_text(self) -> str:
        """The bands as the dialog shows them, ``"green 510–540 nm"``;
        ``""`` with no colour."""
        wl = self.wavelength.value
        stated = f"{wl}nm" if wl is not None else None
        return "; ".join(f"{c} {ledcolor.band_text(c, stated)}"
                         for c in self.colors.value)

    def color_matches(self, ev: LedEvidence) -> Optional[bool]:
        """Whether a part (its supplier evidence) has one of the colours:
        :func:`kicadtools.ledcolor.in_band` for each, ``True`` when any is,
        ``False`` when all say no, ``None`` when none can tell."""
        wl = self.wavelength.value
        stated = f"{wl}nm" if wl is not None else None
        results = [ledcolor.in_band(c, dominant=ev.dominant, peak=ev.peak,
                                    labels=ev.labels, wavelength=stated)
                   for c in self.colors.value]
        if any(r is True for r in results):
            return True
        if results and all(r is False for r in results):
            return False
        return None

    def edited(self, *, colors: Optional[Sequence[str]] = None,
               wavelength: Optional[float] = None,
               intensity_min: Optional[float] = None,
               vf_max: Optional[float] = None,
               lenses: Optional[Sequence[str]] = None,
               clear: Sequence[str] = ()) -> "LedRequirement":
        """A copy with each argument that is not ``None`` in place of its
        field, tagged ``edited``, and each field named in *clear*
        (``"wavelength"``, ``"intensity_min"``, ``"vf_max"``, ``"lenses"``)
        set to no limit, tagged ``any``. Colour names are read through
        :func:`kicadtools.ledcolor.color`; an unknown name raises
        ``ValueError``. The package is set by the footprint."""
        changes: Dict[str, Sourced] = {}
        for name in clear:
            if name not in _CLEARABLE:
                raise ValueError(f"{name} cannot be cleared")
            changes[name] = Sourced(_CLEARABLE[name], ANY)
        if colors is not None:
            names = []
            for text in colors:
                c = ledcolor.color(text)
                if c is None:
                    raise ValueError(f"'{text}' is not a colour")
                if c.name not in names:
                    names.append(c.name)
            changes["colors"] = Sourced(tuple(names), EDITED)
        if wavelength is not None:
            changes["wavelength"] = Sourced(float(wavelength), EDITED)
        if intensity_min is not None:
            changes["intensity_min"] = Sourced(float(intensity_min), EDITED)
        if vf_max is not None:
            changes["vf_max"] = Sourced(float(vf_max), EDITED)
        if lenses is not None:
            changes["lenses"] = Sourced(_lenses(lenses), EDITED)
        return replace(self, **changes)


_CLEARABLE: Dict[str, Any] = {
    "wavelength": None,
    "intensity_min": None,
    "vf_max": None,
    "lenses": (),
}


def _lenses(entries: Sequence[str]) -> Tuple[str, ...]:
    out: List[str] = []
    for e in entries:
        k = lens_kind(e)
        if k and k not in out:
            out.append(k)
    return tuple(out)


def _text(props: Mapping[str, object]) -> str:
    return " ".join(_prop(props, k) for k in ("Value", "Description"))


def _wavelength_in(text: str) -> Optional[float]:
    m = _WAVELENGTH_RE.search(text)
    return float(m.group(1)) if m else None


def _stated_colors(entries: Sequence[str]) -> Tuple[Tuple[str, ...], bool]:
    """The colour names in ``Color:Preferred`` entries, and whether every
    entry was a colour."""
    names: List[str] = []
    ok = True
    for e in entries:
        c = ledcolor.color(e)
        if c is None:
            ok = False
        elif c.name not in names:
            names.append(c.name)
    return tuple(names), ok


def _package(intent: Any, footprint: str, defaults: Mapping[str, str],
             part: Optional[LedEvidence]) -> Sourced:
    if footprint:
        return Sourced(package_code(footprint), SCHEMATIC)
    if intent.package_preferred:
        text = intent.package_preferred[0]
        return Sourced(package_code(text) or chip_size(text) or text, SCHEMATIC)
    if part is not None and part.package:
        return Sourced(part.package, PART)
    pkg = str(defaults.get("led.package", "") or "").strip()
    if pkg:
        return Sourced(package_code(pkg) or chip_size(pkg) or pkg, DEFAULT)
    return Sourced(None, UNSET)


def _package_conflicts(pairs: Sequence[Tuple[str, Optional[str]]]) -> List[str]:
    known = [(label, pkg) for label, pkg in pairs if label and pkg]
    lines = []
    for i, (la, a) in enumerate(known):
        for lb, b in known[i + 1:]:
            if a != b:
                lines.append(f"package: {la} says {a}, {lb} says {b}")
    return lines


def led_requirement(intent: LedIntent, props: Mapping[str, object],
                    defaults: Mapping[str, str], *,
                    part: Optional[LedEvidence] = None) -> LedRequirement:
    """The requirement for one plain LED from its *intent*, raw *props*, the
    *defaults* mapping (:func:`kicadtools.requirement.part_defaults`) and the
    evidence of its existing *part*. See the module docstring for the order
    of sources and the conflicts."""
    footprint = _prop(props, "Footprint")
    value_text = _prop(props, "Value")
    text = _text(props)
    part_label = part.label if part is not None else ""
    conflicts: List[str] = []

    # Colour.
    stated, stated_ok = _stated_colors(intent.color_preferred)
    from_text = ledcolor.colors_of(props)
    if intent.color_preferred:
        colors = Sourced(stated if stated_ok else (), SCHEMATIC)
        if stated and from_text and not set(stated) & set(from_text):
            conflicts.append(
                f"colour: Color:Preferred says {', '.join(stated)}, "
                f"the Value says {', '.join(from_text)}")
    elif len(from_text) == 1:
        colors = Sourced(from_text, TEXT)
    elif part is not None and part.color:
        colors = Sourced((part.color,), PART)
    else:
        colors = Sourced((), UNSET)

    # Wavelength.
    if intent.wavelength_stated:
        wl_values = ledcolor.parse_nm(intent.wavelength_stated)
        wavelength = Sourced(wl_values[0] if wl_values else None, SCHEMATIC)
    else:
        wl = _wavelength_in(text)
        wavelength = Sourced(wl, TEXT) if wl is not None else Sourced(None, ANY)

    # Limits: stated or none.
    if intent.intensity_min:
        mcd = ledcolor.parse_mcd(intent.intensity_min)
        intensity = Sourced(mcd[0] if mcd else None, SCHEMATIC)
    else:
        intensity = Sourced(None, ANY)
    if intent.vf_max:
        vf_max = Sourced(parse_volts(intent.vf_max), SCHEMATIC)
    else:
        vf_max = Sourced(None, ANY)
    lenses = (Sourced(_lenses(intent.lens_preferred), SCHEMATIC)
              if intent.lens_preferred else Sourced((), ANY))

    package = _package(intent, footprint, defaults, part)
    conflicts += _package_conflicts([
        ("Footprint", package_code(footprint) if footprint else None),
        ("Package:Preferred",
         (package_code(intent.package_preferred[0])
          or chip_size(intent.package_preferred[0]))
         if intent.package_preferred else None),
        (part_label, part.package if part is not None else None),
    ])

    req = LedRequirement(
        value_text=value_text,
        footprint=footprint,
        package=package,
        colors=colors,
        wavelength=wavelength,
        intensity_min=intensity,
        vf_max=vf_max,
        lenses=lenses,
    )
    if part is not None:
        if part.dies > 1:
            conflicts.append(f"{part_label} has {part.dies} dies; this is a "
                             "one-colour LED")
        elif colors.value and colors.source != PART:
            if req.color_matches(part) is False:
                conflicts.append(
                    f"colour: {req.band_text()}, but {part_label} is at "
                    f"{part.wavelength_text()}")
    return replace(req, conflicts=tuple(conflicts))


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #

@dataclass
class LedGroup:
    """Plain LEDs that state the same fields (by meaning) and hold the same
    existing part numbers, and so share one requirement and one part."""
    requirement: LedRequirement
    members: List[Member]

    @property
    def kind(self) -> str:
        return LED_KIND

    @property
    def existing(self) -> ExistingPart:
        return self.members[0].existing

    @property
    def references(self) -> List[str]:
        return sorted({m.reference for m in self.members}, key=natural_key)

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def assigned(self) -> bool:
        """Every member has an MPN, Manufacturer, DigiKey PN, LCSC number and
        a Footprint (JLCPCB places the part)."""
        for m in self.members:
            pn = extract_part_numbers(m.props)
            if not (pn.mpn and pn.manufacturer and pn.digikey_pn and pn.lcsc):
                return False
            if not _prop(m.props, "Footprint"):
                return False
        return True

    def requirement_with_part(self, part: Optional[LedEvidence],
                              defaults: Mapping[str, str]) -> LedRequirement:
        """The requirement recomputed from the first member with *part* as
        the existing part's evidence."""
        first = self.members[0]
        return led_requirement(first.intent, first.props, defaults, part=part)


@dataclass
class NamedLedGroup(IcGroup):
    """Multicolour or smart LEDs found by part number, as ICs are; the
    requirement's kind is ``"led"``. ``led_kind`` is
    :data:`~kicadtools.ledcolor.MULTICOLOR` or
    :data:`~kicadtools.ledcolor.SMART`."""
    led_kind: str = ledcolor.MULTICOLOR

    @property
    def kind(self) -> str:
        return LED_KIND


def footprint_tokens(footprint: object) -> Tuple[str, ...]:
    """Part-number tokens of a footprint name (``StockedComponents:LTST-C155GEKT``
    -> ``("LTST-C155GEKT",)``). A footprint in an ``LED*`` library
    (``LED_SMD:LED_RGB_5050-6``) gives none: those names are geometry."""
    lib, _, name = str(footprint or "").rpartition(":")
    name = name.strip()
    if not name or lib.upper().startswith("LED"):
        return ()
    tokens = [name] + [t for t in re.split(r"[_\s]+", name) if t != name]
    return tuple(t for t in tokens if looks_like_part_number(t)
                 and not package_code(t) and not chip_size(t)
                 and not t.lower().endswith("metric"))


def named_led_identifiers(props: Mapping[str, object]) -> Identifiers:
    """The IC identifiers of a symbol, with the footprint-name tokens ahead
    of the Value tokens (a footprint named after the part spells it with its
    hyphens, ``LTST-C155GEKT``; the Value often does not)."""
    ids = ic_identifiers(props)
    seen = set()
    tokens: List[str] = []
    for t in footprint_tokens(props.get("Footprint")) + ids.value_mpns:
        n = normalize_identifier(t).replace("-", "")
        if n not in seen:
            seen.add(n)
            tokens.append(t)
    return replace(ids, value_mpns=tuple(tokens))


def _is_tht(rec: Any, board: Any) -> bool:
    """Whether the record is a through-hole part: by *board*
    (:class:`kicadtools.mounting.BoardMounting`) when given, else by a
    ``_THT`` footprint library (``LED_THT:LED_D5.0mm``)."""
    lib_id = _prop(rec.props, "Footprint")
    if board is not None:
        return board.mounting(rec.reference, lib_id or None).kind == THT
    return lib_id.split(":", 1)[0].upper().endswith("_THT")


def _number_key(value: Optional[float]) -> Any:
    return round(value, 6) if value is not None else None


def group_key(intent: LedIntent, props: Mapping[str, object]) -> Tuple[Any, ...]:
    """The key of a plain LED: footprint, ``Package:Preferred``, colours and
    stated fields by meaning, and the existing part numbers. With no colour
    from any field the Value is compared as written."""
    req = led_requirement(intent, props, {})
    pn = extract_part_numbers(props)
    colors = tuple(sorted(req.colors.value))
    return (
        req.footprint,
        tuple(intent.package_preferred),
        colors if colors else ("value", _prop(props, "Value")),
        req.colors.source == SCHEMATIC and not req.colors.value,
        _number_key(req.wavelength.value),
        _number_key(req.intensity_min.value),
        _number_key(req.vf_max.value),
        tuple(sorted(req.lenses.value)),
        (pn.mpn or "").upper(),
        (pn.digikey_pn or "").upper(),
        (pn.lcsc or "").upper(),
    )


def group_leds(records: Sequence[Any], defaults: Mapping[str, str], *,
               board: Any = None) -> Tuple[List[LedGroup], List[NamedLedGroup]]:
    """The surface-mount LED records (intent :class:`~kicadtools.intent.LedIntent`)
    as plain groups and named groups. Through-hole LEDs (:func:`_is_tht`) are
    skipped: they are through-hole rows. Named groups key as ICs do, on the
    identifiers, the footprint and, with no identifier, the Value. Each list
    is ordered by first reference."""
    plain: Dict[Tuple[Any, ...], LedGroup] = {}
    named: Dict[Tuple[Any, ...], NamedLedGroup] = {}
    for rec in records:
        intent = rec.intent
        if not isinstance(intent, LedIntent) or _is_tht(rec, board):
            continue
        member = Member(reference=rec.reference, source_file=rec.source_file,
                        symbol_uuid=rec.symbol_uuid, props=rec.props,
                        intent=intent)
        kind = ledcolor.led_kind(rec.props)
        if kind == ledcolor.PLAIN:
            key = group_key(intent, rec.props)
            group = plain.get(key)
            if group is None:
                plain[key] = LedGroup(
                    led_requirement(intent, rec.props, defaults), [member])
            else:
                group.members.append(member)
            continue
        ids = named_led_identifiers(rec.props)
        value = normalize_identifier(_prop(rec.props, "Value")) if ids.empty else ""
        key = (kind,) + ids.key() + (_prop(rec.props, "Footprint"), value)
        group = named.get(key)
        if group is None:
            req = IcRequirement(identifiers=ids,
                                value_text=_prop(rec.props, "Value"),
                                footprint=_prop(rec.props, "Footprint"),
                                kind=LED_KIND)
            named[key] = NamedLedGroup(req, [member], led_kind=kind)
        else:
            group.members.append(member)

    def order(groups):
        return sorted(groups, key=lambda g: natural_key(g.references[0]))
    return order(plain.values()), order(named.values())
