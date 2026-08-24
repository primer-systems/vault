"""The GUI Console's input history holds no key material after the wallet locks.

A real keystroke reaches CommandInput._submit, which appends the typed line to
the input history before dispatching it. Three documented commands carry a
secret in the line itself:

    seed import "<12 words>"
    address import <private key>
    wallet open <name> --password <p>

Locking the wallet must clear that history as well as the output pane, so
Up-arrow cannot re-display a secret on a locked Vault.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

pytest.importorskip("PyQt6")

# Known-weak throwaway values. Never used on any chain.
MNEMONIC = ("legal winner thank year wave sausage worth useful legal winner "
            "thank yellow")
TEST_PKEY = "4c0883a69102937d6231471b5dbb6204fe512961708279e5f5bd5f2b7e5c1e1f"
TEST_PASSWORD = "console-history-pw"


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


def _console(core):
    """A Console parented to a stand-in for the long-lived MainWindow."""
    from PyQt6.QtWidgets import QWidget
    from primer_vault.ui.console import ConsoleWindow

    parent = QWidget()
    console = ConsoleWindow(core, parent)
    # Keep the parent alive for the life of the console.
    console._test_parent = parent
    return console


def _type(console, line):
    """Submit `line` the way a keystroke does: through CommandInput._submit."""
    console.input.setText(line)
    console.input._submit()


def _press_up(console):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    console.input.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                  Qt.KeyboardModifier.NoModifier))


def test_locking_clears_the_typed_seed_phrase_from_the_input_history(qapp, core):
    console = _console(core)
    _type(console, f'seed import "{MNEMONIC}"')

    # Premise: the command really was accepted with the phrase on the line.
    assert MNEMONIC in console.output.toPlainText(), (
        "the console did not echo the typed command")

    core.lock_wallet()

    # The existing protection: the output pane is blanked.
    assert MNEMONIC not in console.output.toPlainText(), (
        "the output pane was not cleared on lock")

    # The finding: the same phrase is still sitting in the input history.
    retained = [h for h in console.input._history if MNEMONIC in h]
    assert not retained, (
        "the recovery phrase is still in the console's command history after "
        "the wallet locked")


def test_up_arrow_after_lock_does_not_redisplay_the_seed_phrase(qapp, core):
    console = _console(core)
    _type(console, f'seed import "{MNEMONIC}"')
    core.lock_wallet()

    _press_up(console)

    assert MNEMONIC not in console.input.text(), (
        "pressing Up on a locked Vault put the recovery phrase back on screen")


def test_locking_clears_the_typed_private_key_from_the_input_history(qapp, core):
    console = _console(core)
    _type(console, f"address import {TEST_PKEY} Imported")

    assert TEST_PKEY in console.output.toPlainText(), (
        "the console did not echo the typed command")

    core.lock_wallet()

    retained = [h for h in console.input._history if TEST_PKEY in h]
    assert not retained, (
        "the imported private key is still in the console's command history "
        "after the wallet locked")


def test_locking_clears_the_wallet_password_from_the_input_history(qapp, core):
    console = _console(core)
    # `wallet open <name> --password <pw>` is parsed by the same WalletCommands
    # the console routes to (commands/wallet.py).
    _type(console, f'wallet open test --password "{TEST_PASSWORD}"')

    core.lock_wallet()

    retained = [h for h in console.input._history if TEST_PASSWORD in h]
    assert not retained, (
        "the wallet password is still in the console's command history after "
        "the wallet locked")
