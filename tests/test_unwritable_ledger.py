"""Payments are not authorised while the transaction ledger cannot be written.

A data file that could not be read at startup and could not be copied aside is
the only copy of that data, so every later save to it is refused. Vault must not
then hand out new payment authorizations it knows it cannot record: the record
is what recognises the same purchase again after a restart, so that one purchase
cannot become two independently redeemable authorizations.
"""

import ctypes
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


class unopenable:
    """Hold `path` so nothing else can open it, then let it go.

    A backup or anti-virus tool part-way through the data folder on Windows;
    a restore that got the ownership wrong on POSIX. Same helper as
    test_startup_read_failure.
    """

    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        if os.name == "nt":
            from ctypes import wintypes
            GENERIC_READ, OPEN_EXISTING, INVALID = 0x80000000, 3, -1
            create = ctypes.windll.kernel32.CreateFileW
            create.restype = wintypes.HANDLE
            handle = create(str(self._path), GENERIC_READ, 0, None,
                            OPEN_EXISTING, 0, None)
            if handle in (INVALID, ctypes.c_void_p(-1).value, 0):
                pytest.skip("could not take an exclusive handle on the file")
            self._handle = handle
        else:
            if os.geteuid() == 0:
                pytest.skip("root ignores file permissions")
            self._mode = self._path.stat().st_mode
            os.chmod(self._path, 0o000)
        with pytest.raises(OSError):
            self._path.read_text(encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self._handle)
        else:
            os.chmod(self._path, self._mode)
        return False


def _x402(amount_micro, index=0):
    pay_to = "0x" + ("65" * 19) + f"{index:02x}"
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": str(amount_micro),
            "asset": TOKENS["USDG"].addresses[4663],
            "payTo": pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


def _open_vault(data_dir):
    from primer_vault.core import Vault

    core = Vault(data_dir=data_dir)
    core.load_wallet(str(Path(data_dir) / "wallets" / "test.wallet"), "testpass")
    return core


def _shut(core):
    core.settings_manager.stop()
    core.release_instance_lock()


def _run_the_chain(temp_data_dir):
    """Play out the sequence once and return what each step produced.

    1. Ordinary session: one payment is made, so transactions.json exists.
    2. Vault restarts at a moment when transactions.json cannot be opened. No
       copy can be kept - the copy needs the same read that failed - so the
       file is protected and every save to it is refused.
    3. The agent makes a second payment. Vault signs it and returns a
       redeemable authorization; nothing reaches the ledger.
    4. The file opens again; Vault restarts.
    5. The agent retries that second payment identically - the ordinary retry
       after a restart that the on-disk idempotency record exists for.
    """
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    wallet_path = str(temp_data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass")
    core.load_wallet(wallet_path, "testpass")

    agent, token = core.create_agent(name="Payer", auth_mode="bearer")
    policy = core.create_policy(
        name="P", networks=[4663], daily_limit_micro=50_000_000,
        per_request_max_micro=10_000_000, auto_approve_below_micro=50_000_000)
    address = core.get_wallet_addresses()[0]["address"]
    core.commission_agent(agent.code, policy.id, address)

    first = core._signing_service.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(1_000_000, index=1))
    assert first["status"] == "success", first
    _shut(core)

    ledger = temp_data_dir / "transactions.json"
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 1

    with unopenable(ledger):
        blind = _open_vault(temp_data_dir)
        try:
            assert blind.get_recent_transactions() == []
            assert not (temp_data_dir / "transactions.json.unreadable").exists()

            second = blind._signing_service.handle_sign_request(
                agent_id=agent.id, signature=token,
                x402_data=_x402(2_000_000, index=2))
        finally:
            _shut(blind)

    after = _open_vault(temp_data_dir)
    try:
        recorded = [t.amount_micro for t in after.get_recent_transactions()]
        replay = after._signing_service.handle_sign_request(
            agent_id=agent.id, signature=token,
            x402_data=_x402(2_000_000, index=2))
    finally:
        _shut(after)

    return second, recorded, replay


def test_the_retry_does_not_become_a_second_redeemable_authorization(
        temp_data_dir):
    """The money outcome. One purchase, two authorizations the merchant can
    redeem separately, because the record that would refuse the second was
    never written."""
    second, _recorded, replay = _run_the_chain(temp_data_dir)

    if second.get("status") != "success":
        pytest.skip("Vault refused to authorize a payment it could not record")

    assert not (replay.get("status") == "success"
                and replay.get("header_value") != second.get("header_value")), (
        "the retry after the restart produced a second, independently "
        "redeemable authorization for the same purchase")


def test_a_payment_made_while_the_ledger_is_protected_is_recorded_somewhere(
        temp_data_dir):
    """The audit hole behind it: the payment left no trace at all."""
    second, recorded, _replay = _run_the_chain(temp_data_dir)

    if second.get("status") != "success":
        pytest.skip("Vault refused to authorize a payment it could not record")

    assert 2_000_000 in recorded, (
        "Vault signed and returned a redeemable 2.000000 USDG payment "
        "authorization while transactions.json was protected from writes; "
        "after the restart the payment is absent from the record entirely "
        f"(ledger holds {recorded})")
