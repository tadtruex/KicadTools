"""
Candidate parts for a plain LED (``LED_PLAN.md``, step 3).

The order follows :mod:`kicadtools.partsearch`:

0. **Current part** — the symbol's MPN, DigiKeyPN or LCSC, looked up at
   DigiKey and JLCPCB (:func:`read_existing_led`). Listed first when it can
   be ordered and meets the limits; below the stock floor it is kept with a
   warning.
1. **Stored parts** — accepted LEDs in the package from the parts database
   (:meth:`~kicadtools.partsdb.PartsDb.find_leds`) that pass
   :func:`check_led` on their stored values, with stock and status refreshed
   at JLCPCB and DigiKey. Always listed; below the stock floor with a
   warning.
2. **JLCPCB listing** — when nothing so far meets the stock floor, or when
   asked. JLCPCB's colour keyword does not filter, so the whole LED category
   is listed for the package with an empty keyword (*jlc_listing*, cached per
   package for a run by :func:`cached_listing`) and filtered here:
   manufacturer list (``led.manufacturers``), orderable, JLCPCB stock floor,
   not rejected in the database, the same package, one die, dominant
   wavelength in the band (:meth:`LedRequirement.color_matches`), intensity
   at or above ``Intensity:Min``, forward voltage at or below ``Vf:Max``,
   lens. A stated limit the part does not list rejects it.
3. **DigiKey** — survivors are ranked (:func:`kicadtools.partsearch.rank_key`:
   uses, Basic, preferred, stock, price) and looked up by MPN until *limit*
   pass: same MPN, active, the same package, the DigiKey values meeting the
   requirement where DigiKey lists them, DigiKey stock floor.

Multicolour and smart LEDs (:class:`kicadtools.ledrequirement.NamedLedGroup`)
are IC groups: :func:`kicadtools.icsearch.assess_ic` searches them as they
are.

Pure apart from the injected callables.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from kicadtools import ledcolor
from kicadtools.evidence import ExistingPart
from kicadtools.ledrequirement import (
    LED_KIND,
    LedEvidence,
    LedGroup,
    LedRequirement,
    lens_kind,
)
from kicadtools.partsdb import DbLed, LedChoice, PartParams, PartsDb
from kicadtools.partsearch import (
    CURRENT,
    CURRENT_LABEL,
    DIGIKEY_UNCONFIGURED,
    MAX_LOOKUP_FACTOR,
    SEARCH,
    Candidate,
    SearchResult,
    _CUT_TAPE_RE,
    _best_jlc_hit,
    _from_database,
    _current_digikey_pn,
    _jlc_library,
    _label,
    _stock_floor,
    _stock_reason,
    _try,
    manufacturer_allowed,
    parse_manufacturers,
    rank_key,
)
from kicadtools.parttypes import DK_PACKAGE
from kicadtools.suppliers import DigiKeyProduct, JlcPart, _norm_mpn, preferred_digikey_pn
from kicadtools.units import package_code

# JLCPCB's category for one-colour indicator LEDs.
JLC_LED_CATEGORY = ("Optoelectronics", "LED Indication - Discrete")


@dataclass(frozen=True)
class LedNames:
    """A supplier's parameter names for the LED fields; each a tuple tried
    in order."""
    color: Tuple[str, ...]
    dominant: Tuple[str, ...]
    peak: Tuple[str, ...]
    intensity: Tuple[str, ...]
    vf: Tuple[str, ...]
    lens: Tuple[str, ...]


JLC_NAMES = LedNames(
    color=("Illumination Color", "Emitted Color"),
    dominant=("Wavelength - Dominant", "Dominant Wavelength"),
    peak=("Peak Wavelength", "Wavelength - Peak"),
    intensity=("Luminous Intensity",),
    vf=("Voltage - Forward(Vf)", "Forward Voltage (Vf)"),
    lens=("Lens Color", "Lens Type"),
)
DK_NAMES = LedNames(
    color=("Color",),
    dominant=("Wavelength - Dominant",),
    peak=("Wavelength - Peak",),
    intensity=("Millicandela Rating",),
    vf=("Voltage - Forward (Vf) (Typ)",),
    lens=("Lens Transparency", "Lens Color"),
)


def _first(get: Callable[[str], Optional[str]], names: Sequence[str]) -> Optional[str]:
    for name in names:
        raw = get(name)
        if raw is not None and str(raw).strip() not in ("", "-"):
            return str(raw)
    return None


def led_evidence(label: str, get: Callable[[str], Optional[str]],
                 names: LedNames, package: Optional[str]) -> LedEvidence:
    """:class:`LedEvidence` from supplier parameters read through *get*."""
    return LedEvidence(
        label=label,
        package=package,
        dominant=ledcolor.parse_nm(_first(get, names.dominant)),
        peak=ledcolor.parse_nm(_first(get, names.peak)),
        labels=ledcolor.parse_labels(_first(get, names.color)),
        intensity_mcd=ledcolor.parse_mcd(_first(get, names.intensity)),
        vf=ledcolor.parse_vf(_first(get, names.vf)),
        lens=lens_kind(_first(get, names.lens)),
    )


def jlc_evidence(part: JlcPart, label: str = "") -> LedEvidence:
    return led_evidence(label or _label(part.lcsc, part.mpn), part.attributes.get,
                        JLC_NAMES, package_code(part.package))


def digikey_evidence(product: DigiKeyProduct, label: str = "") -> LedEvidence:
    return led_evidence(label or f"{product.mpn} (DigiKey)", product.param,
                        DK_NAMES, package_code(product.param(DK_PACKAGE)))


@dataclass
class LedCandidate(Candidate):
    """A :class:`~kicadtools.partsearch.Candidate` with the LED's supplier
    values. ``params`` holds the dominant wavelength as ``nominal`` (0 when
    none is listed) and the colour as ``dielectric``, for display."""
    led: LedEvidence = field(default_factory=lambda: LedEvidence(""))

    def choice(self) -> LedChoice:
        """This candidate for :meth:`PartsDb.accept_part` /
        :meth:`PartsDb.reject_part`."""
        ev = self.led
        return LedChoice(
            mpn=self.mpn, manufacturer=self.manufacturer, package=self.package,
            color=ev.color or "",
            dominant_nm=ev.dominant[0] if ev.dominant else None,
            peak_nm=ev.peak[0] if ev.peak else None,
            intensity_mcd=ev.intensity_mcd[0] if ev.intensity_mcd else None,
            vf_v=ev.vf[0] if ev.vf else None,
            lens=ev.lens,
            digikey_pn=self.digikey_pn,
            digikey_packaging=("cut tape" if self.digikey_pn
                               and _CUT_TAPE_RE.search(self.digikey_pn) else None),
            lcsc=self.lcsc, jlc_library=self.jlc_library,
            description=self.description, datasheet_url=self.datasheet_url,
        )


def stored_evidence(hit: DbLed) -> LedEvidence:
    """A stored LED's values as :class:`LedEvidence`."""
    return LedEvidence(
        label=_label(hit.lcsc or "", hit.mpn),
        package=hit.package or None,
        dominant=(hit.dominant_nm,) if hit.dominant_nm is not None else (),
        peak=(hit.peak_nm,) if hit.peak_nm is not None else (),
        labels=(hit.color,) if hit.color else (),
        intensity_mcd=(hit.intensity_mcd,) if hit.intensity_mcd is not None else (),
        vf=(hit.vf_v,) if hit.vf_v is not None else (),
        lens=hit.lens,
    )


