"""Pin header and socket search, and the run's series (``THT_PLAN.md`` step 5).

A header group (parts with one footprint signature, ``thtfootprint``) is
searched at DigiKey with :meth:`~kicadtools.suppliers.DigiKeyClient.category_search`
in leaf category 314 (Headers, Male Pins) or 315 (Headers, Receptacles,
Female Sockets), filtered by the signature: positions, rows, pitch, mounting
type (vertical = "Through Hole", horizontal = "Through Hole, Right Angle",
SMD variant = "Surface Mount"), contact type; in stock at least
:func:`~kicadtools.icsearch.stock_minimum` of the build quantity; active;
cheapest first.

:class:`HeaderRun` holds one series per family (header, socket) for one run:

1. Before a series is set, a group searches with the maker list
   (:data:`HEADER_MAKERS`, the ``hdr.manufacturers`` setting).
2. The first part the user picks for a family sets its series
   (:func:`series_from_pick`): the manufacturer, the values of
   :data:`SERIES_PARAMETERS` the part has (and of
   :data:`SAME_ORIENTATION_PARAMETERS`, applied only to groups with the
   pick's orientation), and an MPN pattern with the position digits replaced (``PRPC004SAAN-RC`` → ``PRPC0\\d\\dSAAN-RC``).
3. Later groups of that family search the series (its manufacturer and
   parameter values) and rank the MPN the pattern predicts first, then other
   pattern matches, then the rest by price. When the series has no match the
   group falls back to the maker list, with a note. Picking a part for a
   later group does not change the series.

The series is not stored between runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from kicadtools.icsearch import stock_floor, stock_minimum
from kicadtools.suppliers import DigiKeyClient, DigiKeyProduct
from kicadtools.thtfootprint import HEADER, HORIZONTAL, SOCKET, FootprintSignature

HEADER_CATEGORY = 314  # Headers, Male Pins
SOCKET_CATEGORY = 315  # Headers, Receptacles, Female Sockets
CATEGORIES = {HEADER: HEADER_CATEGORY, SOCKET: SOCKET_CATEGORY}
KEYWORD = "header"

HEADER_MAKERS: Tuple[str, ...] = (
    "Sullins", "Samtec", "Amphenol", "Harwin", "Wurth", "TE", "Molex",
)

# The non-size parameters a series fixes, when the picked part has them. The
# contact lengths differ between vertical and right-angle parts, so they apply
# only to groups with the pick's orientation and mounting.
SERIES_PARAMETERS: Tuple[str, ...] = (
    "Contact Finish - Mating",
    "Connector Type",
)
SAME_ORIENTATION_PARAMETERS: Tuple[str, ...] = (
    "Contact Length - Mating",
    "Contact Length - Post",
)

# DigiKey's "Number of Rows" filter in the socket category (parameter 2172)
# returns no hits for any value (2026-11-03), so for sockets the rows are
# checked on each hit's own parameter after the search, with more hits asked.
_ROWS_AFTER_SEARCH = {SOCKET}
_ROWS_AFTER_SEARCH_LIMIT = 50

_CONTACT = {HEADER: "Male Pin", SOCKET: "Female Socket"}
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class HeaderSeries:
    """What the first pick of a family fixes for the rest of the run."""

    family: str
    manufacturer: str
    parameters: Tuple[Tuple[str, str], ...]  # (DigiKey parameter name, value name)
    mpn: str  # the picked MPN
    pattern: Optional[str] = None  # regex with the position digits as \d{k}
    digits_span: Optional[Tuple[int, int]] = None  # where the position digits are in mpn
    # Contact lengths, applied only to groups with this orientation and smd.
    orientation_parameters: Tuple[Tuple[str, str], ...] = ()
    orientation: str = ""
    smd: bool = False

    def filters_for(self, signature: FootprintSignature) -> Dict[str, str]:
        """The parameter values this series adds to *signature*'s filters."""
        out = dict(self.parameters)
        if signature.orientation == self.orientation and signature.smd == self.smd:
            out.update(self.orientation_parameters)
        return out

    def predicted_mpn(self, count: int) -> Optional[str]:
        """The MPN of this series for *count* positions (per row), or
        ``None`` when the pattern is unknown or *count* does not fit."""
        if self.digits_span is None:
            return None
        start, end = self.digits_span
        width = end - start
        text = str(count).zfill(width)
        if len(text) > width:
            return None
        return self.mpn[:start] + text + self.mpn[end:]


