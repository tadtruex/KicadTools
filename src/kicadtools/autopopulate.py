"""Autopopulate: parts-database matches for every open Parts-tab row, with no
supplier call.

:func:`find_matches` looks each unassigned group up in the parts database
only:

- capacitors, resistors and inductors: :meth:`PartsDb.find_parts` with the
  group's requirement (value, package, rating at least, class or better,
  tolerance at most);
- ICs: :meth:`PartsDb.find_ics` for the MPN field and each MPN-shaped token
  of the Value;
- through-hole parts: :meth:`PartsDb.find_tht` for the footprint signature
  (and Value, :func:`kicadtools.thtdb.db_key`).

Each match gets a tier, which the dialog shows as the Accept cell's colour:

- :data:`EXACT` (green): the stored part is the one the symbol already names
  (MPN, DigiKey PN or LCSC), or an IC named exactly by its MPN; or, for a
  capacitor, resistor or inductor whose rating, class and tolerance are all
  stated (on the schematic, by the existing part or the Description), the
  stored part's values equal them;
- :data:`NEAR` (orange): the stored part meets stated fields but exceeds one
  (25V for 16V, X7R for X5R, 10 % for 20 %); a part-named through-hole part
  stored for the same footprint and Value;
- :data:`UNSURE` (red): the match rests on a default or unset field (rating,
  class or tolerance not stated anywhere), or a header, socket or terminal
  block found by footprint alone.

Matches are ordered by usage (most first), then tier. Stock and price are
not checked: nothing here calls a supplier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from kicadtools.icrequirement import IcGroup
from kicadtools.icsearch import DATABASE as IC_DATABASE
from kicadtools.icsearch import IcCandidate, check_footprint
from kicadtools.package import parse_footprint
from kicadtools.partsdb import PartsDb, norm_mpn
from kicadtools.partsearch import Candidate, _from_database
from kicadtools.requirement import (
    DESCRIPTION,
    EDITED,
    PART,
    SCHEMATIC,
    RequirementGroup,
    _codes,
    allowed_classes,
)
from kicadtools.suppliers import DigiKeyProduct
from kicadtools.thtassign import DATABASE as THT_DATABASE
from kicadtools.thtassign import ThtCandidate, ThtGroup
from kicadtools.thtdb import SIGNATURE_ONLY, db_key

EXACT = "exact"
NEAR = "near"
UNSURE = "unsure"
_TIER_RANK = {EXACT: 0, NEAR: 1, UNSURE: 2}

# Sources that count as stated for a capacitor, resistor or inductor field.
STATED = frozenset({SCHEMATIC, PART, DESCRIPTION, EDITED})

_EPS = 1e-9


@dataclass
class Match:
    """One stored part for one row. ``candidate`` is what the window queues:
    a :class:`~kicadtools.partsearch.Candidate`, an
    :class:`~kicadtools.icsearch.IcCandidate` or a
    :class:`~kicadtools.thtassign.ThtCandidate`. ``why`` says how the tier
    was decided."""

    candidate: Any
    tier: str
    why: str
    part_id: int
    usage_count: int

    @property
    def mpn(self) -> str:
        return self.candidate.mpn

    @property
    def manufacturer(self) -> str:
        return self.candidate.manufacturer


@dataclass
class RowMatches:
    """The matches for one Parts-tab row, best first."""

    row: int
    group: Any
    matches: List[Match] = field(default_factory=list)

    @property
    def best(self) -> Optional[Match]:
        return self.matches[0] if self.matches else None


def _names_current(group: Any, mpn: str, digikey_pn: Optional[str], lcsc: Optional[str]) -> bool:
    """True when the group's existing part numbers name this stored part."""
    e = group.existing
    pairs = ((e.mpn, mpn), (e.digikey_pn, digikey_pn), (e.lcsc, lcsc))
    return any(a and b and norm_mpn(a) == norm_mpn(b) for a, b in pairs)


def passive_tier(group: RequirementGroup, cand: Candidate) -> Tuple[str, str]:
    """The tier of a stored capacitor, resistor or inductor for *group*."""
    if _names_current(group, cand.mpn, cand.digikey_pn, cand.lcsc):
        return EXACT, "the part the symbol already names"
    req = group.requirement
    t = req.ptype
    fields = [("rating", req.rating_min)]
    if t.has_class:
        fields.append(("class", req.dielectrics))
    fields.append(("tolerance", req.tolerance_max_pct))
    loose = [name for name, f in fields if f.source not in STATED]
    if loose:
        return UNSURE, "not stated: " + ", ".join(loose) + " (default or unset)"
    p = cand.params
    exceeds = []
    if req.rating_min.value is not None and p.rating is not None \
            and p.rating > float(req.rating_min.value) + _EPS:
        exceeds.append(f"rating {t.fmt_rating(p.rating)} for {t.fmt_rating(float(req.rating_min.value))}")
    if t.has_class:
        stated = set(_codes(t, req.dielectrics.value or ()))
        got = set(t.class_tokens(p.dielectric))
        if stated and got and not (got & stated):
            exceeds.append(f"class {', '.join(sorted(got))} for {', '.join(sorted(stated))}")
    if req.tolerance_max_pct.value is not None and p.tolerance_pct is not None \
            and p.tolerance_pct < float(req.tolerance_max_pct.value) - _EPS:
        exceeds.append(f"tolerance {p.tolerance_pct:g}% for {float(req.tolerance_max_pct.value):g}%")
    if exceeds:
        return NEAR, "exceeds: " + "; ".join(exceeds)
    return EXACT, "values equal the stated requirement"


