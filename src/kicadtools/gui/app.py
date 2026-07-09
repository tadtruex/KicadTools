"""Entry point for the BOM Cockpit GUI (`kicadtools-cockpit`)."""
from __future__ import annotations

import sys
from typing import Optional


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from kicadtools.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    schematic: Optional[str] = sys.argv[1] if len(sys.argv) > 1 else None
    window = MainWindow(schematic)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
