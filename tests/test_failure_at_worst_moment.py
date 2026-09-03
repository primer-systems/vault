"""What happens when things fail at the worst possible moment.

Each test encodes the safe behaviour as its assertion:

1. A second instance on one data directory is refused (instance lock).
2. A second bind to a busy admin port fails, also on Windows.
3. A failed settings save leaves the previous file intact and is reported.
4. A corrupted wallet file is reported as damaged, not as "Wrong password",
   and every save keeps a `.previous` copy as the file-level recovery path.
5. Every atomic write fsyncs its directory, so the rename is durable and not
   merely atomic.

Scope: concurrent instances, interrupted writes, corrupted keystore, port
conflicts.
"""

import json
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# A valid BIP-39 phrase for tests that need a seed in the wallet.
PHRASE = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# a second instance on one data directory is refused
# ---------------------------------------------------------------------------
# The original failing test showed two PolicyStore instances erasing each
# other's writes (last-writer-wins whole-file saves). That mechanism still
# exists at the store level, deliberately - the fix blocks it one level up:
# Vault's constructor takes an exclusive OS lock on the data directory, so a
# second core can never come up against the same files.

def test_second_vault_on_same_directory_is_refused(temp_data_dir):
    from primer_vault.core import Vault
    from primer_vault.instance_lock import InstanceAlreadyRunning

    first = Vault(data_dir=temp_data_dir)
    try:
        with pytest.raises(InstanceAlreadyRunning):
            Vault(data_dir=temp_data_dir)
    finally:
        first.release_instance_lock()


def test_released_directory_can_be_reopened(temp_data_dir):
    """An orderly shutdown (or any process death) frees the directory."""
    from primer_vault.core import Vault

    first = Vault(data_dir=temp_data_dir)
    first.release_instance_lock()

    second = Vault(data_dir=temp_data_dir)  # must not raise
    second.release_instance_lock()


# ---------------------------------------------------------------------------
# a second bind to a busy admin port fails, also on Windows
# ---------------------------------------------------------------------------

def test_second_agent_api_bind_on_same_port_is_refused():
    """On Windows, SO_REUSEADDR let a second bind to an actively-listening port
    succeed, so "bind failed" never fired and Vault would announce an agent API
    that something else was answering. The server uses SO_EXCLUSIVEADDRUSE on
    Windows instead.

    This used to be checked on the admin API, which no longer exists. The agent
    API is where it matters now - and it always mattered more, since that is
    the port agents actually send money through.
    """
    from primer_vault.services.server import ThreadedHTTPServer, AgentRequestHandler

    port = 19731  # arbitrary high port unlikely to be in use
    first = ThreadedHTTPServer(("127.0.0.1", port), AgentRequestHandler)
    try:
        with pytest.raises(OSError):
            second = ThreadedHTTPServer(("127.0.0.1", port), AgentRequestHandler)
            second.server_close()
    finally:
        first.server_close()


# ---------------------------------------------------------------------------
# a failed settings save leaves the previous file intact
# ---------------------------------------------------------------------------
# Opened with 'w', settings.json would be truncated in place, so a save that
# failed midway would destroy the previous settings and the next start would
# silently load defaults. Settings go through the store's atomic writer
# (temp file, fsync, rename), and a failed save is reported to the user
# through the on_save_error callback instead of only a log line.

def test_settings_survive_a_write_that_fails_midway(temp_data_dir):
    from primer_vault.core.settings import SettingsManager
    from primer_vault.models import store as store_mod

    reported = []
    sm = SettingsManager(temp_data_dir, on_save_error=reported.append)
    try:
        sm.set_verify_settlements(False)  # a deliberate, non-default choice

        settings_file = temp_data_dir / "settings.json"
        before = settings_file.read_text(encoding="utf-8")
        assert json.loads(before)["signing"]["verify_settlements"] is False

        # Fail the save underneath, the way disk-full does. The settings
        # writer is the store module's, so its json.dump is the seam - the
        # same fault injection test_persistence uses.
        real_dump = store_mod.json.dump
        store_mod.json.dump = lambda *a, **k: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        try:
            sm.set_verify_settlements(True)  # save fails; change stays in memory
        finally:
            store_mod.json.dump = real_dump

        after = settings_file.read_text(encoding="utf-8")
        assert after == before, (
            f"previous settings destroyed by failed save; file now: {after!r}")

        # The failure reached the user, not just the log, and said what the
        # consequence is.
        assert reported, "failed save was not reported through on_save_error"
        assert "lost when Vault restarts" in reported[0]
    finally:
        sm.stop()


