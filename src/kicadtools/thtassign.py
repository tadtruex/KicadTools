"""Through-hole groups for the Parts tab, their search and their write plan
(``THT_PLAN.md`` step 9). No Qt: the Cockpit's model, dialog and worker use
this module.

A through-hole group is a set of BOM parts that are not capacitors,
resistors, inductors or ICs (those have their own rows), whose footprint is
through-hole (:mod:`kicadtools.mounting`), and that share a footprint and a
Value and the part numbers already on them. A connector with no footprint
joins with the footprint its text implies
(:func:`kicadtools.impliedfootprint.implied_footprint`: a 2.54 mm header for
KiCad's generic connector symbols, the JST series the Value names, a barrel
jack), and the write plan writes that footprint when the chosen part fits it.
An implied footprint that is surface mount (JST SH, Stemma QT) gives a
surface-mount connector row instead (:mod:`kicadtools.smdconnector`). Each group has a
:class:`ThtRequirement`: the footprint's signature
(:mod:`kicadtools.thtfootprint`), its pad count, and the Value. What the row
can do depends on the signature's family:

- header or socket: :class:`~kicadtools.headersearch.HeaderRun` (one series
  per run);
- named part: :func:`~kicadtools.namedsearch.find_named_candidates`, which
  needs a part number in the footprint name, the MPN field or the Value;
- no part (solder-wire pads, mounting holes, bare test pads): nothing to
  buy, status "no part";
- anything else (terminal blocks, through-hole passives without an intent,
  loop test points, footprints that name no part): not searchable yet.

:func:`assess_tht` runs the search for one group and gives a
:class:`ThtSearchResult` of :class:`ThtCandidate` rows, the same for both
searches. :func:`tht_assignment_plan` is the write plan for the part the
user accepts: ``MPN``, ``Manufacturer`` and ``DigiKeyPN``; ``LCSC`` removed
(a hand-soldered part needs none, and an LCSC number left on the symbol
could name another part); other spellings of the part-number fields
removed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.assign import _alias_removals, _text
from kicadtools.headersearch import HeaderRun
from kicadtools.icsearch import MATCH, UNVERIFIED
from kicadtools.impliedfootprint import ImpliedFootprint, implied_footprint
from kicadtools.intent import ConnectorIntent, IcIntent
from kicadtools.mounting import BoardMounting
from kicadtools.namedsearch import (MPN_FIELD, NO_TEXT, PadCheck, find_named_candidates,
                                   mpn_fits, named_texts, pad_check)
from kicadtools.naturalsort import natural_key
from kicadtools.partnum import CANONICAL_KEYS, extract_part_numbers
from kicadtools.partsdb import PartsDb, ThtChoice, norm_mpn
from kicadtools.parttypes import type_of_intent
from kicadtools.pcb import SMD, THT
from kicadtools.requirement import Member
from kicadtools.suppliers import DigiKeyProduct, preferred_digikey_pn
from kicadtools.thtdb import DATABASE_LABEL, stock_warning, tht_choice, value_key
from kicadtools.thtfootprint import (
    HEADER,
    NAMED_PART,
    NO_PART,
    SOCKET,
    FootprintSignature,
    parse_signature,
)

THT_KIND = "tht"

DATABASE = "database"
CURRENT = "current"  # named by the symbol's DigiKeyPN or MPN
SEARCH = "search"

SEARCHABLE_FAMILIES = frozenset({HEADER, SOCKET, NAMED_PART})

NO_FOOTPRINT = "no footprint"


def _prop(props: Mapping[str, object], key: str) -> str:
    v = props.get(key)
    return v.strip() if isinstance(v, str) else ""


def signature_text(sig: FootprintSignature) -> str:
    """A signature as one short line for the Package cell: ``"header 1x04,
    2.54 mm, vertical"``, ``"named part JST S3B-PH-K"``, ``"no part (mounting
    hole)"``."""
    if sig.family == NO_PART:
        return f"no part ({sig.what})" if sig.what else "no part"
    if sig.family == NAMED_PART:
        return " ".join(x for x in ("named part", sig.maker, sig.mpn) if x)
    parts = []
    if sig.per_row is not None:
        parts.append(f"{sig.rows or 1}x{sig.per_row:02d}")
    if sig.pitch_mm is not None:
        parts.append(f"{sig.pitch_mm:g} mm")
    if sig.orientation:
        parts.append(sig.orientation)
    if sig.smd:
        parts.append("SMD")
    head = sig.family + (" " + parts[0] if parts else "")
    return ", ".join([head] + parts[1:])


