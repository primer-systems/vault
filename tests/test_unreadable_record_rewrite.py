"""A record the loader could not read survives the next save.

PolicyStore promises a skipped record "stays in the file, untouched, until it is
repaired or removed", and re-appends it verbatim on every save. These tests hold
that promise to account for policies.
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.store import PolicyStore
from primer_vault.models import SpendPolicy


BAD = {
    "id": "policy-under-repair",
    "name": "Trading",
    "networks": [4663],
    "daily_limit_micro": 5_000_000,
    "per_request_max_micro": None,
    "auto_approve_below_micro": None,
    "created_at": "2026-01-01T00:00:00+00:00",
    "allowed_domains": [],
    "blocked_domains": [],
    # Fails TradingRules.from_dict: negative slippage.
    "trading_rules": {
        "enabled": True,
        "per_trade_max_usd": 50.0,
        "daily_volume_limit_usd": 200.0,
        "auto_approve_below_usd": None,
        "min_reserve_eth": 0.0001,
        "max_slippage_percent": -1.0,
        "max_price_impact_percent": 5.0,
    },
    # The user has deliberately turned x402 payments off for this policy.
    "x402_enabled": False,
}


def _write(dir_: Path, records):
    (dir_ / "policies.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


def test_unreadable_record_keeps_its_trading_rules_in_the_file(tmp_path):
    """A save must write the skipped record back as it was found."""
    _write(tmp_path, [BAD])

    store = PolicyStore(tmp_path)
    assert store.get_all_policies() == [], "precondition: the record is skipped"

    # Any ordinary save rewrites the whole file.
    store.add_policy(SpendPolicy.create(
        name="Unrelated", networks=[4663], daily_limit_micro=1_000_000))

    on_disk = json.loads((tmp_path / "policies.json").read_text(encoding="utf-8"))
    kept = [r for r in on_disk if r.get("id") == "policy-under-repair"]
    assert len(kept) == 1, "the skipped record is still in the file"
    assert "trading_rules" in kept[0], "its trading rules survived the save"
    assert kept[0].get("x402_enabled") is False, "its x402 toggle survived the save"


def test_a_skipped_policy_does_not_come_back_to_life_after_a_save(tmp_path):
    """A record Vault refused to honour must not become live on the next start."""
    _write(tmp_path, [BAD])

    store = PolicyStore(tmp_path)
    assert store.get_all_policies() == []

    store.add_policy(SpendPolicy.create(
        name="Unrelated", networks=[4663], daily_limit_micro=1_000_000))

    restarted = PolicyStore(tmp_path)
    revived = restarted.get_policy("policy-under-repair")
    assert revived is None, (
        "the unreadable policy became a live policy after one ordinary save "
        f"(trading_rules={getattr(revived, 'trading_rules', None)!r}, "
        f"x402_enabled={getattr(revived, 'x402_enabled', None)!r})")


def test_record_from_a_newer_build_keeps_both_fields(tmp_path):
    """The forward-compatibility case the skip path is written for: a record
    this build cannot read must still be readable by the build that wrote it."""
    newer = dict(BAD)
    newer["trading_rules"] = None          # valid, so the TypeError below is the fault
    newer["x402_enabled"] = False
    newer["some_field_from_a_newer_build"] = True
    _write(tmp_path, [newer])

    store = PolicyStore(tmp_path)
    assert store.get_all_policies() == [], "precondition: the record is skipped"

    store.add_policy(SpendPolicy.create(
        name="Unrelated", networks=[4663], daily_limit_micro=1_000_000))

    on_disk = json.loads((tmp_path / "policies.json").read_text(encoding="utf-8"))
    kept = [r for r in on_disk if r.get("id") == "policy-under-repair"][0]
    assert "x402_enabled" in kept, "the x402 toggle survived the save"
    assert kept["x402_enabled"] is False


def test_hand_repair_does_not_silently_re_enable_x402(tmp_path):
    """The documented repair path must give back the policy the user wrote.

    A record missing a required field is skipped and logged. Vault rewrites it
    on the next save; the user then adds the missing field back by hand, which
    is exactly what the log invites. The policy must come back as it was.
    """
    broken = dict(BAD)
    broken["trading_rules"] = None
    broken["x402_enabled"] = False      # the user turned x402 payments OFF
    del broken["created_at"]            # the damage: a required field is gone
    _write(tmp_path, [broken])

    store = PolicyStore(tmp_path)
    assert store.get_all_policies() == [], "precondition: the record is skipped"

    store.add_policy(SpendPolicy.create(
        name="Unrelated", networks=[4663], daily_limit_micro=1_000_000))

    # The user reads the log and puts the missing field back.
    on_disk = json.loads((tmp_path / "policies.json").read_text(encoding="utf-8"))
    for record in on_disk:
        if record.get("id") == "policy-under-repair":
            record["created_at"] = "2026-01-01T00:00:00+00:00"
    _write(tmp_path, on_disk)

    repaired = PolicyStore(tmp_path).get_policy("policy-under-repair")
    assert repaired is not None, "the repair worked"
    assert repaired.x402_enabled is False, (
        "x402 came back ENABLED on a policy the user had disabled it on; "
        f"daily_limit_micro={repaired.daily_limit_micro}")
