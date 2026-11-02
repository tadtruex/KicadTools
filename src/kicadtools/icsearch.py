"""
Candidate parts for an IC, found from the part numbers its symbol carries
(``IC_PLAN.md`` steps 5 and 6), and the write plan for the chosen one.

Capacitors and resistors are searched by electrical value
(:mod:`kicadtools.partsearch`); an IC is looked up by part number.
:func:`find_ic_candidates` takes one
:class:`~kicadtools.icrequirement.IcRequirement` and:

1. **Exact lookups** — ``digikey.product_for`` on the ``DigiKeyPN`` and the
   ``MPN`` field; ``jlc_part`` on the ``LCSC`` number, then
   ``digikey.product_for`` on the MPN JLCPCB gives for it. Each distinct MPN
   found is one part.
2. **Database** — accepted ``ic`` parts (:meth:`PartsDb.find_ics`) whose MPN
   equals a found MPN, the ``MPN`` field or a Value token. A database part not
   found in step 1 is refreshed at DigiKey (its DigiKey PN) and at JLCPCB (its
   LCSC number).
3. **Partial MPN** — the ``MPN`` field and the Value's MPN-shaped tokens
   (:func:`kicadtools.icrequirement.value_mpns`), longest first. When steps 1
   and 2 found nothing, each is tried in turn until one finds a part. When
   they found something, the first that no found MPN starts with is tried, so
   a Value that names another part (BuckConverter U1's ``LTC6992CS6-2``
   against its DigiKey PN for ``LTC6992HS6-2``) gives that part as well. A try
   is one DigiKey keyword search (:meth:`DigiKeyClient.keyword_search`),
   keeping only products whose MPN starts with the partial MPN (keyword
   results are fuzzy), then ``digikey.product_for`` on each kept MPN, at most
   :data:`MAX_IC_LOOKUPS` for the whole step.
4. **JLCPCB** — each part without a usable JLCPCB listing from steps 1 and 2
   (none, or one unbuyable or below the stock floor) is searched at JLCPCB by
   its MPN, exact MPN only; JLCPCB's own ``JLCPCB Assembly`` placeholder
   listings are skipped (:func:`kicadtools.partsearch._best_jlc_hit`), and an
   ``LCSC`` field or database LCSC number that names a placeholder, or a
   listing of another MPN, is not used. A part JLCPCB does not list is still a
   candidate, shown as "not at JLCPCB".
5. **Checks** — DigiKey lists the part, its status is ``Active``, its stock
   is at least :func:`stock_minimum` (:data:`IC_STOCK_MIN_FACTOR` times the
   build quantity), and it is not rejected in the parts database; a part that
   fails is dropped and recorded in :attr:`IcSearchResult.rejected`. A part
   with stock between the minimum and :func:`stock_floor` (build quantity +
   :data:`IC_STOCK_MARGIN`) is kept, with :attr:`IcCandidate.digikey_warning`
   set, and ranks after parts that meet the floor. At JLCPCB the same two
   limits apply, but nothing is required: a part below the minimum, or one
   JLCPCB marks unbuyable, keeps its LCSC number but
   :attr:`IcCandidate.jlc_note` says why it cannot be assembled there, and it
   ranks as not at JLCPCB; the write plan and :meth:`IcCandidate.choice`
   leave its LCSC number out. A part between the minimum and the floor is at
   JLCPCB, with :attr:`IcCandidate.jlc_warning` set.
6. **Footprint check** — :func:`check_footprint` (step 6 of the plan) compares
   the footprint's package (parsed from its name) and pad count with the
   package DigiKey states. The result ranks candidates; it never removes one.
7. **Rank** — :func:`ic_rank_key`: database, current part, footprint check,
   stock floor met, at JLCPCB, JLCPCB Basic, DigiKey stock, price.

Identifiers that name parts no other identifier agrees with (BuckConverter
U1) give one candidate each, and :attr:`IcSearchResult.conflicts` has a line
for each disagreement. An exact identifier that names no part while another
identifier found one (BarrelJackAdapter U1's ``DigiKeyPN``
``TPS563207DRLRCT-ND``, which DigiKey does not list, beside an ``MPN`` that
DigiKey does) is a conflict too: accepting a part replaces it.

When no part passes, :attr:`IcSearchResult.problem` says why in one line
(:func:`_no_candidate_problem`).

:func:`ic_requirement_for` fills the requirement's package and pad count;
:func:`assess_ic` is the group-level entry point the Parts tab calls (the IC
counterpart of :func:`kicadtools.partsearch.assess`); :func:`ic_assignment_plan`
is the write plan for an accepted candidate.

Pure apart from the injected callables: *digikey* (``configured``,
``product_for(pn) -> DigiKeyProduct | None`` and
``keyword_search(text) -> [DigiKeyProduct]``, as on
:class:`~kicadtools.suppliers.DigiKeyClient`), *jlc_search* (keyword ->
:class:`~kicadtools.suppliers.JlcPart` hits) and *jlc_part* (LCSC code -> one
``JlcPart`` or ``None``). The pad-count source passed to
:func:`ic_requirement_for` reads footprint files; nothing here writes one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from kicadtools.assign import _alias_removals, _text
from kicadtools.evidence import ExistingPart
from kicadtools.footprints import FootprintPads
from kicadtools.icrequirement import IcGroup, IcRequirement
from kicadtools.package import (
    DK_PIN_PARAMS,
    DK_SUPPLIER_PACKAGE,
    TAB_FAMILIES,
    PackageInfo,
    parse_digikey_package,
    parse_footprint,
)
from kicadtools.partnum import CANONICAL_KEYS
from kicadtools.partsdb import IC, DbIc, IcChoice, PartsDb, norm_mpn
from kicadtools.partsearch import (
    _CUT_TAPE_RE,
    _MemoDigiKey,
    _best_jlc_hit,
    _current_digikey_pn,
    _jlc_library,
    _label,
    _memo,
    _try,
)
from kicadtools.parttypes import DK_PACKAGE
from kicadtools.suppliers import DigiKeyProduct, JlcPart

# Candidate sources.
DATABASE = "database"
CURRENT = "current"     # named by one of the symbol's identifiers
SEARCH = "search"       # found from a partial MPN

# The stock floor at DigiKey (required) and at JLCPCB (when the part is
# there) is the build quantity plus this many. A part below the floor is
# still accepted, with a warning, down to IC_STOCK_MIN_FACTOR times the
# build quantity; below that it is rejected (DigiKey) or not used (JLCPCB).
IC_STOCK_MARGIN = 1000
IC_STOCK_MIN_FACTOR = 10

# The partial-MPN step makes at most this many DigiKey product_for calls.
MAX_IC_LOOKUPS = 5

# Footprint check results (step 6).
MATCH = "match"
MISMATCH = "mismatch"
UNVERIFIED = "cannot verify"

MISMATCH_TEXT = (
    "Footprint does not match the part — check it very carefully against "
    "the datasheet."
)
UNVERIFIED_TEXT = (
    "Footprint could not be checked against the part — check it very "
    "carefully against the datasheet."
)

NOT_AT_JLCPCB = "not at JLCPCB"

DIGIKEY_UNCONFIGURED = (
    "DigiKey credentials are not set (DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET); "
    "every IC must be confirmed at DigiKey, so no part could be offered"
)

# Families with the same land pattern: a TQFP part fits an LQFP footprint of
# the same body and pitch, a TSOT-23 part a SOT-23 footprint. The packages
# differ in height only, which the footprint does not fix.
_SAME_LAND_PATTERN = {"TQFP": "LQFP", "TSOT-23": "SOT-23"}

# Two body sizes closer than this (mm, each side) are the same.
_BODY_TOL_MM = 0.05

# The labels identifiers are shown under, as the symbol names the fields.
_DIGIKEY_FIELD = CANONICAL_KEYS["digikey_pn"]
_MPN_FIELD = CANONICAL_KEYS["mpn"]
_LCSC_FIELD = CANONICAL_KEYS["lcsc"]
_VALUE_FIELD = "Value"

# The pin-count parameter a recorded part's pin count is passed back under.
_PIN_PARAM = DK_PIN_PARAMS[0]


# --------------------------------------------------------------------------- #
# Footprint check.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FootprintCheck:
    """The footprint compared with one candidate's package. ``result`` is
    :data:`MATCH`, :data:`MISMATCH` or :data:`UNVERIFIED`; ``reasons`` says
    what disagrees (``"pitch 0.5 mm vs 0.65 mm"``, footprint first) or, for
    :data:`UNVERIFIED`, what could not be compared."""
    result: str
    reasons: Tuple[str, ...] = ()

    @property
    def message(self) -> str:
        """The line shown above the Accept button; ``""`` for a match."""
        if self.result == MATCH:
            return ""
        text = MISMATCH_TEXT if self.result == MISMATCH else UNVERIFIED_TEXT
        if not self.reasons:
            return text
        lead = "Footprint vs part" if self.result == MISMATCH else "Not compared"
        return f"{text} {lead}: {'; '.join(self.reasons)}."

    @property
    def rank(self) -> int:
        """0 for a match, 1 when not verified, 2 for a mismatch."""
        return {MATCH: 0, UNVERIFIED: 1}.get(self.result, 2)


def _mm(x: float) -> str:
    return f"{x:g}"


def _land_families(info: PackageInfo) -> Set[str]:
    return {_SAME_LAND_PATTERN.get(f, f) for f in info.families}


def check_footprint(footprint: Optional[PackageInfo], pads: Optional[int],
                    part: PackageInfo) -> FootprintCheck:
    """Compare the footprint's *footprint* package (from its name) and *pads*
    (its pad count) with the *part*'s package (from DigiKey).

    * **match** — the pins agree (the footprint name's pin count, or the pad
      count, with the part's), and family, body and pitch agree wherever both
      sides state them;
    * **mismatch** — a field both sides state disagrees, or the pad count is
      not the part's pin count (plus one when the part has an exposed pad;
      either when neither side says whether it has one; for a tab family,
      :data:`kicadtools.package.TAB_FAMILIES`, the tab may or may not be a
      pad of its own);
    * **cannot verify** — no pin count can be compared: the part's package
      or pin count is unknown, or the footprint name states no pin count and
      the pad count is unknown; or the footprint name states a pitch the
      part's package does not. DigiKey states no pitch, so a pitch is
      compared only where the package standard fixes it
      (:func:`kicadtools.package.standard_pitch`); a ``6-DFN (2x2)`` such
      as the LDL212's is made in 0.5 mm and 0.65 mm, and a 2x2 mm DFN-6
      footprint of either pitch has the same 7 pads, so a match would hide a
      footprint of the wrong pitch (``IC_PLAN.md`` step 6).

    Families with the same land pattern (:data:`_SAME_LAND_PATTERN`) agree."""
    fp = footprint or PackageInfo()
    reasons: List[str] = []

    if fp.families and part.families and not (_land_families(fp) & _land_families(part)):
        reasons.append(f"package {fp.family} vs {part.family}")
    if fp.body_mm and part.body_mm and any(
            abs(a - b) > _BODY_TOL_MM for a, b in zip(fp.body_mm, part.body_mm)):
        (a1, a2), (b1, b2) = fp.body_mm, part.body_mm
        reasons.append(f"body {_mm(a1)}x{_mm(a2)} mm vs {_mm(b1)}x{_mm(b2)} mm")
    if (fp.pitch_mm is not None and part.pitch_mm is not None
            and abs(fp.pitch_mm - part.pitch_mm) > 1e-3):
        reasons.append(f"pitch {_mm(fp.pitch_mm)} mm vs {_mm(part.pitch_mm)} mm")
    if fp.ep is not None and part.ep is not None and fp.ep != part.ep:
        reasons.append("exposed pad " + ("yes vs no" if fp.ep else "no vs yes"))

    compared = False
    pins = part.pins
    if pins is not None:
        if fp.pins is not None:
            compared = True
            if fp.pins != pins:
                reasons.append(f"{fp.pins} pins vs {pins} pins")
        if pads is not None:
            compared = True
            # The part's pin count leaves out an exposed pad; the pad count
            # includes it.
            ep = part.ep if part.ep is not None else fp.ep
            allowed = {pins + 1} if ep else ({pins} if ep is False else {pins, pins + 1})
            if set(part.families) & TAB_FAMILIES:
                # A tab is its own pad (KiCad's SOT-223: 4 pads for 3 leads),
                # shares a lead's number (SOT-223-3_TabPin2: 3), or is counted
                # in a name that also gives it a lead's number
                # (SOT-223-6_TabPin3: 5). A count of 3 is always the leads.
                allowed |= {pins, pins + 1} | ({pins - 1} if pins > 3 else set())
            if pads not in allowed:
                tail = " + exposed pad" if ep else ""
                reasons.append(f"{pads} pads vs {pins} pins{tail}")

    if reasons:
        return FootprintCheck(MISMATCH, tuple(reasons))
    unverified: List[str] = []
    if not compared:
        if not part.known:
            unverified.append("DigiKey states no package")
        elif pins is None:
            unverified.append("DigiKey states no pin count")
        else:
            unverified.append(
                "the footprint name states no pin count and its pad count is unknown")
    if fp.pitch_mm is not None and part.pitch_mm is None and part.known:
        unverified.append(f"pitch {_mm(fp.pitch_mm)} mm not stated by DigiKey")
    if unverified:
        return FootprintCheck(UNVERIFIED, tuple(unverified))
    return FootprintCheck(MATCH)


# --------------------------------------------------------------------------- #
# Candidates and results.
# --------------------------------------------------------------------------- #

@dataclass
class IcCandidate:
    """One IC part for a requirement, with its supplier numbers, stock and
    footprint check.

    ``package`` and ``package_case`` are DigiKey's ``Supplier Device
    Package`` and ``Package / Case``; ``pins`` the pin count read from them
    (:func:`kicadtools.package.parse_digikey_package`, an exposed pad not
    counted). ``lcsc`` is ``None`` when JLCPCB does not list the part;
    ``jlc_note`` is ``""`` when the part can be assembled at JLCPCB, else why
    not (below the stock minimum, unbuyable, stock unknown).
    ``digikey_warning`` and ``jlc_warning`` are ``""`` when that supplier's
    stock meets the floor, else a note that it is below the floor (but at
    least the minimum). ``fields`` names the
    identifiers that name this part (``("DigiKeyPN", "Value")``); ``current``
    is true when one of them names it exactly (not only as a partial MPN).
    Prices are unit prices in USD, ``inf`` when unknown."""
    mpn: str
    manufacturer: str
    package: str = ""
    package_case: str = ""
    pins: Optional[int] = None
    digikey_pn: Optional[str] = None
    digikey_stock: Optional[int] = None
    digikey_status: str = ""
    digikey_price: float = float("inf")
    lcsc: Optional[str] = None
    jlc_library: Optional[str] = None       # "basic" | "extended"
    jlc_preferred: bool = False
    jlc_stock: Optional[int] = None
    jlc_price: float = float("inf")
    jlc_note: str = ""
    digikey_warning: str = ""
    jlc_warning: str = ""
    source: str = CURRENT                   # DATABASE | CURRENT | SEARCH
    current: bool = False
    fields: Tuple[str, ...] = ()
    db_part_id: Optional[int] = None
    usage_count: int = 0
    description: str = ""
    datasheet_url: str = ""
    footprint: FootprintCheck = field(default_factory=lambda: FootprintCheck(UNVERIFIED))
    kind: str = IC

    @property
    def at_jlcpcb(self) -> bool:
        """JLCPCB lists the part and can assemble it (stock at least the
        minimum)."""
        return bool(self.lcsc) and not self.jlc_note

    @property
    def below_floor(self) -> bool:
        """DigiKey's stock, or JLCPCB's when the part is at JLCPCB, is below
        the stock floor (but at least the minimum)."""
        return bool(self.digikey_warning or (self.at_jlcpcb and self.jlc_warning))

    @property
    def stock_warnings(self) -> Tuple[str, ...]:
        """The stock warnings that apply, DigiKey first."""
        jlc = self.jlc_warning if self.at_jlcpcb else ""
        return tuple(w for w in (self.digikey_warning, jlc) if w)

    @property
    def jlc_text(self) -> str:
        """``"C12345"``, ``"C12345 (JLCPCB stock 33, need at least 250)"``,
        ``"C12345 (JLCPCB stock 400, below 1025)"``, or ``"not at JLCPCB"``."""
        if not self.lcsc:
            return NOT_AT_JLCPCB
        note = self.jlc_note or self.jlc_warning
        return f"{self.lcsc} ({note})" if note else self.lcsc

    @property
    def price(self) -> float:
        """The JLCPCB unit price when the part is at JLCPCB, else DigiKey's."""
        return self.jlc_price if self.at_jlcpcb else self.digikey_price

    def package_info(self) -> PackageInfo:
        """The part's package as :func:`check_footprint` reads it."""
        params = {DK_SUPPLIER_PACKAGE: self.package, DK_PACKAGE: self.package_case}
        if self.pins is not None:
            params[_PIN_PARAM] = str(self.pins)
        return parse_digikey_package(params)

    def choice(self) -> IcChoice:
        """This candidate as an :class:`~kicadtools.partsdb.IcChoice` for
        :meth:`PartsDb.accept_part` / :meth:`PartsDb.reject_part`; the LCSC
        number only when the part is at JLCPCB (:attr:`at_jlcpcb`), as the
        write plan writes it."""
        packaging = (
            "cut tape"
            if self.digikey_pn and _CUT_TAPE_RE.search(self.digikey_pn)
            else None
        )
        return IcChoice(
            mpn=self.mpn,
            manufacturer=self.manufacturer,
            package=self.package,
            package_case=self.package_case,
            pins=self.pins,
            digikey_pn=self.digikey_pn,
            digikey_packaging=packaging,
            lcsc=self.lcsc if self.at_jlcpcb else None,
            jlc_library=self.jlc_library if self.at_jlcpcb else None,
            description=self.description,
            datasheet_url=self.datasheet_url,
        )


