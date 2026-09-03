"""
The shared machinery, tested directly.

Both of these were fields on TradingService before the DeFi lane needed the same
ones. The trading tests still exercise them through a trade, which is what
catches a wiring mistake; these catch the thing those cannot, which is the
primitive being wrong in a way both lanes would then inherit.

The reservation race is the one that matters. Everything else here is
bookkeeping.
"""

import threading
import time

import pytest

from primer_vault.services.pending import (
    PendingEntry, PendingQueue, Reservations, Shortfall,
)


# ============================================================
# Reservations
# ============================================================


class TestReserving:

    def test_a_reservation_under_the_limit_is_taken(self):
        r = Reservations()
        assert r.check_and_reserve("k1", "agent", 10.0, limit=100.0, committed=0.0) is None
        assert r.reserved_for("agent") == 10.0

    def test_a_reservation_over_the_limit_is_refused_and_nothing_is_taken(self):
        r = Reservations()
        shortfall = r.check_and_reserve("k1", "agent", 150.0, limit=100.0, committed=0.0)

        assert isinstance(shortfall, Shortfall)
        assert r.reserved_for("agent") == 0.0, "a refused reservation was still taken"

    def test_the_shortfall_carries_the_numbers_the_caller_needs_to_explain_it(self):
        r = Reservations()
        r.check_and_reserve("k1", "agent", 30.0, limit=100.0, committed=0.0)

        shortfall = r.check_and_reserve("k2", "agent", 80.0, limit=100.0, committed=0.0)

        assert shortfall.amount == 80.0
        assert shortfall.limit == 100.0
        # 100 limit, 30 already reserved -> 70 left, which is why 80 was refused.
        assert shortfall.remaining == pytest.approx(70.0)

    def test_what_is_already_committed_counts_against_the_limit(self):
        """The caller's own reckoning of what is spent - a daily counter, or a
        position read from chain - is what makes the cap mean anything."""
        r = Reservations()

        shortfall = r.check_and_reserve("k1", "agent", 30.0, limit=100.0, committed=80.0)

        assert shortfall is not None, "the committed 80 was ignored"
        assert shortfall.remaining == pytest.approx(20.0)

    def test_one_owner_does_not_consume_another_owner_s_allowance(self):
        r = Reservations()
        r.check_and_reserve("k1", "alice", 90.0, limit=100.0, committed=0.0)

        assert r.check_and_reserve("k2", "bob", 90.0, limit=100.0, committed=0.0) is None
        assert r.reserved_for("alice") == 90.0
        assert r.reserved_for("bob") == 90.0

    def test_a_request_re_evaluated_later_is_not_counted_against_itself(self):
        """Re-checking at approval time must exclude the reservation the request
        already holds, or a trade that fit on the way in fails on the way out."""
        r = Reservations()
        r.check_and_reserve("k1", "agent", 90.0, limit=100.0, committed=0.0)

        again = r.check_and_reserve("k1", "agent", 90.0, limit=100.0,
                                    committed=0.0, exclude_key="k1")

        assert again is None, "the request was counted against its own allowance"

    def test_releasing_gives_the_allowance_back(self):
        r = Reservations()
        r.check_and_reserve("k1", "agent", 60.0, limit=100.0, committed=0.0)

        r.release("k1")

        assert r.reserved_for("agent") == 0.0
        assert r.check_and_reserve("k2", "agent", 60.0, limit=100.0, committed=0.0) is None

    def test_releasing_something_that_was_never_reserved_is_harmless(self):
        Reservations().release("never-existed")

    def test_committing_drops_the_reservation_and_records_the_amount(self):
        r = Reservations()
        r.check_and_reserve("k1", "agent", 25.0, limit=100.0, committed=0.0)
        recorded = []

        r.commit("k1", lambda: recorded.append(25.0))

        assert recorded == [25.0]
        assert r.reserved_for("agent") == 0.0, (
            "the amount would be counted twice - once reserved, once recorded")


