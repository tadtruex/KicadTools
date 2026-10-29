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
from typing import Callable, Dict, Iterable, Optional, Tuple


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
    esc = _escape(value)
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


def _read_quoted(text: str, i: int) -> int:
    """Return the index just past the closing ``"`` of the KiCad string literal
    that opens at *i* (``text[i]`` must be ``"``), honouring ``\\`` escapes."""
    if text[i] != '"':
        raise ValueError("expected a string literal")
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            return j + 1
        j += 1
    raise ValueError("unterminated string literal")


def _symbol_bounds(text: str, symbol_uuid: str) -> Tuple[int, int]:
    """Return ``(open, end)`` bracketing the placed ``(symbol …)`` block that
    contains ``(uuid "symbol_uuid")``. Raises ``KeyError`` if the uuid isn't
    found, ``ValueError`` if it isn't inside a placed symbol."""
    marker = f'(uuid "{symbol_uuid}")'
    u = text.find(marker)
    if u == -1:
        raise KeyError(f"symbol uuid {symbol_uuid!r} not found")

    # The enclosing placed symbol opens at the nearest preceding one-tab
    # `(symbol`; its close is the matching paren.
    sym_open = text.rfind("\n\t(symbol", 0, u)
    if sym_open == -1:
        raise ValueError(f"uuid {symbol_uuid!r} is not inside a placed symbol")
    sym_open = text.index("(", sym_open)
    return sym_open, _find_node_end(text, sym_open)


def _find_property(text: str, sym_open: int, sym_end: int, key: str) -> int:
    """Return the index of ``(property "key"`` within the symbol block, or -1."""
    return text.find(f'(property "{key}"', sym_open, sym_end)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def insert_property(text: str, symbol_uuid: str, key: str, value: str) -> str:
    """Return *text* with a new ``(property key value)`` added to the placed
    symbol identified by *symbol_uuid*.

    The node is inserted right after the symbol's ``Reference`` property (which
    always exists and is the first property after the uuid), at the same
    indentation. Raises ``KeyError`` if the uuid isn't found, or ``ValueError``
    if the symbol already has *key*.
    """
    u = text.find(f'(uuid "{symbol_uuid}")')
    sym_open, sym_end = _symbol_bounds(text, symbol_uuid)

    if _find_property(text, sym_open, sym_end, key) != -1:
        raise ValueError(f"symbol {symbol_uuid!r} already has a {key!r} property")

    ref = text.find('(property "Reference"', u, sym_end)
    if ref == -1:
        raise ValueError(f"no Reference property found for uuid {symbol_uuid!r}")
    ref_end = _find_node_end(text, ref)

    line_start = text.rfind("\n", 0, ref) + 1
    indent = text[line_start:ref]
    node = _format_property(key, value, indent)
    return text[:ref_end] + "\n" + node + text[ref_end:]


def set_property_value(text: str, symbol_uuid: str, key: str, value: str) -> str:
    """Return *text* with the existing *key* property's value replaced by *value*
    on the placed symbol *symbol_uuid*. Raises ``KeyError`` if the property is
    absent."""
    sym_open, sym_end = _symbol_bounds(text, symbol_uuid)
    p = _find_property(text, sym_open, sym_end, key)
    if p == -1:
        raise KeyError(f"symbol {symbol_uuid!r} has no {key!r} property")
    # The value literal is the first string after the key's own closing quote.
    val_open = text.index('"', p + len(f'(property "{key}"'))
    val_end = _read_quoted(text, val_open)
    return text[:val_open] + f'"{_escape(value)}"' + text[val_end:]


def set_or_insert_property(
    text: str, symbol_uuid: str, key: str, value: str
) -> str:
    """Return *text* with *key* set to *value* on the placed symbol
    *symbol_uuid*: update the value in place if the property already exists
    (including a present-but-empty one), otherwise insert a new one.

    This is the set-or-insert the LCSC write-back needs — a symbol may already
    have an empty ``LCSC`` property, which :func:`insert_property` would refuse.
    """
    sym_open, sym_end = _symbol_bounds(text, symbol_uuid)
    if _find_property(text, sym_open, sym_end, key) != -1:
        return set_property_value(text, symbol_uuid, key, value)
    return insert_property(text, symbol_uuid, key, value)


