"""
Shared machinery for operations that wait on a human and spend an allowance.

Two things live here, and both exist because the trading lane and the DeFi lane
need them identically. Neither knows what it is holding: one keeps requests
alive until somebody answers, the other keeps a ledger of what has been promised
but not yet recorded.

They are collaborators, not base classes. A service *owns* a `PendingQueue` and
a `Reservations` and delegates to them; it does not inherit from anything. The
quoting, the execution and the policy checks differ between lanes, and a base
class holding those would be a fork with extra steps.

Qt-free, so both editions share it.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------
#
# An allowance is set aside at the moment the decision is made, and given back
# if the operation does not happen. Recording it only on success would leave two
# gaps: simultaneous requests would all measure themselves against the same
# starting total, and a queue of requests awaiting approval would commit nothing
# until approved, so a stack of individually-allowed operations could clear the
# cap together. What is checked has to include what is already promised.


@dataclass(frozen=True)
class Shortfall:
    """A refused reservation, and the numbers behind the refusal.

    Returned rather than a formatted string because the wording belongs to the
    lane - "exceeds daily limit" and "exceeds total deployed" are the same
    arithmetic and different sentences, and a caller that has the numbers can
    say either.
    """
    amount: float
    remaining: float
    limit: float


class Reservations:
    """A ledger of amounts promised but not yet recorded against the owner.

    Keys are request ids; owners are agent ids. Every method that touches the
    ledger takes the same lock, which is the entire point of the class - see
    `check_and_reserve`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float]] = {}

    def check_and_reserve(self, key: str, owner_id: str, amount: float,
                          limit: float, committed: float,
                          exclude_key: Optional[str] = None) -> Optional[Shortfall]:
        """Take `amount` off the owner's remaining allowance, or refuse.

        Both halves happen under one hold of the lock, and that is the whole
        point. Split across two acquisitions, two requests arriving together
        would each read the same remaining balance, each find room, and each
        proceed - carrying the owner past the limit by the size of the second
        request. Locking each half correctly does not help; only the pair being
        indivisible does.

        The agent API serves requests concurrently, so "arriving together" is an
        ordinary event rather than a contrived one.

        `committed` is what the owner has already spent by the lane's own
        reckoning - a daily volume counter, or a position read from chain. It is
        passed in rather than read here because the two lanes answer it
        differently, and the ledger has no business knowing which.

        `exclude_key` leaves that request's own reservation out of the sum, so
        re-evaluating a request at approval time does not count it against
        itself.

        Returns None if the amount was reserved, or a `Shortfall` if not.
        """
        with self._lock:
            reserved = sum(a for k, (oid, a) in self._entries.items()
                           if oid == owner_id and k != exclude_key)
            already = committed + reserved
            if already + amount > limit:
                return Shortfall(amount=amount,
                                 remaining=max(0.0, limit - already),
                                 limit=limit)
            self._entries[key] = (owner_id, amount)
            return None

    def reserved_for(self, owner_id: str) -> float:
        """What this owner has promised to requests in flight or awaiting approval.

        Published so an agent can be told a remaining balance it can actually
        spend. Without it the figure counts only settled amounts, so an agent
        with requests queued is told it has room the next one will refuse.
        """
        with self._lock:
            return sum(a for oid, a in self._entries.values() if oid == owner_id)

    def release(self, key: str) -> None:
        """Give back a reservation for an operation that did not happen."""
        with self._lock:
            self._entries.pop(key, None)

    def commit(self, key: str, record: Callable[[], None]) -> None:
        """Turn a reservation into a recorded amount, atomically.

        `record` runs while the lock is held, so no other thread can read the
        ledger in the instant between the reservation disappearing and the
        amount being counted. Anything slow - a disk write - belongs after this
        returns, not inside `record`.
        """
        with self._lock:
            self._entries.pop(key, None)
            record()

    def revalidate(self, still_due: Callable[[], bool],
                   renew: Callable[[], None]) -> bool:
        """Re-check a condition under the ledger's lock, and act on it if it holds.

        For renewals whose decision was made outside the lock and can be stale.
        Two requests arriving as a daily allowance rolls over can both decide a
        reset is due, and if the second one's reset lands after the first has
        already reset, reserved and committed, it sets the counter back to zero
        and forgets an operation already on-chain - so the cap is then measured
        against a total that is short by that amount. Re-checking under the same
        lock every other reader and writer takes closes that window.

        Returns True if `renew` ran. Persisting the result belongs after this
        returns, with the lock released.
        """
        with self._lock:
            if not still_due():
                return False
            renew()
            return True

    def keys(self) -> list[str]:
        """Every outstanding reservation key. For diagnostics and tests."""
        with self._lock:
            return list(self._entries)