@dataclass
class IcSearchResult:
    """The outcome of one :func:`find_ic_candidates` call.

    ``rejected`` lists each dropped part as (label, reason); ``notes`` are
    display lines saying what was looked up and found; ``conflicts`` has one
    line per pair of identifiers that name different parts, and one for each
    exact identifier that names no part when another names one;
    ``dropped_by_limit`` counts partial-MPN matches not looked up because of
    :data:`MAX_IC_LOOKUPS`; ``problem`` is a one-line reason when the search
    could not run, ran only in part, or offers no part. ``requirement`` is the requirement
    searched (with its package and pad count)."""
    candidates: List[IcCandidate]
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    dropped_by_limit: int = 0
    problem: str = ""
    requirement: Optional[IcRequirement] = None


def ic_rank_key(c: IcCandidate) -> tuple:
    """Sort key, lower is better: a database part (more recorded uses
    first), then a part the symbol names exactly, then the footprint check
    (match, cannot verify, mismatch), then stock meeting the floor, then at
    JLCPCB, then JLCPCB Basic, then more DigiKey stock, then lower unit
    price."""
    return (
        0 if c.db_part_id is not None else 1,
        -c.usage_count,
        0 if c.current else 1,
        c.footprint.rank,
        1 if c.below_floor else 0,
        0 if c.at_jlcpcb else 1,
        0 if c.at_jlcpcb and c.jlc_library == "basic" else 1,
        -(c.digikey_stock or 0),
        c.price,
    )


