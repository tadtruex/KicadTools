"""``SSH_ASKPASS`` program for the parts-database tunnel
(:mod:`kicadtools.dbtunnel`): ssh runs it with its prompt as the one
argument (``tadtruex@tadtruex.host.dartmouth.edu's password:``) and reads
the answer from stdout. Shows a password dialog; exits 1 when cancelled,
which ssh takes as no answer.

Run as ``python -m kicadtools.gui.askpass PROMPT``, through the wrapper
:func:`kicadtools.dbtunnel.askpass_script` writes.
"""
from __future__ import annotations

import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    prompt = argv[0] if argv else "SSH password:"
    from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit

    app = QApplication.instance() or QApplication([])
    text, ok = QInputDialog.getText(None, "SSH login for the parts database",
                                    prompt.strip(), QLineEdit.Password)
    del app
    if not ok:
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
