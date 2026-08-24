"""Crash and corruption resilience."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.wallet import VaultWallet, CorruptedWalletFile


# A wallet file damaged so that it is no longer valid UTF-8. This is what a
# bad sector, a half-restored cloud-sync copy, or a torn write on cheap flash
# actually leaves behind.
NOT_UTF8 = b'{"version": 3, "encrypted": true, "wrapped_key": \xff\xfe\x00\x80}'


@pytest.fixture
def damaged_wallet(tmp_path):
    p = tmp_path / "primer_vault.wallet"
    p.write_bytes(NOT_UTF8)
    return p


class TestDamagedWalletIsReportedNotRaised:
    """load() names the fault and points at .previous. The check that runs
    BEFORE it does not, so the user never reaches that message."""

    def test_load_gives_the_user_a_named_fault(self, damaged_wallet):
        """Control - this is the behaviour the other tests should match."""
        with pytest.raises(CorruptedWalletFile):
            VaultWallet.load(damaged_wallet, "anything")

    def test_is_file_encrypted_survives_a_non_utf8_wallet_file(self, damaged_wallet):
        """crypto.py - guards JSONDecodeError and IOError, but a file that
        is not UTF-8 raises UnicodeDecodeError, which is neither."""
        assert VaultWallet.is_file_encrypted(damaged_wallet) is True

    def test_is_file_encrypted_survives_json_that_is_not_a_wallet_record(self, tmp_path):
        """Same site: valid JSON that is not an object reaches data.get()."""
        p = tmp_path / "primer_vault.wallet"
        p.write_text('"this is valid json but not a wallet"', encoding="utf-8")
        assert VaultWallet.is_file_encrypted(p) is True


class TestGuiStartsWithADamagedWallet:

    def test_wallet_tab_builds_when_the_default_wallet_is_damaged(self, tmp_path):
        """tabs.py -> 1198 -> 1175 runs during widget construction, so this
        is the whole GUI failing to open, not one dialog misbehaving."""
        pytest.importorskip("PyQt6")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from primer_vault.core import Vault
        from primer_vault.ui.tabs import WalletTab

        app = QApplication.instance() or QApplication([])  # noqa: F841 - holds the QApplication reference alive
        core = Vault(data_dir=tmp_path / "data")
        try:
            (core.get_wallet_dir() / "primer_vault.wallet").write_bytes(NOT_UTF8)
            WalletTab(core)  # must not raise
        finally:
            core.settings_manager.stop()
            core.release_instance_lock()


class TestOneWalletFileTwoInstances:

    @pytest.mark.xfail(strict=True, reason=(
        "Known, documented (docs/security.md): two separate installs, each with "
        "its own data directory, can both open the same wallet file placed "
        "outside both directories and overwrite each other's saves. Requires a "
        "deliberate unusual setup; the per-file lock fix is deferred because it "
        "conflicts with the atomic-save rename."))
    def test_a_second_instance_cannot_silently_erase_a_seed(self, tmp_path):
        """The instance lock is per data directory. Two Vault installs have two
        data directories, so both start - and both may hold the same wallet
        file, which each of them saves whole."""
        from primer_vault.core import Vault

        shared = tmp_path / "shared" / "main.wallet"
        first = Vault(data_dir=tmp_path / "install_a")
        second = Vault(data_dir=tmp_path / "install_b")
        try:
            assert first.create_wallet(str(shared), "password12345")["success"]
            assert second.load_wallet(str(shared), "password12345")["success"]

            made = first.create_seed(12)
            assert made["success"]
            new_seed = made["seed_id"]

            # The second instance does something ordinary and harmless.
            existing = second.get_wallet_seeds()[0]["id"]
            assert second.add_address_from_seed(existing, 5)["success"]

            on_disk = json.loads(shared.read_text(encoding="utf-8"))
            assert new_seed in [s["id"] for s in on_disk["seeds"]], (
                f"{new_seed} was erased from the wallet file by the other instance")
        finally:
            for core in (first, second):
                core.settings_manager.stop()
                core.release_instance_lock()
