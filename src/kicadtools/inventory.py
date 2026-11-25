"""Keyword search over the lab inventory (``INVENTORY_PLAN.md``, step 3).

The inventory is small (hundreds to a few thousand items), so :func:`search`
loads every item and location once and matches in Python rather than through
MariaDB ``FULLTEXT``, which ignores short words ("M6", "V3") and does no
substring match.

Matching, per query word (every word must match somewhere):

- a word matches a field when it is a substring of it, ignoring case
  ("nano" finds "Nano Every", "m6" finds "M6 x 5mm"); ``µ``/``μ`` read as
  ``u`` and ``Ω`` as ``ohm``;
- the same comparison with everything but letters and digits removed from
  both, so "stlink" finds "ST-LINK/V3" and "rmcf0603" finds "RMCF-0603";
- failing both, a word of 5 or more letters matches a word of the field
  within a small edit distance (1, or 2 from 8 letters): "arduiono" finds
  "Arduino". A typo match scores less than an exact one. Words with a digit
  in them (values, part numbers) are never typo-matched: one edit turns
  ``100nf`` into ``10nF``, a different part.

Fields searched: the item's name, tags, category, MPN, DigiKey PN, LCSC
number, manufacturer, description, the paths of the locations it has stock
in, and the descriptions of those locations and of every location they are
inside (a drawer is found by its cabinet's description; the top level, the
lab, is part of every path).

Ranking: each word scores the weight of the best field it matched (a name
match above a tag or category match, above a part number, above the
location, above the description), the item's score is the sum, and ties
put items with some on hand above items with none or zero, then sort by
name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rapidfuzz.distance import Levenshtein

from kicadtools.inventorydb import (SEP, InventoryDb, Item, Location, Stock, in_location,
                                    natural_key, norm_path, path_key)

# Field weights: a match in a heavier field ranks the item higher.
WEIGHTS: Dict[str, float] = {
    "name": 10.0,
    "tags": 6.0,
    "category": 6.0,
    "mpn": 5.0,
    "digikey_pn": 5.0,
    "lcsc": 5.0,
    "manufacturer": 4.0,
    "location": 3.0,
    "location_description": 2.0,
    "description": 1.0,
}
# A typo match counts this fraction of the field's weight.
FUZZY_FACTOR = 0.5
# Shortest query word tried for a typo match.
FUZZY_MIN_LEN = 5

_FOLD = str.maketrans({"µ": "u", "μ": "u", "Ω": "ohm", "Ω": "ohm"})
_NON_ALNUM = re.compile(r"[\W_]+")


def fold(text: str) -> str:
    """*text* for comparison: ``µ`` → ``u``, ``Ω`` → ``ohm``, case folded
    (in that order: case folding turns ``Ω`` into ``ω``)."""
    return text.translate(_FOLD).casefold()


def _squash(text: str) -> str:
    return _NON_ALNUM.sub("", text)


def query_words(query: str) -> List[str]:
    """The words of *query*, folded, in order, without repeats."""
    seen: List[str] = []
    for word in fold(query or "").split():
        if word not in seen:
            seen.append(word)
    return seen


@dataclass(frozen=True)
class _Field:
    folded: str
    squashed: str
    words: Tuple[str, ...]

    @classmethod
    def of(cls, text: str) -> "_Field":
        folded = fold(text)
        return cls(folded, _squash(folded),
                   tuple(w for w in _NON_ALNUM.split(folded) if w))


def _max_typos(word: str) -> int:
    if len(word) < FUZZY_MIN_LEN or not word.isalpha():
        return 0
    return 1 if len(word) < 8 else 2


def _match(word: str, field: _Field) -> float:
    """1.0 for an exact (substring) match, :data:`FUZZY_FACTOR` for a typo
    match, 0.0 for none."""
    if word in field.folded:
        return 1.0
    squashed = _squash(word)
    if squashed and squashed in field.squashed:
        return 1.0
    typos = _max_typos(word)
    if typos:
        for candidate in field.words:
            if candidate.isalpha() and abs(len(candidate) - len(word)) <= typos and \
                    Levenshtein.distance(word, candidate, score_cutoff=typos) <= typos:
                return FUZZY_FACTOR
    return 0.0


@dataclass(frozen=True)
class SearchHit:
    """One item found: the item, its stock rows the filters looked at (all
    of them without a location filter), its score, and the fields that
    matched, heaviest first."""
    item: Item
    rows: Tuple[Stock, ...]
    score: float
    matched: Tuple[str, ...]

    @property
    def on_hand(self) -> Optional[int]:
        """The known quantities of :attr:`rows`, summed; ``None`` when none
        is known."""
        known = [s.quantity for s in self.rows if s.quantity is not None]
        return sum(known) if known else None


def _chain(path: str, by_path: Dict[str, Location]) -> List[Location]:
    """The location at *path* and every location it is inside."""
    parts = path.split(SEP)
    return [by_path[p] for p in (SEP.join(parts[:d]) for d in range(len(parts), 0, -1))
            if p in by_path]


def _joined(values: Iterable[Optional[str]]) -> Optional[str]:
    seen: List[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return "\n".join(seen) or None


def _fields(item: Item, by_path: Dict[str, Location]) -> Dict[str, _Field]:
    texts = {
        "name": item.name,
        "tags": " ".join(item.tags),
        "category": item.category,
        "mpn": item.mpn,
        "digikey_pn": item.digikey_pn,
        "lcsc": item.lcsc,
        "manufacturer": item.manufacturer,
        "description": item.description,
        "location": "\n".join(item.locations),
        "location_description": _joined(
            loc.description for path in item.locations for loc in _chain(path, by_path)),
    }
    return {k: _Field.of(v) for k, v in texts.items() if v}


def _stocked(row: Stock) -> bool:
    return row.quantity is None or row.quantity > 0


def search_items(
    items: Sequence[Item],
    locations: Sequence[Location],
    query: str,
    *,
    top: Optional[str] = None,
    category: Optional[str] = None,
    location: Optional[str] = None,
    in_stock: bool = False,
) -> List[SearchHit]:
    """The items matching *query* (see the module docstring), best first.

    Filters, all ignoring case: *top* (a top-level location, the lab),
    *location* (that location and every location inside it): each keeps the
    items with a stock row there, and the hit's rows are those rows.
    *category*; and *in_stock*, which keeps items with a row (among those
    the location filters kept) whose quantity is above 0 or unknown.

    An empty *query* lists every item the filters let through, by name."""
    by_path = {loc.path: loc for loc in locations}
    words = query_words(query)
    want_top = norm_path(top) if top and top.strip() else None
    want_cat = fold(category.strip()) if category and category.strip() else None
    want_loc = norm_path(location) if location and location.strip() else None

    hits: List[SearchHit] = []
    for item in items:
        if want_cat is not None and (item.category is None or fold(item.category) != want_cat):
            continue
        rows = item.stock
        if want_top is not None:
            rows = tuple(r for r in rows if in_location(r.location_path, want_top))
        if want_loc is not None:
            rows = tuple(r for r in rows if in_location(r.location_path, want_loc))
        if (want_top is not None or want_loc is not None) and not rows:
            continue
        if in_stock and not any(_stocked(r) for r in rows):
            continue

        fields = _fields(item, by_path)
        score = 0.0
        matched: Dict[str, float] = {}
        for word in words:
            best, best_field = 0.0, None
            for name, field in fields.items():
                value = _match(word, field) * WEIGHTS[name]
                if value > best:
                    best, best_field = value, name
            if best_field is None:
                break
            score += best
            matched[best_field] = max(matched.get(best_field, 0.0), best)
        else:
            order = tuple(sorted(matched, key=lambda f: -WEIGHTS[f]))
            hits.append(SearchHit(item, rows, score, order))

    if not words:
        return sorted(hits, key=lambda h: natural_key(h.item.name))
    return sorted(hits, key=lambda h: (-h.score, 0 if h.on_hand else 1,
                                       natural_key(h.item.name),
                                       path_key(h.rows[0].location_path if h.rows else "")))


def search(inv: InventoryDb, query: str, **filters) -> List[SearchHit]:
    """:func:`search_items` over every item and location in *inv*, read once."""
    return search_items(inv.items(), inv.locations(), query, **filters)