def _position_digits(mpn: str, counts: Sequence[int]) -> Optional[Tuple[int, int]]:
    """The span of the position digits in *mpn*: in the first digit run, the
    longest suffix that reads as one of *counts* (``PRPC004SAAN-RC``, 4 →
    "004"; ``TSW-104-07-G-S``, 4 → "04"); failing that, in the first digit
    run, the longest shorter prefix that does (``PPTC041LFBN-RC``, 4 → "04",
    the trailing 1 being the row count)."""
    wanted = [c for c in counts if c is not None and c > 0]
    runs = list(_DIGITS.finditer(mpn))
    for m in runs:
        run = m.group(0)
        for count in wanted:
            best = None
            for k in range(len(str(count)), len(run) + 1):
                if int(run[-k:]) == count:
                    best = k
            if best is not None:
                return m.end() - best, m.end()
    for m in runs:
        run = m.group(0)
        for count in wanted:
            best = None
            for k in range(len(str(count)), len(run)):
                if int(run[:k]) == count:
                    best = k
            if best is not None:
                return m.start(), m.start() + best
    return None


def series_from_pick(product: DigiKeyProduct, signature: FootprintSignature) -> HeaderSeries:
    """The :class:`HeaderSeries` the user's pick *product* sets for groups
    like *signature*. The position digits are looked for as the count per row,
    then as the total."""
    def present(names):
        return tuple(
            (name, product.parameters[name])
            for name in names
            if product.parameters.get(name) not in (None, "", "-")
        )

    params = present(SERIES_PARAMETERS)
    mpn = product.mpn or ""
    span = _position_digits(mpn, [signature.per_row, signature.positions])
    pattern = None
    if span is not None:
        start, end = span
        pattern = re.escape(mpn[:start]) + r"\d{%d}" % (end - start) + re.escape(mpn[end:])
    return HeaderSeries(
        family=signature.family,
        manufacturer=product.manufacturer or "",
        parameters=params,
        mpn=mpn,
        pattern=pattern,
        digits_span=span,
        orientation_parameters=present(SAME_ORIENTATION_PARAMETERS),
        orientation=signature.orientation,
        smd=signature.smd,
    )


def header_filters(signature: FootprintSignature) -> Dict[str, object]:
    """The DigiKey filters for *signature* (parameter name → wanted value)."""
    filters: Dict[str, object] = {"Contact Type": _CONTACT[signature.family]}
    if signature.positions is not None:
        filters["Number of Positions"] = signature.positions
    if signature.rows is not None and signature.family not in _ROWS_AFTER_SEARCH:
        filters["Number of Rows"] = signature.rows
    # One pin has no pitch (DigiKey lists single-pin headers with "-").
    if signature.pitch_mm is not None and signature.positions != 1:
        filters["Pitch - Mating"] = signature.pitch_mm
    if signature.smd:
        filters["Mounting Type"] = "Surface Mount"
    elif signature.orientation == HORIZONTAL:
        filters["Mounting Type"] = "Through Hole, Right Angle"
    elif signature.orientation:
        filters["Mounting Type"] = "Through Hole"
    return filters


@dataclass
class HeaderSearchResult:
    """One header group's search. ``candidates`` are ranked (series MPN
    first when a series is used, then by price); ``proposed`` is the first."""

    signature: FootprintSignature
    candidates: List[DigiKeyProduct] = field(default_factory=list)
    total: int = 0
    series: Optional[HeaderSeries] = None  # the series searched, when one was
    notes: List[str] = field(default_factory=list)
    unmatched: Tuple[str, ...] = ()
    error: str = ""

    @property
    def proposed(self) -> Optional[DigiKeyProduct]:
        return self.candidates[0] if self.candidates else None


