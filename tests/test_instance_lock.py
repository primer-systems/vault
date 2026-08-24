"""One running Vault per data directory.

The lock exists because every instance saves whole files from memory: a second
instance on the same directory silently erases the first one's records - spend
against daily limits, or a freshly created seed. See instance_lock.py for the
mechanism and its deliberate limits.
"""

import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.instance_lock import (
    InstanceLock, InstanceAlreadyRunning, LOCK_FILE_NAME)


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


class TestLockMechanics:

    def test_second_acquire_on_same_directory_is_refused(self, temp_data_dir):
        first = InstanceLock(temp_data_dir)
        first.acquire()
        try:
            second = InstanceLock(temp_data_dir)
            with pytest.raises(InstanceAlreadyRunning):
                second.acquire()
        finally:
            first.release()

    def test_release_frees_the_directory(self, temp_data_dir):
        first = InstanceLock(temp_data_dir)
        first.acquire()
        first.release()

        second = InstanceLock(temp_data_dir)
        second.acquire()  # must not raise
        second.release()

    def test_different_directories_do_not_conflict(self, temp_data_dir):
        other_dir = temp_data_dir / "other"
        a = InstanceLock(temp_data_dir)
        b = InstanceLock(other_dir)
        a.acquire()
        try:
            b.acquire()  # must not raise
        finally:
            b.release()
            a.release()

    def test_a_leftover_lock_file_is_not_a_lock(self, temp_data_dir):
        """The file lingering after a crash means nothing; only a held OS lock
        blocks. This is what makes stale-lock cleanup unnecessary."""
        stale = temp_data_dir / LOCK_FILE_NAME
        temp_data_dir.mkdir(parents=True, exist_ok=True)
        stale.write_text("99999\n", encoding="utf-8")  # a dead process's pid

        lock = InstanceLock(temp_data_dir)
        lock.acquire()  # must not raise
        lock.release()

    def test_acquire_is_idempotent_for_the_holder(self, temp_data_dir):
        lock = InstanceLock(temp_data_dir)
        lock.acquire()
        try:
            lock.acquire()  # second call by the same holder is a no-op
            assert lock.is_held
        finally:
            lock.release()

    def test_release_is_safe_to_call_twice(self, temp_data_dir):
        lock = InstanceLock(temp_data_dir)
        lock.acquire()
        lock.release()
        lock.release()  # must not raise
        assert not lock.is_held

    def test_error_carries_a_message_a_user_can_act_on(self, temp_data_dir):
        first = InstanceLock(temp_data_dir)
        first.acquire()
        try:
            with pytest.raises(InstanceAlreadyRunning) as exc:
                InstanceLock(temp_data_dir).acquire()
            message = exc.value.user_message()
            assert "already running" in message
            assert str(temp_data_dir) in message
        finally:
            first.release()


class TestCrossProcess:

    def test_lock_held_by_another_process_is_refused(self, temp_data_dir):
        """The real case: a different process holds the lock. Also proves the
        OS releases it when that process exits - the reacquire at the end
        succeeds with no cleanup of any kind."""
        src_dir = str(Path(__file__).parent.parent / "src")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1])\n"
             "from primer_vault.instance_lock import InstanceLock\n"
             "lock = InstanceLock(sys.argv[2])\n"
             "lock.acquire()\n"
             "print('locked', flush=True)\n"
             "sys.stdin.read()\n",  # hold until stdin closes
             src_dir, str(temp_data_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "locked"

            with pytest.raises(InstanceAlreadyRunning):
                InstanceLock(temp_data_dir).acquire()
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        # Holder is dead; the lock died with it.
        lock = InstanceLock(temp_data_dir)
        lock.acquire()
        lock.release()


class TestVaultIntegration:

    def test_vault_takes_the_lock_before_touching_any_file(self, temp_data_dir):
        """A second core must be refused before it can write anything - the
        lock exists to prevent."""
        from primer_vault.core import Vault

        first = Vault(data_dir=temp_data_dir)
        try:
            with pytest.raises(InstanceAlreadyRunning):
                Vault(data_dir=temp_data_dir)
        finally:
            first.release_instance_lock()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