def test_settings_external_change_is_still_detected(temp_data_dir):
    """The watcher now recognises its own writes by content, not by a
    skip-one-event flag - so a genuinely external edit must still reload."""
    from primer_vault.core.settings import SettingsManager

    sm = SettingsManager(temp_data_dir)
    try:
        assert sm.get_verify_settlements() is True

        doc = json.loads(
            (temp_data_dir / "settings.json").read_text(encoding="utf-8"))
        doc["signing"]["verify_settlements"] = False
        (temp_data_dir / "settings.json").write_text(
            json.dumps(doc, indent=2), encoding="utf-8")

        sm._on_file_changed()  # what the watcher calls; direct call avoids timing
        assert sm.get_verify_settlements() is False
    finally:
        sm.stop()


def test_settings_own_write_is_not_reloaded_as_external(temp_data_dir):
    from primer_vault.core.settings import SettingsManager

    changes = []
    sm = SettingsManager(temp_data_dir, on_change=lambda s: changes.append(s))
    try:
        sm.set_verify_settlements(False)  # notifies once, from the setter
        seen = len(changes)

        sm._on_file_changed()  # the watcher seeing our own save land
        assert len(changes) == seen, "own write was reloaded as an external change"
        assert sm.get_verify_settlements() is False
    finally:
        sm.stop()


# ---------------------------------------------------------------------------
# a corrupted wallet is reported as damaged, with recovery
# ---------------------------------------------------------------------------
# A JSON parse error is a ValueError, so a damaged keystore must not fall into
# the wrong-password handler and come back as "Wrong password" - sending the
# user after a password they had not forgotten. Corruption raises
# CorruptedWalletFile, the message names the real fault and the recovery
# paths, and every save keeps the version it replaces as `<name>.previous`.

def test_corrupted_wallet_is_not_reported_as_wrong_password(temp_data_dir):
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        wallet_path = temp_data_dir / "wallets" / "damaged.wallet"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        # A bad copy or disk damage: syntactically broken JSON.
        wallet_path.write_text(
            '{"version": 3, "encrypted": true, "wrapped', encoding="utf-8")

        result = core.load_wallet(str(wallet_path), "any-password-at-all")

        assert result["success"] is False
        assert result["error"] != "Wrong password", (
            "corrupted keystore misreported as a wrong password")
        assert "damaged" in result["error"]
        assert "password may well be correct" in result["error"]
    finally:
        core.release_instance_lock()


def test_wrong_password_is_still_reported_as_wrong_password(temp_data_dir):
    """The split must not widen: an intact wallet opened with the wrong
    password still gets the plain answer."""
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        wallet_path = temp_data_dir / "wallets" / "fine.wallet"
        core.create_wallet(str(wallet_path), "password-123", unlock=False)

        result = core.load_wallet(str(wallet_path), "not-the-password")
        assert result["success"] is False
        assert result["error"] == "Wrong password"
    finally:
        core.release_instance_lock()


def test_every_save_keeps_the_previous_wallet_version(temp_data_dir):
    """`<name>.previous` is the file-level recovery a damaged wallet points
    at: one save ago is always on disk, and it opens normally."""
    from primer_vault.wallet import VaultWallet

    path = temp_data_dir / "w.wallet"
    wallet = VaultWallet.create("password-123")
    wallet.save(path)

    previous = path.with_name(path.name + ".previous")
    assert not previous.exists(), "no previous exists before a second save"

    wallet.add_seed(PHRASE, "m/44'/60'/0'/0/{}")
    wallet.save(path)

    assert previous.exists()
    old = VaultWallet.load(previous, "password-123")
    assert len(old.seeds) == 0, ".previous holds the version before the save"
    new = VaultWallet.load(path, "password-123")
    assert len(new.seeds) == 1


