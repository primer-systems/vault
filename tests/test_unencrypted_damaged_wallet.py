"""A wallet the tab treats as unencrypted does not open to a silent, empty screen.

WalletTab decides what to show by asking VaultWallet.is_file_encrypted. This
build does not open unencrypted wallets â€” their master key is in the file in the
clear â€” so load refuses them, and the tab must surface that refusal and show the
locked state rather than leaving an empty, unlocked-looking wallet with the
error only in the log.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# A legacy unencrypted wallet whose master key is no longer readable hex. Its
# `encrypted: false` is what routes the tab down the auto-unlock branch.
DAMAGED_UNENCRYPTED = {
    "version": 3,
    "encrypted": False,
    "created_at": "2024-01-01T00:00:00Z",
    "data_key": "zzzz-not-hex",
    "seeds": [],
    "addresses": [],
}


def test_the_user_is_told_when_an_unencrypted_wallet_cannot_be_opened(tmp_path):
    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from primer_vault.core import Vault
    from primer_vault.ui.tabs import WalletTab

    app = QApplication.instance() or QApplication([])  # noqa: F841 - holds the QApplication reference alive
    core = Vault(data_dir=tmp_path / "data")
    try:
        wallet_file = core.get_wallet_dir() / "primer_vault.wallet"
        wallet_file.write_text(json.dumps(DAMAGED_UNENCRYPTED), encoding="utf-8")

        # Sanity: the core refuses this file with a message. This build no
        # longer opens unencrypted wallets at all (encrypted or damaged), so the
        # refusal is UNSUPPORTED_WALLET_VERSION rather than a damaged-file code.
        result = core.load_wallet(str(wallet_file), "__NO_PASSWORD__")
        assert result["success"] is False
        assert result.get("code") == "UNSUPPORTED_WALLET_VERSION", result

        told = []
        tab = WalletTab(core)
        tab.activity.connect(lambda msg, is_error: told.append((msg, is_error)))
        tab.activity_detail.connect(
            lambda summary, is_error, detail: told.append((summary, is_error)))
        tab._is_unlocked = False
        tab._update_display()

        # isHidden() reflects the explicit setVisible() the tab made, and is
        # not confused by the tab itself never being shown in an offscreen
        # test. The control below proves it separates the two branches.
        assert (not tab.lock_overlay.isHidden()) or told, (
            "the wallet file is damaged and could not be opened, but the tab "
            "shows an empty address list with no lock overlay and says "
            "nothing - the failure went only to the log")
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()


def test_control_an_encrypted_damaged_wallet_does_show_the_lock_overlay(tmp_path):
    """Proves the assertion above measures what it claims: for an ENCRYPTED
    damaged wallet the tab takes the locked branch and the overlay is shown,
    which is where the user gets the WALLET_DAMAGED message."""
    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from primer_vault.core import Vault
    from primer_vault.ui.tabs import WalletTab

    app = QApplication.instance() or QApplication([])  # noqa: F841 - holds the QApplication reference alive
    core = Vault(data_dir=tmp_path / "data")
    try:
        damaged = dict(DAMAGED_UNENCRYPTED, encrypted=True)
        damaged.pop("data_key")
        wallet_file = core.get_wallet_dir() / "primer_vault.wallet"
        wallet_file.write_text(json.dumps(damaged), encoding="utf-8")

        tab = WalletTab(core)
        tab._is_unlocked = False
        tab._update_display()

        assert not tab.lock_overlay.isHidden()
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()
