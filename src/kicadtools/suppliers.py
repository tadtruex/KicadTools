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
from typing import Optional

import requests

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
V4_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
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


class DigiKeyClient:
    """Resolve a DigiKey part number to its manufacturer part number (MPN).

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

    def mpn_for(self, digikey_pn: str) -> Optional[str]:
        """Return the manufacturer part number for *digikey_pn*, or ``None``.

        Uses DigiKey Product Search v4 (keyword), asking for a single record.
        """
        if not self.configured or not digikey_pn or not self._ensure_token():
            return None

        payload = {"Keywords": digikey_pn, "Limit": 1, "Offset": 0}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-DIGIKEY-Client-Id": self.client_id,
        }

        try:
            resp = requests.post(
                V4_SEARCH_URL, headers=headers, json=payload, timeout=_TIMEOUT
            )
            if resp.status_code == 401 and self._obtain_token():
                headers["Authorization"] = f"Bearer {self._token}"
                resp = requests.post(
                    V4_SEARCH_URL, headers=headers, json=payload, timeout=_TIMEOUT
                )
        except requests.RequestException as exc:
            print(f"⚠️  Network error searching DigiKey for {digikey_pn}: {exc}")
            return None

        if resp.status_code != 200:
            try:
                msg = resp.json().get("message") or resp.text
            except ValueError:
                msg = resp.text
            print(f"⚠️  DigiKey search failed ({resp.status_code}) for {digikey_pn}: {msg}")
            return None

        products = resp.json().get("Products", [])
        if not products:
            return None
        return products[0].get("ManufacturerProductNumber")


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

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://jlcpcb.com",
        "Referer": "https://jlcpcb.com/parts",
    }
    body = {"currentPage": 1, "pageSize": 25, "keyword": mpn, "searchSource": "search"}

    try:
        resp = requests.post(
            LCSC_SEARCH_URL, headers=headers, json=body, timeout=_TIMEOUT
        )
    except requests.RequestException as exc:
        print(f"⚠️  Network error searching LCSC for {mpn}: {exc}")
        return None
    if resp.status_code != 200:
        print(f"⚠️  LCSC search failed ({resp.status_code}) for {mpn}")
        return None
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        print(f"⚠️  LCSC search returned non-JSON for {mpn}")
        return None

    parts = (data.get("componentPageInfo") or {}).get("list") or []
    target = _norm_mpn(mpn)
    matches = [
        p for p in parts if _norm_mpn(str(p.get("componentModelEn", ""))) == target
    ]
    if not matches:
        return None

    def _in_stock(part: dict) -> bool:
        for key in ("stockCount", "stock", "leastNumber"):
            try:
                if float(part.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    best = next((p for p in matches if _in_stock(p)), matches[0])
    code = best.get("componentCode")
    return code or None
