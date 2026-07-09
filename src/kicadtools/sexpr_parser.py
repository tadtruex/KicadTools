"""
Parse KiCad schematic (`.kicad_sch`) s-expressions.

Walks a schematic and all of its hierarchical subsheets, yielding the parsed
tree per file, and provides helpers to pull symbols, their properties, and
their per-instance reference designators out of a parsed tree.
"""
from pathlib import Path
from typing import Dict, Iterable, Tuple

from sexpdata import Symbol, loads

# ----------------------------------------------------------------------
# Helper predicates (private)
# ----------------------------------------------------------------------
def _is_property(expr) -> bool:
    return isinstance(expr, list) and expr and expr[0] == Symbol("property")


# Symbol attribute directives that carry a yes/no value.
_BOOL_DIRECTIVES = ("dnp", "in_bom", "on_board")


def _is_bom_directive(expr) -> bool:
    return (
        isinstance(expr, list)
        and expr
        and any(expr[0] == Symbol(name) for name in _BOOL_DIRECTIVES)
    )


def _parse_bool(value: str) -> bool:
    """Parse a KiCad ``yes``/``no`` token into a bool."""
    return str(value).lower() == "yes"


# ----------------------------------------------------------------------
# Core API
# ----------------------------------------------------------------------
def load_schematic_tree(root: Path) -> Iterable[Tuple[Path, list]]:
    """
    Walk a KiCad schematic and all its subsheets, yielding
    ``(path, parsed_sexpr)`` for each file exactly once.
    """
    visited = set()
    stack = [root.resolve()]

    while stack:
        path = stack.pop()
        if path in visited:
            continue
        visited.add(path)

        sexpr = loads(path.read_text(encoding="utf-8"))
        yield path, sexpr

        for sub in iter_sheet_files(sexpr, path.parent):
            stack.append(sub)


def iter_symbols_from_sexpr(sexpr) -> Iterable[list]:
    """Yield top-level ``(symbol …)`` nodes from a parsed schematic."""
    if not isinstance(sexpr, list) or not sexpr or sexpr[0] != Symbol("kicad_sch"):
        return
    for node in sexpr[1:]:
        if isinstance(node, list) and node and node[0] == Symbol("symbol"):
            yield node


def symbol_uuid(symbol_expr) -> str | None:
    """Return the ``(uuid "…")`` of a placed ``(symbol …)`` node, or ``None``.

    This is the stable per-instance identifier used to locate the symbol again
    when writing a property back into the schematic text.
    """
    for item in symbol_expr:
        if isinstance(item, list) and item and item[0] == Symbol("uuid"):
            value = item[1]
            return value if isinstance(value, str) else None
    return None


def iter_symbol_instances(symbol_expr) -> Iterable[Tuple[str, str]]:
    """Yield ``(reference, sheet_path)`` for every instance of *symbol_expr*."""
    for item in symbol_expr:
        if isinstance(item, list) and item and item[0] == Symbol("instances"):
            for project in item[1:]:
                for entry in project[1:]:
                    if entry and entry[0] == Symbol("path"):
                        path = entry[1]
                        ref = None
                        for sub in entry[2:]:
                            if sub and sub[0] == Symbol("reference"):
                                ref = sub[1]
                        if ref:
                            yield ref, path


def extract_properties(symbol_expr) -> Dict[str, str]:
    """
    Convert a symbol's ``(property "Key" "Value" …)`` lists into ``{key: value}``,
    also folding in the ``dnp``/``in_bom``/``on_board`` yes/no directives.
    """
    props: Dict[str, str] = {}
    for item in symbol_expr:
        if _is_property(item) and len(item) >= 3:
            key, value = item[1], item[2]
            if isinstance(key, str) and isinstance(value, str):
                props[key] = value
        if _is_bom_directive(item):
            name = str(item[0])
            key = next((d for d in _BOOL_DIRECTIVES if d in name.lower()), "")
            if key != "":
                props[key] = _parse_bool(item[1])

    return props


def iter_sheet_files(sexpr, base_dir: Path) -> Iterable[Path]:
    """Resolve paths to subsheet schematics referenced by ``(sheet ...)`` nodes."""
    for node in sexpr:
        if isinstance(node, list) and node and node[0] == Symbol("sheet"):
            for item in node:
                if (
                    isinstance(item, list)
                    and len(item) >= 3
                    and item[0] == Symbol("property")
                    and item[1] == "Sheetfile"
                ):
                    yield (base_dir / item[2]).resolve()