def test_corrupted_wallet_message_points_at_the_previous_copy(temp_data_dir):
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        wallet_path = temp_data_dir / "wallets" / "w.wallet"
        core.create_wallet(str(wallet_path), "password-123")
        core.save_wallet()  # second save creates the .previous copy
        assert wallet_path.with_name("w.wallet.previous").exists()

        core.lock_wallet()
        wallet_path.write_text("garbage, not a wallet", encoding="utf-8")

        result = core.load_wallet(str(wallet_path), "password-123")
        assert result["success"] is False
        assert ".previous" in result["error"], (
            "recovery copy exists but the error does not mention it")
    finally:
        core.release_instance_lock()


def test_deleting_the_wallet_also_deletes_the_previous_copy(temp_data_dir):
    """The backup exists for recoverability; it must not outlive a deliberate
    deletion of the keys it copies."""
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        wallet_path = temp_data_dir / "wallets" / "w.wallet"
        core.create_wallet(str(wallet_path), "password-123")
        core.save_wallet()
        previous = wallet_path.with_name("w.wallet.previous")
        assert previous.exists()

        assert core.delete_wallet_file() is True
        assert not wallet_path.exists()
        assert not previous.exists(), "deleted wallet left its key backup behind"
    finally:
        core.release_instance_lock()


# ---------------------------------------------------------------------------
# the rename is made durable, not merely atomic
# ---------------------------------------------------------------------------
# os.replace is atomic, but the new directory entry sits in the filesystem
# journal until its next commit. A power loss before then reboots to the
# previous file - for agents.json that can revive daily allowance whose signed
# authorization is already in an agent's hands. Both atomic writers now fsync
# the directory after the rename.
#
# Durability across an actual power loss cannot be tested without a
# fault-injecting filesystem. What is testable, and is what these check, is
# that each writer calls the fsync, that it calls it AFTER the rename (called
# before, it would flush nothing), and that a filesystem refusing it costs
# only the durability and never the write.
#
# The ordering probe records whether the target file exists at the moment
# fsync_dir is called. On a first write it exists only once os.replace has
# run, so "it existed" is exactly the claim "the fsync came after the rename"
# - established without patching anything in the os module, which is shared
# process-wide and takes tempfile down with it.

class TestDirectoryFsyncOrdering:

    def _probe(self, monkeypatch, module, target: Path) -> list:
        calls = []
        monkeypatch.setattr(
            module, "fsync_dir",
            lambda d: calls.append((Path(d), target.exists())))
        return calls

    def test_store_writes_fsync_the_directory_after_the_rename(
            self, temp_data_dir, monkeypatch):
        from primer_vault.models import store as store_mod

        target = temp_data_dir / "policies.json"
        calls = self._probe(monkeypatch, store_mod, target)

        store_mod.write_json_atomic(target, [])

        assert calls == [(temp_data_dir, True)]

    def test_wallet_saves_fsync_the_directory_after_the_rename(
            self, temp_data_dir, monkeypatch):
        from primer_vault.wallet import crypto as crypto_mod

        target = temp_data_dir / "w.wallet"
        calls = self._probe(monkeypatch, crypto_mod, target)

        crypto_mod.VaultWallet.create("password-123").save(target)

        assert calls == [(temp_data_dir, True)]


class TestDirectoryFsyncIsBestEffort:
    """The rename has already succeeded by the time the fsync runs, so a
    filesystem that refuses the directory open (some network mounts do) must
    cost only the durability nicety - never the write. fsync_dir swallows its
    own failures to guarantee that."""

    def test_a_directory_that_cannot_be_opened_is_silent(self, temp_data_dir):
        from primer_vault.utils import fsync_dir

        fsync_dir(temp_data_dir / "does-not-exist")  # must not raise

    def test_a_path_that_is_not_a_directory_is_silent(self, temp_data_dir):
        from primer_vault.utils import fsync_dir

        a_file = temp_data_dir / "not-a-dir"
        a_file.write_text("x", encoding="utf-8")
        fsync_dir(a_file)  # must not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