@dataclass(frozen=True)
class ThtRequirement:
    """What a through-hole group is searched by."""

    signature: FootprintSignature
    footprint: str = ""
    value_text: str = ""
    pad_count: Optional[int] = None
    props: Mapping[str, object] = field(default_factory=dict)  # the first member's
    mounting_note: str = ""
    # The footprint the text implies, for a symbol with none (the signature
    # is read from it).
    implied: Optional[ImpliedFootprint] = None
    kind: str = THT_KIND

    @property
    def family(self) -> str:
        return self.signature.family

    @property
    def no_part(self) -> bool:
        return self.family == NO_PART

    @property
    def identifiers(self) -> Tuple[Tuple[str, str], ...]:
        """``(field, text)`` for the symbol's DigiKeyPN and MPN, when set."""
        pn = extract_part_numbers(self.props)
        return tuple((name, text) for name, text in (
            (CANONICAL_KEYS["digikey_pn"], pn.digikey_pn), (CANONICAL_KEYS["mpn"], pn.mpn)) if text)

    def problem(self) -> str:
        """``""`` when the group can be searched (or needs no part), else a
        one-line reason. A DigiKeyPN or MPN on the symbol makes any family
        searchable: the part it names is looked up."""
        if not self.footprint and self.implied is None:
            return NO_FOOTPRINT
        if self.no_part or self.identifiers:
            return ""
        if self.family not in SEARCHABLE_FAMILIES:
            return f"{self.family} footprints are not searched yet"
        if self.family == NAMED_PART and not named_texts(self.signature, self.props):
            return NO_TEXT
        return ""

    @property
    def searchable(self) -> bool:
        return not self.no_part and not self.problem()


@dataclass
class ThtGroup:
    """Through-hole parts that share one requirement and one part choice."""

    requirement: ThtRequirement
    members: List[Member]

    @property
    def kind(self) -> str:
        return THT_KIND

    @property
    def existing(self):
        return self.members[0].existing

    @property
    def references(self) -> List[str]:
        return sorted({m.reference for m in self.members}, key=natural_key)

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def assigned(self) -> bool:
        """Every member has an MPN, Manufacturer, DigiKey PN (no LCSC
        number: the part is soldered by hand) and a Footprint."""
        for m in self.members:
            pn = extract_part_numbers(m.props)
            if not (pn.mpn and pn.manufacturer and pn.digikey_pn):
                return False
            if not _prop(m.props, "Footprint"):
                return False
        return True


def _has_own_row(intent: Any) -> bool:
    """Capacitors, resistors and inductors (value search) and ICs (part
    number search) have rows of their own."""
    return isinstance(intent, IcIntent) or type_of_intent(intent) is not None


def group_key(props: Mapping[str, object]) -> Tuple[str, ...]:
    pn = extract_part_numbers(props)
    return (_prop(props, "Footprint"), value_key(_prop(props, "Value")),
            norm_mpn(pn.mpn), norm_mpn(pn.digikey_pn))


