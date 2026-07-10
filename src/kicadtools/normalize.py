"""
Normalize part-number property keys across a KiCad schematic hierarchy.

Boards in this repo spell the same part-number field a dozen ways (``DigikeyPN``,
``Digi-Key_PN``, ``Manufacturer_PN``, ``MANUFACTURER`` …). :mod:`kicadtools.partnum`
already *reads* through that mess; this module *rewrites* it, collapsing every
variant onto the one canonical spelling in :data:`kicadtools.partnum.CANONICAL_KEYS`
so the next read is clean and downstream tools can rely on the key.

Edits are surgical (see :mod:`kicadtools.writer`) — a value keeps its position,
formatting and hidden state; only its key is renamed (or a stray duplicate
dropped). The planning half (:func:`plan_normalization`) does no I/O, so the CLI
can preview it (``--dry-run``) before anything is written.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kicadtools.partnum import PropEdit, normalization_edits
from kicadtools.sexpr_parser import (
    extract_properties,
    iter_symbol_instances,
    iter_symbols_from_sexpr,
    load_schematic_tree,
    symbol_uuid,
)
from kicadtools.writer import apply_key_edits


@dataclass
class SymbolNormalization:
    """The key edits planned for one placed symbol, with its write-back provenance."""
    reference: str
    source_file: Path
    symbol_uuid: Optional[str]
    edits: List[PropEdit]


@dataclass
class FileChange:
    """Before/after text for one schematic file and the symbols it touches."""
    path: Path
    before: str
    after: str
    symbols: List[SymbolNormalization] = field(default_factory=list)

    @property
    def edit_count(self) -> int:
        return sum(len(s.edits) for s in self.symbols)


def plan_normalization(root_path: Path) -> List[SymbolNormalization]:
    """Walk the schematic at *root_path* and its subsheets, returning one
    :class:`SymbolNormalization` per symbol that needs key edits (pure analysis,
    no writes). Symbols already canonical — or without a locatable uuid — are
    skipped."""
    plan: List[SymbolNormalization] = []
    for sch_path, sexpr in load_schematic_tree(Path(root_path)):
        for symbol in iter_symbols_from_sexpr(sexpr):
            props = extract_properties(symbol)
            edits = normalization_edits(props)
            if not edits:
                continue
            uuid = symbol_uuid(symbol)
            if uuid is None:
                continue  # can't locate it for a surgical write; leave it be
            refs = [ref for ref, _ in iter_symbol_instances(symbol)]
            plan.append(
                SymbolNormalization(
                    reference=", ".join(refs) if refs else "?",
                    source_file=sch_path,
                    symbol_uuid=uuid,
                    edits=edits,
                )
            )
    return plan


def compute_file_changes(plan: List[SymbolNormalization]) -> List[FileChange]:
    """Group *plan* by source file and apply its edits in memory, returning a
    :class:`FileChange` (before + after text) per touched file. Reads each file
    once; writes nothing."""
    by_file: Dict[Path, List[SymbolNormalization]] = {}
    for sym in plan:
        by_file.setdefault(sym.source_file, []).append(sym)

    changes: List[FileChange] = []
    for path, syms in by_file.items():
        before = path.read_text(encoding="utf-8")
        after = before
        for sym in syms:
            after = apply_key_edits(after, sym.symbol_uuid, sym.edits)
        changes.append(FileChange(path, before, after, syms))
    return changes


def write_file_changes(
    changes: List[FileChange], *, dest: Optional[Callable[[Path], Path]] = None
) -> List[Path]:
    """Write each :class:`FileChange`'s *after* text. Writes in place by default;
    pass *dest* (a ``src -> dst`` mapper) to write elsewhere. Returns the paths
    written."""
    written: List[Path] = []
    for ch in changes:
        target = dest(ch.path) if dest is not None else ch.path
        target.write_text(ch.after, encoding="utf-8")
        written.append(target)
    return written