def stock_floor(needed: int) -> int:
    """The stock a supplier should have: *needed* + :data:`IC_STOCK_MARGIN`."""
    return max(0, needed) + IC_STOCK_MARGIN


def stock_minimum(needed: int) -> int:
    """The least stock accepted: :data:`IC_STOCK_MIN_FACTOR` × *needed* (at
    least one unit's worth)."""
    return IC_STOCK_MIN_FACTOR * max(1, needed)


def _stock_reason(supplier: str, stock: Optional[int], needed: int) -> str:
    """``""`` when *stock* is known and at least :func:`stock_minimum`, else
    the reason."""
    least = stock_minimum(needed)
    if stock is None:
        return f"{supplier} stock unknown"
    if stock < least:
        return f"{supplier} stock {stock}, need at least {least}"
    return ""


def _stock_warning(supplier: str, stock: Optional[int], needed: int) -> str:
    """``""`` when *stock* is unknown, below the minimum (that is
    :func:`_stock_reason`'s case) or at least :func:`stock_floor`, else the
    warning."""
    floor = stock_floor(needed)
    if stock is None or stock < stock_minimum(needed) or stock >= floor:
        return ""
    return f"{supplier} stock {stock}, below {floor}"


# --------------------------------------------------------------------------- #
# Collecting parts.
# --------------------------------------------------------------------------- #

