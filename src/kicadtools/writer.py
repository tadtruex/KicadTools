"""
Write ``Intent:Type`` properties back into a KiCad schematic.

The parser reads via :mod:`sexpdata`, but re-serialising the whole tree with
``sexpdata.dumps`` collapses the file onto a single line — a valid s-expression
KiCad can still open, but a catastrophic git diff. So instead of round-tripping,
this module performs a **surgical text insertion**: it locates a placed symbol by
its unique ``uuid`` and inserts one new ``(property …)`` node, leaving every other
byte of the file untouched. The result differs from the original only by the
added lines.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional


def _find_node_end(text: str, open_paren: int) -> int:
    """Return the index just past the ``)`` that closes the ``(`` at *open_paren*.

    Respects KiCad string literals so parens inside a quoted value (or escaped
    quotes) don't throw off the depth count.
    """
    if text[open_paren] != "(":
        raise ValueError("open_paren must point at '('")
    depth = 0
    in_str = False
    escaped = False
    i = open_paren
    while i < len(text):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def _format_property(key: str, value: str, indent: str) -> str:
    """Render a KiCad ``(property …)`` node, hidden, matching *indent* (the tab
    prefix of its sibling properties)."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    t = indent
    return (
        f'{t}(property "{key}" "{esc}"\n'
        f"{t}\t(at 0 0 0)\n"
        f"{t}\t(effects\n"
        f"{t}\t\t(font\n"
        f"{t}\t\t\t(size 1.27 1.27)\n"
        f"{t}\t\t)\n"
        f"{t}\t\t(hide yes)\n"
        f"{t}\t)\n"
        f"{t})"
    )


def insert_property(text: str, symbol_uuid: str, key: str, value: str) -> str:
    """Return *text* with a new ``(property key value)`` added to the placed
    symbol identified by *symbol_uuid*.

    The node is inserted right after the symbol's ``Reference`` property (which
    always exists and is the first property after the uuid), at the same
    indentation. Raises ``KeyError`` if the uuid isn't found, or ``ValueError``
    if the symbol already carries *key*.
    """
    marker = f'(uuid "{symbol_uuid}")'
    u = text.find(marker)
    if u == -1:
        raise KeyError(f"symbol uuid {symbol_uuid!r} not found")

    # Bound the enclosing placed-symbol block: opener is the nearest preceding
    # one-tab `(symbol`; close is its matching paren.
    sym_open = text.rfind("\n\t(symbol", 0, u)
    if sym_open == -1:
        raise ValueError(f"uuid {symbol_uuid!r} is not inside a placed symbol")
    sym_open = text.index("(", sym_open)
    sym_end = _find_node_end(text, sym_open)

    if text.find(f'(property "{key}"', sym_open, sym_end) != -1:
        raise ValueError(f"symbol {symbol_uuid!r} already has a {key!r} property")

    ref = text.find('(property "Reference"', u, sym_end)
    if ref == -1:
        raise ValueError(f"no Reference property found for uuid {symbol_uuid!r}")
    ref_end = _find_node_end(text, ref)

    line_start = text.rfind("\n", 0, ref) + 1
    indent = text[line_start:ref]
    node = _format_property(key, value, indent)
    return text[:ref_end] + "\n" + node + text[ref_end:]


def add_intent_types(text: str, uuid_to_type: Dict[str, str]) -> str:
    """Apply ``Intent:Type`` for each ``{uuid: intent_type}`` in one file's text."""
    for uuid, intent_type in uuid_to_type.items():
        text = insert_property(text, uuid, "Intent:Type", intent_type)
    return text


def write_back(
    path: Path,
    uuid_to_type: Dict[str, str],
    *,
    dest: Optional[Callable[[Path], Path]] = None,
) -> Path:
    """Read *path*, add the accepted ``Intent:Type`` properties, and write the
    result. Writes in place by default; pass *dest* (a ``src -> dst`` mapper) to
    write elsewhere. Returns the path written.
    """
    path = Path(path)
    updated = add_intent_types(path.read_text(encoding="utf-8"), uuid_to_type)
    target = dest(path) if dest is not None else path
    target.write_text(updated, encoding="utf-8")
    return target


def sibling_dest(suffix: str = ".intents") -> Callable[[Path], Path]:
    """A *dest* mapper that writes ``<name><suffix><ext>`` beside the source,
    e.g. ``Board.kicad_sch`` -> ``Board.intents.kicad_sch``."""
    return lambda p: p.with_name(f"{p.stem}{suffix}{p.suffix}")