def group_tht(records: Sequence[Any], board: BoardMounting) -> List[ThtGroup]:
    """The through-hole groups among *records* (from
    :func:`kicadtools.resolve.iter_bom_part_records`), with mounting and pad
    counts read through *board*. Ordered by first reference."""
    by_key: Dict[Tuple[str, ...], ThtGroup] = {}
    for rec in records:
        if _has_own_row(rec.intent):
            continue
        lib_id = _prop(rec.props, "Footprint")
        implied = None
        if not lib_id and isinstance(rec.intent, ConnectorIntent):
            implied = implied_footprint(rec.props)
            if implied is None:
                continue
            mounting = board.mounting(rec.reference, implied.lib_id)
            if mounting.kind == SMD:
                continue    # a surface-mount connector row (smdconnector.py)
        else:
            mounting = board.mounting(rec.reference, lib_id or None)
            if mounting.kind != THT:
                continue
        key = group_key(rec.props)
        member = Member(reference=rec.reference, source_file=rec.source_file,
                        symbol_uuid=rec.symbol_uuid, props=rec.props, intent=rec.intent)
        group = by_key.get(key)
        if group is not None:
            group.members.append(member)
            continue
        placed = board.footprints.placed(rec.reference)
        search_lib = lib_id or (implied.lib_id if implied is not None else "")
        if placed is not None:
            pads = len({p.number for p in placed.pads if p.number})
        elif implied is not None:
            fp = board.footprints.library_footprint(implied.lib_id)
            pads = len({p.number for p in fp.pads if p.number}) if fp is not None else None
        else:
            pads = board.footprints.count(rec.reference, lib_id) if lib_id else None
        req = ThtRequirement(
            signature=parse_signature(search_lib, pads),
            footprint=lib_id,
            value_text=_prop(rec.props, "Value"),
            pad_count=pads,
            props=dict(rec.props),
            mounting_note=mounting.note if implied is None else "",
            implied=implied,
        )
        by_key[key] = ThtGroup(req, [member])
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))


# --- candidates -----------------------------------------------------------------


@dataclass
class ThtCandidate:
    """One DigiKey part for a through-hole group."""

    product: DigiKeyProduct
    signature: FootprintSignature
    value: str
    source: str = SEARCH  # DATABASE or SEARCH
    label: str = ""
    exact: bool = False
    pads: PadCheck = field(default_factory=lambda: PadCheck(UNVERIFIED))
    stock_warning: str = ""
    db_part_id: Optional[int] = None
    usage_count: int = 0
    series_match: bool = False  # the run's series predicts this MPN

    @property
    def mpn(self) -> str:
        return (self.product.mpn or "").strip()

    @property
    def manufacturer(self) -> str:
        return (self.product.manufacturer or "").strip()

    @property
    def digikey_pn(self) -> Optional[str]:
        return preferred_digikey_pn(list(self.product.digikey_pns or []))

    @property
    def stock(self) -> Optional[int]:
        return self.product.quantity_available

    @property
    def price(self) -> float:
        p = self.product.unit_price
        return float(p) if p is not None else float("inf")

    @property
    def description(self) -> str:
        return (self.product.description or "").strip()

    @property
    def lcsc(self) -> None:
        return None

    def choice(self) -> ThtChoice:
        """The database record for this part (accept or reject)."""
        return tht_choice(self.product, self.signature, self.value)


@dataclass
class ThtSearchResult:
    """One :func:`assess_tht` call."""

    requirement: ThtRequirement
    candidates: List[ThtCandidate] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    problem: str = ""
    total: int = 0
    series: str = ""  # the header series searched, as text
    from_database: bool = False


def _current(req: ThtRequirement, digikey, db: Optional[PartsDb], needed: int,
             out: "ThtSearchResult", skip_mpn: bool) -> List[ThtCandidate]:
    """The part the symbol's DigiKeyPN (else MPN) names, looked up exactly:
    one candidate, or none (with a note or a rejection)."""
    from kicadtools.thtdb import is_rejected, reject_reason

    for name, text in req.identifiers:
        if skip_mpn and name == CANONICAL_KEYS["mpn"]:
            continue
        try:
            product = digikey.product_for(text)
        except Exception as exc:  # noqa: BLE001 - a failed call becomes a note
            out.notes.append(f"{name} {text}: DigiKey lookup failed: {exc}")
            continue
        if product is None or not (product.mpn or "").strip():
            out.notes.append(f"{name} {text}: not found at DigiKey")
            continue
        out.notes.append(f"{name} {text}: DigiKey {product.mpn.strip()}")
        why = ("rejected in the parts database" if is_rejected(db, product)
               else reject_reason(product, needed))
        if why:
            out.rejected.append((product.mpn.strip(), why))
            return []
        return [ThtCandidate(
            product=product, signature=req.signature, value=req.value_text,
            source=CURRENT, label=f"{name} {text}", exact=True,
            pads=pad_check(req.pad_count, product),
            stock_warning=stock_warning(product, needed),
        )]
    return []


