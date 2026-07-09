"""The BOM Cockpit main window."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from kicadtools import writer
from kicadtools.components import bom_components, get_components
from kicadtools.gui.model import IntentTableModel


class MainWindow(QMainWindow):
    def __init__(self, schematic: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("BOM Cockpit")
        self.resize(960, 640)

        self._board: Optional[Path] = None
        self.model = IntentTableModel()

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)

        accept_checked = QPushButton("Accept Checked")
        accept_all = QPushButton("Accept All")
        self._write_btn = QPushButton("Write Accepted Intents…")
        accept_checked.clicked.connect(self._accept_checked)
        accept_all.clicked.connect(self._accept_all)
        self._write_btn.clicked.connect(self._write)

        buttons = QHBoxLayout()
        buttons.addWidget(accept_checked)
        buttons.addWidget(accept_all)
        buttons.addStretch(1)
        buttons.addWidget(self._write_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        self._build_menu()
        self.statusBar().showMessage("Open a schematic to begin.")

        if schematic:
            self._open_path(Path(schematic))

    # ---- menu --------------------------------------------------------
    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_act = file_menu.addAction("&Open Schematic…")
        open_act.triggered.connect(self._open_dialog)
        write_act = file_menu.addAction("&Write Accepted Intents…")
        write_act.triggered.connect(self._write)
        file_menu.addSeparator()
        quit_act = file_menu.addAction("&Quit")
        quit_act.triggered.connect(self.close)

    # ---- open --------------------------------------------------------
    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open KiCad schematic", "", "KiCad schematic (*.kicad_sch)"
        )
        if path:
            self._open_path(Path(path))

    def _open_path(self, path: Path) -> None:
        try:
            components = bom_components(get_components(path))
        except Exception as exc:  # noqa: BLE001 — surface any read error to the user
            QMessageBox.critical(self, "Open failed", f"Could not read {path.name}:\n{exc}")
            return
        self._board = path
        self.model.load(components)
        self.setWindowTitle(f"BOM Cockpit — {path.name}")
        self._refresh_status()

    # ---- accept ------------------------------------------------------
    def _accept_checked(self) -> None:
        self.model.accept_checked()
        self._refresh_status()

    def _accept_all(self) -> None:
        self.model.accept_all()
        self._refresh_status()

    # ---- write -------------------------------------------------------
    def _write(self) -> None:
        edits = self.model.accepted_edits()
        if not edits:
            QMessageBox.information(
                self, "Nothing to write", "Accept one or more guesses first."
            )
            return
        n = sum(len(m) for m in edits.values())
        files = "\n".join(f"  • {p.name}  ({len(m)} part{'s' * (len(m) != 1)})" for p, m in edits.items())
        noun = "property" if n == 1 else "properties"
        resp = QMessageBox.question(
            self,
            "Write Intent:Type properties",
            f"Write {n} accepted Intent:Type {noun} into these files, in place?\n\n"
            f"{files}\n\nThe change is a minimal insertion; git is your backstop.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        try:
            for src, uuid_to_type in edits.items():
                writer.write_back(src, uuid_to_type)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Write failed", str(exc))
            return
        if self._board:
            self._open_path(self._board)  # reload → accepted rows now read as ✅
        QMessageBox.information(self, "Done", f"Wrote {n} {noun}. Reloaded the board.")

    # ---- status bar --------------------------------------------------
    def _refresh_status(self) -> None:
        s = self.model.summary()
        self.statusBar().showMessage(
            f"{sum(s.values())} components — {s['typed']} typed, "
            f"{s['guessed']} guessed, {s['accepted']} accepted, "
            f"{s['unclassified']} unclassified"
        )
