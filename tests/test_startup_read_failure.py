"""A data file that cannot be read at startup is not replaced by an empty one.

PolicyStore loses a whole file two ways: the bytes are not valid JSON, or the
file cannot be opened at all (a backup or anti-virus tool holding it open with
no sharing, a cloud-sync placeholder, a restore that got the ownership wrong).
Both take the same branch, and in the second case the set-aside copy fails for
the same reason the load did â€” so the original on disk is the only copy, and
every later save to it must be refused.

The file is made genuinely unopenable here â€” a real exclusive-share handle on
Windows, real permissions on POSIX â€” rather than by patching open().
"""

import ctypes
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import SpendPolicy, Transaction
from primer_vault.models.store import PolicyStore


class unopenable:
    """Hold `path` so that nothing else can open it, then let it go.

    Models a transient condition - a backup tool part-way through the data
    folder - that has cleared by the time the user next does something that
    saves.
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
            # dwShareMode 0: no other handle may be opened on this file.
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
        # Prove the premise rather than assume it.
        with pytest.raises(OSError):
            self._path.read_text(encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self._handle)
        else:
            os.chmod(self._path, self._mode)
        return False


def test_a_transaction_ledger_that_could_not_be_read_survives_the_next_save(
        tmp_path):
    """The record that refuses a replayed payment after a restart.

    get_transaction_by_cache_key is the only thing between an agent that
    resends a payment request after a restart and a second, separately
    redeemable authorisation for the same purchase. It reads what is in
    transactions.json.
    """
    store = PolicyStore(tmp_path)
    tx = Transaction.create(
        agent_id="ABC123", agent_name="Bot", agent_code="code-1",
        amount_micro=5_000_000, recipient="0x" + "11" * 20,
        network="eip155:4663")
    tx.cache_key = "the-idempotency-key"
    store.add_transaction(tx)

    ledger = tmp_path / "transactions.json"
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 1

    # Restart while the file cannot be opened.
    with unopenable(ledger):
        restarted = PolicyStore(tmp_path)
        assert restarted.get_all_transactions() == []
        # No set-aside copy was possible - it needed the same read.
        assert not (tmp_path / "transactions.json.unreadable").exists()

    # The backup tool has finished; the file opens again. One ordinary
    # mutation later - any payment, any trade, any status update.
    restarted.add_transaction(Transaction.create(
        agent_id="ABC123", agent_name="Bot", agent_code="code-1",
        amount_micro=1_000_000, recipient="0x" + "22" * 20,
        network="eip155:4663"))

    on_disk = json.loads(ledger.read_text(encoding="utf-8"))
    keys = [t.get("cache_key") for t in on_disk]
    assert "the-idempotency-key" in keys, (
        "the earlier payment record was erased by the first save after a "
        "startup that could not read the file, and no .unreadable copy was "
        "kept - a replay of that payment is no longer recognised")


def test_policies_that_could_not_be_read_survive_the_next_save(tmp_path):
    """Same file handling, same promise, for the spend policies."""
    store = PolicyStore(tmp_path)
    store.add_policy(SpendPolicy.create(
        name="Daily", networks=[4663], daily_limit_micro=1_000_000,
        per_request_max_micro=100_000))

    policies = tmp_path / "policies.json"

    with unopenable(policies):
        restarted = PolicyStore(tmp_path)
        assert restarted.get_all_policies() == []

    restarted.add_policy(SpendPolicy.create(
        name="Other", networks=[4663], daily_limit_micro=2_000_000,
        per_request_max_micro=200_000))

    names = [p["name"] for p in json.loads(policies.read_text(encoding="utf-8"))]
    assert "Daily" in names, (
        "the policy file was replaced by the first save after a startup that "
        "could not read it, and no .unreadable copy was kept")


def test_settings_that_could_not_be_read_survive_the_next_save(tmp_path):
    """settings.json has no set-aside copy at all.

    SettingsManager._load catches every exception and falls back to the
    defaults (settings.py). _save then writes those defaults over the
    file at the next change - which is the outcome _save's own docstring
    names as the reason it writes atomically: "re-enabling networks the user
    had disabled and discarding a custom RPC endpoint".
    """
    from primer_vault.core.settings import SettingsManager

    manager = SettingsManager(tmp_path)
    try:
        manager.set_rpc_endpoint(4663, "http://192.168.1.50:8545")  # own node
    finally:
        manager.stop()

    settings_file = tmp_path / "settings.json"
    assert "192.168.1.50" in settings_file.read_text(encoding="utf-8")

    with unopenable(settings_file):
        restarted = SettingsManager(tmp_path)
    try:
        # Any later change at all - here, an unrelated one.
        restarted.set_max_request_age(600)
    finally:
        restarted.stop()

    assert "192.168.1.50" in settings_file.read_text(encoding="utf-8"), (
        "the user's own RPC endpoint was replaced by the built-in default "
        "after a startup that could not read settings.json, with no copy kept")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
