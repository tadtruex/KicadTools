"""The Cockpit's prompt for the parts database password (``DB_PLAN.md``
decision 3): the *ask* that :func:`kicadtools.dbconfig.obtain_password`
calls when the password file is missing or the server refused it."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QInputDialog, QLineEdit, QWidget


def dialog_ask(parent: Optional[QWidget] = None) -> Callable[[str], Optional[str]]:
    """An *ask* that shows *reason* in a password dialog (characters
    hidden); ``None`` when the user cancels or enters nothing."""

    def ask(reason: str) -> Optional[str]:
        text, ok = QInputDialog.getText(parent, "Parts database password", reason,
                                        QLineEdit.Password)
        return text if ok and text else None

    return ask