@dataclass
class _Found:
    """One distinct part (by normalized MPN) while the search collects them."""
    mpn: str
    labels: List[str] = field(default_factory=list)   # "DigiKeyPN 497-…"
    product: Optional[DigiKeyProduct] = None
    part: Optional[JlcPart] = None     # never a JLCPCB placeholder listing
    db: Optional[DbIc] = None
    current: bool = False
    source: str = CURRENT


class _Memo(_MemoDigiKey):
    """:class:`kicadtools.partsearch._MemoDigiKey` plus the client's
    ``keyword_search``, kept per argument too (``None`` when the client has
    none)."""

    def __init__(self, client) -> None:
        super().__init__(client)
        self.configured = bool(self.configured)
        search = getattr(client, "keyword_search", None)
        self.keyword_search = _memo(search) if search is not None else None


def _jlc_placeholder(part: JlcPart) -> bool:
    """True for a listing under JLCPCB's own name (``"JLCPCB Assembly"``,
    such as C9900013546), which is not the part itself; the test
    :func:`kicadtools.partsearch._best_jlc_hit` applies to search hits."""
    return (part.manufacturer or "").strip().upper().startswith("JLCPCB")


def _jlc_usable(part: Optional[JlcPart], needed: int) -> bool:
    """True when *part* is a JLCPCB listing (not a placeholder) that can be
    ordered with stock at the floor, so no search by MPN could do better."""
    return (part is not None and not _jlc_placeholder(part)
            and not part.no_buy_reason
            and not _stock_reason("JLCPCB", part.stock, needed)
            and not _stock_warning("JLCPCB", part.stock, needed))


