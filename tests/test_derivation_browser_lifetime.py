"""The wallet-creation derivation browser holds no seed after the wizard closes.

The creation path builds a second VaultWallet to populate the browser. Both it
and the wizard must be scrubbed once the wizard returns, so locking the wallet
leaves no plaintext phrase behind.
"""

import gc
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

pytest.importorskip("PyQt6")

# wallet_dialogs and tabs both live in ui/ now; import order is no longer delicate.
import primer_vault.ui.dialogs  # noqa: F401

from primer_vault.wallet.crypto import VaultWallet

TEST_PASSWORD = "pass-1-derivation-browser"

# Standard BIP-39 test vector. Public, never funded, never used on any chain.
TEST_SEED = ("abandon abandon abandon abandon abandon abandon "
             "abandon abandon abandon abandon abandon about")


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _drain(qapp):
    """Let Qt run the deferred deletions scrub_dialog schedules, the way a
    live event loop would, then collect the Python side."""
    from PyQt6.QtCore import QEvent, QCoreApplication
    for _ in range(3):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()


def test_creation_derivation_browser_does_not_strand_the_seed(qapp):
    from PyQt6.QtWidgets import QWidget
    from primer_vault.ui.wallet_dialogs import (
        CreateWalletWizard, DerivationBrowserDialog)

    wallet_tab = QWidget()  # stands in for WalletTab, which lives all session

    def run_creation_flow():
        # Exactly what CreateWalletWizard._show_derivation_dialog does, minus
        # the blocking exec().
        wizard = CreateWalletWizard(wallet_tab)
        wizard.password = TEST_PASSWORD
        wizard.seed_phrase = TEST_SEED
        wizard.derivation_path = "m/44'/60'/0'/0/{}"

        temp_wallet = VaultWallet.create(wizard.password)
        seed_id = temp_wallet.add_seed(wizard.seed_phrase, wizard.derivation_path)
        browser = DerivationBrowserDialog(
            temp_wallet, seed_id, wizard, creation_mode=True)
        assert browser.wallet.get_seed_phrase(seed_id) == TEST_SEED
        browser.accept()  # how the user leaves it; sends no closeEvent

        # ui/tabs.py - the wizard is scrubbed once its values are read.
        wizard.scrub()

    run_creation_flow()
    _drain(qapp)

    stranded = []
    for d in wallet_tab.findChildren(DerivationBrowserDialog):
        w = getattr(d, "wallet", None)
        if w is None:
            continue
        for sid in w.get_seed_ids():
            try:
                if w.get_seed_phrase(sid) == TEST_SEED:
                    stranded.append(sid)
            except ValueError:
                pass

    assert not stranded, (
        "the wallet-creation derivation browser still holds a second decrypted "
        f"VaultWallet after the wizard was scrubbed; the plaintext recovery "
        f"phrase is readable from {stranded}, and no lock() can reach it"
    )