def _passive_matches(group: RequirementGroup, db: PartsDb) -> List[Match]:
    req = group.requirement
    if not req.searchable or req.conflicts:
        return []
    hits = db.find_parts(
        kind=req.kind,
        nominal=float(req.nominal),
        package=req.package.value,
        rating_min=req.rating_min.value,
        dielectrics=allowed_classes(req.ptype, tuple(req.dielectrics.value or ())),
        tolerance_max_pct=req.tolerance_max_pct.value,
    )
    out = []
    for hit in hits:
        cand = _from_database(hit, lambda code: None, None)
        tier, why = passive_tier(group, cand)
        out.append(Match(cand, tier, why, hit.part_id, hit.usage_count))
    return out


def _ic_matches(group: IcGroup, db: PartsDb, pads: Optional[int]) -> List[Match]:
    req = group.requirement
    ids = req.identifiers
    seen = set()
    out = []
    for text in (ids.mpn, *ids.value_mpns):
        if not text:
            continue
        for hit in db.find_ics(text):
            if hit.part_id in seen:
                continue
            seen.add(hit.part_id)
            cand = IcCandidate(
                mpn=hit.mpn, manufacturer=hit.manufacturer, package=hit.package,
                package_case=hit.package_case, pins=hit.pins, digikey_pn=hit.digikey_pn,
                lcsc=hit.lcsc, jlc_library=hit.jlc_library, source=IC_DATABASE, current=True,
                fields=("MPN",) if ids.mpn and norm_mpn(ids.mpn) == norm_mpn(hit.mpn) else ("Value",),
                db_part_id=hit.part_id, usage_count=hit.usage_count,
                description=hit.description, datasheet_url=hit.datasheet_url,
            )
            cand.footprint = check_footprint(parse_footprint(req.footprint), pads,
                                             cand.package_info())
            out.append(Match(cand, EXACT, f"named exactly by {cand.fields[0]} {text}",
                             hit.part_id, hit.usage_count))
    return out


def _tht_matches(group: ThtGroup, db: PartsDb) -> List[Match]:
    req = group.requirement
    if req.no_part or not req.footprint:
        return []
    sig = req.signature
    out = []
    for hit in db.find_tht(*db_key(sig, req.value_text)):
        product = DigiKeyProduct(mpn=hit.mpn, manufacturer=hit.manufacturer,
                                 digikey_pns=[hit.digikey_pn] if hit.digikey_pn else [],
                                 description=hit.description, datasheet_url=hit.datasheet_url)
        cand = ThtCandidate(product=product, signature=sig, value=req.value_text,
                            source=THT_DATABASE, label="parts database",
                            db_part_id=hit.part_id, usage_count=hit.usage_count)
        if _names_current(group, hit.mpn, hit.digikey_pn, None):
            tier, why = EXACT, "the part the symbol already names"
        elif sig.family in SIGNATURE_ONLY:
            tier, why = UNSURE, "found by footprint alone"
        else:
            tier, why = NEAR, "stored for the same footprint and Value"
        out.append(Match(cand, tier, why, hit.part_id, hit.usage_count))
    return out


def find_matches(groups: Sequence[Any], db: PartsDb, *, open_rows: Sequence[int],
                 pad_count=None) -> List[RowMatches]:
    """The stored matches for each row in *open_rows* (indexes into *groups*),
    rows without a match left out. *pad_count* is an optional
    ``(reference, footprint) -> int | None`` for the IC footprint check."""
    out: List[RowMatches] = []
    for row in open_rows:
        group = groups[row]
        if isinstance(group, IcGroup):
            pads = None
            if pad_count is not None and group.requirement.footprint:
                pads = pad_count(group.references[0], group.requirement.footprint)
            matches = _ic_matches(group, db, pads)
        elif isinstance(group, ThtGroup):
            matches = _tht_matches(group, db)
        elif isinstance(group, RequirementGroup):
            matches = _passive_matches(group, db)
        else:
            matches = []
        if matches:
            matches.sort(key=lambda m: (-m.usage_count, _TIER_RANK[m.tier], m.mpn))
            out.append(RowMatches(row, group, matches))
    return out