# ---------------------------------------------------------------------------
# PendingQueue
# ---------------------------------------------------------------------------


@dataclass
class PendingEntry:
    """Something waiting for a human, with the deadline it waits until."""
    key: str
    owner_id: str
    payload: Any
    deadline: float  # time.monotonic() value

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class PendingQueue:
    """Requests awaiting a human, and the outcomes of those already settled.

    Deliberately not internally locked. The maps are plain dicts, and every
    mutation here is a single dict operation - `pop(k, None)`, an assignment -
    which CPython performs atomically. This matches how the trading lane has
    always behaved; introducing a lock during an extraction that is supposed to
    change no behaviour would be the wrong moment to change the concurrency
    story, and a lock held across the `on_expire` callback could deadlock
    against the caller's own.

    The allowance ledger is a different matter and *is* locked - see
    `Reservations.check_and_reserve` for why that one has to be.
    """

    def __init__(self, ttl_seconds: float, max_resolved: int,
                 max_per_owner: int,
                 on_expire: Optional[Callable[[PendingEntry], None]] = None):
        """
        Args:
            ttl_seconds: how long an entry may wait before it is abandoned.
            max_resolved: how many finished outcomes to keep for polling.
            max_per_owner: how many entries one owner may have waiting at once.
            on_expire: called with each entry as it is abandoned, after it has
                been removed from the pending map. Whatever the lane needs to do
                about it - release an allowance, write a record, tell somebody -
                happens there. An exception is logged and does not stop the
                sweep, because one bad entry must not leave the rest pending
                forever.
        """
        self.ttl_seconds = ttl_seconds
        self.max_resolved = max_resolved
        self.max_per_owner = max_per_owner
        self._on_expire = on_expire
        self._pending: dict[str, PendingEntry] = {}
        self._resolved: dict[str, dict] = {}

    # ---- pending ---------------------------------------------------------

    def add(self, key: str, owner_id: str, payload: Any) -> PendingEntry:
        """Put something in front of a human, with the clock started."""
        entry = PendingEntry(key=key, owner_id=owner_id, payload=payload,
                             deadline=time.monotonic() + self.ttl_seconds)
        self._pending[key] = entry
        return entry

    def sweep(self) -> None:
        """Abandon anything that has waited past its deadline.

        Call before every read or decision on the pending set, so an expired
        entry cannot be approved by a caller that happened to arrive first.
        """
        for key, entry in list(self._pending.items()):
            if not entry.expired:
                continue
            self._pending.pop(key, None)
            if self._on_expire is None:
                continue
            try:
                self._on_expire(entry)
            except Exception:
                logger.exception("pending expiry handler failed for %s", key)

    def get(self, key: str) -> Optional[PendingEntry]:
        return self._pending.get(key)

    def pop(self, key: str) -> Optional[PendingEntry]:
        return self._pending.pop(key, None)

    def entries(self) -> list[PendingEntry]:
        return list(self._pending.values())

    def count_for(self, owner_id: str) -> int:
        """How many entries this owner has waiting.

        Per-owner rather than global, so one noisy agent cannot crowd another's
        requests out of the queue or bury the person approving them.
        """
        return sum(1 for e in self._pending.values() if e.owner_id == owner_id)

    def is_full_for(self, owner_id: str) -> bool:
        return self.count_for(owner_id) >= self.max_per_owner

    def seconds_remaining(self, key: str) -> int:
        entry = self._pending.get(key)
        if entry is None:
            return 0
        return max(0, int(entry.deadline - time.monotonic()))

    # ---- resolved --------------------------------------------------------

    def remember(self, key: str, result: dict) -> dict:
        """Record an outcome for the caller to poll, oldest dropped first.

        A result is held so whoever submitted the request can come back and read
        what happened; once they have, the entry is dead weight. An unattended
        engine runs for weeks, so the set needs a ceiling.

        dict preserves insertion order, so the oldest keys are simply the first
        ones - no timestamp needed to know what to evict. Past the cap a poll
        for a dropped result gets whatever the lane returns for an unknown id,
        which is the same answer it already gives for one it has never seen.
        """
        self._resolved[key] = result
        while len(self._resolved) > self.max_resolved:
            self._resolved.pop(next(iter(self._resolved)))
        return result

    def resolved(self, key: str) -> Optional[dict]:
        return self._resolved.get(key)

    def has_resolved(self, key: str) -> bool:
        return key in self._resolved