def _params(ev: LedEvidence) -> PartParams:
    return PartParams(ev.dominant[0] if ev.dominant else 0.0, None,
                      ev.color or "", None)


def _fmt(values: Sequence[float], unit: str) -> str:
    return ", ".join(f"{v:g}" for v in values) + f" {unit}"


def check_led(req: LedRequirement, ev: LedEvidence, supplier: str, *,
              missing_ok: bool = False) -> str:
    """``""`` when *ev* meets *req*, else the reason. With *missing_ok* a
    value the supplier does not list passes (DigiKey after JLCPCB has
    already checked it)."""
    if ev.dies > 1:
        return f"{supplier} lists {ev.dies} dies"
    pkg = req.package.value
    if ev.package and ev.package != pkg:
        return f"{supplier} package {ev.package}, need {pkg}"
    if not ev.package and not missing_ok:
        return f"{supplier} lists no package"
    ok = req.color_matches(ev)
    if ok is False:
        return f"{supplier} {ev.wavelength_text()}, need {req.band_text()}"
    if ok is None and not missing_ok:
        return f"{supplier} lists no wavelength or colour"
    lo = req.intensity_min.value
    if lo is not None:
        if not ev.intensity_mcd:
            if not missing_ok:
                return f"{supplier} lists no luminous intensity"
        elif ev.intensity_mcd[0] < lo:
            return f"{supplier} {_fmt(ev.intensity_mcd, 'mcd')}, need at least {lo:g} mcd"
    hi = req.vf_max.value
    if hi is not None:
        if not ev.vf:
            if not missing_ok:
                return f"{supplier} lists no forward voltage"
        elif ev.vf[0] > hi + 1e-6:
            return f"{supplier} Vf {_fmt(ev.vf, 'V')}, need at most {hi:g} V"
    lenses = req.lenses.value
    if lenses:
        if ev.lens is None:
            if not missing_ok:
                return f"{supplier} lists no lens"
        elif ev.lens not in lenses:
            return f"{supplier} lens {ev.lens}, need {', '.join(lenses)}"
    return ""