def _starts(mpn: str, partial: str) -> bool:
    """True when *mpn* starts with *partial*, both normalized."""
    p = norm_mpn(partial)
    return bool(p) and norm_mpn(mpn).startswith(p)


class _Collector:
    """The parts found for one requirement, keyed by normalized MPN."""

    def __init__(self, result: IcSearchResult) -> None:
        self.result = result
        self.by_mpn: Dict[str, _Found] = {}
        # identifier label -> the normalized MPNs it names, and the text a
        # partial MPN compares against (the MPN field and Value tokens).
        self.named: Dict[str, Set[str]] = {}
        self.partials: Dict[str, str] = {}
        # identifier label -> the supplier that was asked for it and does not
        # list it (only lookups that ran; a failed call is not "not found").
        self.missing: Dict[str, str] = {}

    @property
    def notes(self) -> List[str]:
        return self.result.notes

    def add(self, mpn: str, label: str, *, current: bool, source: str = CURRENT,
            product: Optional[DigiKeyProduct] = None,
            part: Optional[JlcPart] = None) -> _Found:
        key = norm_mpn(mpn)
        f = self.by_mpn.get(key)
        if f is None:
            f = self.by_mpn[key] = _Found(mpn=mpn.strip(), source=source)
        if label not in f.labels:
            f.labels.append(label)
        f.current = f.current or current
        if source == CURRENT:
            f.source = CURRENT
        if product is not None and f.product is None:
            f.product = product
        if part is not None and f.part is None:
            f.part = part
        self.named.setdefault(label, set()).add(key)
        return f

    def agrees(self, text: str) -> bool:
        """True when some found MPN starts with *text* (a partial MPN)."""
        return any(_starts(f.mpn, text) for f in self.by_mpn.values())


def _exact(ids, digikey: _Memo, jlc_part, col: _Collector) -> None:
    """Step 1: the DigiKeyPN, MPN and LCSC fields looked up exactly."""
    notes = col.notes
    seen: Set[str] = set()
    for name, value in ((_DIGIKEY_FIELD, ids.digikey_pn), (_MPN_FIELD, ids.mpn)):
        text = (value or "").strip()
        if not text or text.upper() in seen:
            continue
        seen.add(text.upper())
        label = f"{name} {text}"
        if name == _MPN_FIELD:
            col.partials[label] = text
        if not digikey.configured:
            notes.append(f"{label}: not looked up, DigiKey credentials are not set")
            continue
        product, ok = _try(digikey.product_for, text, notes, "DigiKey lookup of")
        mpn = (getattr(product, "mpn", None) or "").strip()
        if product is None or not mpn:
            if ok:
                notes.append(f"{label}: not found at DigiKey")
                col.missing[label] = "DigiKey"
            continue
        status = (product.product_status or "").strip() or "status unknown"
        notes.append(f"{label}: DigiKey {mpn}, {status}")
        col.add(mpn, label, current=True, product=product)

    lcsc = (ids.lcsc or "").strip()
    if not lcsc:
        return
    label = f"{_LCSC_FIELD} {lcsc}"
    part, ok = _try(jlc_part, lcsc, notes, "JLCPCB lookup of")
    mpn = (getattr(part, "mpn", None) or "").strip()
    if part is None or not mpn:
        if ok:
            notes.append(f"{label}: not found at JLCPCB")
            col.missing[label] = "JLCPCB"
        return
    if _jlc_placeholder(part):
        # The placeholder names the MPN but is not a listing of the part:
        # step 4 searches JLCPCB by that MPN instead.
        notes.append(f"{label}: JLCPCB placeholder listing for {mpn} "
                     f"({part.manufacturer.strip()}), not used")
        part = None
    else:
        notes.append(f"{label}: JLCPCB {mpn}")
    f = col.add(mpn, label, current=True, part=part)
    if f.product is None and digikey.configured:
        product, _ = _try(digikey.product_for, mpn, notes, "DigiKey lookup of")
        if product is not None and norm_mpn(product.mpn) == norm_mpn(mpn):
            f.product = product
            notes.append(f"{label} MPN {mpn}: DigiKey "
                         f"{(product.product_status or '').strip() or 'status unknown'}")
        else:
            notes.append(f"{label} MPN {mpn}: not found at DigiKey")


def _database(ids, db: PartsDb, digikey: _Memo, jlc_part, col: _Collector) -> None:
    """Step 2: accepted parts with the same MPN as a found part, the MPN
    field or a Value token."""
    texts = [f.mpn for f in col.by_mpn.values()]
    texts += [t for t in (ids.mpn, *ids.value_mpns) if t]
    done: Set[str] = set()
    for text in texts:
        key = norm_mpn(text)
        if not key or key in done:
            continue
        done.add(key)
        hits = db.find_ics(text)
        if not hits:
            continue
        hit = hits[0]   # most used, Basic first
        f = col.by_mpn.get(norm_mpn(hit.mpn))
        if f is None:
            label = (f"{_MPN_FIELD} {ids.mpn.strip()}"
                     if ids.mpn and norm_mpn(ids.mpn) == key
                     else f"{_VALUE_FIELD} {text}")
            f = col.add(hit.mpn, label, current=True, source=DATABASE)
            col.partials.setdefault(label, text)
            col.notes.append(f"{label}: parts database {hit.mpn}")
        f.db = hit
        if f.product is None and digikey.configured:
            pn = hit.digikey_pn or hit.mpn
            product, _ = _try(digikey.product_for, pn, col.notes, "DigiKey lookup of")
            if product is not None and norm_mpn(product.mpn) == norm_mpn(hit.mpn):
                f.product = product
        if f.part is None and hit.lcsc:
            # Kept only when JLCPCB still lists this MPN under the recorded
            # number; otherwise step 4 searches JLCPCB by the MPN.
            part, _ = _try(jlc_part, hit.lcsc, col.notes, "JLCPCB lookup of")
            if (part is not None and not _jlc_placeholder(part)
                    and norm_mpn(part.mpn) == norm_mpn(hit.mpn)):
                f.part = part
            else:
                col.notes.append(f"parts database {hit.mpn}: JLCPCB does not list "
                                 f"it as {hit.lcsc}")


