"""
Candidate parts for a capacitor requirement: the symbol's existing part, the
parts database, then a JLCPCB keyword search confirmed at DigiKey.

:func:`read_existing` looks up the part numbers a symbol already holds
(:class:`~kicadtools.evidence.ExistingPart`) at DigiKey and JLCPCB. Fields
that resolve to the same MPN are one :class:`CurrentOption`; fields that name
different parts give one option each. Each option holds the part's parameters
as :class:`~kicadtools.evidence.PartEvidence` and, when both suppliers list
it, the part as a :class:`Candidate` (source ``current``).
:func:`requirement_from_lookup` keeps the options that fit the schematic and
builds the requirement from the first of them; :func:`assess` runs both and
calls :func:`find_candidates`.

:func:`find_candidates` lists the parts that meet one
:class:`~kicadtools.requirement.CapacitorRequirement` and can be ordered at both
suppliers:

0. **Current parts** — the existing parts that fit the schematic, when given.
   Each is checked against the requirement and the stock floor like any other
   candidate; when one passes it is listed first and the supplier search runs
   only when *search_suppliers* is true. One that fails is recorded under the
   label ``"current part"`` (``"current part <MPN>"`` when there are several)
   in :attr:`SearchResult.rejected`, and a database entry with the same MPN is
   rejected with the same reason.
1. **Database** — accepted parts from :class:`~kicadtools.partsdb.PartsDb` that
   meet the requirement. Each has its JLCPCB stock and price refreshed through
   *jlc_part* and its DigiKey stock through *digikey* when DigiKey credentials
   are set. Source ``database``; always listed.
2. **Supplier search** — when neither the current part nor the database gives
   a candidate, or when *search_suppliers* is true. One JLCPCB keyword search per allowed dielectric
   (``"100nF 0603 X7R"``). JLCPCB ranks hits by text relevance, so each hit is
   re-checked against its catalog attributes: capacitance equal within 0.1 %,
   voltage rating at or above the floor, dielectric in the allowed set,
   tolerance at or below the maximum, and the same chip package. A hit lacking
   a constrained attribute is rejected, since an unknown rating is not an
   acceptable one. Hits that are not orderable, rejected in the database, or
   already listed from the database are dropped. The survivors are ranked and
   the top *limit* are looked up at DigiKey by MPN; a candidate is kept only
   when DigiKey lists the same MPN, as an active product, with parameters that
   also meet the requirement. Source ``search``.
3. **Stock floor** — every candidate, from any source, needs a known stock of
   at least :data:`MIN_STOCK` and at least the build quantity at JLCPCB and at
   DigiKey. A search hit's JLCPCB stock is checked before its DigiKey lookup,
   so the lookup limit is spent on hits that can pass.
4. **Rank** — :func:`rank_key`: the current part, database usage, JLCPCB Basic,
   JLCPCB preferred, JLCPCB stock covers the build, DigiKey stock covers the
   build, unit price.

Every part dropped in steps 0 to 3 is recorded with its reason in
:attr:`SearchResult.rejected`, for the assign dialog's "rejected N" note.

Pure apart from the injected callables: *jlc_search* (keyword ->
:class:`~kicadtools.suppliers.JlcPart` hits), *jlc_part* (LCSC code -> one
``JlcPart`` or ``None``) and *digikey* (``configured`` plus
``product_for(pn) -> DigiKeyProduct | None``, as on
:class:`~kicadtools.suppliers.DigiKeyClient`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from kicadtools.evidence import ExistingPart, PartEvidence
from kicadtools.partsdb import CapacitorChoice, CapacitorParams, DbCapacitor, PartsDb
from kicadtools.requirement import (
    DEFAULT,
    PART,
    RELAXED,
    CapacitorRequirement,
    RequirementGroup,
    Sourced,
)
from kicadtools.suppliers import (
    DigiKeyProduct,
    JlcPart,
    _norm_mpn,
    preferred_digikey_pn,
)
from kicadtools.units import (
    dielectric_tokens,
    fmt_farads,
    fmt_percent,
    fmt_volts,
    package_code,
    parse_capacitance,
    parse_percent,
    parse_volts,
)

DATABASE = "database"
SEARCH = "search"
CURRENT = "current"

# Generic parts (every capacitor, for now) need at least this many in stock at
# JLCPCB and at DigiKey, and at least the build quantity.
MIN_STOCK = 1000

# find_candidates confirms up to *limit* hits at DigiKey, trying at most this
# many times *limit* of them.
MAX_LOOKUP_FACTOR = 3

# The rejection label of the symbol's existing part.
CURRENT_LABEL = "current part"

# Relative band within which two capacitances are equal (0.1 %), and the slack
# on a floor or ceiling that absorbs float error in parsed values.
_CAP_REL_TOL = 1e-3
_LIMIT_EPS = 1e-6

# JLCPCB attribute names (attribute_name_en) for the constrained parameters.
JLC_CAPACITANCE = "Capacitance"
JLC_VOLTAGE = "Voltage Rating"
JLC_DIELECTRIC = "Temperature Coefficient"
JLC_TOLERANCE = "Tolerance"

# DigiKey parameter names (ParameterText) for the same parameters.
DK_CAPACITANCE = "Capacitance"
DK_VOLTAGE = "Voltage - Rated"
DK_DIELECTRIC = "Temperature Coefficient"
DK_TOLERANCE = "Tolerance"
DK_PACKAGE = "Package / Case"

# JLCPCB library codes as recorded in the parts database.
_JLC_LIBRARY = {"base": "basic", "basic": "basic",
                "expand": "extended", "extended": "extended"}

_CUT_TAPE_RE = re.compile(r"(-1-ND|CT-ND)$")

DIGIKEY_UNCONFIGURED = (
    "DigiKey credentials are not set (DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET); "
    "supplier search confirms every part at DigiKey, so it did not run"
)


@dataclass
class Candidate:
    """One part that meets a requirement, with its supplier numbers, stock and
    price. ``jlc_price`` is the lowest-tier JLCPCB unit price in USD, ``inf``
    when unknown; ``jlc_stock`` and ``digikey_stock`` are ``None`` when not
    looked up."""
    mpn: str
    manufacturer: str
    package: str
    params: CapacitorParams
    lcsc: Optional[str]
    jlc_library: Optional[str]          # "basic" | "extended"
    jlc_preferred: bool
    jlc_stock: Optional[int]
    jlc_price: float
    digikey_pn: Optional[str]
    digikey_stock: Optional[int]
    source: str                         # DATABASE | SEARCH | CURRENT
    db_part_id: Optional[int] = None
    usage_count: int = 0
    description: str = ""
    datasheet_url: str = ""
    # Why the part cannot be ordered although both suppliers list it (DigiKey
    # status not Active, JLCPCB marks it unbuyable); "" when it can. Set by
    # read_existing on the current part only.
    unavailable_reason: str = ""

    def choice(self) -> CapacitorChoice:
        """This candidate as a :class:`~kicadtools.partsdb.CapacitorChoice` for
        :meth:`PartsDb.accept_capacitor` / :meth:`PartsDb.reject_capacitor`.
        The DigiKey packaging is ``"cut tape"`` when the PN is a cut-tape one
        (``…-1-ND`` / ``…CT-ND``)."""
        packaging = (
            "cut tape"
            if self.digikey_pn and _CUT_TAPE_RE.search(self.digikey_pn)
            else None
        )
        return CapacitorChoice(
            mpn=self.mpn,
            manufacturer=self.manufacturer,
            package=self.package,
            params=self.params,
            digikey_pn=self.digikey_pn,
            digikey_packaging=packaging,
            lcsc=self.lcsc,
            jlc_library=self.jlc_library,
            description=self.description,
            datasheet_url=self.datasheet_url,
        )


@dataclass
class SearchResult:
    """The outcome of one :func:`find_candidates` call.

    ``examined`` counts the distinct JLCPCB search hits checked;
    ``rejected`` lists each dropped hit as (part label, reason);
    ``dropped_by_limit`` counts hits that passed the JLCPCB checks but ranked
    below *limit* and so were not looked up at DigiKey; ``problem`` is a
    one-line reason when the search could not run, or ran only in part.
    ``relaxed_tolerance_pct`` is the default tolerance limit that was dropped
    because no part met it, or ``None`` when the limit applied.
    ``requirement`` and ``existing`` are the requirement searched and the
    existing-part lookup, set by :func:`assess`."""
    candidates: List[Candidate]
    examined: int = 0
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    dropped_by_limit: int = 0
    problem: str = ""
    relaxed_tolerance_pct: Optional[float] = None
    requirement: Optional[CapacitorRequirement] = None
    existing: Optional["ExistingLookup"] = None

    @property
    def relaxed_note(self) -> str:
        """The line shown to the user when the tolerance limit was dropped,
        else ``""``."""
        if self.relaxed_tolerance_pct is None:
            return ""
        return (
            f"No part met the inferred tolerance limit of "
            f"{fmt_percent(self.relaxed_tolerance_pct)}; the limit was dropped, "
            f"and the chosen part's own tolerance is written to the symbol"
        )


def rank_key(c: Candidate, needed: int) -> tuple:
    """Sort key, lower is better: the current part, then more recorded uses,
    then JLCPCB Basic (no per-part setup fee), then JLCPCB preferred, then
    JLCPCB stock of at least *needed*, then DigiKey stock of at least *needed*,
    then lower unit price."""
    return (
        0 if c.source == CURRENT else 1,
        -c.usage_count,
        0 if c.jlc_library == "basic" else 1,
        0 if c.jlc_preferred else 1,
        0 if c.jlc_stock is not None and c.jlc_stock >= needed else 1,
        0 if c.digikey_stock is not None and c.digikey_stock >= needed else 1,
        c.jlc_price,
    )


# --------------------------------------------------------------------------- #
# Parameter checks shared by the JLCPCB and DigiKey filters.
# --------------------------------------------------------------------------- #

def _check_params(
    req: CapacitorRequirement,
    supplier: str,
    get: Callable[[str], Optional[str]],
    names: Tuple[str, str, str, str],
) -> Tuple[Optional[CapacitorParams], str]:
    """Parse and check capacitance, voltage, dielectric and tolerance read
    through *get* under the supplier's parameter *names* (in that order).
    Returns the parsed parameters and ``""`` when every check passes, else
    ``None`` and the reason. Dielectric and tolerance are checked only when the
    requirement constrains them; capacitance and voltage always."""
    cap_name, volt_name, diel_name, tol_name = names
    want_c = float(req.capacitance_f)
    want_v = float(req.voltage_min_v.value)
    allowed: Tuple[str, ...] = tuple(
        t for d in (req.dielectrics.value or ()) for t in dielectric_tokens(d)
    )
    max_tol: Optional[float] = req.tolerance_max_pct.value

    raw = get(cap_name)
    if raw is None:
        return None, f"{supplier} lists no {cap_name}"
    c = parse_capacitance(raw)
    if c is None:
        return None, f"{supplier} {cap_name} '{raw}' does not parse"
    if abs(c - want_c) > _CAP_REL_TOL * max(abs(c), abs(want_c)):
        return None, (
            f"{supplier} capacitance {fmt_farads(c)}, need {fmt_farads(want_c)}"
        )

    raw = get(volt_name)
    if raw is None:
        return None, f"{supplier} lists no {volt_name}"
    v = parse_volts(raw)
    if v is None:
        return None, f"{supplier} {volt_name} '{raw}' does not parse"
    if v < want_v * (1 - _LIMIT_EPS):
        return None, (
            f"{supplier} rating {fmt_volts(v)}, need at least {fmt_volts(want_v)}"
        )

    raw = get(diel_name)
    codes = dielectric_tokens(raw)
    if allowed:
        if raw is None or not codes:
            return None, f"{supplier} lists no {diel_name}"
        if not set(codes).intersection(allowed):
            return None, (
                f"{supplier} dielectric {', '.join(codes)} is not one of "
                f"{', '.join(allowed)}"
            )

    raw = get(tol_name)
    tol = _parse_tolerance(raw, c)
    if max_tol is not None:
        if raw is None:
            return None, f"{supplier} lists no {tol_name}"
        if tol is None:
            return None, f"{supplier} {tol_name} '{raw}' does not parse"
        if tol > max_tol * (1 + _LIMIT_EPS):
            return None, (
                f"{supplier} tolerance {fmt_percent(tol)}, need at most "
                f"{fmt_percent(max_tol)}"
            )

    return CapacitorParams(c, v, ", ".join(codes), tol), ""


def _parse_tolerance(raw: Optional[str], capacitance_f: float) -> Optional[float]:
    """Percent from a supplier tolerance. Small capacitors are specified with
    an absolute tolerance (``"±0.25pF"``); that is converted to percent of
    *capacitance_f*, so ±0.25 pF on 10 pF is 2.5 %. ``None`` when *raw* does
    not parse."""
    tol = parse_percent(raw)
    if tol is not None or raw is None or "%" in raw:
        return tol
    absolute = parse_capacitance(str(raw).replace("±", "").replace("+/-", ""))
    if absolute is None or capacitance_f <= 0:
        return None
    return round(absolute / capacitance_f * 100.0, 6)


def _stock_reason(supplier: str, stock: Optional[int], needed: int) -> str:
    """``""`` when *stock* is known and at least ``max(MIN_STOCK, needed)``,
    else the reason (``"JLCPCB stock 33, need at least 1000"``)."""
    floor = max(MIN_STOCK, needed)
    if stock is None:
        return f"{supplier} stock unknown"
    if stock < floor:
        return f"{supplier} stock {stock}, need at least {floor}"
    return ""


def _stock_floor(cand: Candidate, needed: int) -> str:
    """``""`` when *cand* meets the stock floor at both suppliers, else the
    first reason it does not."""
    return (_stock_reason("JLCPCB", cand.jlc_stock, needed)
            or _stock_reason("DigiKey", cand.digikey_stock, needed))


def _jlc_library(library: Optional[str]) -> Optional[str]:
    """JLCPCB's ``"base"`` / ``"expand"`` as ``"basic"`` / ``"extended"``;
    ``None`` when empty or unrecognized."""
    return _JLC_LIBRARY.get((library or "").strip().lower())


def _label(lcsc: str, mpn: str) -> str:
    """``"C14663 CL10B104KB8NNNC"``, either part omitted when empty."""
    parts = ((lcsc or "").strip(), (mpn or "").strip())
    return " ".join(s for s in parts if s) or "(unnamed part)"


# --------------------------------------------------------------------------- #
# Database candidates.
# --------------------------------------------------------------------------- #

def _from_database(hit: DbCapacitor, jlc_part, digikey) -> Candidate:
    """A database part as a :class:`Candidate`, with JLCPCB stock and price
    from *jlc_part* and DigiKey stock from *digikey* when configured."""
    cand = Candidate(
        mpn=hit.mpn,
        manufacturer=hit.manufacturer,
        package=hit.package,
        params=hit.params,
        lcsc=hit.lcsc,
        jlc_library=_jlc_library(hit.jlc_library),
        jlc_preferred=False,
        jlc_stock=None,
        jlc_price=float("inf"),
        digikey_pn=hit.digikey_pn,
        digikey_stock=None,
        source=DATABASE,
        db_part_id=hit.part_id,
        usage_count=hit.usage_count,
        description=hit.description,
        datasheet_url=hit.datasheet_url,
    )
    if hit.lcsc:
        part: Optional[JlcPart] = jlc_part(hit.lcsc)
        if part is not None:
            cand.jlc_stock = part.stock if not part.no_buy_reason else 0
            cand.jlc_price = part.unit_price
            cand.jlc_preferred = part.preferred
            cand.jlc_library = _jlc_library(part.library) or cand.jlc_library
    if hit.digikey_pn and getattr(digikey, "configured", False):
        product: Optional[DigiKeyProduct] = digikey.product_for(hit.digikey_pn)
        if product is not None:
            cand.digikey_stock = product.quantity_available
    return cand


# --------------------------------------------------------------------------- #
# Supplier search.
# --------------------------------------------------------------------------- #

def search_keywords(req: CapacitorRequirement) -> List[str]:
    """The JLCPCB search texts for *req*: capacitance and package, once per
    allowed dielectric (``"100nF 0603 X7R"``). Relevance ranking buries an X5R
    part under a search for X7R, so each dielectric gets its own search. The
    voltage floor and tolerance are left out of the text, since a higher
    rating or a tighter tolerance also qualifies, and are checked on the
    hits."""
    base = f"{fmt_farads(float(req.capacitance_f))} {req.package.value}"
    dielectrics: List[str] = []
    for entry in req.dielectrics.value or ():
        for code in dielectric_tokens(entry):
            if code not in dielectrics:
                dielectrics.append(code)
    plain = [f"{base} {d}" for d in dielectrics] if dielectrics else [base]
    # With the voltage floor in the text the results are mostly parts at that
    # rating; without it, higher-rated parts (often JLCPCB Basic) come back.
    # Both are searched, the voltage-bearing ones first.
    volts = req.voltage_min_v.value
    if volts:
        v = fmt_volts(float(volts))
        rated = [f"{base} {v} {d}" for d in dielectrics] if dielectrics else [f"{base} {v}"]
        return rated + plain
    return plain


def _jlc_check(req: CapacitorRequirement, part: JlcPart) -> Tuple[Optional[Candidate], str]:
    """*part* as a :class:`Candidate` (no DigiKey data yet) and ``""`` when it
    is orderable and its JLCPCB attributes meet *req*, else ``None`` and the
    reason."""
    if not part.mpn.strip():
        return None, "JLCPCB lists no MPN"
    if not part.orderable:
        why = part.no_buy_reason or "out of stock"
        return None, f"not orderable at JLCPCB ({why})"
    pkg = package_code(part.package)
    if pkg != req.package.value:
        return None, (
            f"JLCPCB package '{part.package}', need {req.package.value}"
        )
    params, why = _check_params(
        req, "JLCPCB", part.attributes.get,
        (JLC_CAPACITANCE, JLC_VOLTAGE, JLC_DIELECTRIC, JLC_TOLERANCE),
    )
    if params is None:
        return None, why
    return Candidate(
        mpn=part.mpn.strip(),
        manufacturer=part.manufacturer.strip(),
        package=pkg,
        params=params,
        lcsc=part.lcsc or None,
        jlc_library=_jlc_library(part.library),
        jlc_preferred=part.preferred,
        jlc_stock=part.stock,
        jlc_price=part.unit_price,
        digikey_pn=None,
        digikey_stock=None,
        source=SEARCH,
        description=part.description,
    ), ""


def _digikey_check(req: CapacitorRequirement, cand: Candidate,
                   product: Optional[DigiKeyProduct]) -> str:
    """``""`` when *product* confirms *cand* (same MPN, active, parameters
    meeting *req*, a DigiKey PN), after filling in the DigiKey PN, stock and
    datasheet; else the reason."""
    if product is None:
        return "not found at DigiKey"
    if _norm_mpn(product.mpn or "") != _norm_mpn(cand.mpn):
        return f"DigiKey returned MPN '{product.mpn or ''}'"
    status = (product.product_status or "").strip()
    if status.lower() != "active":
        return f"DigiKey status '{status or 'unknown'}'"
    params, why = _check_params(
        req, "DigiKey", product.param,
        (DK_CAPACITANCE, DK_VOLTAGE, DK_DIELECTRIC, DK_TOLERANCE),
    )
    if params is None:
        return why
    raw_pkg = product.param(DK_PACKAGE)
    if raw_pkg is None:
        return f"DigiKey lists no {DK_PACKAGE}"
    if package_code(raw_pkg) != req.package.value:
        return f"DigiKey package '{raw_pkg}', need {req.package.value}"
    pn = preferred_digikey_pn(list(product.digikey_pns or []))
    if not pn:
        return "DigiKey lists no part number"
    cand.digikey_pn = pn
    cand.digikey_stock = product.quantity_available
    cand.datasheet_url = product.datasheet_url or ""
    if not cand.description:
        cand.description = product.description or ""
    return ""


def find_candidates(
    req: CapacitorRequirement,
    *,
    db: PartsDb,
    jlc_search: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    digikey,
    needed: int,
    limit: int = 10,
    search_suppliers: bool = False,
    currents: Sequence[Candidate] = (),
) -> SearchResult:
    """The parts meeting *req*, best first (see :func:`rank_key`).

    *currents* are the symbol's existing parts that fit the schematic (from
    :func:`requirement_from_lookup`). Each that meets *req* and the stock
    floor is listed first; the others are recorded in
    :attr:`SearchResult.rejected` under ``"current part"``.
    Database matches that meet the stock floor are always listed. The supplier
    search runs when *search_suppliers* is true or neither the current part
    nor the database gives a candidate; it needs DigiKey credentials, and
    without them :attr:`SearchResult.problem` says so while the other
    candidates are still returned. At most *limit* DigiKey lookups are made
    for search hits. *needed* is the number of units the build places, for the
    stock floor and the stock terms of the ranking.

    When nothing meets *req* and its tolerance limit is a default (not stated
    on the schematic, not edited in the dialog), the search runs again without
    the limit and :attr:`SearchResult.relaxed_tolerance_pct` records the limit
    that was dropped. :func:`requirement_for` then gives the requirement to
    write for the chosen part."""
    # The second pass reuses the first pass's supplier answers: tolerance is
    # not part of the search text, so the same keywords and MPNs come back.
    kwargs = dict(db=db, jlc_search=_memo(jlc_search), jlc_part=_memo(jlc_part),
                  digikey=_MemoDigiKey(digikey), needed=needed, limit=limit,
                  search_suppliers=search_suppliers, currents=tuple(currents))
    result = _find(req, **kwargs)
    tol = req.tolerance_max_pct
    if (result.candidates or result.problem or not req.searchable
            or tol.source not in (DEFAULT, PART) or tol.value is None):
        return result
    relaxed = _find(replace(req, tolerance_max_pct=Sourced(None, DEFAULT)), **kwargs)
    relaxed.relaxed_tolerance_pct = tol.value
    return relaxed


def _memo(fn: Callable):
    """*fn* with its results kept per argument for the length of one search."""
    seen: Dict[str, object] = {}

    def call(arg: str):
        if arg not in seen:
            seen[arg] = fn(arg)
        return seen[arg]
    return call


class _MemoDigiKey:
    """A DigiKey client whose ``product_for`` answers are kept per part
    number for the length of one search."""

    def __init__(self, client) -> None:
        self.configured = getattr(client, "configured", False)
        self.product_for = _memo(client.product_for)


def requirement_for(
    req: CapacitorRequirement, result: SearchResult, cand: Candidate
) -> CapacitorRequirement:
    """The requirement to write to the symbols when *cand* is chosen from
    *result*. Unchanged unless the tolerance limit was dropped; then the
    tolerance is the part's own (source ``relaxed``, which the write plan
    writes and the remembered settings ignore), or no value when the part
    states none."""
    if result.relaxed_tolerance_pct is None:
        return req
    return replace(req, tolerance_max_pct=Sourced(cand.params.tolerance_pct, RELAXED))


def _find(
    req: CapacitorRequirement,
    *,
    db: PartsDb,
    jlc_search: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    digikey,
    needed: int,
    limit: int,
    search_suppliers: bool,
    currents: Sequence[Candidate],
) -> SearchResult:
    """One pass of :func:`find_candidates` with *req* as given."""
    if not req.searchable:
        return SearchResult([], problem=req.problem())

    result = SearchResult([])
    candidates = result.candidates
    current_failed: Dict[str, str] = {}   # normalized MPN -> reason
    for current in currents:
        cand = replace(current)  # a copy: the database merge below edits it
        why = _current_check(req, cand, db, needed)
        if why:
            label = CURRENT_LABEL if len(currents) == 1 else f"{CURRENT_LABEL} {cand.mpn}"
            result.rejected.append((label, why))
            current_failed[_norm_mpn(cand.mpn)] = why
        else:
            candidates.append(cand)

    hits = db.find_capacitors(
        capacitance_f=float(req.capacitance_f),
        package=req.package.value,
        voltage_min_v=float(req.voltage_min_v.value),
        dielectrics=tuple(req.dielectrics.value or ()),
        tolerance_max_pct=req.tolerance_max_pct.value,
    )
    for hit in hits:
        if _norm_mpn(hit.mpn) in current_failed:
            # The database refresh reads stock only; the current-part check
            # also read the DigiKey status, so its reason applies here.
            result.rejected.append((_label(hit.lcsc or "", hit.mpn),
                                    current_failed[_norm_mpn(hit.mpn)]))
            continue
        cand = _from_database(hit, jlc_part, digikey)
        same = [c for c in candidates if _norm_mpn(c.mpn) == _norm_mpn(cand.mpn)]
        if same:
            # The current part is also a database part: one entry, with the
            # database's usage record.
            same[0].db_part_id = cand.db_part_id
            same[0].usage_count = cand.usage_count
            continue
        why = _stock_floor(cand, needed)
        if why:
            result.rejected.append((_label(cand.lcsc or "", cand.mpn), why))
            continue
        candidates.append(cand)

    if candidates and not search_suppliers:
        candidates.sort(key=lambda c: rank_key(c, needed))
        return result
    if not getattr(digikey, "configured", False):
        result.problem = DIGIKEY_UNCONFIGURED
        return result

    listed: Set[str] = {_norm_mpn(c.mpn) for c in candidates}
    seen_lcsc: Set[str] = set()
    passed: List[Candidate] = []
    for keyword in search_keywords(req):
        for part in jlc_search(keyword):
            code = (part.lcsc or "").strip().upper()
            key = code or f"mpn:{_norm_mpn(part.mpn)}"
            if key in seen_lcsc:
                continue
            seen_lcsc.add(key)
            result.examined += 1
            label = _label(part.lcsc, part.mpn)
            cand, why = _jlc_check(req, part)
            if cand is None:
                result.rejected.append((label, why))
                continue
            if db.is_rejected(cand.manufacturer, cand.mpn):
                result.rejected.append((label, "rejected in the parts database"))
                continue
            if _norm_mpn(cand.mpn) in listed:
                continue  # already listed as the current part or from the database
            why = _stock_reason("JLCPCB", cand.jlc_stock, needed)
            if why:
                result.rejected.append((label, why))
                continue
            passed.append(cand)

    # Rank before the DigiKey lookups so the limit keeps the best hits. Several
    # LCSC codes can name one MPN; the best-ranked code is kept.
    passed.sort(key=lambda c: rank_key(c, needed))
    unique: List[Candidate] = []
    mpns: Set[str] = set()
    for cand in passed:
        norm = _norm_mpn(cand.mpn)
        if norm not in mpns:
            mpns.add(norm)
            unique.append(cand)
    # Confirm hits at DigiKey in rank order until *limit* pass, making at most
    # MAX_LOOKUP_FACTOR x limit lookups; many JLCPCB parts are not sold there.
    confirmed = looked_up = 0
    max_lookups = max(0, limit) * MAX_LOOKUP_FACTOR
    for cand in unique:
        if confirmed >= max(0, limit) or looked_up >= max_lookups:
            break
        looked_up += 1
        why = (_digikey_check(req, cand, digikey.product_for(cand.mpn))
               or _stock_reason("DigiKey", cand.digikey_stock, needed))
        if why:
            result.rejected.append((_label(cand.lcsc or "", cand.mpn), why))
            continue
        confirmed += 1
        candidates.append(cand)
    result.dropped_by_limit = len(unique) - looked_up

    candidates.sort(key=lambda c: rank_key(c, needed))
    return result


# --------------------------------------------------------------------------- #
# The symbol's existing part.
# --------------------------------------------------------------------------- #

def _current_check(req: CapacitorRequirement, cand: Candidate, db: PartsDb,
                   needed: int) -> str:
    """``""`` when the current part *cand* can be ordered, meets *req* and the
    stock floor and is not rejected in the parts database, else the reason.
    Its parameters are the DigiKey ones (see :func:`read_existing`), checked
    with the same rules as a search hit's."""
    if cand.unavailable_reason:
        return cand.unavailable_reason
    if cand.package != req.package.value:
        return f"DigiKey package '{cand.package or 'unknown'}', need {req.package.value}"
    p = cand.params
    shown = {
        DK_CAPACITANCE: fmt_farads(p.capacitance_f) if p.capacitance_f else None,
        DK_VOLTAGE: fmt_volts(p.voltage_v) if p.voltage_v is not None else None,
        DK_DIELECTRIC: p.dielectric or None,
        DK_TOLERANCE: (fmt_percent(p.tolerance_pct)
                       if p.tolerance_pct is not None else None),
    }
    params, why = _check_params(
        req, "DigiKey", shown.get,
        (DK_CAPACITANCE, DK_VOLTAGE, DK_DIELECTRIC, DK_TOLERANCE),
    )
    if params is None:
        return why
    if db.is_rejected(cand.manufacturer, cand.mpn):
        return "rejected in the parts database"
    return _stock_floor(cand, needed)


