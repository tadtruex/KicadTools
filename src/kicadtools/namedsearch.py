"""Search for through-hole parts whose footprint names the part
(``THT_PLAN.md`` step 6).

A footprint such as ``Connector_JST:JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal``
or ``digikey-footprints:CUI_PJ-002B`` gives a part-number token
(:attr:`kicadtools.thtfootprint.FootprintSignature.mpn`). The token is often
a whole MPN, but can be a series prefix (``Bourns_3386P`` → ``3386P``) or a
pattern with placeholder segments (``SAMTEC_USB-A-S-X-X-TH``).

:func:`find_named_candidates` searches DigiKey only (through-hole parts are
soldered by hand and need no LCSC number). The texts tried, in order:

1. the symbol's ``MPN`` field, looked up exactly
   (:meth:`~kicadtools.suppliers.DigiKeyClient.product_for`);
2. the footprint's token;
3. the part-number tokens of the ``Value`` (:func:`value_tokens`); a Value
   token that fits the footprint token is tried before it, since it says
   more (``USB-A-S-F-B-TH`` on ``SAMTEC_USB-A-S-X-X-TH``).

Texts 2 and 3 are partial: one DigiKey keyword search, keeping only hits
whose MPN starts with the text (placeholder segments of ``X`` match any
segment), the exact MPN first and then the shortest. The kept hits are
confirmed with ``product_for`` (keyword hits can omit the stock), at most
:data:`MAX_LOOKUPS` per group. The first text that gives a part ends the
search.

Each part is then checked:

- DigiKey status ``Active`` and stock at least
  :func:`~kicadtools.icsearch.stock_minimum`, else rejected with a reason;
- stock below :func:`~kicadtools.icsearch.stock_floor` gives a warning;
- the footprint's pad count against the part's positions or pins
  (:func:`pad_check`). Connectors often have mounting or shield pads beyond
  their positions, so more pads than positions is "cannot verify", not a
  mismatch; fewer pads is a mismatch. The check ranks candidates and never
  removes one.

Rank: an MPN equal to the text tried, then the pad check (match, cannot
verify, mismatch), then stock meeting the floor, then DigiKey stock, then
price. Pure apart from the injected *digikey* (``configured``,
``product_for``, ``keyword_search``, as on
:class:`~kicadtools.suppliers.DigiKeyClient`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from kicadtools.icsearch import MATCH, MISMATCH, UNVERIFIED, stock_floor, stock_minimum
from kicadtools.package import DK_PIN_PARAMS
from kicadtools.partnum import extract_part_numbers
from kicadtools.partsdb import norm_mpn
from kicadtools.suppliers import DigiKeyProduct, preferred_digikey_pn
from kicadtools.thtfootprint import NAMED_PART, FootprintSignature, looks_like_part_number

# product_for confirmations per group.
MAX_LOOKUPS = 5

MPN_FIELD = "MPN"
FOOTPRINT = "footprint"
VALUE = "Value"

# The DigiKey parameters a pad count is compared with, first found wins.
POSITION_PARAMS: Tuple[str, ...] = ("Number of Positions", *DK_PIN_PARAMS)

NO_DIGIKEY = "DigiKey credentials are not set (DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET)"
NO_TEXT = "no part number in the footprint name, the MPN field or the Value"

_PLACEHOLDER = re.compile(r"^[xX]+$")
_INT = re.compile(r"^\s*(\d+)")


@dataclass(frozen=True)
class PadCheck:
    """The footprint's pad count against the part's positions. ``result`` is
    :data:`~kicadtools.icsearch.MATCH`, ``MISMATCH`` or ``UNVERIFIED``."""

    result: str
    reason: str = ""

    @property
    def rank(self) -> int:
        return {MATCH: 0, UNVERIFIED: 1}.get(self.result, 2)


@dataclass
class NamedCandidate:
    """One DigiKey part for a part-named footprint."""

    product: DigiKeyProduct
    label: str  # the text that found it: "footprint S3B-PH-K", "MPN …", "Value …"
    exact: bool  # its MPN equals that text (normalized)
    pads: PadCheck = field(default_factory=lambda: PadCheck(UNVERIFIED))
    stock_warning: str = ""

    @property
    def mpn(self) -> str:
        return (self.product.mpn or "").strip()

    @property
    def manufacturer(self) -> str:
        return (self.product.manufacturer or "").strip()

    @property
    def digikey_pn(self) -> Optional[str]:
        return preferred_digikey_pn(self.product.digikey_pns)

    @property
    def stock(self) -> Optional[int]:
        return self.product.quantity_available

    @property
    def price(self) -> float:
        p = self.product.unit_price
        return float(p) if p is not None else float("inf")


@dataclass
class NamedSearchResult:
    """One :func:`find_named_candidates` call. ``rejected`` holds (MPN,
    reason); ``notes`` say what was searched and found; ``problem`` is a
    one-line reason when the search could not run or found no part."""

    signature: FootprintSignature
    candidates: List[NamedCandidate] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    problem: str = ""
    dropped_by_limit: int = 0

    @property
    def proposed(self) -> Optional[NamedCandidate]:
        return self.candidates[0] if self.candidates else None


# --- texts and matching ---------------------------------------------------------


_VALUE_SPLIT = re.compile(r"[\s_,]+")


def value_tokens(value: object) -> Tuple[str, ...]:
    """The part-number tokens of a *value*: split on whitespace, ``_`` and
    ``,``; kept when :func:`~kicadtools.thtfootprint.looks_like_part_number`
    (so ``USB-A-S-F-B-TH`` is kept, though it has no digit, and the
    ``01x04`` of ``Conn_01x04`` is not); longest first."""
    if not isinstance(value, str):
        return ()
    tokens = [t for t in _VALUE_SPLIT.split(value.strip()) if looks_like_part_number(t)]
    return tuple(sorted(dict.fromkeys(tokens), key=len, reverse=True))


def named_texts(signature: FootprintSignature,
                props: Mapping[str, object]) -> List[Tuple[str, str, str]]:
    """``(kind, label, text)`` to try, in order, without repeats
    (normalized):

    1. the MPN field (:data:`MPN_FIELD`);
    2. Value tokens that fit the footprint token (:func:`mpn_fits`): they
       say more than the footprint (Value ``USB-A-S-F-B-TH`` on footprint
       ``SAMTEC_USB-A-S-X-X-TH``);
    3. the footprint token (:data:`FOOTPRINT`);
    4. the other Value tokens (:data:`VALUE`)."""
    out: List[Tuple[str, str, str]] = []
    seen = set()

    def add(kind: str, text: Optional[str]) -> None:
        text = (text or "").strip()
        key = norm_mpn(text)
        if key and key not in seen:
            seen.add(key)
            out.append((kind, f"{kind} {text}", text))

    add(MPN_FIELD, extract_part_numbers(props).mpn)
    tokens = value_tokens(props.get("Value"))
    footprint = signature.mpn if signature.family == NAMED_PART else ""
    if footprint:
        for token in tokens:
            if mpn_fits(token, footprint):
                add(VALUE, token)
        add(FOOTPRINT, footprint)
    for token in tokens:
        add(VALUE, token)
    return out


def search_pattern(text: str) -> Tuple[str, Optional[re.Pattern]]:
    """``(keyword, pattern)`` for a partial MPN *text*. Without placeholder
    segments the keyword is *text* and the pattern ``None`` (a prefix match).
    With them (``USB-A-S-X-X-TH``) the keyword is the text before the first
    placeholder (``USB-A-S``) and the pattern matches the start of a
    normalized MPN (:func:`kicadtools.partsdb.norm_mpn`: upper case, hyphens
    kept), a placeholder segment standing for any letters and digits."""
    segments = text.strip().split("-")
    if not any(_PLACEHOLDER.match(s) for s in segments):
        return text.strip(), None
    first = next(i for i, s in enumerate(segments) if _PLACEHOLDER.match(s))
    keyword = "-".join(segments[:first]).strip()
    parts = ["[A-Z0-9]+" if _PLACEHOLDER.match(s) else re.escape(norm_mpn(s)) for s in segments]
    return keyword, re.compile("-".join(parts))


def mpn_fits(mpn: str, text: str) -> bool:
    """True when *mpn* starts with partial MPN *text*, both normalized
    (upper case, whitespace removed, punctuation kept); placeholder segments
    of *text* match any letters and digits."""
    _, pattern = search_pattern(text)
    key = norm_mpn(mpn)
    if pattern is None:
        want = norm_mpn(text)
        return bool(want) and key.startswith(want)
    return pattern.match(key) is not None


# --- checks ---------------------------------------------------------------------


def positions_of(product: DigiKeyProduct) -> Optional[int]:
    """The part's positions or pins from its DigiKey parameters, or ``None``."""
    for name in POSITION_PARAMS:
        m = _INT.match(product.param(name) or "")
        if m:
            return int(m.group(1))
    return None


