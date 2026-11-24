"""Where the Boards repository is on this machine.

Nothing assumes a fixed location (such as ``~/Code/Boards``): the root is
found from where this package is, so the checkout can be moved or renamed.

:func:`repo_root` is, in order:

1. ``$KICADTOOLS_REPO_ROOT`` when set;
2. the first folder above this package that looks like the Boards root
   (it holds both ``.gitmodules`` and a ``Libraries`` folder), which is the
   checkout KicadTools is installed from (an editable install);
3. the same search upward from the current directory;
4. ``None`` when none of these finds one (KicadTools installed on its own,
   run outside a checkout).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ENV_VAR = "KICADTOOLS_REPO_ROOT"


def is_repo_root(folder: Path) -> bool:
    """*folder* looks like the root of the Boards repository. ``Code/`` has a
    ``Libraries`` folder of its own, so ``.gitmodules`` is required too."""
    return (folder / ".gitmodules").is_file() and (folder / "Libraries").is_dir()


def find_repo_root(start: Path) -> Optional[Path]:
    """The first folder at or above *start* that :func:`is_repo_root`."""
    start = Path(start).resolve()
    for folder in [start, *start.parents]:
        if is_repo_root(folder):
            return folder
    return None


def repo_root() -> Optional[Path]:
    """The Boards repository root (see the module docstring), or ``None``."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return find_repo_root(Path(__file__).parent) or find_repo_root(Path.cwd())