def stock_note(product: DigiKeyProduct, needed: int) -> str:
    """A warning when *product*'s stock is below build + 1000
    (:func:`~kicadtools.icsearch.stock_floor`); ``""`` otherwise. Parts below
    :func:`~kicadtools.icsearch.stock_minimum` are not returned by the
    search."""
    stock = product.quantity_available
    floor = stock_floor(needed)
    if stock is not None and stock < floor:
        return f"DigiKey stock {stock}, below {floor}"
    return ""


def rank_for_series(products: Sequence[DigiKeyProduct], series: HeaderSeries,
                    signature: FootprintSignature) -> List[DigiKeyProduct]:
    """*products* with the series' predicted MPN first, then other MPNs
    that fit the pattern, then the rest; DigiKey's order (price) within each."""
    predicted = {
        series.predicted_mpn(n)
        for n in (signature.per_row, signature.positions)
        if n is not None
    } - {None}
    pattern = re.compile(series.pattern) if series.pattern else None

    def tier(p: DigiKeyProduct) -> int:
        mpn = p.mpn or ""
        if mpn in predicted:
            return 0
        if pattern is not None and pattern.fullmatch(mpn):
            return 1
        return 2

    return sorted(products, key=tier)  # stable: keeps price order in a tier


class HeaderRun:
    """Header and socket searches for one run, holding each family's series.

    *client* is a :class:`DigiKeyClient`; *makers* the maker list for
    searches without a series; *needed* the build quantity of each group
    is passed per search."""

    def __init__(self, client: DigiKeyClient, *, makers: Sequence[str] = HEADER_MAKERS,
                 limit: int = 20):
        self.client = client
        self.makers = tuple(makers)
        self.limit = limit
        self.series: Dict[str, HeaderSeries] = {}

    def search(self, signature: FootprintSignature, needed: int) -> HeaderSearchResult:
        """Search one group of *needed* parts with footprint *signature*."""
        result = HeaderSearchResult(signature=signature)
        category = CATEGORIES.get(signature.family)
        if category is None:
            result.error = f"not a header or socket footprint ({signature.family})"
            return result
        filters = header_filters(signature)
        min_stock = stock_minimum(needed)
        series = self.series.get(signature.family)
        rows_after = signature.family in _ROWS_AFTER_SEARCH and signature.rows is not None
        limit = _ROWS_AFTER_SEARCH_LIMIT if rows_after else self.limit

        def keep(products):
            if not rows_after:
                return list(products)
            return [p for p in products if p.param("Number of Rows") == str(signature.rows)]

        if series is not None:
            series_filters = {**filters, **series.filters_for(signature)}
            found = self.client.category_search(
                KEYWORD, category, series_filters, manufacturers=[series.manufacturer],
                min_stock=min_stock, limit=limit,
            )
            if found.error:
                result.error = found.error
                return result
            products = keep(found.products)
            if products:
                result.series = series
                result.candidates = rank_for_series(products, series, signature)
                result.total = found.total
                self._note_stock(result, needed)
                return result
            why = "; ".join(found.unmatched) if found.unmatched else "no part in stock"
            result.notes.append(f"series {series.mpn} ({series.manufacturer}): {why}; searched the maker list")

        found = self.client.category_search(
            KEYWORD, category, filters, manufacturers=self.makers,
            min_stock=min_stock, limit=limit,
        )
        result.candidates = keep(found.products)
        result.total = found.total
        result.unmatched = found.unmatched
        result.error = found.error
        if not result.candidates and not result.error and not result.unmatched:
            result.notes.append(f"no {signature.family} in stock (at least {min_stock}) from the maker list")
        self._note_stock(result, needed)
        return result

    def pick(self, product: DigiKeyProduct, signature: FootprintSignature) -> HeaderSeries:
        """Record the user's pick for a group. The first pick of a family sets
        its series; later picks leave it. Returns the family's series."""
        family = signature.family
        if family not in self.series:
            self.series[family] = series_from_pick(product, signature)
        return self.series[family]

    @staticmethod
    def _note_stock(result: HeaderSearchResult, needed: int) -> None:
        if result.proposed is not None:
            note = stock_note(result.proposed, needed)
            if note:
                result.notes.append(note)