def assess_tht(group: ThtGroup, *, run: HeaderRun, digikey, db: Optional[PartsDb],
               needed: int, use_stored: bool = True) -> ThtSearchResult:
    """Search one through-hole *group* of *needed* units: the part the
    symbol's DigiKeyPN or MPN names first (any family), then the family's
    search (headers and sockets, part-named footprints). *run* holds the
    header series for this run (its client is used for headers, *digikey*
    for the rest); *db* gives stored and rejected parts; with *use_stored*
    false, stored parts are not offered. Candidates: stored parts, then the
    current part, then the search's, without repeats."""
    req = group.requirement
    sig = req.signature
    out = ThtSearchResult(requirement=req)
    if not req.searchable:
        out.problem = req.problem() or "nothing to buy for this footprint"
        return out
    if not getattr(digikey, "configured", False):
        from kicadtools.namedsearch import NO_DIGIKEY
        out.problem = NO_DIGIKEY
        return out
    # The part-named search looks the MPN field up itself.
    current = _current(req, digikey, db, needed, out, skip_mpn=sig.family == NAMED_PART)
    if sig.family in (HEADER, SOCKET, NAMED_PART):
        _family_search(group, run, digikey, db, needed, use_stored, out)
    seen = set()
    merged = []
    stored = [c for c in out.candidates if c.source == DATABASE]
    rest = [c for c in out.candidates if c.source != DATABASE]
    for c in stored + current + rest:
        key = norm_mpn(c.mpn)
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    out.candidates = merged
    if out.candidates:
        out.problem = ""
    elif not out.problem:
        out.problem = "; ".join(f"{m} ({why})" for m, why in out.rejected) or "no part found"
    return out


def _family_search(group: ThtGroup, run: HeaderRun, digikey, db: Optional[PartsDb],
                   needed: int, use_stored: bool, out: "ThtSearchResult") -> None:
    req = group.requirement
    sig = req.signature
    if sig.family in (HEADER, SOCKET):
        found = run.search(sig, needed, db=db, use_stored=use_stored)
        stored = {norm_mpn(r.mpn): r for r in found.stored}
        series = found.series
        predicted = set()
        if series is not None:
            predicted = {series.predicted_mpn(n) for n in (sig.per_row, sig.positions)
                         if n is not None} - {None}
            out.series = f"{series.manufacturer} {series.mpn}"
        for product in found.candidates:
            row = stored.get(norm_mpn(product.mpn or ""))
            out.candidates.append(ThtCandidate(
                product=product, signature=sig, value=req.value_text,
                source=DATABASE if row is not None else SEARCH,
                label=DATABASE_LABEL if row is not None else (
                    f"series {out.series}" if series is not None else "maker list"),
                pads=pad_check(req.pad_count, product),
                stock_warning=stock_warning(product, needed),
                db_part_id=row.part_id if row is not None else None,
                usage_count=row.usage_count if row is not None else 0,
                series_match=(product.mpn or "") in predicted,
            ))
        out.rejected.extend(found.rejected)
        out.notes.extend(found.notes)
        out.total = found.total
        out.from_database = found.from_database
        out.problem = found.error or ("; ".join(found.unmatched) if found.unmatched else "")
        return
    named = find_named_candidates(sig, req.props, digikey=digikey, needed=needed,
                                  pad_count=req.pad_count, db=db, use_stored=use_stored)
    for c in named.candidates:
        out.candidates.append(ThtCandidate(
            product=c.product, signature=sig, value=req.value_text,
            source=(DATABASE if c.stored else
                    CURRENT if c.label.startswith(MPN_FIELD + " ") else SEARCH),
            label=c.label, exact=c.exact,
            pads=c.pads, stock_warning=c.stock_warning,
            db_part_id=c.db_part_id, usage_count=c.usage_count,
        ))
    out.rejected.extend(named.rejected)
    out.notes.extend(named.notes)
    out.problem = named.problem
    out.total = len(named.candidates)
    out.from_database = any(c.stored for c in named.candidates)