def _partial_texts(ids) -> List[Tuple[str, str]]:
    """(label, text) for the MPN field and each Value token, without
    repeats (normalized)."""
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    pairs = [(f"{_MPN_FIELD} {ids.mpn.strip()}", ids.mpn.strip())] if ids.mpn else []
    pairs += [(f"{_VALUE_FIELD} {t}", t) for t in ids.value_mpns]
    for label, text in pairs:
        key = norm_mpn(text)
        if key and key not in seen:
            seen.add(key)
            out.append((label, text))
    return out


def _partial(label: str, text: str, digikey: _Memo, col: _Collector,
             budget: List[int]) -> bool:
    """Step 3 for one partial MPN: keyword search, keep the MPNs that start
    with *text*, confirm each with ``product_for`` while *budget* (a one-item
    remaining-lookups counter) lasts. True when a part was found."""
    notes = col.notes
    col.partials[label] = text
    hits, ok = _try(digikey.keyword_search, text, notes, "DigiKey keyword search for")
    if not ok:
        return False
    by_mpn: Dict[str, DigiKeyProduct] = {}
    for p in hits or []:
        mpn = (p.mpn or "").strip()
        if mpn and _starts(mpn, text) and norm_mpn(mpn) not in by_mpn:
            by_mpn[norm_mpn(mpn)] = p
    if not by_mpn:
        notes.append(f"{label}: no DigiKey part starts with {text}")
        return False
    want = norm_mpn(text)
    # The exact MPN first, then the closest (shortest) variants; sorted() is
    # stable, so DigiKey's order breaks ties.
    order = sorted(by_mpn, key=lambda k: (0 if k == want else 1, len(k)))
    found = 0
    for key in order:
        if budget[0] <= 0:
            col.result.dropped_by_limit += 1
            continue
        budget[0] -= 1
        hit = by_mpn[key]
        product, _ = _try(digikey.product_for, hit.mpn.strip(), notes, "DigiKey lookup of")
        if product is None or norm_mpn(product.mpn) != key:
            product = hit     # the keyword hit is the same product
        col.add(product.mpn.strip(), label, current=(key == want), source=SEARCH,
                product=product)
        found += 1
    if found:
        mpns = ", ".join(by_mpn[k].mpn.strip() for k in order[:found])
        notes.append(f"{label}: DigiKey {mpns}")
    return found > 0


def _conflicts(col: _Collector) -> List[str]:
    """One line per pair of identifiers that name different parts. Two
    identifiers agree when they name a common part, or when a part one names
    starts with the other's partial MPN (an MPN field or Value token)."""
    def agree(a: str, b: str) -> bool:
        ma, mb = col.named.get(a, set()), col.named.get(b, set())
        if ma & mb:
            return True
        for x, other in ((a, mb), (b, ma)):
            text = col.partials.get(x)
            if text and any(m.startswith(norm_mpn(text)) for m in other):
                return True
        return False

    def names(label: str) -> str:
        mpns = [f.mpn for k, f in col.by_mpn.items() if k in col.named.get(label, set())]
        return ", ".join(mpns)

    labels = [lab for lab in col.named if col.named[lab]]
    lines: List[str] = []
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if not agree(a, b):
                lines.append(f"{a} names {names(a)}; {b} names {names(b)}")
    if labels:
        # An MPN field not found exactly but found as a partial MPN is named
        # under the same label, so it is not listed here.
        for label, supplier in col.missing.items():
            if not col.named.get(label):
                lines.append(f"{label} names no {supplier} part; "
                             f"{labels[0]} names {names(labels[0])}")
    return lines


# At most this many rejected parts are named in the no-candidate problem line.
_PROBLEM_REJECTED = 3


def _no_candidate_problem(result: IcSearchResult) -> str:
    """The one-line :attr:`IcSearchResult.problem` for a search that ran and
    offers no part: ``"no part passed: DRV8313RHHR DigiKey stock 164, need at
    least 1050"`` (the first :data:`_PROBLEM_REJECTED` rejected parts), or
    ``"no part found for the symbol's identifiers"`` when nothing was found,
    with ``"; a supplier lookup failed"`` when one did (the note
    :func:`kicadtools.partsearch._try` adds says ``"… failed: <error>"``)."""
    if not result.rejected:
        failed = any(" failed: " in n for n in result.notes)
        return ("no part found for the symbol's identifiers"
                + ("; a supplier lookup failed" if failed else ""))
    shown = [f"{label} {why}" for label, why in result.rejected[:_PROBLEM_REJECTED]]
    more = len(result.rejected) - len(shown)
    tail = f"; and {more} more" if more > 0 else ""
    return "no part passed: " + "; ".join(shown) + tail


# --------------------------------------------------------------------------- #
# Search.
# --------------------------------------------------------------------------- #