@dataclass
class CurrentOption:
    """One part named by a symbol's part-number fields.

    ``fields`` names the fields that resolve to it (``("DigiKeyPN", "LCSC")``);
    ``evidence`` is its parameters, from DigiKey when DigiKey lists it, else
    from JLCPCB; ``candidate`` is the part as a :class:`Candidate` (source
    ``current``), set only when both DigiKey and JLCPCB list it."""
    fields: Tuple[str, ...]
    evidence: PartEvidence
    candidate: Optional[Candidate] = None


@dataclass
class ExistingLookup:
    """What :func:`read_existing` found for a symbol's existing part numbers.

    ``options`` holds one :class:`CurrentOption` per distinct part, in field
    order (DigiKeyPN, MPN, LCSC); ``notes`` are display lines saying what was
    looked up and found."""
    options: List[CurrentOption] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def evidence(self) -> Optional[PartEvidence]:
        """The first option's evidence, ``None`` with no options."""
        return self.options[0].evidence if self.options else None

    @property
    def current(self) -> Optional[Candidate]:
        """The first option's candidate, ``None`` with no options."""
        return self.options[0].candidate if self.options else None


def _try(fn: Callable, arg: str, notes: List[str], what: str) -> Tuple[object, bool]:
    """``(fn(arg), True)``; on any exception a note naming *what* and
    ``(None, False)``, so the caller adds no second "not found" note."""
    try:
        return fn(arg), True
    except Exception as exc:  # noqa: BLE001 — a failed lookup becomes a note
        notes.append(f"{what} {arg} failed: {exc}")
        return None, False