# --------------------------------------------------------------------------- #
# The symbol's existing part.
# --------------------------------------------------------------------------- #

@dataclass
class LedLookup:
    """What :func:`read_existing_led` found: the part's evidence (DigiKey's
    when DigiKey lists it, else JLCPCB's), the part as a candidate when both
    list it, and display notes."""
    evidence: Optional[LedEvidence] = None
    current: Optional[LedCandidate] = None
    notes: List[str] = field(default_factory=list)


def read_existing_led(existing: ExistingPart, *, jlc_part, jlc_search,
                      digikey) -> LedLookup:
    """Look up the symbol's part numbers: ``DigiKeyPN`` then ``MPN`` at
    DigiKey (the first found is the part), then ``LCSC`` at JLCPCB, else the
    part's MPN searched at JLCPCB. An LCSC that names a different MPN from
    DigiKey's is noted and not used. Nothing raises; failures become notes."""
    out = LedLookup()
    if existing.empty:
        return out
    product: Optional[DigiKeyProduct] = None
    configured = bool(getattr(digikey, "configured", False))
    for name, value in (("DigiKeyPN", existing.digikey_pn), ("MPN", existing.mpn)):
        text = (value or "").strip()
        if not text or product is not None:
            continue
        if not configured:
            out.notes.append(f"{name} {text}: not looked up, DigiKey credentials are not set")
            continue
        found, ok = _try(digikey.product_for, text, out.notes, "DigiKey lookup of")
        if found is None:
            if ok:
                out.notes.append(f"{name} {text}: not found at DigiKey")
            continue
        product = found
        out.notes.append(f"{name} {text}: DigiKey {product.mpn or '(no MPN)'}, "
                         f"{(product.product_status or '').strip() or 'status unknown'}")

    part: Optional[JlcPart] = None
    lcsc = (existing.lcsc or "").strip()
    if lcsc:
        part, ok = _try(jlc_part, lcsc, out.notes, "JLCPCB lookup of")
        if part is None and ok:
            out.notes.append(f"LCSC {lcsc}: not found at JLCPCB")
        elif (part is not None and product is not None
              and _norm_mpn(part.mpn) != _norm_mpn(product.mpn or "")):
            out.notes.append(f"LCSC {lcsc} is {part.mpn}, DigiKey's part is "
                             f"{product.mpn}; LCSC not used")
            part = None
    if part is None:
        mpn = ((product.mpn if product is not None else None) or existing.mpn or "").strip()
        if mpn:
            hits, ok = _try(jlc_search, mpn, out.notes, "JLCPCB search for")
            part = _best_jlc_hit(hits or [], mpn)
            if part is None and ok:
                out.notes.append(f"MPN {mpn}: no exact match at JLCPCB")

    if product is not None:
        shown = (existing.digikey_pn or existing.mpn or product.mpn or "").strip()
        out.evidence = digikey_evidence(product, f"{CURRENT_LABEL} {shown} (DigiKey)")
    elif part is not None:
        out.evidence = jlc_evidence(part, f"{CURRENT_LABEL} {part.lcsc} (JLCPCB)")
    if product is None or part is None:
        if out.evidence is not None:
            missing = "DigiKey" if product is None else "JLCPCB"
            out.notes.append(f"{out.evidence.label} is not a candidate: "
                             f"not found at {missing}")
        return out

    status = (product.product_status or "").strip()
    unavailable = ""
    if status.lower() != "active":
        unavailable = f"DigiKey status '{status or 'unknown'}'"
    elif part.no_buy_reason:
        unavailable = f"not orderable at JLCPCB ({part.no_buy_reason})"
    ev = out.evidence
    out.current = LedCandidate(
        kind=LED_KIND,
        mpn=(product.mpn or part.mpn).strip(),
        manufacturer=part.manufacturer.strip() or (existing.manufacturer or "").strip(),
        package=ev.package or "",
        params=_params(ev),
        lcsc=part.lcsc or lcsc or None,
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
        led=ev,
    )
    return out


