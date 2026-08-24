"""
Dialogs that display or accept key material must not keep it after they close.

Qt keeps a parented dialog alive for as long as its parent, and these dialogs
are parented to widgets that live for the whole session - so anything left in
a widget buffer or attribute would survive close() and outlive lock(). Each
dialog therefore scrubs itself on close; these tests pin that for the Export
Keys dialog, the new-wallet wizard, and the unlock dialog.
"""

import gc
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

pytest.importorskip("PyQt6")

from primer_vault.wallet.crypto import VaultWallet

TEST_PASSWORD = "dialog-scrub-test-pw"
# Deterministic, well-known throwaway key. Never used on any chain.
TEST_PKEY = "4c0883a69102937d6231471b5dbb6204fe512961708279e5f5bd5f2b7e5c1e1f"


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_export_dialog_does_not_outlive_its_close(qapp, monkeypatch):
    """After Export Keys is closed, no widget should still hold the key."""
    from PyQt6.QtWidgets import QWidget, QTextEdit
    from primer_vault.ui import dialogs as dlg

    wallet = VaultWallet.create(TEST_PASSWORD)
    addr_id = wallet.add_imported_key(TEST_PKEY, "Test")
    entry = next(a for a in wallet.addresses if a.id == addr_id)

    # Stand in for MainWindow: the long-lived parent the real dialog is given.
    main_window = QWidget()

    # The reveal path asks for confirmation and reports errors in message
    # boxes; answer yes and keep the test headless.
    monkeypatch.setattr(dlg.FramelessMessageBox, "question",
                        staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(dlg.FramelessMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dlg.FramelessMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))

    def open_reveal_and_close():
        dialog = dlg.ExportKeysDialog(
            wallets=[entry],
            get_wallet_fn=lambda _address: wallet,
            parent=main_window,
        )
        dialog.export_pkey_checkbox.setChecked(True)
        dialog._reveal_keys()
        assert TEST_PKEY in dialog.output_text.toPlainText(), "reveal did not run"
        dialog.close()

    open_reveal_and_close()

    # The user then locks the wallet: every other decrypted copy is dropped.
    wallet.lock()
    gc.collect()

    leaked = [w.toPlainText() for w in main_window.findChildren(QTextEdit)
              if TEST_PKEY in w.toPlainText()]
    assert not leaked, (
        f"{len(leaked)} closed-dialog widget(s) still hold the plaintext "
        "private key after the wallet was locked"
    )


def test_create_wallet_wizard_does_not_outlive_its_close(qapp):
    """The new-wallet wizard is parented the same way (tabs.py).

    It keeps the chosen wallet password and the generated seed phrase as plain
    Python attributes, so if the closed wizard survives, so do they.
    """
    from PyQt6.QtWidgets import QWidget
    from primer_vault.wallet.dialogs import CreateWalletWizard

    wallet_tab = QWidget()  # stands in for WalletTab, which lives for the session
    password = "correct-horse-battery"

    def run_wizard_pages():
        wizard = CreateWalletWizard(wallet_tab)
        wizard.pw_password_input.setText(password)
        wizard.pw_confirm_input.setText(password)
        assert wizard._validate_password(), "password page did not accept"
        wizard._generate_seed()
        assert wizard._generated_seed, "seed was not generated"
        wizard.close()
        return wizard._generated_seed

    seed = run_wizard_pages()
    gc.collect()

    survivors = wallet_tab.findChildren(CreateWalletWizard)
    held = [w for w in survivors if w.password == password or w._generated_seed == seed]
    assert not held, (
        "a closed CreateWalletWizard still holds the wallet password and the "
        "generated seed phrase"
    )


def test_unlock_dialog_does_not_keep_the_password_after_success(qapp, tmp_path):
    """A successful unlock leaves the typed password in the dialog.

    wallet/dialogs.py clears the field only on a WRONG password; the
    success branch (1017-1018) calls accept() with the field untouched, and the
    dialog is a child of the wallet tab (ui/tabs.py), so it is never
    destroyed.
    """
    from PyQt6.QtWidgets import QWidget, QLineEdit
    from primer_vault.wallet.dialogs import VaultWalletUnlockDialog

    password = "unlock-me-please"
    wallet_path = tmp_path / "t.wallet"
    VaultWallet.create(password).save(wallet_path)

    wallet_tab = QWidget()  # stands in for WalletTab

    def unlock():
        dialog = VaultWalletUnlockDialog(str(wallet_path), wallet_tab)
        dialog.password_input.setText(password)
        dialog.on_unlock()
        assert dialog.wallet is not None, "unlock did not succeed"
        dialog.close()

    unlock()
    gc.collect()

    held = [f for f in wallet_tab.findChildren(QLineEdit) if f.text() == password]
    assert not held, "the wallet password is still readable in a closed dialog"


def test_change_password_dialog_does_not_keep_the_passwords(qapp, tmp_path):
    """All three password fields are cleared when the dialog closes, and the
    wallet it was pointed at stays usable - it is the core's own wallet, not a
    private copy."""
    from PyQt6.QtWidgets import QWidget, QLineEdit
    from primer_vault.wallet.dialogs import ChangePasswordDialog

    wallet = VaultWallet.create(TEST_PASSWORD)
    wallet_path = tmp_path / "t.wallet"
    wallet.save(wallet_path)

    parent = QWidget()
    dialog = ChangePasswordDialog(wallet=wallet, wallet_path=wallet_path,
                                  parent=parent)
    dialog.current_input.setText(TEST_PASSWORD)
    dialog.new_input.setText("a-brand-new-passphrase")
    dialog.confirm_input.setText("a-brand-new-passphrase")
    dialog.close()

    held = [f for f in parent.findChildren(QLineEdit) if f.text()]
    assert not held, "a closed change-password dialog still holds a password"
    assert wallet.verify_password(TEST_PASSWORD), (
        "scrubbing the dialog must not touch the shared wallet object")