class TestTheReservationRace:
    """Check-and-reserve is one lock hold, and this is why.

    Split across two acquisitions, requests arriving together each read the same
    remaining balance, each find room, and each proceed - carrying the owner past
    the limit by the size of every request but the first.
    """

    def test_concurrent_requests_cannot_all_be_told_there_is_room(self):
        r = Reservations()
        limit, each = 100.0, 10.0
        threads, taken = [], []
        start = threading.Barrier(20)

        def attempt(n):
            start.wait()
            if r.check_and_reserve(f"k{n}", "agent", each, limit=limit,
                                   committed=0.0) is None:
                taken.append(each)

        for n in range(20):
            t = threading.Thread(target=attempt, args=(n,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert sum(taken) <= limit, (
            f"{len(taken)} of 20 concurrent requests were each told there was "
            f"room, reserving {sum(taken)} against a limit of {limit}")
        assert r.reserved_for("agent") == pytest.approx(sum(taken))

    def test_the_ledger_agrees_with_itself_under_churn(self):
        """Reserve and release from many threads; the total must never drift."""
        r = Reservations()
        errors = []

        def churn(n):
            try:
                for i in range(50):
                    key = f"k{n}-{i}"
                    if r.check_and_reserve(key, "agent", 1.0, limit=1e9,
                                           committed=0.0) is None:
                        r.release(key)
            except Exception as e:  # pragma: no cover - only fires on a bug
                errors.append(e)

        threads = [threading.Thread(target=churn, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert r.reserved_for("agent") == 0.0
        assert r.keys() == []


class TestRevalidate:
    """A renewal decided outside the lock has to be re-confirmed inside it."""

    def test_the_renewal_runs_when_it_is_still_due(self):
        r = Reservations()
        renewed = []

        acted = r.revalidate(still_due=lambda: True,
                             renew=lambda: renewed.append(True))

        assert acted is True
        assert renewed == [True]

    def test_the_renewal_does_not_run_when_it_is_no_longer_due(self):
        """Another thread got there first. Renewing again would zero a counter
        that already has an executed operation recorded against it."""
        r = Reservations()
        renewed = []

        acted = r.revalidate(still_due=lambda: False,
                             renew=lambda: renewed.append(True))

        assert acted is False
        assert renewed == []

    def test_only_one_of_many_concurrent_renewals_lands(self):
        r = Reservations()
        done = threading.Event()
        renewals = []

        def still_due():
            return not done.is_set()

        def renew():
            renewals.append(True)
            done.set()

        threads = [threading.Thread(target=r.revalidate, args=(still_due, renew))
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(renewals) == 1, f"the renewal ran {len(renewals)} times"


# ============================================================
# PendingQueue
# ============================================================


def a_queue(**kw):
    kw.setdefault("ttl_seconds", 900)
    kw.setdefault("max_resolved", 5)
    kw.setdefault("max_per_owner", 3)
    return PendingQueue(**kw)


class TestHoldingRequests:

    def test_something_added_can_be_read_back(self):
        q = a_queue()
        q.add("k1", "agent", {"what": "a trade"})

        entry = q.get("k1")

        assert isinstance(entry, PendingEntry)
        assert entry.payload == {"what": "a trade"}
        assert entry.owner_id == "agent"

    def test_popping_removes_it(self):
        q = a_queue()
        q.add("k1", "agent", "payload")

        assert q.pop("k1").payload == "payload"
        assert q.get("k1") is None
        assert q.pop("k1") is None

    def test_entries_are_counted_per_owner_not_globally(self):
        """One noisy agent must not crowd another's requests out of the queue."""
        q = a_queue(max_per_owner=3)
        for i in range(3):
            q.add(f"a{i}", "alice", i)

        assert q.count_for("alice") == 3
        assert q.is_full_for("alice") is True
        assert q.count_for("bob") == 0
        assert q.is_full_for("bob") is False

    def test_time_remaining_counts_down_and_never_goes_negative(self):
        q = a_queue(ttl_seconds=900)
        q.add("k1", "agent", None)

        assert 0 < q.seconds_remaining("k1") <= 900
        assert q.seconds_remaining("never-existed") == 0


class TestExpiry:

    def test_an_entry_past_its_deadline_is_swept_and_handed_to_the_handler(self):
        expired = []
        q = a_queue(on_expire=expired.append)
        q.add("k1", "agent", "payload")
        q.get("k1").deadline = time.monotonic() - 1

        q.sweep()

        assert q.get("k1") is None, "an expired entry was still approvable"
        assert [e.key for e in expired] == ["k1"]
        assert expired[0].payload == "payload"

    def test_an_entry_inside_its_deadline_is_left_alone(self):
        expired = []
        q = a_queue(on_expire=expired.append)
        q.add("k1", "agent", None)

        q.sweep()

        assert q.get("k1") is not None
        assert expired == []

    def test_the_entry_is_gone_before_the_handler_runs(self):
        """The handler settles what the entry left behind. If it could still be
        found in the pending set, a caller arriving mid-sweep could approve it."""
        seen = []
        q = a_queue()
        q._on_expire = lambda e: seen.append(q.get(e.key))
        q.add("k1", "agent", None)
        q.get("k1").deadline = time.monotonic() - 1

        q.sweep()

        assert seen == [None]

    def test_one_failing_handler_does_not_leave_the_rest_pending_forever(self):
        handled = []

        def explode(entry):
            handled.append(entry.key)
            raise RuntimeError("boom")

        q = a_queue(on_expire=explode)
        for i in range(3):
            q.add(f"k{i}", "agent", None)
            q.get(f"k{i}").deadline = time.monotonic() - 1

        q.sweep()

        assert sorted(handled) == ["k0", "k1", "k2"]
        assert q.entries() == []

    def test_a_queue_with_no_handler_still_expires_entries(self):
        q = a_queue(on_expire=None)
        q.add("k1", "agent", None)
        q.get("k1").deadline = time.monotonic() - 1

        q.sweep()

        assert q.entries() == []


class TestRememberingOutcomes:

    def test_an_outcome_can_be_polled_back(self):
        q = a_queue()
        q.remember("k1", {"status": "executed"})

        assert q.has_resolved("k1") is True
        assert q.resolved("k1") == {"status": "executed"}

    def test_an_unknown_key_resolves_to_nothing(self):
        q = a_queue()
        assert q.has_resolved("never-existed") is False
        assert q.resolved("never-existed") is None

    def test_the_oldest_outcomes_are_dropped_once_the_cap_is_reached(self):
        """An unattended engine runs for weeks, so the set needs a ceiling."""
        q = a_queue(max_resolved=5)
        for i in range(8):
            q.remember(f"k{i}", {"n": i})

        assert len(q._resolved) == 5
        assert q.resolved("k0") is None, "the oldest should have been evicted"
        assert q.resolved("k7") == {"n": 7}, "the newest should be kept"

    def test_remember_returns_what_it_stored_so_callers_can_return_it_directly(self):
        q = a_queue()
        result = {"status": "rejected"}

        assert q.remember("k1", result) is result
