"""Through-hole parts in the parts database (``THT_PLAN.md`` step 8).

An accepted through-hole part is stored (:class:`~kicadtools.partsdb.ThtChoice`,
table ``tht``) under the footprint signature it was accepted for, so the next
group with the same signature, on any board, finds it before DigiKey is
searched.

The lookup key (:func:`db_key`) is the signature (:func:`signature_key`:
family and geometry from the footprint name, and the part-number token of a
part-named footprint) and, for every family but headers, sockets and
terminal blocks, the normalized Value (:func:`value_key`). A header is fixed
by its geometry; a part-named footprint is not: ``Bourns_3386P`` takes a
200K and a 10K pot alike, so a stored 200K part must not be offered for a 10K
one. A different Value gives a fresh search, not a wrong part.

:func:`stored_candidates` reads the stored parts for a group and refreshes
each at DigiKey (one ``product_for`` call each): status and stock change, so
a stored part is offered only while it passes the same checks as a searched
one (:func:`reject_reason`). The header and part-named searches call it
first and do not search DigiKey when a stored part passes, which keeps
within DigiKey's 1000 calls a day.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from kicadtools.icsearch import stock_floor, stock_minimum
from kicadtools.package import DK_PIN_PARAMS
from kicadtools.partsdb import DbTht, PartsDb, ThtChoice, norm_mpn
from kicadtools.suppliers import DigiKeyProduct, preferred_digikey_pn
from kicadtools.thtfootprint import HEADER, SOCKET, TERMINAL_BLOCK, FootprintSignature

# Families found again by their signature alone.
SIGNATURE_ONLY = frozenset({HEADER, SOCKET, TERMINAL_BLOCK})

DATABASE_LABEL = "parts database"


def _num(x: Optional[float]) -> str:
    return "" if x is None else f"{x:g}"


def signature_key(sig: FootprintSignature) -> str:
    """The signature as stored: ``field=value`` pairs joined with ``;``, for
    the fields that say which part fits (not the library id or the maker of
    a terminal-block library). Two footprints with the same geometry from
    different libraries give the same key."""
    fields = [
        ("family", sig.family),
        ("rows", _num(sig.rows)),
        ("per_row", _num(sig.per_row)),
        ("pitch", _num(sig.pitch_mm)),
        ("orientation", sig.orientation),
        ("smd", "1" if sig.smd else ""),
        ("style", sig.style),
        ("polarized", "1" if sig.polarized else ""),
        ("d", _num(sig.diameter_mm)),
        ("l", _num(sig.length_mm)),
        ("w", _num(sig.width_mm)),
        ("h", _num(sig.height_mm)),
        # Lead spacings beyond the pitch (a film box's two spacings).
        ("spacing", ",".join(_num(x) for x in sig.lead_spacing_mm)
         if tuple(sig.lead_spacing_mm) not in ((), (sig.pitch_mm,)) else ""),
        ("mpn", norm_mpn(sig.mpn)),
    ]
    return ";".join(f"{k}={v}" for k, v in fields if v)


def value_key(value: object) -> str:
    """The Value as compared: whitespace removed, upper case."""
    return "".join(str(value or "").split()).upper()


def db_key(sig: FootprintSignature, value: object) -> Tuple[str, str]:
    """``(signature key, value key)`` for a group: the value key is ``""`` for
    :data:`SIGNATURE_ONLY` families."""
    return signature_key(sig), ("" if sig.family in SIGNATURE_ONLY else value_key(value))


# The DigiKey parameters a pad count is compared with, first found wins.
POSITION_PARAMS: Tuple[str, ...] = ("Number of Positions", *DK_PIN_PARAMS)
_INT = re.compile(r"^\s*(\d+)")


def positions_of(product: DigiKeyProduct) -> Optional[int]:
    """The part's positions or pins from its DigiKey parameters
    (:data:`POSITION_PARAMS`), or ``None``."""
    for name in POSITION_PARAMS:
        m = _INT.match(product.param(name) or "")
        if m:
            return int(m.group(1))
    return None


def tht_choice(product: DigiKeyProduct, sig: FootprintSignature, value: object) -> ThtChoice:
    """The :class:`ThtChoice` that records *product* for groups with
    footprint *sig* and *value*."""
    signature, vkey = db_key(sig, value)
    dk_pns = list(product.digikey_pns or [])
    pn = preferred_digikey_pn(dk_pns)
    return ThtChoice(
        mpn=(product.mpn or "").strip(),
        manufacturer=(product.manufacturer or "").strip(),
        family=sig.family,
        signature=signature,
        value_key=vkey,
        footprint=sig.lib_id,
        positions=positions_of(product) or sig.positions,
        package=(product.param("Package / Case") or "").strip(),
        digikey_pn=pn,
        digikey_packaging="cut tape" if pn and (pn.endswith("-1-ND") or pn.endswith("CT-ND")) else None,
        description=product.description or "",
        datasheet_url=product.datasheet_url or "",
    )


def reject_reason(product: DigiKeyProduct, needed: int) -> str:
    """Why *product* cannot be offered: status not Active, or DigiKey stock
    unknown or below :func:`~kicadtools.icsearch.stock_minimum`; ``""``
    when it can."""
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


def stock_warning(product: DigiKeyProduct, needed: int) -> str:
    """A warning when DigiKey's stock is below build + 1000; ``""``
    otherwise."""
    stock = product.quantity_available
    floor = stock_floor(needed)
    if stock is not None and stock < floor:
        return f"DigiKey stock {stock}, below {floor}"
    return ""


@dataclass
class Stored:
    """The stored parts for one group, refreshed at DigiKey."""

    parts: List[Tuple[DbTht, DigiKeyProduct]] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)  # (MPN, reason)
    notes: List[str] = field(default_factory=list)


def stored_candidates(db: Optional[PartsDb], digikey, sig: FootprintSignature,
                      value: object, needed: int) -> Stored:
    """The accepted parts stored for *sig* and *value* that pass
    :func:`reject_reason` after a ``product_for`` refresh (by DigiKey PN, else
    MPN), best first (:meth:`PartsDb.find_tht`). Without *db*, or without
    DigiKey credentials, nothing is looked up."""
    out = Stored()
    if db is None:
        return out
    signature, vkey = db_key(sig, value)
    rows = db.find_tht(signature, vkey)
    if not rows:
        return out
    if not getattr(digikey, "configured", False):
        out.notes.append(f"{DATABASE_LABEL}: {len(rows)} stored part(s) not checked, "
                         "DigiKey credentials are not set")
        return out
    for row in rows:
        try:
            product = digikey.product_for(row.digikey_pn or row.mpn)
        except Exception as exc:  # noqa: BLE001 - a failed call becomes a note
            out.notes.append(f"{DATABASE_LABEL}: DigiKey lookup of {row.mpn} failed: {exc}")
            continue
        if product is None or norm_mpn(product.mpn or "") != norm_mpn(row.mpn):
            out.rejected.append((row.mpn, "not found at DigiKey"))
            continue
        why = reject_reason(product, needed)
        if why:
            out.rejected.append((row.mpn, why))
            continue
        out.parts.append((row, product))
    if out.parts:
        out.notes.append(f"{DATABASE_LABEL}: " + ", ".join(r.mpn for r, _ in out.parts))
    for mpn, why in out.rejected:
        out.notes.append(f"{DATABASE_LABEL}: {mpn} not offered ({why})")
    return out


def is_rejected(db: Optional[PartsDb], product: DigiKeyProduct) -> bool:
    """True when *db* records *product* (manufacturer, MPN) as rejected."""
    return db is not None and db.is_rejected(product.manufacturer or "", product.mpn or "")
