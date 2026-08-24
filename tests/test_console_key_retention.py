"""Key material shown in the GUI Console does not outlive the wallet lock.

Qt keeps a parented dialog alive for as long as its parent, so anything left
in a widget buffer survives close(). Locking the wallet must clear the
Console's output pane.
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

# Deterministic throwaway key. Never used on any chain.
TEST_PKEY = "4c0883a69102937d6231471b5dbb6204fe512961708279e5f5bd5f2b7e5c1e1f"
TEST_PASSWORD = "console-retention-pw"


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def core(tmp_path):
    from primer_vault.core import Vault

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    wallet_path = str(data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, TEST_PASSWORD)
    core.load_wallet(wallet_path, TEST_PASSWORD)
    return core


def test_console_does_not_keep_the_exported_key_after_close(qapp, core):
    """After the Console is closed and the wallet locked, no widget under the
    long-lived parent should still hold the exported private key."""
    from PyQt6.QtWidgets import QWidget, QTextEdit
    from primer_vault.ui.console import ConsoleWindow

    addr_id = core.add_imported_key(TEST_PKEY, "Test")["address_id"]

    # Stand in for MainWindow: the long-lived parent the real Console is given
    # (main_window.py passes `self`).
    main_window = QWidget()

    console = ConsoleWindow(core, main_window)
    console._handle_input(f"address export {addr_id}")
    console._handle_input("YES")

    # Premise: the export really did put the raw key in the output pane.
    assert TEST_PKEY in console.output.toPlainText(), (
        "the console export path did not print the key")

    console.close()
    core.lock_wallet()
    del console
    gc.collect()

    leaked = [w for w in main_window.findChildren(QTextEdit)
              if TEST_PKEY in w.toPlainText()]
    assert not leaked, (
        f"{len(leaked)} widget(s) still hold the private key after the console "
        "was closed and the wallet locked")


def test_locking_the_wallet_clears_the_key_from_the_open_console(qapp, core):
    """Auto-lock exists for the walk-away case. If the wallet locks itself while
    the Console is still open, the exported key must not stay on screen."""
    from PyQt6.QtWidgets import QWidget
    from primer_vault.ui.console import ConsoleWindow

    addr_id = core.add_imported_key(TEST_PKEY, "Test")["address_id"]

    main_window = QWidget()
    console = ConsoleWindow(core, main_window)
    console._handle_input(f"address export {addr_id}")
    console._handle_input("YES")

    assert TEST_PKEY in console.output.toPlainText(), (
        "the console export path did not print the key")

    # What WalletTab._on_auto_lock_timeout does after the inactivity timeout.
    core.lock_wallet()

    assert TEST_PKEY not in console.output.toPlainText(), (
        "the private key is still displayed in the console after the wallet locked")


def test_locking_the_wallet_clears_the_seed_phrase_from_the_open_console(qapp, core):
    """`seed create` prints the whole recovery phrase into the same pane."""
    from PyQt6.QtWidgets import QWidget
    from primer_vault.ui.console import ConsoleWindow

    main_window = QWidget()
    console = ConsoleWindow(core, main_window)
    console._handle_input("seed create")

    shown = console.output.toPlainText()
    phrase = [ln.strip() for ln in shown.splitlines()
              if len(ln.split()) == 12 and ln.strip().islower()]
    assert phrase, "seed create did not print the phrase"

    core.lock_wallet()

    assert phrase[0] not in console.output.toPlainText(), (
        "the recovery phrase is still displayed in the console after the wallet locked")