def pad_check(pad_count: Optional[int], product: DigiKeyProduct) -> PadCheck:
    """*pad_count* (distinct pads on the footprint) against the part's
    positions (:func:`positions_of`)."""
    positions = positions_of(product)
    if pad_count is None:
        return PadCheck(UNVERIFIED, "footprint pad count unknown")
    if positions is None:
        return PadCheck(UNVERIFIED, "DigiKey states no positions or pins")
    if pad_count == positions:
        return PadCheck(MATCH)
    if pad_count > positions:
        return PadCheck(
            UNVERIFIED,
            f"{pad_count} pads vs {positions} positions (mounting or shield pads?)",
        )
    return PadCheck(MISMATCH, f"{pad_count} pads vs {positions} positions")


def _reject_reason(product: DigiKeyProduct, needed: int) -> str:
    status = (product.product_status or "").strip()
    if status and status.lower() != "active":
        return f"DigiKey status {status}"
    stock = product.quantity_available
    least = stock_minimum(needed)
    if stock is None:
        return "DigiKey stock unknown"
    if stock < least:
        return f"DigiKey stock {stock}, need at least {least}"
    return ""


def _stock_warning(product: DigiKeyProduct, needed: int) -> str:
    stock = product.quantity_available
    floor = stock_floor(needed)
    if stock is not None and stock < floor:
        return f"DigiKey stock {stock}, below {floor}"
    return ""


