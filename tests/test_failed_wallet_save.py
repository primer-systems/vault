"""
A wallet change whose save failed must not be reported as a success.

A seed the user is told was created, but which never reached disk, is gone at
the next start - along with any funds sent to its addresses. A seed the user
is told was removed, but which is still in the file, comes back. Every
wallet-mutating method must surface a failed save; each test injects a
disk-full failure and asserts the honest answer.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.wallet import VaultWallet

PASSWORD = "password-123"

# A valid BIP-39 phrase, for the wallet that starts with a seed already in it.
PHRASE = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")

# A well-known test key (Hardhat account #0). Never used for real funds.
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _disk_full(monkeypatch):
    """Make every subsequent wallet save fail the way a full disk does."""
    def boom(self, filepath):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(VaultWallet, "save", boom)


@pytest.fixture
def core(tmp_path):
    c = Vault(data_dir=tmp_path)
    yield c
    c.release_instance_lock()


@pytest.fixture
def wallet_path(tmp_path):
    return str(tmp_path / "wallets" / "primer.wallet")


def test_new_seed_is_not_reported_as_created_when_the_save_failed(
        core, wallet_path, monkeypatch):
    """The GUI's "New seed" button: the phrase is shown once, then scrubbed."""
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    _disk_full(monkeypatch)

    result = core.add_seed(
        "legal winner thank year wave sausage worth useful legal "
        "winner thank yellow")

    assert result["success"] is False, (
        "a seed that never reached disk was reported as added")


def test_the_seed_the_user_was_told_about_is_on_disk(
        core, wallet_path, monkeypatch):
    """What the next start will actually find. This is the loss itself."""
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    _disk_full(monkeypatch)

    result = core.add_seed(
        "legal winner thank year wave sausage worth useful legal "
        "winner thank yellow")
    if not result.get("success"):
        pytest.skip("reported as failed, so the user was not misled")

    monkeypatch.undo()  # read the file as it stands
    on_disk = VaultWallet.load(wallet_path, PASSWORD)
    assert len(on_disk.seeds) == 2, (
        f"Vault reported seed {result.get('seed_id')} created; the wallet file "
        f"holds {len(on_disk.seeds)} seed(s). It is gone at the next start.")


def test_imported_key_is_not_reported_as_imported_when_the_save_failed(
        core, wallet_path, monkeypatch):
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    _disk_full(monkeypatch)

    result = core.add_imported_key(PRIVATE_KEY, "Imported")

    assert result["success"] is False, (
        "a private key that never reached disk was reported as imported")


def test_derived_address_is_not_reported_as_added_when_the_save_failed(
        core, wallet_path, monkeypatch):
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    seed_id = core.get_wallet_seeds()[0]["id"]
    _disk_full(monkeypatch)

    result = core.add_address_from_seed(seed_id, 7)

    assert result["success"] is False, (
        "an address that never reached disk was reported as added")


def test_removed_seed_is_not_reported_as_removed_when_the_save_failed(
        core, wallet_path, monkeypatch):
    """The dangerous direction: a key the user believes destroyed comes back."""
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    seed_id = core.get_wallet_seeds()[0]["id"]
    _disk_full(monkeypatch)

    result = core.remove_seed(seed_id)

    assert result["success"] is False, (
        "a seed still in the wallet file was reported as removed")


def test_removed_seed_really_is_gone_from_the_file(
        core, wallet_path, monkeypatch):
    """The consequence of the above: the file on disk still holds the seed."""
    core.create_wallet(wallet_path, PASSWORD, seed_phrase=PHRASE)
    seed_id = core.get_wallet_seeds()[0]["id"]
    _disk_full(monkeypatch)

    result = core.remove_seed(seed_id)
    if not result.get("success"):
        pytest.skip("reported as failed, so the user was not misled")

    monkeypatch.undo()
    on_disk = VaultWallet.load(wallet_path, PASSWORD)
    assert len(on_disk.seeds) == 0, (
        "Vault reported the seed removed; the wallet file still holds it, and "
        "the next start loads it again")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
