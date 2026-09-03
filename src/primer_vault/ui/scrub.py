"""Dropping sensitive text out of a dialog once it has closed.

Lives in `ui/` rather than `utils.py` because it touches Qt. `utils.py` is
imported by every layer including the core, and the Terminal edition ships
without Qt installed at all - one Qt import in the universal dependency would
make that build unimportable.
"""


def scrub_dialog(dialog, attrs: tuple = ()) -> None:
    """Blank every text-bearing widget in a closed dialog, None the named
    attributes, and schedule the dialog for destruction.

    Qt keeps a parented dialog alive for as long as its parent lives, and the
    wallet dialogs are parented to widgets that live for the whole session -
    so a seed phrase, private key or password left in a widget or attribute
    survives close() and outlives lock(). Locking the wallet cannot reach
    these copies; the dialog has to drop them itself.

    Call it from the dialog's closeEvent (covers the user closing the window)
    AND from the caller after exec() returns and the needed values were read
    (accept()/reject() do not send a closeEvent). Safe to call twice.

    Python cannot zero immutable strings, so this drops references and clears
    widget buffers - the same limit lock() itself has.
    """
    from PyQt6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QTextEdit

    for w in dialog.findChildren(QLineEdit):
        w.clear()
    for w in dialog.findChildren(QTextEdit):
        w.clear()
    for w in dialog.findChildren(QPlainTextEdit):
        w.clear()
    for w in dialog.findChildren(QLabel):
        w.setText("")
    for name in attrs:
        if hasattr(dialog, name):
            setattr(dialog, name, None)
    dialog.deleteLater()