# --------------------------------------------------------------------------- #
# Search.
# --------------------------------------------------------------------------- #

def cached_listing(fetch: Callable[..., Sequence[JlcPart]]
                   ) -> Callable[[str], Sequence[JlcPart]]:
    """A package -> JLCPCB listing callable that fetches each package once.
    *fetch* is :func:`kicadtools.suppliers.jlcpcb_category_search`; it is
    called with an empty keyword and :data:`JLC_LED_CATEGORY`."""
    seen: Dict[str, Sequence[JlcPart]] = {}

    def listing(package: str) -> Sequence[JlcPart]:
        if package not in seen:
            seen[package] = list(fetch("", package, JLC_LED_CATEGORY,
                                       max_pages=20, page_size=100))
        return seen[package]
    return listing


def _from_jlc(req: LedRequirement, part: JlcPart) -> Tuple[Optional[LedCandidate], str]:
    if not part.mpn.strip():
        return None, "JLCPCB lists no MPN"
    ev = jlc_evidence(part)
    why = check_led(req, ev, "JLCPCB")
    if why:
        return None, why
    return LedCandidate(
        kind=LED_KIND, mpn=part.mpn.strip(), manufacturer=part.manufacturer.strip(),
        package=ev.package or "", params=_params(ev), lcsc=part.lcsc or None,
        jlc_library=_jlc_library(part.library), jlc_preferred=part.preferred,
        jlc_stock=part.stock, jlc_price=part.unit_price, digikey_pn=None,
        digikey_stock=None, source=SEARCH, description=part.description, led=ev,
    ), ""


def _confirm(req: LedRequirement, cand: LedCandidate,
             product: Optional[DigiKeyProduct], needed: int) -> str:
    if product is None:
        return "not found at DigiKey"
    if _norm_mpn(product.mpn or "") != _norm_mpn(cand.mpn):
        return f"DigiKey returned MPN '{product.mpn or ''}'"
    status = (product.product_status or "").strip()
    if status.lower() != "active":
        return f"DigiKey status '{status or 'unknown'}'"
    why = check_led(req, digikey_evidence(product), "DigiKey", missing_ok=True)
    if why:
        return why
    pn = preferred_digikey_pn(list(product.digikey_pns or []))
    if not pn:
        return "DigiKey lists no part number"
    cand.digikey_pn = pn
    cand.digikey_stock = product.quantity_available
    cand.datasheet_url = product.datasheet_url or ""
    cand.description = cand.description or product.description or ""
    return _stock_reason("DigiKey", cand.digikey_stock, needed)


