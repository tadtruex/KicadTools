"""DigiKey category filters: value names to filter IDs (``THT_PLAN.md`` step 4).

DigiKey's v4 keyword search narrows by IDs, not names. A search limited to a
leaf category (``FilterOptionsRequest.CategoryFilter``) returns that
category's options under ``FilterOptions``:

- ``ParametricFilters``: one entry per parameter, with ``ParameterId``,
  ``ParameterName`` and ``FilterValues`` of ``{ValueId, ValueName,
  ProductCount}``;
- ``Manufacturers``: ``{Id, Value, ProductCount}``.

Value IDs are only unique within a parameter: ``39665`` is "1" under both
Number of Positions and Number of Rows. Sizes are text with the mm figure in
parentheses (``0.100" (2.54mm)``).

This module turns those options into :class:`CategoryFilters`, matches the
caller's wanted values against them, and builds the request body. Pure: no
I/O; :meth:`kicadtools.suppliers.DigiKeyClient.category_search` makes the
calls.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

# A wanted value: a name (matched without case), a number (matched against
# the value's mm figure or plain number), a predicate on the value name, or a
# list of these (any of them).
ValueSpec = Union[str, float, int, Callable[[str], bool], Sequence["ValueSpec"]]

ACTIVE_STATUS = 0  # FilterOptions.Status id of "Active"

_MM = re.compile(r"\((-?\d+(?:\.\d+)?)\s*mm\)")
_PLAIN_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:mm)?\s*$")
_TOLERANCE_MM = 0.01


@dataclass(frozen=True)
class FilterValue:
    id: str
    name: str
    count: int = 0


@dataclass(frozen=True)
class Parameter:
    id: int
    name: str
    values: Tuple[FilterValue, ...]


@dataclass(frozen=True)
class CategoryFilters:
    """The filter options of one leaf category (for one keyword)."""

    category_id: int
    category_name: str
    parameters: Tuple[Parameter, ...]
    manufacturers: Tuple[FilterValue, ...]

    def parameter(self, key: Union[int, str]) -> Optional[Parameter]:
        """The parameter with id *key* (int) or name *key* (str, without
        case)."""
        for p in self.parameters:
            if isinstance(key, int) and p.id == key:
                return p
            if isinstance(key, str) and p.name.lower() == key.lower():
                return p
        return None

    def value_ids(self, key: Union[int, str], wanted: ValueSpec) -> List[str]:
        """IDs of the values of parameter *key* that match *wanted*; ``[]`` when
        the parameter or a matching value is missing."""
        p = self.parameter(key)
        if p is None:
            return []
        return [v.id for v in p.values if value_matches(v.name, wanted)]

    def manufacturer_ids(self, names: Iterable[str]) -> List[str]:
        """IDs of the manufacturers whose name starts with one of *names* as a
        whole word, without case or accents (``Wurth`` matches ``Würth
        Elektronik``, ``TE`` matches ``TE Connectivity AMP Connectors``)."""
        wanted = [_fold(n) for n in names if n and n.strip()]
        out = []
        for m in self.manufacturers:
            value = _fold(m.name)
            if any(value == w or re.match(re.escape(w) + r"\b", value) for w in wanted):
                out.append(m.id)
        return out


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip().lower()


def mm_figure(name: str) -> Optional[float]:
    """The size a DigiKey value name gives in mm: the figure in parentheses
    (``0.100" (2.54mm)`` → 2.54), else a plain number (``4`` → 4.0)."""
    m = _MM.search(name or "")
    if m:
        return float(m.group(1))
    m = _PLAIN_NUMBER.match(name or "")
    return float(m.group(1)) if m else None


def value_matches(name: str, wanted: ValueSpec) -> bool:
    """Whether value *name* matches *wanted* (see :data:`ValueSpec`)."""
    if callable(wanted):
        return bool(wanted(name))
    if isinstance(wanted, bool):
        return False
    if isinstance(wanted, (int, float)):
        figure = mm_figure(name)
        return figure is not None and abs(figure - float(wanted)) <= _TOLERANCE_MM
    if isinstance(wanted, str):
        return (name or "").strip().lower() == wanted.strip().lower()
    return any(value_matches(name, w) for w in wanted)


def parse_filter_options(data: Mapping, category_id: int) -> CategoryFilters:
    """:class:`CategoryFilters` from a v4 keyword search response *data*
    limited to *category_id*. Parameters whose ``Category`` names another
    category are skipped."""
    options = data.get("FilterOptions") or {}
    parameters: List[Parameter] = []
    category_name = ""
    for raw in options.get("ParametricFilters") or []:
        if not isinstance(raw, dict):
            continue
        category = raw.get("Category") or {}
        if category.get("Id") not in (None, category_id, str(category_id)):
            continue
        category_name = category_name or str(category.get("Value") or "")
        try:
            pid = int(raw.get("ParameterId"))
        except (TypeError, ValueError):
            continue
        values = tuple(
            FilterValue(str(v.get("ValueId")), str(v.get("ValueName") or ""), int(v.get("ProductCount") or 0))
            for v in raw.get("FilterValues") or []
            if isinstance(v, dict) and v.get("ValueId") is not None
        )
        parameters.append(Parameter(pid, str(raw.get("ParameterName") or ""), values))
    manufacturers = tuple(
        FilterValue(str(m.get("Id")), str(m.get("Value") or ""), int(m.get("ProductCount") or 0))
        for m in options.get("Manufacturers") or []
        if isinstance(m, dict) and m.get("Id") is not None
    )
    return CategoryFilters(int(category_id), category_name, tuple(parameters), manufacturers)


@dataclass(frozen=True)
class ResolvedFilters:
    """The wanted filters as IDs. ``unmatched`` names each wanted parameter or
    value with no ID in the options: a search without it would be broader
    than asked, so the client does not search."""

    parameters: Tuple[Tuple[int, Tuple[str, ...]], ...]
    manufacturers: Tuple[str, ...]
    unmatched: Tuple[str, ...]


def resolve_filters(
    options: CategoryFilters,
    wanted: Mapping[Union[int, str], ValueSpec],
    manufacturers: Sequence[str] = (),
) -> ResolvedFilters:
    """Map *wanted* (parameter name or id → :data:`ValueSpec`) and the
    *manufacturers* names to IDs from *options*."""
    params: List[Tuple[int, Tuple[str, ...]]] = []
    unmatched: List[str] = []
    for key, spec in wanted.items():
        p = options.parameter(key)
        if p is None:
            unmatched.append(f"parameter {key!r}")
            continue
        ids = tuple(v.id for v in p.values if value_matches(v.name, spec))
        if not ids:
            unmatched.append(f"{p.name} = {spec!r}")
            continue
        params.append((p.id, ids))
    maker_ids: Tuple[str, ...] = ()
    if manufacturers:
        maker_ids = tuple(options.manufacturer_ids(manufacturers))
        if not maker_ids:
            unmatched.append(f"manufacturers {list(manufacturers)!r}")
    return ResolvedFilters(tuple(params), maker_ids, tuple(unmatched))


def search_body(
    keyword: str,
    category_id: int,
    resolved: Optional[ResolvedFilters] = None,
    *,
    min_stock: int = 0,
    limit: int = 50,
    offset: int = 0,
    sort_by_price: bool = True,
) -> dict:
    """The JSON body of a v4 keyword search in *category_id*, active parts
    only, with *resolved* filters."""
    options: dict = {
        "CategoryFilter": [{"Id": str(category_id)}],
        "StatusFilter": [{"Id": ACTIVE_STATUS}],
    }
    if min_stock:
        options["MinimumQuantityAvailable"] = int(min_stock)
    if resolved is not None:
        if resolved.manufacturers:
            options["ManufacturerFilter"] = [{"Id": m} for m in resolved.manufacturers]
        if resolved.parameters:
            options["ParameterFilterRequest"] = {
                "CategoryFilter": {"Id": str(category_id)},
                "ParameterFilters": [
                    {"ParameterId": pid, "FilterValues": [{"Id": v} for v in ids]}
                    for pid, ids in resolved.parameters
                ],
            }
    body = {
        "Keywords": keyword,
        "Limit": max(1, min(int(limit), 50)),
        "Offset": max(0, int(offset)),
        "FilterOptionsRequest": options,
    }
    if sort_by_price:
        body["SortOptions"] = {"Field": "Price", "SortOrder": "Ascending"}
    return body
