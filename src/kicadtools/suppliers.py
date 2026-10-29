"""
Supplier API clients: DigiKey (part number -> MPN) and JLCPCB/LCSC (MPN -> LCSC).

Logic lifted from ``Code/Digikey/main.py`` and cleaned up: the DigiKey OAuth
token is held on a :class:`DigiKeyClient` instance instead of module globals, and
the JLCPCB lookup is a plain function. Every network failure degrades to
``None`` (never raises) so the resolver can flag the part and move on.

Requires ``requests``. DigiKey needs ``DIGIKEY_CLIENT_ID`` /
``DIGIKEY_CLIENT_SECRET`` (passed in or read from the environment).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
V4_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
# Exact-lookup endpoint: accepts a DigiKey PN *or* an MPN and returns the single
# matching product as ``{"Product": {...}}``. Unlike the keyword search (a fuzzy
# relevance search whose rank-1 hit can be an unrelated part), this resolves the
# exact part — so it is what we use for parametric confirm/fill.
V4_DETAILS_URL = "https://api.digikey.com/products/v4/search/{pn}/productdetails"
# JLCPCB's SMT parts search. The old GET ``/parts/componentSearch`` endpoint now
# serves an HTML SPA shell (no JSON), so scraping it yields garbage; this JSON
# API returns each hit's ``componentModelEn`` (its MPN), which we verify against
# the query so an unrelated first-result can never be accepted.
LCSC_SEARCH_URL = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1"
    "/shoppingCart/smtGood/selectSmtComponentList"
)

_TIMEOUT = 12


def _norm_mpn(mpn: str) -> str:
    """Normalize an MPN for comparison: upper-case, alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", mpn.upper())


