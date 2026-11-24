"""Keyword search over the lab inventory (``INVENTORY_PLAN.md``, step 3).

The inventory is small (hundreds to a few thousand items), so :func:`search`
loads every item and bin once and matches in Python rather than through
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
number, manufacturer, description, and its bin's path, site (its own or
inherited), and the description and location of its bin and of every bin
that bin is inside (a drawer is found by its cabinet's location).

Ranking: each word scores the weight of the best field it matched (a name
match above a tag or category match, above a part number, above the bin,
above the description), the item's score is the sum, and ties put items with
a quantity above items with none or zero, then sort by name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rapidfuzz.distance import Levenshtein

from kicadtools.inventorydb import SEP, Bin, InventoryDb, Item, natural_key, norm_path

# Field weights: a match in a heavier field ranks the item higher.
WEIGHTS: Dict[str, float] = {
    "name": 10.0,
    "tags": 6.0,
    "category": 6.0,
    "mpn": 5.0,
    "digikey_pn": 5.0,
    "lcsc": 5.0,
    "manufacturer": 4.0,
    "bin": 3.0,
    "site": 2.0,
    "bin_description": 2.0,
    "bin_location": 2.0,
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
    """One item found: the item, its bin (``None`` when in no bin), the site
    it is at (the bin's own or inherited), its score, and the fields that
    matched, heaviest first."""
    item: Item
    bin: Optional[Bin]
    site: Optional[str]
    score: float
    matched: Tuple[str, ...]


def effective_sites(bins: Iterable[Bin]) -> Dict[str, Optional[str]]:
    """Each bin's site by path: its own, else its nearest ancestor's."""
    own = {b.path: b.site for b in bins}
    out: Dict[str, Optional[str]] = {}
    for path in own:
        parts = path.split(SEP)
        site = None
        for depth in range(len(parts), 0, -1):
            site = own.get(SEP.join(parts[:depth]))
            if site:
                break
        out[path] = site or None
    return out


def _ancestors(bin: Optional[Bin], by_path: Dict[str, Bin]) -> List[Bin]:
    """*bin* and every bin it is inside, innermost first."""
    if bin is None:
        return []
    parts = bin.path.split(SEP)
    return [by_path[p] for p in (SEP.join(parts[:d]) for d in range(len(parts), 0, -1))
            if p in by_path]


def _joined(values: Iterable[Optional[str]]) -> Optional[str]:
    return "\n".join(v for v in values if v) or None


def _fields(item: Item, chain: Sequence[Bin], site: Optional[str]) -> Dict[str, _Field]:
    texts = {
        "name": item.name,
        "tags": " ".join(item.tags),
        "category": item.category,
        "mpn": item.mpn,
        "digikey_pn": item.digikey_pn,
        "lcsc": item.lcsc,
        "manufacturer": item.manufacturer,
        "description": item.description,
        "bin": item.bin_path,
        "site": site,
        "bin_description": _joined(b.description for b in chain),
        "bin_location": _joined(b.location for b in chain),
    }
    return {k: _Field.of(v) for k, v in texts.items() if v}


def _in_bin(item: Item, path: str) -> bool:
    if item.bin_path is None:
        return False
    a, b = item.bin_path.casefold(), path.casefold()
    return a == b or a.startswith(b + SEP)


def search_items(
    items: Sequence[Item],
    bins: Sequence[Bin],
    query: str,
    *,
    site: Optional[str] = None,
    category: Optional[str] = None,
    bin: Optional[str] = None,
    in_stock: bool = False,
) -> List[SearchHit]:
    """The items matching *query* (see the module docstring), best first.

    Filters, all ignoring case: *site* (the item's bin's site, its own or
    inherited; an item in no bin has none), *category*, *bin* (that bin and
    every bin inside it), and *in_stock*, which leaves out items whose
    quantity is zero (an unknown quantity is kept).

    An empty *query* lists every item the filters let through, by bin and
    name."""
    by_id = {b.id: b for b in bins}
    by_path = {b.path: b for b in bins}
    sites = effective_sites(bins)
    words = query_words(query)
    want_site = fold(site.strip()) if site and site.strip() else None
    want_cat = fold(category.strip()) if category and category.strip() else None
    want_bin = norm_path(bin) if bin and bin.strip() else None

    hits: List[SearchHit] = []
    for item in items:
        item_bin = by_id.get(item.bin_id) if item.bin_id is not None else None
        item_site = sites.get(item_bin.path) if item_bin else None
        if want_site is not None and (item_site is None or fold(item_site) != want_site):
            continue
        if want_cat is not None and (item.category is None or fold(item.category) != want_cat):
            continue
        if want_bin is not None and not _in_bin(item, want_bin):
            continue
        if in_stock and item.quantity == 0:
            continue

        fields = _fields(item, _ancestors(item_bin, by_path), item_site)
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
            hits.append(SearchHit(item, item_bin, item_site, score, order))

    def key(hit: SearchHit):
        stocked = 0 if hit.item.quantity else 1
        return (-hit.score, stocked, natural_key(hit.item.name),
                [natural_key(n) for n in (hit.item.bin_path or "").split(SEP)])

    if not words:
        return sorted(hits, key=lambda h: (
            [natural_key(n) for n in (h.item.bin_path or "").split(SEP)],
            natural_key(h.item.name)))
    return sorted(hits, key=key)


def search(inv: InventoryDb, query: str, **filters) -> List[SearchHit]:
    """:func:`search_items` over every item and bin in *inv*, read once."""
    return search_items(inv.items(), inv.bins(), query, **filters)