def _evidence(label: str, get: Callable[[str], Optional[str]],
              names: Tuple[str, str, str, str],
              raw_package: Optional[str]) -> PartEvidence:
    """:class:`PartEvidence` from supplier parameters read through *get*
    under *names* (capacitance, voltage, dielectric, tolerance)."""
    cap_name, volt_name, diel_name, tol_name = names
    c = parse_capacitance(get(cap_name))
    tol_raw = get(tol_name)
    tol = _parse_tolerance(tol_raw, c) if c else parse_percent(tol_raw)
    return PartEvidence(
        label=label,
        capacitance_f=c,
        voltage_v=parse_volts(get(volt_name)),
        dielectrics=dielectric_tokens(get(diel_name)),
        tolerance_pct=tol,
        package=package_code(raw_package),
    )


def _best_jlc_hit(hits: Sequence[JlcPart], mpn: str) -> Optional[JlcPart]:
    """The JLCPCB hit whose MPN equals *mpn* (normalized), preferring an
    orderable one, then Basic, then more stock; ``None`` when none matches.
    Hits listed under JLCPCB's own name (``"JLCPCB Assembly"``, placeholder
    entries such as C9900013546 with no stock) are skipped."""
    want = _norm_mpn(mpn)
    exact = [h for h in hits if want and _norm_mpn(h.mpn or "") == want
             and not (h.manufacturer or "").strip().upper().startswith("JLCPCB")]
    if not exact:
        return None
    return min(exact, key=lambda h: (0 if h.orderable else 1,
                                     0 if h.is_basic else 1, -h.stock))