def _candidate(f: _Found, ids, req: IcRequirement, needed: int) -> IcCandidate:
    """The :class:`IcCandidate` for one collected part (checks not applied)."""
    product, part, hit = f.product, f.part, f.db
    params = dict(product.parameters) if product is not None else {}
    if product is None and hit is not None:
        params = {DK_SUPPLIER_PACKAGE: hit.package, DK_PACKAGE: hit.package_case}
        if hit.pins is not None:
            params[_PIN_PARAM] = str(hit.pins)
    info = parse_digikey_package(params)
    manufacturer = (
        (product.manufacturer if product is not None else None)
        or (hit.manufacturer if hit is not None else None)
        or (part.manufacturer if part is not None else None)
        or ""
    ).strip()
    cand = IcCandidate(
        mpn=(product.mpn if product is not None else f.mpn).strip(),
        manufacturer=manufacturer,
        package=str(params.get(DK_SUPPLIER_PACKAGE) or "").strip(),
        package_case=str(params.get(DK_PACKAGE) or "").strip(),
        pins=info.pins,
        source=DATABASE if hit is not None else f.source,
        current=f.current,
        fields=tuple(dict.fromkeys(lab.split(" ", 1)[0] for lab in f.labels)),
        footprint=check_footprint(req.package, req.pad_count, info),
    )
    if product is not None:
        cand.digikey_pn = _current_digikey_pn(
            product, ExistingPart(mpn=ids.mpn, digikey_pn=ids.digikey_pn))
        cand.digikey_stock = product.quantity_available
        cand.digikey_warning = _stock_warning("DigiKey", cand.digikey_stock, needed)
        cand.digikey_status = (product.product_status or "").strip()
        if product.unit_price is not None:
            cand.digikey_price = product.unit_price
        cand.description = (product.description or "").strip()
        cand.datasheet_url = (product.datasheet_url or "").strip()
    elif hit is not None:
        cand.digikey_pn = hit.digikey_pn
    if hit is not None:
        cand.db_part_id, cand.usage_count = hit.part_id, hit.usage_count
        cand.description = cand.description or hit.description
        cand.datasheet_url = cand.datasheet_url or hit.datasheet_url
    if part is not None:
        cand.lcsc = (part.lcsc or "").strip() or None
        cand.jlc_library = _jlc_library(part.library)
        cand.jlc_preferred = part.preferred
        cand.jlc_stock = part.stock if not part.no_buy_reason else 0
        cand.jlc_price = part.unit_price
        cand.description = cand.description or part.description
        if part.no_buy_reason:
            cand.jlc_note = f"not orderable at JLCPCB ({part.no_buy_reason})"
        else:
            cand.jlc_note = _stock_reason("JLCPCB", part.stock, needed)
            cand.jlc_warning = _stock_warning("JLCPCB", part.stock, needed)
    return cand


def _reject_reason(f: _Found, cand: IcCandidate, db: PartsDb, needed: int,
                   configured: bool) -> str:
    """``""`` when the part passes the required checks, else the reason."""
    if f.product is None:
        return "not found at DigiKey" if configured else "not looked up at DigiKey"
    if not cand.digikey_pn:
        return "no DigiKey part number"
    if cand.digikey_status.lower() != "active":
        return f"DigiKey status '{cand.digikey_status or 'unknown'}'"
    why = _stock_reason("DigiKey", cand.digikey_stock, needed)
    if why:
        return why
    if db.is_rejected(cand.manufacturer, cand.mpn):
        return "rejected in the parts database"
    return ""


def find_ic_candidates(
    req: IcRequirement,
    *,
    db: PartsDb,
    digikey,
    jlc_search: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    needed: int,
) -> IcSearchResult:
    """The parts for *req*, best first (:func:`ic_rank_key`); see the module
    docstring for the steps. *needed* is the number of units the build
    places: the stock floor is :func:`stock_floor` and the minimum
    :func:`stock_minimum`.

    *req*'s ``package`` and ``pad_count`` (see :func:`ic_requirement_for`)
    feed the footprint check; when ``package`` is ``None`` it is parsed from
    ``footprint`` here. A requirement that is not searchable gives a result
    with :attr:`IcSearchResult.problem` set and no supplier calls. Without
    DigiKey credentials no part can pass (DigiKey is required) and
    :attr:`IcSearchResult.problem` says so. Nothing raises for a failed
    lookup: it becomes a note."""
    if req.package is None:
        req = replace(req, package=parse_footprint(req.footprint))
    result = IcSearchResult([], requirement=req)
    if not req.searchable:
        result.problem = req.problem()
        return result
    ids = req.identifiers
    dk = _Memo(digikey)
    jlc_part = _memo(jlc_part)
    jlc_search = _memo(jlc_search)
    col = _Collector(result)

    _exact(ids, dk, jlc_part, col)
    _database(ids, db, dk, jlc_part, col)

    if dk.configured and dk.keyword_search is not None:
        budget = [MAX_IC_LOOKUPS]
        texts = _partial_texts(ids)
        if not col.by_mpn:
            for label, text in texts:
                if _partial(label, text, dk, col, budget):
                    break
        else:
            for label, text in texts:
                if col.agrees(text):
                    # Name the part under this identifier too.
                    for f in col.by_mpn.values():
                        if _starts(f.mpn, text):
                            col.add(f.mpn, label, current=norm_mpn(f.mpn) == norm_mpn(text))
                    continue
                _partial(label, text, dk, col, budget)
                break   # one try for an identifier that disagrees
    elif dk.configured and not col.by_mpn:
        result.notes.append("partial MPN not searched: the DigiKey client has no keyword search")

    # Step 4: JLCPCB by MPN for each part without a usable JLCPCB listing
    # (none yet, or one that is unbuyable or below the stock floor: another
    # listing of the same MPN may be orderable). A listing already found is
    # kept when the search finds nothing.
    for f in col.by_mpn.values():
        if _jlc_usable(f.part, needed):
            continue
        mpn = (f.product.mpn if f.product is not None else f.mpn).strip()
        hits, ok = _try(jlc_search, mpn, result.notes, "JLCPCB search for")
        best = _best_jlc_hit(hits or [], mpn)
        if best is not None:
            f.part = best
        if f.part is None:
            if ok:
                result.notes.append(f"MPN {mpn}: {NOT_AT_JLCPCB}")
        elif best is not None:
            result.notes.append(f"MPN {mpn}: JLCPCB {f.part.lcsc}")

    result.conflicts = _conflicts(col)

    for f in col.by_mpn.values():
        cand = _candidate(f, ids, req, needed)
        why = _reject_reason(f, cand, db, needed, dk.configured)
        if why:
            result.rejected.append((_label(cand.lcsc or "", cand.mpn), why))
            continue
        result.candidates.append(cand)
    result.candidates.sort(key=ic_rank_key)
    if not dk.configured:
        result.problem = DIGIKEY_UNCONFIGURED
    elif not result.candidates:
        result.problem = _no_candidate_problem(result)
    return result


