"""The History tab's Time column format.

`Transaction.format_time()` feeds exactly one surface - the Desktop History
table's Time column - and abbreviates deliberately: a clock time for today's
rows, a day-and-month for older ones. Everything else that shows a time
(the details dialog, the CSV export, the Terminal edition) uses the full
timestamp and is unaffected by this; that separation is asserted below so a
future change to this helper cannot quietly shorten those too.

The two properties worth protecting:

  - The two forms must stay visually distinguishable. "19 Aug" and "14:22"
    cannot be confused; "08-19" and "14:22" can, which is why the date form
    is not numeric.
  - Rows are dated in the *user's* timezone. Timestamps are stored UTC, and
    rendering them raw put every row on UTC's calendar - an evening
    transaction could show the wrong day, and "is this today?" was answered
    against the wrong clock entirely.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.transaction import Transaction


def _at(ts) -> Transaction:
    """A Transaction carrying `ts`, without invoking the full constructor."""
    tx = Transaction.__new__(Transaction)
    tx.timestamp = ts if isinstance(ts, str) else ts.isoformat()
    return tx


def test_todays_rows_show_a_clock_time():
    now = datetime.now(timezone.utc)
    assert ":" in _at(now).format_time()


def test_older_rows_show_day_and_abbreviated_month():
    assert _at(datetime(2026, 8, 19, 11, 10, tzinfo=timezone.utc)).format_time() == "19 Aug"
    # No zero padding, and no platform-specific %-d/%#d - single-digit days
    # must render the same on Windows and POSIX.
    assert _at(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)).format_time() == "1 Jan"


def test_the_two_forms_cannot_be_mistaken_for_each_other():
    """A date must never contain a colon, nor a time a month name."""
    today = _at(datetime.now(timezone.utc)).format_time()
    old = _at(datetime(2026, 8, 19, 11, 10, tzinfo=timezone.utc)).format_time()

    assert ":" in today and ":" not in old
    assert any(c.isalpha() for c in old) and not any(c.isalpha() for c in today)


def test_rows_are_dated_in_local_time_not_utc():
    """A timestamp whose UTC date differs from its local date uses the local one.

    Picks an instant that is 'today' locally but a different date in UTC (or
    vice versa) by construction, so this holds in any timezone the suite runs
    in - including UTC itself, where the two dates coincide and the row is
    simply today.
    """
    local_now = datetime.now().astimezone()
    # 23:30 local today: in any timezone east of UTC this is already tomorrow
    # in UTC, and west of it, still today.
    instant = local_now.replace(hour=23, minute=30, second=0, microsecond=0)

    rendered = _at(instant.astimezone(timezone.utc)).format_time()
    # It is today in local terms, so it must render as a clock time regardless
    # of what date UTC thinks it is.
    assert ":" in rendered, f"local-today row rendered as a date: {rendered!r}"
    assert rendered == instant.strftime("%H:%M")


def test_naive_timestamps_are_read_as_utc():
    """Pre-tz-aware records must not shift by the local offset."""
    naive = _at("2026-08-19T11:10:00").format_time()
    aware = _at("2026-08-19T11:10:00+00:00").format_time()
    assert naive == aware == "19 Aug"


def test_unparseable_timestamps_do_not_raise():
    """A bad or missing timestamp renders as text, never takes the table down."""
    assert _at("garbage").format_time() == "garbage"
    assert _at("").format_time() == ""
    # A None timestamp used to reach len(None) in the fallback and raise.
    tx = Transaction.__new__(Transaction)
    tx.timestamp = None
    assert tx.format_time() == ""


def test_the_full_timestamp_is_still_available_elsewhere():
    """Abbreviating the column must not abbreviate the record.

    format_datetime() is what the details dialog shows and what the Time
    cell's tooltip carries; the CSV export writes `tx.timestamp` raw.
    """
    instant = datetime(2026, 8, 19, 11, 10, 33, tzinfo=timezone.utc)
    tx = _at(instant)

    # Local, like every other human-facing surface - asserted by converting
    # rather than hardcoding a clock time, so this passes in any timezone.
    assert tx.format_datetime() == instant.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    # The stored record itself stays UTC, which is what the CSV export writes.
    assert tx.timestamp.startswith("2026-08-19T11:10:33")


def test_stored_records_stay_utc():
    """Display is localised; storage is not. The CSV export depends on this."""
    instant = datetime(2026, 8, 19, 11, 10, 33, tzinfo=timezone.utc)
    tx = _at(instant)
    assert tx.timestamp.endswith("+00:00")


def test_receipts_state_utc_and_actually_convert_to_it():
    """The one deliberate exception to local-everywhere, and its past bug.

    A receipt is a formal shareable artifact, so it prints UTC and labels it.
    The label was previously applied to whatever offset the stored string
    carried - so an issuedAt of "+01:00" printed its own local clock time with
    "UTC" appended, which is a false statement on the one document meant to be
    trustworthy. This asserts the conversion, not just the label.

    Mirrors the formatting in ui/dialogs.py's receipt block; kept here rather
    than importing it because that code builds a QLabel, and this property is
    about the arithmetic.
    """
    def receipt_stamp(iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # An instant expressed in +01:00 is one hour earlier in UTC.
    assert receipt_stamp("2026-08-19T11:10:33+01:00") == "2026-08-19 10:10:33 UTC"
    # Already-UTC input is unchanged.
    assert receipt_stamp("2026-08-19T11:10:33+00:00") == "2026-08-19 11:10:33 UTC"
    # The same instant always prints identically regardless of how it is
    # expressed, which is the whole point of a receipt timestamp.
    assert receipt_stamp("2026-08-19T11:10:33+01:00") == receipt_stamp("2026-08-19T10:10:33Z")


def test_format_stamp_renders_local_and_survives_bad_input():
    """The shared helper behind the details dialog and Terminal `history`."""
    from primer_vault.models.transaction import format_stamp

    instant = datetime(2026, 8, 19, 11, 10, 33, tzinfo=timezone.utc)
    assert format_stamp(instant.isoformat()) == instant.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    # A custom format is passed through (Terminal's listing uses a shorter one).
    assert format_stamp(instant.isoformat(), "%Y-%m-%d %H:%M") == instant.astimezone().strftime("%Y-%m-%d %H:%M")
    # Naive input is read as UTC, not as local.
    assert format_stamp("2026-08-19T11:10:33") == format_stamp("2026-08-19T11:10:33+00:00")
    # Bad input renders, never raises.
    assert format_stamp(None) == ""
    assert format_stamp("") == ""
    assert format_stamp("garbage") == "garbage"