def _current_digikey_pn(product: DigiKeyProduct, existing: ExistingPart) -> Optional[str]:
    """The product's cut-tape DigiKey PN; else the existing DigiKeyPN or MPN
    field value when it is one of the product's PNs; else its first PN."""
    pns = list(product.digikey_pns or [])
    for pn in pns:
        if _CUT_TAPE_RE.search(pn):
            return pn
    upper = {pn.upper(): pn for pn in pns}
    for value in (existing.digikey_pn, existing.mpn):
        key = (value or "").strip().upper()
        if key in upper:
            return upper[key]
    return preferred_digikey_pn(pns)


def _label_digikey_pn(product: DigiKeyProduct, existing: ExistingPart,
                      looked_up: str = "") -> Optional[str]:
    """The name of the current part in display lines, as written on the
    schematic: the ``DigiKeyPN`` or ``MPN`` field value when it is one of the
    product's PNs (swapped fields included), else *looked_up* (the field value
    DigiKey resolved, an MPN), else the product's cut-tape PN."""
    upper = {pn.upper(): pn for pn in product.digikey_pns or []}
    for value in (existing.digikey_pn, existing.mpn):
        key = (value or "").strip().upper()
        if key in upper:
            return upper[key]
    return looked_up or preferred_digikey_pn(list(product.digikey_pns or []))