# --------------------------------------------------------------------------- #
# Group entry point.
# --------------------------------------------------------------------------- #

def board_pads(board: Optional[Path]) -> Optional[FootprintPads]:
    """The pad-count source for a board: the ``.kicad_pcb`` beside the
    schematic *board* (missing is fine; the libraries are still read), or
    ``None`` when *board* is ``None``."""
    if board is None:
        return None
    return FootprintPads(Path(board).with_suffix(".kicad_pcb"))


def ic_requirement_for(group: IcGroup, pads=None) -> IcRequirement:
    """The group's requirement with ``package`` parsed from its footprint
    name and ``pad_count`` from *pads* (a :class:`FootprintPads`, or anything
    with ``count(reference, lib_id) -> int | None``): the first member whose
    footprint is found. ``pad_count`` stays ``None`` without *pads* or when no
    member's footprint is found."""
    req = group.requirement
    count: Optional[int] = None
    if pads is not None and req.footprint:
        for ref in group.references:
            count = pads.count(ref, req.footprint)
            if count is not None:
                break
    return replace(req, package=parse_footprint(req.footprint), pad_count=count)


def assess_ic(
    group: IcGroup,
    *,
    db: PartsDb,
    digikey,
    jlc_search: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    needed: int,
    pads=None,
    requirement: Optional[IcRequirement] = None,
) -> IcSearchResult:
    """The candidates for *group*: :func:`ic_requirement_for` (with *pads*),
    then :func:`find_ic_candidates`. *requirement* (an earlier result's
    requirement) is used as given instead, so the footprint is read once per
    dialog. The result's ``requirement`` is set."""
    req = requirement if requirement is not None else ic_requirement_for(group, pads)
    return find_ic_candidates(req, db=db, digikey=digikey, jlc_search=jlc_search,
                              jlc_part=jlc_part, needed=needed)


# --------------------------------------------------------------------------- #
# Write plan.
# --------------------------------------------------------------------------- #

def ic_assignment_plan(
    group: IcGroup, cand: IcCandidate
) -> Dict[Path, Dict[str, Dict[str, Optional[str]]]]:
    """Plan the property writes that assign *cand* to every member of
    *group*, in the ``{source_file: {symbol_uuid: {key: value}}}`` shape of
    :func:`kicadtools.writer.write_properties` (``None`` removes the
    property).

    Per symbol: ``Intent:Type = ic`` when the type was guessed; ``MPN``,
    ``Manufacturer`` and ``DigiKeyPN`` from the candidate; ``LCSC`` when the
    part is at JLCPCB (:attr:`IcCandidate.at_jlcpcb`: listed, orderable, stock
    at least the minimum), else ``None`` (removed when the symbol has one), so its
    part numbers all name the chosen part; ``None`` for every
    other spelling of the four part-number fields
    (:func:`kicadtools.assign._alias_removals`). ``Value``, ``Footprint``,
    ``Datasheet`` and ``Reference`` are never written. Members without a
    source file or symbol uuid are skipped. Raises ``ValueError`` when the
    candidate has no MPN, manufacturer or DigiKey PN."""
    part_props: Dict[str, Optional[str]] = {
        CANONICAL_KEYS["mpn"]: _text(cand.mpn),
        CANONICAL_KEYS["manufacturer"]: _text(cand.manufacturer),
        CANONICAL_KEYS["digikey_pn"]: _text(cand.digikey_pn),
    }
    missing = [key for key, value in part_props.items() if not value]
    if missing:
        raise ValueError(
            f"candidate {_text(cand.mpn) or '?'} has no {', '.join(missing)}; "
            "MPN, Manufacturer and DigiKeyPN are required"
        )
    lcsc = _text(cand.lcsc) if cand.at_jlcpcb else None

    plan: Dict[Path, Dict[str, Dict[str, Optional[str]]]] = {}
    for m in group.members:
        if m.source_file is None or not m.symbol_uuid:
            continue
        props: Dict[str, Optional[str]] = {}
        if getattr(m.intent, "intent_guessed", False):
            props["Intent:Type"] = IC
        props.update(part_props)
        # A part JLCPCB does not stock writes LCSC None even when the symbol
        # has no LCSC yet: None removes the property or leaves the symbol
        # unchanged, and it replaces an LCSC the LCSC tab resolved for the
        # old part when the window merges the tabs' edits.
        props[_LCSC_FIELD] = lcsc or None
        props.update(_alias_removals(m.props))
        plan.setdefault(Path(m.source_file), {})[m.symbol_uuid] = props
    return plan
