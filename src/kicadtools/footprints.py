"""Pad count of a footprint, read from the footprint itself.

The IC footprint check (``IC_PLAN.md`` steps 4 and 6) compares the number of
pads on the footprint with the pin count DigiKey gives for the part. Many IC
footprints are custom (``TexasInstruments:RHH0036C``,
``ECG_Footprints:CP_20_10_ADI_NOTH``), so the package cannot be read from the
name; the pad count still can.

Pad count = distinct non-empty pad numbers. An exposed pad split into several
copper pads that share one number counts once; unnumbered paste apertures and
mechanical holes do not count.

Sources, in order:

1. the board's ``.kicad_pcb``: the footprint placed under the reference, when
   it is the same library footprint the schematic names;
2. the footprint library: the board's ``fp-lib-table`` (``$KIPRJMOD`` and
   ``${KIPRJMOD}`` expanded), else ``<standard>/<lib>.pretty`` for KiCad's own
   libraries (``Libraries/kicad-footprints`` in this repo). The file read is
   ``<name>.kicad_mod``.

No source found → ``None`` (pad count unknown). Nothing here writes a file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional, Tuple, TypeVar

from sexpdata import Symbol, loads

from kicadtools import repo
from kicadtools.pcb import (
    Pad,
    Placement,
    _direct_child,
    _footprint_mounting,
    _parse_pads,
    parse_footprint_placements,
)

# This repo's checkout of KiCad's standard footprint libraries, relative to the
# repo root. A board outside the repo (a scratch copy) falls back to the repo
# KicadTools runs from (:func:`kicadtools.repo.repo_root`).
STANDARD_LIBS_RELATIVE = Path("Libraries") / "kicad-footprints"

# ``${KICAD9_FOOTPRINT_DIR}`` and friends name KiCad's standard library folder.
_STANDARD_DIR_VAR = re.compile(r"^KICAD\d*_FOOTPRINT_DIR$")
# ``$NAME`` or ``${NAME}`` in an fp-lib-table uri.
_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)")

_T = TypeVar("_T")


def _parse_file(path: Path, parse: Callable[[str], _T]) -> Optional[_T]:
    """*parse* applied to the text of *path*, or ``None`` when the file cannot
    be read or parsed. A damaged file counts as a missing source. The catch on
    *parse* is broad because sexpdata reports malformed input with
    AssertionError (empty text), AttributeError (unterminated string),
    IndexError and its own Expect* errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return parse(text)
    except Exception:
        return None


def pad_count(pads: Iterable[Pad]) -> int:
    """Number of distinct non-empty pad numbers in *pads*."""
    return len({p.number for p in pads if p.number})


# --- .kicad_mod -------------------------------------------------------------


@dataclass(frozen=True)
class LibraryFootprint:
    """A footprint read from its library ``.kicad_mod`` file."""

    attr: str  # the (attr …) verdict: SMD, THT or UNKNOWN (no attr node)
    pads: Tuple[Pad, ...]


def parse_kicad_mod(text: str) -> Optional[LibraryFootprint]:
    """The ``attr`` and pads of a ``.kicad_mod`` file from its *text*. Pure — no
    I/O. The root node is ``(footprint …)`` (KiCad 6+) or ``(module …)``
    (KiCad 5); anything else gives ``None``."""
    tree = loads(text)
    if not (isinstance(tree, list) and tree and tree[0] in (Symbol("footprint"), Symbol("module"))):
        return None
    return LibraryFootprint(attr=_footprint_mounting(tree), pads=_parse_pads(tree))


def parse_kicad_mod_pads(text: str) -> Tuple[Pad, ...]:
    """The pads of a ``.kicad_mod`` file from its *text*; no pads when the root
    node is not a footprint. Pure — no I/O."""
    footprint = parse_kicad_mod(text)
    return footprint.pads if footprint is not None else ()


# --- fp-lib-table -----------------------------------------------------------


def expand_uri(uri: str, *, project_dir: Optional[Path], standard_dir: Optional[Path]) -> Optional[Path]:
    """*uri* from an fp-lib-table as a path, or ``None`` when it names a
    variable that cannot be expanded. ``KIPRJMOD`` is *project_dir*;
    ``KICADn_FOOTPRINT_DIR`` is *standard_dir*; any other variable is read
    from the environment. A relative result is taken relative to
    *project_dir*."""
    unresolved = False

    def sub(match: "re.Match[str]") -> str:
        nonlocal unresolved
        name = match.group(1) or match.group(2)
        if name == "KIPRJMOD" and project_dir is not None:
            return str(project_dir)
        if _STANDARD_DIR_VAR.match(name) and standard_dir is not None:
            return str(standard_dir)
        value = os.environ.get(name)
        if value is None:
            unresolved = True
            return ""
        return value

    expanded = _VAR.sub(sub, uri)
    if unresolved or not expanded:
        return None
    path = Path(expanded).expanduser()
    if not path.is_absolute() and project_dir is not None:
        path = project_dir / path
    return path


def parse_fp_lib_table(
    text: str, *, project_dir: Optional[Path] = None, standard_dir: Optional[Path] = None
) -> Dict[str, Path]:
    """Map each library nickname in fp-lib-table *text* to its folder. Pure —
    no I/O. Libraries whose uri cannot be expanded are left out."""
    tree = loads(text)
    libs: Dict[str, Path] = {}
    if not isinstance(tree, list):
        return libs
    for node in tree:
        if not (isinstance(node, list) and node and node[0] == Symbol("lib")):
            continue
        name = _direct_child(node, "name")
        uri = _direct_child(node, "uri")
        if not name or len(name) < 2 or not uri or len(uri) < 2:
            continue
        path = expand_uri(str(uri[1]), project_dir=project_dir, standard_dir=standard_dir)
        if path is not None:
            libs[str(name[1])] = path
    return libs