@dataclass
class _Found:
    """One distinct part while :func:`read_existing` collects them."""
    fields: List[str]
    product: Optional[DigiKeyProduct] = None
    dk_text: str = ""                    # the field value DigiKey resolved
    part: Optional[JlcPart] = None
    lcsc: str = ""                       # the LCSC field value, when it names this part


def read_existing(
    existing: ExistingPart,
    *,
    jlc_part: Callable[[str], Optional[JlcPart]],
    jlc_search: Callable[[str], Sequence[JlcPart]],
    digikey,
) -> ExistingLookup:
    """Look up the symbol's existing part numbers at DigiKey and JLCPCB.

    Each distinct ``MPN`` / ``DigiKeyPN`` value is looked up with
    ``digikey.product_for``, which accepts either kind, so swapped fields
    resolve. Values that resolve to the same MPN are one part; values that
    resolve to different MPNs are separate parts. The ``LCSC`` field is looked
    up at JLCPCB and joins the part with its MPN, else is a part of its own,
    looked up at DigiKey by its MPN. A part with a DigiKey product and no LCSC
    field is searched at JLCPCB by its MPN (exact match, see
    :func:`_best_jlc_hit`). With no DigiKey product and no ``LCSC`` field, the
    ``MPN`` then ``DigiKeyPN`` field values are searched at JLCPCB. Each part
    becomes a :class:`CurrentOption`; its evidence label names it by the field
    value that is one of the product's DigiKey PNs, else the cut-tape PN.
    Every failed lookup becomes a note; nothing raises."""
    out = ExistingLookup()
    if existing.empty:
        return out
    configured = bool(getattr(digikey, "configured", False))
    parts: List[_Found] = []

    def by_mpn(mpn: str) -> Optional[_Found]:
        key = _norm_mpn(mpn or "")
        for f in parts:
            have = f.product.mpn if f.product is not None else (f.part.mpn if f.part else "")
            if key and _norm_mpn(have or "") == key:
                return f
        return None

    # ---- DigiKey: each distinct MPN / DigiKeyPN value ----------------------
    seen: Set[str] = set()
    for name, value in (("DigiKeyPN", existing.digikey_pn), ("MPN", existing.mpn)):
        text = (value or "").strip()
        if not text or text.upper() in seen:
            continue
        seen.add(text.upper())
        if not configured:
            out.notes.append(f"{name} {text}: not looked up, DigiKey credentials are not set")
            continue
        product, ok = _try(digikey.product_for, text, out.notes, "DigiKey lookup of")
        if product is None:
            if ok:
                out.notes.append(f"{name} {text}: not found at DigiKey")
            continue
        status = (product.product_status or "").strip() or "status unknown"
        out.notes.append(f"{name} {text}: DigiKey {product.mpn or '(no MPN)'}, {status}")
        # A product listed without an MPN cannot name a different part: it
        # joins the other field's part, which keeps the product with an MPN.
        nameless = [f for f in parts if not (f.product.mpn or "").strip()]
        if not (product.mpn or "").strip() and parts:
            parts[0].fields.append(name)
        elif by_mpn(product.mpn) is not None:
            by_mpn(product.mpn).fields.append(name)
        elif nameless:
            nameless[0].fields.append(name)
            nameless[0].product, nameless[0].dk_text = product, text
        else:
            parts.append(_Found([name], product=product, dk_text=text))

    # ---- JLCPCB: the LCSC field --------------------------------------------
    lcsc = (existing.lcsc or "").strip()
    if lcsc:
        part, ok = _try(jlc_part, lcsc, out.notes, "JLCPCB lookup of")
        if part is None:
            if ok:
                out.notes.append(f"LCSC {lcsc}: not found at JLCPCB")
        else:
            out.notes.append(f"LCSC {lcsc}: JLCPCB {part.mpn or '(no MPN)'}")
            same = by_mpn(part.mpn or "")
            if same is None and len(parts) == 1 and not (parts[0].product.mpn or "").strip():
                same = parts[0]          # a DigiKey product without an MPN
            if same is not None:
                same.fields.append("LCSC")
                same.part, same.lcsc = part, lcsc
            else:
                found = _Found(["LCSC"], part=part, lcsc=lcsc)
                if configured and part.mpn:
                    product, _ = _try(digikey.product_for, part.mpn.strip(), out.notes,
                                      "DigiKey lookup of")
                    if product is not None and _norm_mpn(product.mpn or "") == _norm_mpn(part.mpn):
                        found.product, found.dk_text = product, part.mpn.strip()
                        out.notes.append(f"LCSC {lcsc} MPN {part.mpn}: DigiKey "
                                         f"{(product.product_status or '').strip() or 'status unknown'}")
                    else:
                        out.notes.append(f"LCSC {lcsc} MPN {part.mpn}: not found at DigiKey")
                parts.append(found)

    # ---- JLCPCB: a search by MPN for DigiKey parts without an LCSC ---------
    for f in parts:
        if f.part is not None or f.product is None:
            continue
        mpn = (f.product.mpn or "").strip()
        if not mpn:
            continue
        hits, ok = _try(jlc_search, mpn, out.notes, "JLCPCB search for")
        f.part = _best_jlc_hit(hits or [], mpn)
        if f.part is None:
            if ok:
                out.notes.append(f"MPN {mpn}: no exact match at JLCPCB")
        else:
            out.notes.append(f"MPN {mpn}: JLCPCB {f.part.lcsc}")
    if not parts and not lcsc:
        # Without a DigiKey product (DigiKey not configured, or no field
        # resolved there), a field that holds an MPN still matches at JLCPCB.
        for name, value in (("MPN", existing.mpn), ("DigiKeyPN", existing.digikey_pn)):
            text = (value or "").strip()
            if not text:
                continue
            hits, ok = _try(jlc_search, text, out.notes, "JLCPCB search for")
            part = _best_jlc_hit(hits or [], text)
            if part is not None:
                out.notes.append(f"{name} {text}: JLCPCB {part.lcsc}")
                parts.append(_Found([name], part=part))
                break
            if ok:
                out.notes.append(f"{name} {text}: no exact match at JLCPCB")

    for f in parts:
        out.options.append(_option(f, existing, out.notes))
    return out


