"""State files must survive damage, and damage must be reported honestly.

A. gui_settings.json holds security choices (auto-lock, server auto-start),
   so it saves through write_json_atomic: a save that fails midway must leave
   the previous file intact, and a file that cannot be read must be reported
   to the user rather than silently replaced with the less-safe defaults.

B. A record in policies/agents/transactions.json that cannot be parsed is
   skipped at load and kept raw in PolicyStore._unreadable; every save writes
   it back untouched, and a wholly unparseable file is copied aside as
   <name>.unreadable before a save can replace it - so a recoverable mistake
   stays recoverable.

C. The unlock screen classifies errors by the code the core returns, not by
   searching the message text - a damaged wallet file must be reported as
   damaged (with the recovery copy named), never as "Wrong password".
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
# A. gui_settings.json is written in place
# ---------------------------------------------------------------------------

def _gui_settings_saver():
    from primer_vault.ui import main_window as mw
    return mw, mw.MainWindow._save_settings, mw.MainWindow._load_settings


def test_gui_settings_survive_a_write_that_fails_midway(tmp_path, monkeypatch):
    """A failed save must not destroy the settings already on disk."""
    mw, save, load = _gui_settings_saver()
    monkeypatch.setattr(mw, "get_app_dir", lambda: tmp_path)

    told = []
    window = SimpleNamespace(
        _settings={
            "auto_lock_minutes": 5,    # deliberate, non-default choices
            "auto_start_server": False,
        },
        update_activity=lambda msg, is_error=False, **kw: told.append((msg, is_error)),
    )
    save(window)

    settings_file = tmp_path / "gui_settings.json"
    before = settings_file.read_text(encoding="utf-8")
    assert json.loads(before)["auto_lock_minutes"] == 5

    # Fail the write the way a full disk does. The writer is the store
    # module's, so its json.dump is the seam - the same fault injection
    # test_persistence and test_failure_at_worst_moment use.
    from primer_vault.models import store as store_mod
    real_dump = store_mod.json.dump
    store_mod.json.dump = lambda *a, **k: (_ for _ in ()).throw(
        OSError(28, "No space left on device"))
    try:
        window._settings["theme"] = "dark"
        save(window)  # swallowed: reported, not raised
    finally:
        store_mod.json.dump = real_dump

    after = settings_file.read_text(encoding="utf-8")
    assert after == before, (
        f"previous GUI settings destroyed by a failed save; file now: {after!r}")

    # The user hears it, not just the log: the change is live in the window but
    # will not survive a restart.
    assert told, "failed save was not reported to the user"
    assert told[0][1] is True and "lost when Vault restarts" in told[0][0]

    # And nothing half-written is left lying in the data directory.
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_damaged_gui_settings_file_is_reported_not_silently_defaulted(
        tmp_path, monkeypatch):
    """Falling back to defaults is right; doing it silently is not.

    The defaults this file falls back to are the less careful ones - auto-lock
    disabled (main_window.py) and the agent server auto-started
    (main_window.py) - so the user has to be told the setting was lost.
    """
    mw, save, load = _gui_settings_saver()
    monkeypatch.setattr(mw, "get_app_dir", lambda: tmp_path)

    (tmp_path / "gui_settings.json").write_text("", encoding="utf-8")  # damaged

    told = []
    window = SimpleNamespace(
        update_activity=lambda msg, is_error=False, **kw: told.append((msg, is_error)))
    loaded = load(window)

    assert loaded == {}, "a damaged file must not be guessed at"
    assert told, "the wallet quietly lost its auto-lock setting and said nothing"
    assert told[0][1] is True
    assert "auto-lock" in told[0][0]


# ---------------------------------------------------------------------------
# B. a record that could not be read must survive the next save
# ---------------------------------------------------------------------------

def test_unreadable_policy_record_survives_the_next_save(tmp_path):
    """Skipping is documented as a read-time decision, not a repair."""
    from primer_vault.models.store import PolicyStore
    from primer_vault.models import SpendPolicy

    good = SpendPolicy.create(name="Good", networks=[4663],
                              daily_limit_micro=1_000_000)
    (tmp_path / "policies.json").write_text(
        json.dumps([{"id": "damaged"}, good.to_dict()], indent=2), encoding="utf-8")

    store = PolicyStore(tmp_path)              # skips the damaged record
    assert [p.name for p in store.get_all_policies()] == ["Good"]

    store.add_policy(SpendPolicy.create(name="Another", networks=[4663],
                                        daily_limit_micro=1_000_000))

    on_disk = json.loads((tmp_path / "policies.json").read_text(encoding="utf-8"))
    assert any(r.get("id") == "damaged" for r in on_disk), (
        "the unreadable record was erased by the next save, so it can no "
        "longer be repaired by hand")


def test_unreadable_transaction_record_survives_the_next_save(tmp_path):
    """The same for the payment history, where the record is the audit trail."""
    from primer_vault.models.store import PolicyStore
    from primer_vault.models import Transaction

    tx = Transaction.create(
        agent_id="ABC123", agent_name="Bot", agent_code="C1",
        amount_micro=1_000_000, recipient="0x" + "11" * 20, network="eip155:4663")
    damaged = tx.to_dict() | {"status": "not-a-status"}   # e.g. written by another build
    (tmp_path / "transactions.json").write_text(
        json.dumps([damaged], indent=2), encoding="utf-8")

    store = PolicyStore(tmp_path)
    assert store.get_all_transactions() == []

    store.add_transaction(Transaction.create(
        agent_id="ABC123", agent_name="Bot", agent_code="C1",
        amount_micro=2_000_000, recipient="0x" + "22" * 20, network="eip155:4663"))

    on_disk = json.loads((tmp_path / "transactions.json").read_text(encoding="utf-8"))
    assert any(r.get("id") == damaged["id"] for r in on_disk), (
        "the unreadable payment record was erased by the next save")


def test_a_wholly_unparseable_file_is_copied_aside_before_a_save_replaces_it(
        tmp_path):
    """When not even one record can be extracted, the bytes are still kept."""
    from primer_vault.models.store import PolicyStore
    from primer_vault.models import SpendPolicy

    corrupt = '[{"id": "was-half-writ'          # truncated mid-write
    (tmp_path / "policies.json").write_text(corrupt, encoding="utf-8")

    store = PolicyStore(tmp_path)               # starts, with zero policies
    store.add_policy(SpendPolicy.create(name="New", networks=[4663],
                                        daily_limit_micro=1_000_000))

    aside = tmp_path / "policies.json.unreadable"
    assert aside.exists() and aside.read_text(encoding="utf-8") == corrupt, (
        "the unreadable file's contents are gone: the save replaced the only "
        "copy")


# ---------------------------------------------------------------------------
# C. the unlock screen still says "Wrong password" for a damaged wallet
# ---------------------------------------------------------------------------

@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_unlock_screen_does_not_call_a_damaged_wallet_a_wrong_password(
        qt_app, tmp_path):
    """The real GUI path: damaged file + the correct password."""
    from primer_vault.core import Vault
    from primer_vault.ui.tabs import WalletTab

    core = Vault(data_dir=tmp_path)
    try:
        wallet_path = tmp_path / "wallets" / "primer.wallet"
        core.create_wallet(str(wallet_path), "password-123", unlock=False)
        core.save_wallet()

        tab = WalletTab(core)
        tab._wallet_path = wallet_path

        # Damage the file the way an interrupted write or a bad sector does.
        wallet_path.write_text('{"version": 3, "encrypted": true, "wrapp',
                               encoding="utf-8")

        tab.password_input.setText("password-123")   # the correct password
        tab.try_unlock()

        shown = tab.lock_error_label.text()
        assert "Wrong password" not in shown, (
            f"damaged keystore reported to the user as a wrong password: {shown!r}")
        assert "damaged" in shown.lower(), (
            f"the user is not told the file is damaged; screen says: {shown!r}")
    finally:
        core.release_instance_lock()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
