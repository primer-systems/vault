"""
What the one endpoint left open in the default lockdown actually says.

`gui_only` is the shipped default and it refuses every Admin API route but one:
`/status` stays reachable so a CLI can tell whether an instance is already
running (daemon/admin_api.py). Nothing authenticates that request -
there is no credential on this API at all - so whatever /status returns is
returned to any process that can open a TCP connection to 127.0.0.1:4664,
including a process belonging to a different local user account.

The CLI needs three fields from it: `server_running`, `server_port`
(client/core_client.py) and `data_dir` (client/core_client.py,
which refuses attaching to a different installation). It never reads
`wallet_unlocked`, `pending_approvals` or `pending_trades`; nothing in the tree
does.

`wallet_unlocked` is the one that matters. It is a live answer to "are this
user's keys in memory right now", handed to an unauthenticated caller in the
default configuration, and it changes as the user locks and unlocks. This test
asserts the property that would make that not so: to a caller that has proved
nothing, /status must read the same whether the wallet is open or shut.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.core.settings import ADMIN_API_MODE_GUI_ONLY
from primer_vault.daemon.admin_api import AdminAPIServer

TEST_PORT = 19409  # avoids clashing with a real instance on 4664


@pytest.fixture
def locked_down_server(tmp_path):
    """A core in the shipped default mode, with a wallet on disk but shut."""
    data_dir = tmp_path / "data"
    (data_dir / "wallets").mkdir(parents=True)
    core = Vault(data_dir=data_dir)
    wallet_path = str(data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass1")
    core.lock_wallet()
    core.settings_manager.set_admin_api_mode(ADMIN_API_MODE_GUI_ONLY)

    srv = AdminAPIServer(core, port=TEST_PORT)
    srv.start()
    time.sleep(0.2)  # let the server thread bind
    try:
        yield core, wallet_path
    finally:
        srv.stop()


def status():
    """GET /status the way any local process would: no credential, no headers."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{TEST_PORT}/status", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_status_is_reachable_without_a_credential_in_the_default_mode(
        locked_down_server):
    """Ground truth for the test below: this really is an open endpoint."""
    code, _ = status()
    assert code == 200


def test_status_is_not_a_wallet_lock_oracle(locked_down_server):
    """An unauthenticated caller must not be able to watch the wallet open.

    Polling this endpoint is a reliable, cheap "the keys are in memory now"
    signal for any process on the machine, in the configuration Vault ships.
    """
    core, wallet_path = locked_down_server

    _, while_locked = status()
    core.load_wallet(wallet_path, "testpass1")
    _, while_unlocked = status()

    assert while_locked == while_unlocked, (
        "GET /status tells an unauthenticated caller whether the wallet is "
        f"open: locked={while_locked.get('wallet_unlocked')} "
        f"unlocked={while_unlocked.get('wallet_unlocked')}"
    )