def _option(f: _Found, existing: ExistingPart, notes: List[str]) -> CurrentOption:
    """The :class:`CurrentOption` for one collected part; a note when it is
    not a candidate because one supplier does not list it."""
    product, part = f.product, f.part
    if product is not None:
        ev = _evidence(
            f"current part {_label_digikey_pn(product, existing, f.dk_text)} (DigiKey)",
            product.param,
            (DK_CAPACITANCE, DK_VOLTAGE, DK_DIELECTRIC, DK_TOLERANCE),
            product.param(DK_PACKAGE),
        )
    else:
        ev = _evidence(
            f"current part {part.lcsc or f.lcsc} (JLCPCB)", part.attributes.get,
            (JLC_CAPACITANCE, JLC_VOLTAGE, JLC_DIELECTRIC, JLC_TOLERANCE),
            part.package,
        )
    option = CurrentOption(tuple(f.fields), ev)
    if product is None or part is None:
        missing = "DigiKey" if product is None else "JLCPCB"
        notes.append(f"{ev.label} is not a candidate: not found at {missing}")
        return option
    status = (product.product_status or "").strip()
    unavailable = ""
    if status.lower() != "active":
        unavailable = f"DigiKey status '{status or 'unknown'}'"
    elif not part.orderable:
        unavailable = f"not orderable at JLCPCB ({part.no_buy_reason or 'out of stock'})"
    mpn = (product.mpn or "").strip()
    option.candidate = Candidate(
        mpn=mpn or part.mpn.strip(),
        manufacturer=part.manufacturer.strip() or (existing.manufacturer or "").strip(),
        package=ev.package or package_code(part.package) or "",
        params=CapacitorParams(ev.capacitance_f, ev.voltage_v,
                               ", ".join(ev.dielectrics), ev.tolerance_pct),
        lcsc=part.lcsc or f.lcsc or None,
        jlc_library=_jlc_library(part.library),
        jlc_preferred=part.preferred,
        jlc_stock=part.stock if not part.no_buy_reason else 0,
        jlc_price=part.unit_price,
        digikey_pn=_current_digikey_pn(product, existing),
        digikey_stock=product.quantity_available,
        source=CURRENT,
        description=product.description or part.description or "",
        datasheet_url=product.datasheet_url or "",
        unavailable_reason=unavailable,
    )
    return option