# --- write plan -------------------------------------------------------------------


def implied_fits(req: ThtRequirement, mpn: str) -> bool:
    """Whether a part with *mpn* fits *req*'s implied footprint: any part
    the header or socket search found (it searched that signature), or a
    part whose MPN fits the footprint's part-number token
    (:func:`kicadtools.namedsearch.mpn_fits`: ``B3B-PH-K`` and
    ``B3B-PH-K-S(LF)(SN)`` on ``JST_PH_B3B-PH-K``)."""
    if req.implied is None:
        return False
    sig = req.signature
    if sig.family in (HEADER, SOCKET):
        return True
    return bool(sig.mpn) and mpn_fits(mpn or "", sig.mpn)


def tht_assignment_plan(group: ThtGroup, cand: ThtCandidate
                        ) -> Dict[Path, Dict[str, Dict[str, Optional[str]]]]:
    """The property writes that assign *cand* to every member of *group*,
    in the shape of :func:`kicadtools.writer.write_properties` (``None``
    removes the property): ``MPN``, ``Manufacturer``, ``DigiKeyPN``; ``LCSC``
    removed; other spellings of the part-number fields removed; ``Footprint``
    = the implied footprint (:attr:`ThtRequirement.implied`) on a member
    with none, when the part fits it (:func:`implied_fits`). ``Value`` and
    ``Reference`` are never written, nor a footprint the symbol has.
    Raises ``ValueError`` when the candidate has no MPN, manufacturer or
    DigiKey PN."""
    part_props: Dict[str, Optional[str]] = {
        CANONICAL_KEYS["mpn"]: _text(cand.mpn),
        CANONICAL_KEYS["manufacturer"]: _text(cand.manufacturer),
        CANONICAL_KEYS["digikey_pn"]: _text(cand.digikey_pn),
    }
    missing = [k for k, v in part_props.items() if not v]
    if missing:
        raise ValueError(
            f"candidate {cand.mpn or '?'} has no {', '.join(missing)}; "
            "MPN, Manufacturer and DigiKeyPN are required")
    implied = group.requirement.implied
    footprint = (implied.lib_id if implied is not None
                 and implied_fits(group.requirement, cand.mpn) else None)
    plan: Dict[Path, Dict[str, Dict[str, Optional[str]]]] = {}
    for m in group.members:
        if m.source_file is None or not m.symbol_uuid:
            continue
        props: Dict[str, Optional[str]] = dict(part_props)
        props[CANONICAL_KEYS["lcsc"]] = None
        if footprint and not _prop(m.props, "Footprint"):
            props["Footprint"] = footprint
        props.update(_alias_removals(m.props))
        plan.setdefault(Path(m.source_file), {})[m.symbol_uuid] = props
    return plan


def rank_text(c: ThtCandidate) -> str:
    """The Source cell of the dialog."""
    if c.source == DATABASE:
        uses = f" ({c.usage_count} use{'s' * (c.usage_count != 1)})" if c.usage_count else ""
        return "database" + uses
    if c.source == CURRENT:
        return f"current part ({c.label})"
    if c.series_match:
        return "series"
    return c.label or "search"


def pads_text(c: ThtCandidate) -> str:
    return "match" if c.pads.result == MATCH else c.pads.result