def default_standard_dir(start: Optional[Path] = None) -> Optional[Path]:
    """KiCad's standard footprint libraries: ``Libraries/kicad-footprints`` in
    the first folder above *start* that has one, else in the repo KicadTools
    runs from (:func:`kicadtools.repo.repo_root`); ``None`` when neither has
    one."""
    if start is not None:
        for folder in [Path(start), *Path(start).parents]:
            candidate = folder / STANDARD_LIBS_RELATIVE
            if candidate.is_dir():
                return candidate
    root = repo.repo_root()
    if root is not None and (root / STANDARD_LIBS_RELATIVE).is_dir():
        return root / STANDARD_LIBS_RELATIVE
    return None


def split_lib_id(lib_id: str) -> Optional[Tuple[str, str]]:
    """``"Lib:Name"`` → ``("Lib", "Name")``; ``None`` without a nickname."""
    lib, sep, name = (lib_id or "").partition(":")
    if not sep or not lib or not name:
        return None
    return lib, name


# --- lookup -----------------------------------------------------------------


class FootprintPads:
    """Pad counts for one board. Reads the ``.kicad_pcb`` and ``fp-lib-table``
    once and caches each library footprint it reads.

    *pcb_path* is the board file (may be missing or ``None``); *project_dir* is
    the folder holding the ``fp-lib-table`` and the value of ``$KIPRJMOD``
    (default: the board's folder); *standard_dir* is KiCad's standard library
    folder (default: :func:`default_standard_dir` from the project folder)."""

    def __init__(
        self,
        pcb_path: Optional[Path] = None,
        *,
        project_dir: Optional[Path] = None,
        standard_dir: Optional[Path] = None,
    ):
        self.pcb_path = Path(pcb_path) if pcb_path is not None else None
        if project_dir is None and self.pcb_path is not None:
            project_dir = self.pcb_path.parent
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self.standard_dir: Optional[Path] = (
            Path(standard_dir) if standard_dir is not None else default_standard_dir(self.project_dir)
        )
        self._placements = None
        self._libs: Optional[Dict[str, Path]] = None
        self._library: Dict[str, Optional[LibraryFootprint]] = {}

    def _board_placements(self):
        if self._placements is None:
            self._placements = {}
            if self.pcb_path is not None and self.pcb_path.is_file():
                self._placements = _parse_file(self.pcb_path, parse_footprint_placements) or {}
        return self._placements

    def _lib_table(self) -> Dict[str, Path]:
        if self._libs is None:
            self._libs = {}
            if self.project_dir is not None:
                table = self.project_dir / "fp-lib-table"
                if table.is_file():
                    self._libs = _parse_file(
                        table,
                        lambda text: parse_fp_lib_table(
                            text, project_dir=self.project_dir, standard_dir=self.standard_dir
                        ),
                    ) or {}
        return self._libs

    def footprint_file(self, lib_id: str) -> Optional[Path]:
        """The ``.kicad_mod`` file for *lib_id*, or ``None`` when it is not in
        the board's fp-lib-table nor the standard libraries."""
        parts = split_lib_id(lib_id)
        if parts is None:
            return None
        lib, name = parts
        folder = self._lib_table().get(lib)
        if folder is None:
            if self.standard_dir is None:
                return None
            folder = self.standard_dir / f"{lib}.pretty"
        path = folder / f"{name}.kicad_mod"
        return path if path.is_file() else None

    def library_footprint(self, lib_id: str) -> Optional[LibraryFootprint]:
        """*lib_id* read from its library file, or ``None`` when the file is
        not found or cannot be parsed. Cached per library id."""
        if lib_id not in self._library:
            path = self.footprint_file(lib_id)
            self._library[lib_id] = _parse_file(path, parse_kicad_mod) if path is not None else None
        return self._library[lib_id]

    def library_count(self, lib_id: str) -> Optional[int]:
        """Pad count of *lib_id* read from its library file, or ``None``."""
        footprint = self.library_footprint(lib_id)
        return pad_count(footprint.pads) if footprint is not None else None

    def placed(self, reference: str, lib_id: Optional[str] = None) -> Optional[Placement]:
        """The board's footprint placed under *reference*, when it is the same
        library footprint as *lib_id* (the schematic's Footprint field) or
        *lib_id* is not given; else ``None``. A board copy that names another
        footprint is stale (the board has not been updated from the
        schematic), so it is not returned."""
        placed = self._board_placements().get(reference)
        if placed is not None and (not lib_id or placed.footprint == lib_id):
            return placed
        return None

    def count(self, reference: str, lib_id: Optional[str] = None) -> Optional[int]:
        """Pad count for *reference* with footprint *lib_id*: from the board
        copy (:meth:`placed`) when there is one, else from the library file for
        *lib_id*. ``None`` when neither is found."""
        placed = self.placed(reference, lib_id)
        if placed is not None:
            return pad_count(placed.pads)
        if lib_id:
            return self.library_count(lib_id)
        return None


def footprint_pad_count(
    reference: str,
    lib_id: Optional[str],
    *,
    pcb_path: Optional[Path] = None,
    project_dir: Optional[Path] = None,
    standard_dir: Optional[Path] = None,
) -> Optional[int]:
    """One-off :meth:`FootprintPads.count`. Build a :class:`FootprintPads`
    instead when counting many references on one board."""
    return FootprintPads(pcb_path, project_dir=project_dir, standard_dir=standard_dir).count(
        reference, lib_id
    )