def _disagreement(ref: PartEvidence, ev: PartEvidence) -> List[str]:
    """Lines for a capacitance (more than 0.1 % apart) or package that *ev*
    states differently from *ref*."""
    out: List[str] = []
    a, b = ref.capacitance_f, ev.capacitance_f
    if a and b and abs(a - b) > _CAP_REL_TOL * max(a, b):
        out.append(f"{ref.label} says {fmt_farads(a)}; {ev.label} says {fmt_farads(b)}")
    if ref.package and ev.package and ref.package != ev.package:
        out.append(f"{ref.label} says {ref.package}; {ev.label} says {ev.package}")
    return out


def _loosest(options: Sequence[CurrentOption]) -> PartEvidence:
    """The first option's evidence with the lowest voltage rating, the widest
    tolerance and every dielectric of *options*, so each option meets the
    requirement built from it on those fields."""
    first = options[0].evidence
    evs = [o.evidence for o in options]
    volts = [e.voltage_v for e in evs if e.voltage_v is not None]
    tols = [e.tolerance_pct for e in evs if e.tolerance_pct is not None]
    diels: List[str] = []
    for e in evs:
        diels += [d for d in e.dielectrics if d not in diels]
    return replace(first, voltage_v=min(volts) if volts else None,
                   tolerance_pct=max(tols) if tols else None,
                   dielectrics=tuple(diels))