def _stock_count(part: dict) -> float:
    """Best-effort JLCPCB stock for a search hit (0.0 if unknown/unparseable)."""
    for key in ("stockCount", "stock", "leastNumber"):
        try:
            return max(0.0, float(part.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0.0


def _in_stock(part: dict) -> bool:
    return _stock_count(part) > 0


def _min_price(part: dict) -> float:
    """Lowest tier unit price for a hit (``inf`` when no price tiers present),
    so a cheaper part sorts ahead of a pricier one as a final tiebreak."""
    prices = part.get("componentPrices") or []
    tiers = []
    for tier in prices:
        try:
            tiers.append(float(tier.get("productPrice")))
        except (TypeError, ValueError, AttributeError):
            continue
    return min(tiers) if tiers else float("inf")


def _is_placeholder(part: dict) -> bool:
    """True for a JLCPCB-internal ``C9900…`` SKU that carries no LCSC catalog
    page — a synthetic entry (brand ``JLCPCB Assembly``, no ``lcscGoodsUrl``) we
    deprioritize behind the genuine catalog part for the same MPN. A ``C9900…``
    code that *does* have a catalog page is left alone (it can be orderable)."""
    code = str(part.get("componentCode") or "")
    return bool(re.match(r"^C9900\d+$", code)) and not part.get("lcscGoodsUrl")


def _rank_match(part: dict) -> tuple:
    """Sort key (lower is better) to pick the best of several exact-MPN hits.

    Priority: in-stock (assemblable) first; then a genuine catalog part over a
    ``C9900…`` placeholder; then a Basic-library part (no setup fee) over an
    Extended one; then a "preferred" Extended part; then more stock; then a
    lower unit price. Rooted in the JLCPCB parts-API semantics confirmed by the
    spec research — the real orderability signal is stock, not the C-number."""
    lib = str(part.get("componentLibraryType") or "").lower()
    return (
        0 if _in_stock(part) else 1,
        1 if _is_placeholder(part) else 0,
        0 if lib == "base" else 1,
        0 if part.get("preferredComponentFlag") else 1,
        -_stock_count(part),
        _min_price(part),
    )


def lcsc_url(lcsc: str) -> Optional[str]:
    """The LCSC store search URL for an LCSC code (e.g. ``C12345``), or ``None``
    when *lcsc* is empty. Search (not a product-detail slug) resolves reliably
    from just the code."""
    lcsc = (lcsc or "").strip()
    return f"https://www.lcsc.com/search?q={quote(lcsc, safe='')}" if lcsc else None


def digikey_url(digikey_pn: str) -> Optional[str]:
    """The DigiKey keyword-search URL for a DigiKey part number, or ``None`` when
    *digikey_pn* is empty."""
    pn = (digikey_pn or "").strip()
    return (
        f"https://www.digikey.com/en/products/result?keywords={quote(pn, safe='')}"
        if pn
        else None
    )


@dataclass
class DigiKeyProduct:
    """A resolved DigiKey product with its full parametric attribute set.

    ``parameters`` maps DigiKey's own ``ParameterText`` (e.g. ``"Resistance"``,
    ``"Power (Watts)"``, ``"Supplier Device Package"``) to the raw ``ValueText``
    (e.g. ``"10 kOhms"``, ``"0.063W, 1/16W"``, ``"0402"``). Values are kept raw
    — normalizing DigiKey's rendering against an intent's opaque strings is the
    confirm layer's job, not this client's.
    """
    mpn: Optional[str] = None
    digikey_pns: List[str] = field(default_factory=list)
    datasheet_url: Optional[str] = None
    product_url: Optional[str] = None
    quantity_available: Optional[int] = None
    product_status: Optional[str] = None
    description: Optional[str] = None
    parameters: Dict[str, str] = field(default_factory=dict)

    def param(self, name: str) -> Optional[str]:
        """The raw ValueText for a DigiKey parameter name, or ``None``."""
        return self.parameters.get(name)


class DigiKeyClient:
    """Look a DigiKey part up and return its MPN + parametric attributes.

    Holds its own OAuth2 client-credentials token and refreshes it as needed.
    Construct once and reuse across many lookups so the token is shared.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self.client_id = client_id or os.getenv("DIGIKEY_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("DIGIKEY_CLIENT_SECRET")
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        """True when both credentials are present (env or constructor)."""
        return bool(self.client_id and self.client_secret)

    def _obtain_token(self) -> bool:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            resp = requests.post(TOKEN_URL, data=payload, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            print(f"⚠️  Network error requesting DigiKey token: {exc}")
            return False
        if resp.status_code != 200:
            print(f"⚠️  DigiKey token request failed ({resp.status_code}): {resp.text}")
            return False
        data = resp.json()
        self._token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = time.monotonic() + expires_in - 30  # safety margin
        if not self._token:
            print("⚠️  DigiKey token response contained no access_token.")
            return False
        return True

    def _ensure_token(self) -> bool:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            return self._obtain_token()
        return True

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "X-DIGIKEY-Client-Id": self.client_id,
        }

    def product_for(self, part_number: str) -> Optional[DigiKeyProduct]:
        """Return the exact :class:`DigiKeyProduct` for *part_number*, or ``None``.

        *part_number* may be a DigiKey PN or an MPN. Uses the v4
        ``productdetails`` endpoint (exact lookup) — never the fuzzy keyword
        search, whose rank-1 hit can be an unrelated part. Every failure
        degrades to ``None`` so the resolver can flag the part and move on.
        """
        if not self.configured or not part_number or not self._ensure_token():
            return None

        url = V4_DETAILS_URL.format(pn=quote(part_number, safe=""))
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=_TIMEOUT)
            if resp.status_code == 401 and self._obtain_token():
                resp = requests.get(url, headers=self._auth_headers(), timeout=_TIMEOUT)
        except requests.RequestException as exc:
            print(f"⚠️  Network error looking up DigiKey part {part_number}: {exc}")
            return None

        if resp.status_code == 404:
            return None  # no such part — a normal "not found", not an error
        if resp.status_code != 200:
            try:
                msg = resp.json().get("message") or resp.text
            except ValueError:
                msg = resp.text
            print(f"⚠️  DigiKey lookup failed ({resp.status_code}) for {part_number}: {msg}")
            return None

        try:
            product = resp.json().get("Product")
        except ValueError:
            print(f"⚠️  DigiKey lookup returned non-JSON for {part_number}")
            return None
        if not product:
            return None
        return _parse_product(product)

    def mpn_for(self, digikey_pn: str) -> Optional[str]:
        """Return the manufacturer part number for *digikey_pn*, or ``None``.

        Thin wrapper over :meth:`product_for` kept for the resolver's
        ``mpn_for(dk_pn) -> Optional[str]`` contract.
        """
        product = self.product_for(digikey_pn)
        return product.mpn if product else None


def _quantity(product: dict) -> Optional[int]:
    """DigiKey's stock for a v4 ``Product``: ``QuantityAvailable`` when given,
    else the largest per-packaging quantity, else 0 when the product lists
    packagings (DigiKey leaves the field out at zero stock), else ``None``."""
    qty = product.get("QuantityAvailable")
    if qty is not None:
        return int(qty)
    per_pack = [
        int(v.get("QuantityAvailableforPackageType") or 0)
        for v in (product.get("ProductVariations") or [])
    ]
    return max(per_pack) if per_pack else None


def _parse_product(product: dict) -> DigiKeyProduct:
    """Build a :class:`DigiKeyProduct` from a v4 ``Product`` object."""
    parameters = {
        str(p.get("ParameterText")): str(p.get("ValueText"))
        for p in (product.get("Parameters") or [])
        if p.get("ParameterText") is not None
    }
    dk_pns = [
        v.get("DigiKeyProductNumber")
        for v in (product.get("ProductVariations") or [])
        if v.get("DigiKeyProductNumber")
    ]
    desc = product.get("Description") or {}
    status = product.get("ProductStatus") or {}
    return DigiKeyProduct(
        mpn=product.get("ManufacturerProductNumber"),
        digikey_pns=dk_pns,
        datasheet_url=product.get("DatasheetUrl"),
        product_url=product.get("ProductUrl"),
        # DigiKey omits QuantityAvailable when the product has none in stock;
        # the per-packaging quantities are then present (and zero).
        quantity_available=_quantity(product),
        # ProductStatus is an object like {"Id": 0, "Status": "Active"}.
        product_status=status.get("Status") if isinstance(status, dict) else status,
        description=desc.get("ProductDescription") if isinstance(desc, dict) else None,
        parameters=parameters,
    )


_JLC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://jlcpcb.com",
    "Referer": "https://jlcpcb.com/parts",
}


def _jlc_search_raw(
    keyword: str, *, page: int = 1, page_size: int = 25
) -> Optional[List[dict]]:
    """One page of raw JLCPCB search hits for *keyword*, or ``None`` on any
    network/HTTP/JSON failure (reported on stdout, never raised)."""
    body = {
        "currentPage": page,
        "pageSize": page_size,
        "keyword": keyword,
        "searchSource": "search",
    }
    try:
        resp = requests.post(
            LCSC_SEARCH_URL, headers=_JLC_HEADERS, json=body, timeout=_TIMEOUT
        )
    except requests.RequestException as exc:
        print(f"⚠️  Network error searching LCSC for {keyword}: {exc}")
        return None
    if resp.status_code != 200:
        print(f"⚠️  LCSC search failed ({resp.status_code}) for {keyword}")
        return None
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        print(f"⚠️  LCSC search returned non-JSON for {keyword}")
        return None
    return (data.get("componentPageInfo") or {}).get("list") or []


@dataclass
class JlcPart:
    """One JLCPCB catalog entry: what the assembly line can place, how many are
    in stock, and the part's parametric attributes.

    ``attributes`` maps JLCPCB's ``attribute_name_en`` (``"Capacitance"``,
    ``"Voltage Rating"``, ``"Power(Watts)"`` …) to the raw value text
    (``"100nF"``, ``"25V"``, ``"100mW"``). Placeholder ``C9900…`` SKUs carry no
    attributes. ``no_buy_reason`` is JLCPCB's own explanation when the part can't
    be bought (e.g. ``"This product is no longer manufactured."``).
    """
    lcsc: str
    mpn: str = ""
    manufacturer: str = ""
    library: str = ""          # "base" (Basic) | "expand" (Extended)
    preferred: bool = False    # JLCPCB "preferred" Extended part
    stock: int = 0
    unit_price: float = float("inf")  # lowest tier price, USD
    package: str = ""          # e.g. "0603"
    description: str = ""
    no_buy_reason: str = ""
    catalog_url: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)

    @property
    def is_basic(self) -> bool:
        return self.library.lower() == "base"

    @property
    def orderable(self) -> bool:
        """In stock and not marked unbuyable by JLCPCB."""
        return self.stock > 0 and not self.no_buy_reason


def _parse_jlc_part(hit: dict) -> JlcPart:
    attrs = {
        str(a.get("attribute_name_en")): str(a.get("attribute_value_name"))
        for a in (hit.get("attributes") or [])
        if a.get("attribute_name_en") and a.get("attribute_value_name") is not None
    }
    return JlcPart(
        lcsc=str(hit.get("componentCode") or ""),
        mpn=str(hit.get("componentModelEn") or ""),
        manufacturer=str(hit.get("componentBrandEn") or ""),
        library=str(hit.get("componentLibraryType") or ""),
        preferred=bool(hit.get("preferredComponentFlag")),
        stock=int(_stock_count(hit)),
        unit_price=_min_price(hit),
        package=str(hit.get("componentSpecificationEn") or ""),
        description=str(hit.get("describe") or ""),
        no_buy_reason=str(hit.get("noBuyReason") or ""),
        catalog_url=str(hit.get("lcscGoodsUrl") or ""),
        attributes=attrs,
    )


def jlcpcb_part(lcsc: str) -> Optional[JlcPart]:
    """The JLCPCB catalog entry for LCSC code *lcsc* (exact code match), or
    ``None`` when it isn't found or the lookup fails."""
    code = (lcsc or "").strip().upper()
    if not code:
        return None
    hits = _jlc_search_raw(code, page_size=10)
    for hit in hits or []:
        if str(hit.get("componentCode") or "").upper() == code:
            return _parse_jlc_part(hit)
    return None


def jlcpcb_search(keyword: str, *, pages: int = 3, page_size: int = 100) -> List[JlcPart]:
    """Up to ``pages × page_size`` JLCPCB catalog hits for *keyword*, in
    JLCPCB's relevance order. Stops early on a short page or a failed request."""
    parts: List[JlcPart] = []
    for page in range(1, pages + 1):
        hits = _jlc_search_raw(keyword, page=page, page_size=page_size)
        if not hits:
            break
        parts.extend(_parse_jlc_part(h) for h in hits)
        if len(hits) < page_size:
            break
    return parts


def preferred_digikey_pn(digikey_pns: List[str]) -> Optional[str]:
    """Pick the cut-tape variant from a product's DigiKey part numbers.

    DigiKey lists one product under several packagings — tape & reel
    (``…-2-ND`` / ``…TR-ND``), cut tape (``…-1-ND`` / ``…CT-ND``) and Digi-Reel
    (``…-6-ND`` / ``…DKR-ND``). Cut tape is what a small build orders and what
    the boards here already store, so it wins; otherwise the first listed."""
    for pn in digikey_pns:
        if re.search(r"(-1-ND|CT-ND)$", pn):
            return pn
    return digikey_pns[0] if digikey_pns else None


def jlcpcb_lcsc_for(mpn: str) -> Optional[str]:
    """Return the LCSC part number for *mpn* via JLCPCB's parts API, or ``None``.

    Only a hit whose ``componentModelEn`` matches *mpn* (compared on normalized
    alphanumerics) is accepted, so an unrelated first result is never returned;
    among matches, an in-stock one is preferred. NB: this endpoint is unofficial
    and unauthenticated — it can change or rate-limit without notice, in which
    case the part is simply flagged.
    """
    if not mpn:
        return None

    parts = _jlc_search_raw(mpn)
    if parts is None:
        return None
    target = _norm_mpn(mpn)
    matches = [
        p for p in parts if _norm_mpn(str(p.get("componentModelEn", ""))) == target
    ]
    if not matches:
        return None

    # Among exact-MPN hits, pick the best orderable part (see _rank_match) so a
    # synthetic C9900 placeholder never wins over the genuine catalog part.
    best = min(matches, key=_rank_match)
    code = best.get("componentCode")
    return code or None