def named_rank_key(c: NamedCandidate) -> tuple:
    """Sort key, lower is better."""
    return (
        0 if c.exact else 1,
        c.pads.rank,
        1 if c.stock_warning else 0,
        -(c.stock or 0),
        c.price,
    )


# --- search ---------------------------------------------------------------------


def _confirm(digikey, mpn: str, hit: Optional[DigiKeyProduct], notes: List[str]) -> Optional[DigiKeyProduct]:
    """The full product for *mpn* (``product_for``), else the keyword *hit*."""
    try:
        product = digikey.product_for(mpn)
    except Exception as exc:  # noqa: BLE001 - a failed call becomes a note
        notes.append(f"DigiKey lookup of {mpn} failed: {exc}")
        product = None
    if product is not None and norm_mpn(product.mpn or "") == norm_mpn(mpn):
        return product
    return hit


def _partial(digikey, label: str, text: str, budget: List[int],
             result: NamedSearchResult) -> Dict[str, Tuple[DigiKeyProduct, bool]]:
    """One keyword search for partial MPN *text*: ``{normalized MPN:
    (product, exact)}`` for the hits that fit, confirmed while *budget*
    lasts."""
    keyword, _ = search_pattern(text)
    if not keyword:
        result.notes.append(f"{label}: nothing to search before the placeholders")
        return {}
    try:
        hits = digikey.keyword_search(keyword) or []
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"{label}: DigiKey keyword search failed: {exc}")
        return {}
    by_mpn: Dict[str, DigiKeyProduct] = {}
    for p in hits:
        mpn = (p.mpn or "").strip()
        if mpn and mpn_fits(mpn, text) and norm_mpn(mpn) not in by_mpn:
            by_mpn[norm_mpn(mpn)] = p
    if not by_mpn:
        result.notes.append(f"{label}: no DigiKey part starts with {text}")
        return {}
    want = norm_mpn(text)
    order = sorted(by_mpn, key=lambda k: (0 if k == want else 1, len(k)))
    found: Dict[str, Tuple[DigiKeyProduct, bool]] = {}
    for key in order:
        if budget[0] <= 0:
            result.dropped_by_limit += 1
            continue
        budget[0] -= 1
        hit = by_mpn[key]
        product = _confirm(digikey, hit.mpn.strip(), hit, result.notes)
        found[key] = (product, key == want)
    result.notes.append(
        f"{label}: DigiKey " + ", ".join(p.mpn.strip() for p, _ in found.values())
        + (f" ({result.dropped_by_limit} more not looked up)" if result.dropped_by_limit else "")
    )
    return found


def find_named_candidates(
    signature: FootprintSignature,
    props: Mapping[str, object],
    *,
    digikey,
    needed: int,
    pad_count: Optional[int] = None,
) -> NamedSearchResult:
    """The DigiKey parts for a group whose footprint has *signature* and
    whose first member's properties are *props*; *needed* is the build
    quantity, *pad_count* the footprint's pad count. See the module
    docstring."""
    result = NamedSearchResult(signature=signature)
    if not getattr(digikey, "configured", False):
        result.problem = NO_DIGIKEY
        return result
    texts = named_texts(signature, props)
    if not texts:
        result.problem = NO_TEXT
        return result

    found: Dict[str, Tuple[DigiKeyProduct, bool, str]] = {}
    budget = [MAX_LOOKUPS]
    for kind, label, text in texts:
        if kind == MPN_FIELD:
            product = _confirm(digikey, text, None, result.notes)
            if product is None:
                result.notes.append(f"{label}: not found at DigiKey")
                continue
            result.notes.append(f"{label}: DigiKey {product.mpn.strip()}")
            found[norm_mpn(product.mpn)] = (product, True, label)
            break
        hits = _partial(digikey, label, text, budget, result)
        if hits:
            for key, (product, exact) in hits.items():
                found[key] = (product, exact, label)
            break

    for product, exact, label in found.values():
        why = _reject_reason(product, needed)
        if why:
            result.rejected.append(((product.mpn or "").strip(), why))
            continue
        result.candidates.append(NamedCandidate(
            product=product,
            label=label,
            exact=exact,
            pads=pad_check(pad_count, product),
            stock_warning=_stock_warning(product, needed),
        ))
    result.candidates.sort(key=named_rank_key)
    if not result.candidates:
        if result.rejected:
            result.problem = "no part passes: " + "; ".join(f"{m} ({r})" for m, r in result.rejected)
        else:
            result.problem = "no DigiKey part found for " + ", ".join(t for _, _, t in texts)
    return result
