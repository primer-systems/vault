"""
Locking the wallet must clear every decrypted secret - including copies held
by dialogs.

VaultWallet.lock() clears the core's own decrypted material, but it cannot see
Qt dialogs. Each dialog that handles a seed phrase, private key or password
scrubs its widgets and attributes on close; these tests exercise the unlock
dialog (which loads a second decrypted wallet of its own), the Export Keys
dialog, and the seed-import dialog through a close-then-lock sequence.
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

# primer_vault.wallet.dialogs and primer_vault.ui.tabs import each other, so
# whichever is imported first has to be the ui side or the cycle bites. Not a
# defect, just an import-order fact this test has to respect.
import primer_vault.ui.dialogs  # noqa: F401  (breaks the wallet/ui import cycle)

from primer_vault.wallet.crypto import VaultWallet

TEST_PASSWORD = "pass-1b-review-password"

# Standard BIP-39 test vector. Public, never funded, never used on any chain.
TEST_SEED = ("abandon abandon abandon abandon abandon abandon "
             "abandon abandon abandon abandon abandon about")


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_unlock_dialog_strands_a_second_unlocked_wallet(qapp, tmp_path):
    """VaultWalletUnlockDialog decrypts the wallet itself and keeps it.

    Production wiring: ui/tabs.py constructs it with the WalletTab as
    parent. on_unlock (wallet/dialogs.py) assigns the fully decrypted
    VaultWallet to self.wallet and calls accept() - it never clears it.

    That object is a SECOND VaultWallet, separate from the one Vault.lock_wallet
    holds (core/vault.py), so locking cannot reach it. Its
    _decrypted_seeds still hold the plaintext recovery phrase.
    """
    from PyQt6.QtWidgets import QWidget
    from primer_vault.wallet.dialogs import VaultWalletUnlockDialog

    wallet_path = tmp_path / "test.wallet"
    source = VaultWallet.create(TEST_PASSWORD)
    source.add_seed(TEST_SEED)
    source.save(wallet_path)

    wallet_tab = QWidget()  # stands in for WalletTab, which lives for the session

    # Whatever the core holds is the copy that lock() can reach.
    core_wallet = VaultWallet.load(wallet_path, TEST_PASSWORD)

    def open_and_close():
        dialog = VaultWalletUnlockDialog(wallet_path, wallet_tab)
        dialog.password_input.setText(TEST_PASSWORD)
        dialog.on_unlock()
        assert dialog.wallet is not None, "unlock did not succeed"
        dialog.close()

    open_and_close()

    # The user locks the wallet. This is the action that is supposed to drop
    # every decrypted secret.
    core_wallet.lock()
    gc.collect()

    stranded = []
    for d in wallet_tab.findChildren(VaultWalletUnlockDialog):
        w = getattr(d, "wallet", None)
        if w is None:
            continue
        for seed_id in w.get_seed_ids():
            try:
                if w.get_seed_phrase(seed_id) == TEST_SEED:
                    stranded.append(seed_id)
            except ValueError:
                pass

    assert not stranded, (
        "a closed unlock dialog still holds a fully decrypted VaultWallet; "
        f"the plaintext seed phrase is readable from {stranded} after lock()"
    )


def test_export_dialog_retains_seed_phrase_after_lock(qapp, monkeypatch):
    """Export Keys leaves the revealed seed phrase in a live QTextEdit.

    Production wiring: ui/main_window.py constructs ExportKeysDialog with
    the MainWindow as parent. _reveal_keys (ui/dialogs.py) writes the
    plaintext into self.output_text and nothing clears or destroys it.
    """
    from PyQt6.QtWidgets import QWidget, QTextEdit
    from primer_vault.ui import dialogs as dlg

    wallet = VaultWallet.create(TEST_PASSWORD)
    seed_id = wallet.add_seed(TEST_SEED)
    addr_id = wallet.add_address_from_seed(seed_id, 0, "Primary")
    entry = next(a for a in wallet.addresses if a.id == addr_id)

    main_window = QWidget()  # stands in for MainWindow

    monkeypatch.setattr(dlg.FramelessMessageBox, "question",
                        staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(dlg.FramelessMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dlg.FramelessMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))

    def reveal_and_close():
        dialog = dlg.ExportKeysDialog(
            wallets=[entry],
            get_wallet_fn=lambda _address: wallet,
            parent=main_window,
        )
        dialog.export_seed_checkbox.setChecked(True)
        dialog._reveal_keys()
        assert TEST_SEED in dialog.output_text.toPlainText(), "reveal did not run"
        dialog.close()

    reveal_and_close()

    wallet.lock()
    gc.collect()

    leaked = [w for w in main_window.findChildren(QTextEdit)
              if TEST_SEED in w.toPlainText()]
    assert not leaked, (
        "a closed Export Keys dialog still displays the plaintext seed phrase "
        "after the wallet was locked"
    )


def test_import_seed_dialog_retains_phrase_after_close(qapp):
    """The typed recovery phrase survives the import dialog closing.

    Production wiring: ui/tabs.py constructs ImportSeedToWalletDialog with
    the WalletTab as parent. on_import (wallet/dialogs.py) stores the
    phrase on self.seed_phrase, and the QTextEdit the user typed it into keeps
    its own copy. Neither is cleared.
    """
    from PyQt6.QtWidgets import QWidget
    from primer_vault.wallet.dialogs import ImportSeedToWalletDialog

    wallet_tab = QWidget()  # stands in for WalletTab

    def import_and_close():
        dialog = ImportSeedToWalletDialog(wallet_tab)
        dialog.seed_input.setPlainText(TEST_SEED)
        dialog.on_import()
        assert dialog.seed_phrase == TEST_SEED, "import did not accept the phrase"
        dialog.close()

    import_and_close()
    gc.collect()

    survivors = wallet_tab.findChildren(ImportSeedToWalletDialog)
    held = [d for d in survivors
            if d.seed_phrase == TEST_SEED
            or TEST_SEED in d.seed_input.toPlainText()]
    assert not held, (
        "a closed seed-import dialog still holds the plaintext recovery phrase"
    )