def find_led_candidates(
    req: LedRequirement,
    *,
    db: PartsDb,
    jlc_listing: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    digikey,
    needed: int,
    limit: int = 10,
    search_suppliers: bool = False,
    currents: Sequence[LedCandidate] = (),
    manufacturers: Sequence[str] = (),
) -> SearchResult:
    """The parts meeting *req*, best first; see the module docstring.
    *jlc_part* refreshes stored parts' JLCPCB stock."""
    if not req.searchable:
        return SearchResult([], problem=req.problem())
    result = SearchResult([])
    candidates = result.candidates
    for current in currents:
        cand = replace(current)
        why = (cand.unavailable_reason or check_led(req, cand.led, "DigiKey",
                                                    missing_ok=True)
               or ("rejected in the parts database"
                   if db.is_rejected(cand.manufacturer, cand.mpn) else ""))
        if why:
            result.rejected.append((CURRENT_LABEL, why))
            continue
        cand.stock_warning = _stock_floor(cand, needed)
        candidates.append(cand)

    for hit in db.find_leds(req.package.value):
        ev = stored_evidence(hit)
        if check_led(req, ev, "parts database", missing_ok=True):
            continue
        same = [c for c in candidates if _norm_mpn(c.mpn) == _norm_mpn(hit.mpn)]
        if same:
            same[0].db_part_id = hit.part_id
            same[0].usage_count = hit.usage_count
            continue
        base = _from_database(hit, jlc_part, digikey)
        cand = LedCandidate(**{k: getattr(base, k) for k in base.__dataclass_fields__},
                            led=ev)
        if cand.unavailable_reason:
            result.rejected.append((_label(cand.lcsc or "", cand.mpn),
                                    cand.unavailable_reason))
            continue
        cand.stock_warning = _stock_floor(cand, needed)
        candidates.append(cand)

    if any(not c.stock_warning for c in candidates) and not search_suppliers:
        candidates.sort(key=lambda c: rank_key(c, needed))
        return result
    if not getattr(digikey, "configured", False):
        result.problem = DIGIKEY_UNCONFIGURED
        return result

    listed: Set[str] = {_norm_mpn(c.mpn) for c in candidates}
    seen: Set[str] = set()
    passed: List[LedCandidate] = []
    for part in jlc_listing(req.package.value):
        key = (part.lcsc or "").strip().upper() or f"mpn:{_norm_mpn(part.mpn)}"
        if key in seen:
            continue
        seen.add(key)
        result.examined += 1
        label = _label(part.lcsc, part.mpn)
        if not manufacturer_allowed(part.manufacturer, manufacturers):
            result.rejected.append(
                (label, f"manufacturer '{part.manufacturer}' is not in the list"))
            continue
        why = (f"not orderable at JLCPCB ({part.no_buy_reason or 'out of stock'})"
               if not part.orderable else _stock_reason("JLCPCB", part.stock, needed))
        if not why and db.is_rejected(part.manufacturer, part.mpn):
            why = "rejected in the parts database"
        cand = None
        if not why:
            cand, why = _from_jlc(req, part)
        if cand is None:
            result.rejected.append((label, why))
            continue
        if _norm_mpn(cand.mpn) not in listed:
            passed.append(cand)
    result.pool_size = len(passed)

    passed.sort(key=lambda c: rank_key(c, needed))
    unique: List[LedCandidate] = []
    mpns: Set[str] = set()
    for cand in passed:
        if _norm_mpn(cand.mpn) not in mpns:
            mpns.add(_norm_mpn(cand.mpn))
            unique.append(cand)
    confirmed = looked_up = 0
    for cand in unique:
        if confirmed >= limit or looked_up >= limit * MAX_LOOKUP_FACTOR:
            break
        looked_up += 1
        why = _confirm(req, cand, digikey.product_for(cand.mpn), needed)
        if why:
            result.rejected.append((_label(cand.lcsc or "", cand.mpn), why))
            continue
        confirmed += 1
        candidates.append(cand)
    result.dropped_by_limit = len(unique) - looked_up
    candidates.sort(key=lambda c: rank_key(c, needed))
    return result


def assess_led(
    group: LedGroup,
    defaults: Mapping[str, str],
    *,
    db: PartsDb,
    jlc_listing: Callable[[str], Sequence[JlcPart]],
    jlc_part: Callable[[str], Optional[JlcPart]],
    jlc_search: Callable[[str], Sequence[JlcPart]],
    digikey,
    needed: int,
    limit: int = 10,
    search_suppliers: bool = False,
    requirement: Optional[LedRequirement] = None,
    existing: Optional[LedLookup] = None,
) -> SearchResult:
    """The candidates for *group*: the existing part looked up
    (:func:`read_existing_led`, or *existing* when given), the requirement
    built with its evidence (or *requirement* as edited), then
    :func:`find_led_candidates`. The result's ``requirement`` and
    ``existing`` are set."""
    lookup = existing
    if lookup is None:
        lookup = read_existing_led(group.existing, jlc_part=jlc_part,
                                   jlc_search=jlc_search, digikey=digikey)
    req = requirement or group.requirement_with_part(lookup.evidence, defaults)
    currents = [lookup.current] if lookup.current is not None else []
    if not req.searchable:
        return SearchResult([], problem=req.problem(), requirement=req,
                            existing=lookup)
    result = find_led_candidates(
        req, db=db, jlc_listing=jlc_listing, jlc_part=jlc_part, digikey=digikey,
        needed=needed,
        limit=limit, search_suppliers=search_suppliers, currents=currents,
        manufacturers=parse_manufacturers(defaults.get("led.manufacturers")),
    )
    result.requirement = req
    result.existing = lookup
    return result
