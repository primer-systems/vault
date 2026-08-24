"""One running Vault per data directory, enforced with an OS file lock.

Why a lock at all: every instance loads the wallet, the policies and the spend
ledger into memory and saves each file whole. Two instances on one data
directory therefore erase each other's writes - the last save wins, and what it
erases can be a recorded spend (the daily limit then under-counts) or a freshly
created seed (the funds on it are then unrecoverable). Detection by "is the
admin port taken" does not work: an unrelated program can hold the port, and on
Windows SO_REUSEADDR let a second bind quietly succeed.

Why this kind of lock: the operating system releases it the instant the holding
process dies - kill, crash, power loss, anything - so there is no stale-lock
state and nothing to clean up. The lock file itself is allowed to linger; a
present file means nothing, only a *held* lock does. That is also why release()
never deletes the file: deleting opens a race where a second process locks the
directory entry the first is about to unlink.

The lock is per data directory, not global. A portable build on a USB stick and
a pip install on the same machine share no state and may run side by side.

Known limit: file locks over network shares (SMB, NFS) are unreliable. The lock
protects against same-machine double launches, which is the accident that
actually happens; two machines sharing a data directory over a network share
are not protected, by this or by anything else in Vault.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = "vault.lock"

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class InstanceAlreadyRunning(Exception):
    """Another Vault process holds the lock on this data directory.

    Raised out of Vault's constructor, so every entry point - GUI, CLI,
    daemon - hits it before touching a single data file. Carries a message
    written for the user, because for the GUI this surfaces as a dialog before
    any window exists.
    """

    def __init__(self, data_dir: Path, holder_pid: str = ""):
        self.data_dir = Path(data_dir)
        self.holder_pid = holder_pid
        held_by = f" (process {holder_pid})" if holder_pid else ""
        super().__init__(
            f"Another Vault instance{held_by} is already using {self.data_dir}")

    def user_message(self) -> str:
        return (
            "Vault is already running.\n\n"
            f"Another Vault process is using this data folder:\n{self.data_dir}\n\n"
            "Use the Vault that is already open - check your open windows and "
            "terminal sessions - or close it before starting Vault again."
        )


class InstanceLock:
    """Holds an exclusive OS lock on <data_dir>/vault.lock for this process."""

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / LOCK_FILE_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise InstanceAlreadyRunning. Non-blocking.

        Waiting would be wrong here: the holder is a long-lived interactive
        process, not a transaction about to finish, so the honest answer to a
        held lock is "already running", now, not after a timeout.
        """
        if self._fd is not None:
            return  # already held by us

        self._data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                # Lock one byte at offset 0. Locking past EOF is allowed on
                # Windows, so this works on a freshly created empty file.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder_pid = self._read_holder_pid(fd)
            os.close(fd)
            raise InstanceAlreadyRunning(self._data_dir, holder_pid) from None

        self._fd = fd
        self._write_our_pid(fd)
        logger.info("Acquired instance lock on %s", self._path)

    def release(self) -> None:
        """Release the lock. Safe to call more than once.

        Process death releases the lock regardless; this exists for orderly
        shutdowns and for tests. The lock file is left in place on purpose -
        see the module docstring.
        """
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass  # closing the descriptor below drops the lock anyway
        finally:
            os.close(self._fd)
            self._fd = None
        logger.info("Released instance lock on %s", self._path)

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    # The PID in the file is purely informational - for a human wondering who
    # holds the lock, and for the error message. Correctness never depends on
    # it: the lock is the OS lock, not the file's contents.

    def _write_our_pid(self, fd: int) -> None:
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass

    def _read_holder_pid(self, fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, 32).decode("ascii", errors="replace").strip()
        except OSError:
            return ""