def rename_property_key(
    text: str, symbol_uuid: str, old_key: str, new_key: str
) -> str:
    """Return *text* with property *old_key* renamed to *new_key* (value and
    formatting untouched) on the placed symbol *symbol_uuid*. Raises ``KeyError``
    if *old_key* is absent, ``ValueError`` if *new_key* already exists."""
    sym_open, sym_end = _symbol_bounds(text, symbol_uuid)
    needle = f'(property "{old_key}"'
    p = text.find(needle, sym_open, sym_end)
    if p == -1:
        raise KeyError(f"symbol {symbol_uuid!r} has no {old_key!r} property")
    if _find_property(text, sym_open, sym_end, new_key) != -1:
        raise ValueError(
            f"symbol {symbol_uuid!r} already has a {new_key!r} property"
        )
    return text[:p] + f'(property "{_escape(new_key)}"' + text[p + len(needle):]


def remove_property(text: str, symbol_uuid: str, key: str) -> str:
    """Return *text* with the whole *key* property node (and its line) removed
    from the placed symbol *symbol_uuid*. Raises ``KeyError`` if absent."""
    sym_open, sym_end = _symbol_bounds(text, symbol_uuid)
    p = _find_property(text, sym_open, sym_end, key)
    if p == -1:
        raise KeyError(f"symbol {symbol_uuid!r} has no {key!r} property")
    node_end = _find_node_end(text, p)
    line_start = text.rfind("\n", 0, p) + 1
    end = node_end + 1 if node_end < len(text) and text[node_end] == "\n" else node_end
    return text[:line_start] + text[end:]


def apply_key_edits(text: str, symbol_uuid: str, edits: Iterable[object]) -> str:
    """Apply a sequence of :class:`kicadtools.partnum.PropEdit`-shaped edits
    (``op`` / ``key`` / ``old_key`` / ``value``) to one symbol, in order."""
    for e in edits:
        if e.op == "rename":
            text = rename_property_key(text, symbol_uuid, e.old_key, e.key)
        elif e.op == "set":
            text = set_property_value(text, symbol_uuid, e.key, e.value)
        elif e.op == "remove":
            text = remove_property(text, symbol_uuid, e.key)
        else:  # pragma: no cover - guards against a malformed edit
            raise ValueError(f"unknown edit op {e.op!r}")
    return text


def add_intent_types(text: str, uuid_to_type: Dict[str, str]) -> str:
    """Apply ``Intent:Type`` for each ``{uuid: intent_type}`` in one file's text."""
    for uuid, intent_type in uuid_to_type.items():
        text = insert_property(text, uuid, "Intent:Type", intent_type)
    return text


def add_properties(
    text: str, uuid_to_props: Dict[str, Dict[str, Optional[str]]]
) -> str:
    """Apply a ``{uuid: {key: value}}`` plan to one file's text, set-or-insert.

    Generic counterpart to :func:`add_intent_types`: each property is updated in
    place when the symbol already has it (even empty) and inserted otherwise,
    so mixed Intent:Type + LCSC/MPN write-backs share one surgical pass.

    A value of ``None`` removes the property: the whole node is deleted when
    the symbol has it, and the symbol is left unchanged when it does not.
    """
    for uuid, props in uuid_to_props.items():
        for key, value in props.items():
            if value is None:
                sym_open, sym_end = _symbol_bounds(text, uuid)
                if _find_property(text, sym_open, sym_end, key) != -1:
                    text = remove_property(text, uuid, key)
                continue
            text = set_or_insert_property(text, uuid, key, value)
    return text


def write_properties(
    path: Path,
    uuid_to_props: Dict[str, Dict[str, Optional[str]]],
    *,
    dest: Optional[Callable[[Path], Path]] = None,
) -> Path:
    """Read *path*, set-or-insert every ``{uuid: {key: value}}`` property
    (``None`` removes the property, see :func:`add_properties`), and write the
    result (in place by default; pass *dest* to redirect). Returns the path
    written."""
    path = Path(path)
    updated = add_properties(path.read_text(encoding="utf-8"), uuid_to_props)
    target = dest(path) if dest is not None else path
    target.write_text(updated, encoding="utf-8")
    return target


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
