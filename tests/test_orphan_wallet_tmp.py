"""A hard kill during a wallet save leaves no readable wallet behind.

VaultWallet.save writes through temporary files in the wallets directory. The
exception handler removes them if anything is raised, but a power loss or a
kill -9 raises nothing, so they must be swept on the next start: each orphan is
a complete, loadable wallet file under the password in force when it was
written.

That is the rule _discard_previous_version exists to enforce â€” after a password
change, no copy readable under the old password may survive.

The crash is simulated by killing a real child process with os._exit inside the
save, which is what a power loss does and what the exception handler cannot
catch.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = str(Path(__file__).parent.parent / "src")
sys.path.insert(0, SRC)

from primer_vault.wallet.crypto import VaultWallet

OLD_PASSWORD = "leaked-password-1234"
NEW_PASSWORD = "replacement-password-5678"
SEED = "test test test test test test test test test test test junk"


CRASHING_SAVE = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from primer_vault.wallet.crypto import VaultWallet

    path = sys.argv[2]
    w = VaultWallet.create(sys.argv[3])
    w.add_seed(sys.argv[4])
    w.save(path)          # first save: nothing to keep a copy of yet

    # Die the way a power loss does, at the first rename of the next save -
    # the one that puts <name>.previous in place. Nothing is raised, so the
    # cleanup handler in save() never runs.
    os.replace = lambda *a, **k: os._exit(9)
    w.save(path)
""")


@pytest.fixture
def crashed_wallet_dir(tmp_path):
    """A wallets directory as a power loss during a save leaves it."""
    wallet = tmp_path / "primer_vault.wallet"
    proc = subprocess.run(
        [sys.executable, "-c", CRASHING_SAVE, SRC, str(wallet),
         OLD_PASSWORD, SEED],
        capture_output=True, text=True)
    assert proc.returncode == 9, (
        f"the child was meant to die inside save(): {proc.stderr}")
    assert wallet.exists(), "the wallet from the first save should still be there"
    return tmp_path, wallet


def test_a_killed_save_leaves_no_loadable_copy_behind(crashed_wallet_dir):
    """Opening the wallet - Vault's startup moment - clears the orphans a
    crashed save left behind, so none survives as a loadable copy."""
    tmp_dir, wallet = crashed_wallet_dir

    # The crash really did leave loadable temp copies.
    before = [p for p in tmp_dir.iterdir()
              if p != wallet and p.name.endswith(".tmp")]
    assert before, "the crashed save left no temp files"

    # Open the wallet the way Vault does at startup. That sweeps this wallet's
    # stale temp files.
    VaultWallet.load(wallet, OLD_PASSWORD)

    leftovers = [p for p in tmp_dir.iterdir()
                 if p != wallet and p.name.endswith(".tmp")]
    loadable = []
    for path in leftovers:
        try:
            VaultWallet.load(path, OLD_PASSWORD)
            loadable.append(path.name)
        except Exception:
            pass

    assert loadable == [], (
        f"{len(loadable)} leftover file(s) open as complete wallets with the "
        f"wallet's password after the wallet was opened: {loadable}")


def test_changing_the_password_leaves_nothing_readable_under_the_old_one(
        crashed_wallet_dir):
    """The rule _discard_previous_version enforces, applied to the whole folder.

    The user's password leaked, so they change it. Afterwards nothing in the
    wallets folder may still open with the leaked password - that is the
    stated reason .previous is dropped on a re-key.
    """
    tmp_dir, wallet = crashed_wallet_dir

    live = VaultWallet.load(wallet, OLD_PASSWORD)
    live.change_password(NEW_PASSWORD)
    live.save(wallet)

    still_open = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        try:
            opened = VaultWallet.load(path, OLD_PASSWORD)
        except Exception:
            continue
        seeds = len(opened.seeds)
        still_open.append(f"{path.name} ({seeds} seed(s))")

    assert still_open == [], (
        "after the password change these files still open with the leaked "
        f"password: {still_open}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