def requirement_from_lookup(
    group: RequirementGroup, lookup: Optional[ExistingLookup],
    defaults: Mapping[str, str],
) -> Tuple[CapacitorRequirement, List[Candidate]]:
    """The group's requirement built from the existing parts that fit the
    schematic, and those parts' candidates.

    An option fits when its capacitance and package agree with every
    schematic source (Value, Footprint, ``Package:Preferred``, the Value's
    size suffix) and with the first fitting option. The requirement's part
    evidence is the first fitting option's, with the lowest voltage, widest
    tolerance and every dielectric of all fitting options
    (:func:`_loosest`). Each option that does not fit adds a
    warning naming the disagreement, ending ``(not used)``; when none fits,
    the requirement is the schematic's alone and the search runs from it."""
    base = group.requirement_with_part(None, defaults)
    if lookup is None or not lookup.options:
        return base, []
    used: List[CurrentOption] = []
    warnings: List[str] = []
    for opt in lookup.options:
        req = group.requirement_with_part(opt.evidence, defaults)
        lines = [c for c in req.conflicts if c not in base.conflicts]
        if not lines and used:
            lines = _disagreement(used[0].evidence, opt.evidence)
        if lines:
            warnings += [f"{line} ({opt.evidence.label} not used)" for line in lines]
        else:
            used.append(opt)
    req = group.requirement_with_part(_loosest(used) if used else None, defaults)
    req = replace(req, warnings=tuple(req.warnings) + tuple(warnings))
    return req, [o.candidate for o in used if o.candidate is not None]


def assess(
    group: RequirementGroup,
    defaults: Mapping[str, str],
    *,
    db: PartsDb,
    jlc_search: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    digikey,
    needed: int,
    limit: int = 10,
    search_suppliers: bool = False,
    requirement: Optional[CapacitorRequirement] = None,
    existing: Optional[ExistingLookup] = None,
) -> SearchResult:
    """The candidates for *group*, starting from its existing part.

    *existing* (an :class:`ExistingLookup` from an earlier call) is reused
    when given; otherwise the group's existing part numbers are looked up with
    :func:`read_existing`. *requirement* (one edited in the assign dialog) is
    used as given; otherwise the group's requirement comes from
    :func:`requirement_from_lookup`. Either way the current parts are the
    lookup's options that fit the schematic. A requirement that is not
    searchable gives a result with :attr:`SearchResult.problem` set and no
    supplier calls. The result's ``requirement`` and ``existing`` are set."""
    lookup = existing
    if lookup is None:
        lookup = ExistingLookup()
        if not group.existing.empty:
            lookup = read_existing(group.existing, jlc_part=jlc_part,
                                   jlc_search=jlc_search, digikey=digikey)
    fitted, currents = requirement_from_lookup(group, lookup, defaults)
    req = requirement if requirement is not None else fitted
    if not req.searchable:
        return SearchResult([], problem=req.problem(), requirement=req,
                            existing=lookup)
    result = find_candidates(
        req, db=db, jlc_search=jlc_search, jlc_part=jlc_part, digikey=digikey,
        needed=needed, limit=limit, search_suppliers=search_suppliers,
        currents=currents,
    )
    result.requirement = req
    result.existing = lookup
    return result
