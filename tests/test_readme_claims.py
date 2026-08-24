"""Each test takes one instruction or one documented shape from README.md and
checks the shipped code does what the README says it does.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


def _drive(handler, command, password="a-strong-passphrase"):
    """Run one console command, answering password/confirm prompts."""
    result = handler.execute(command)
    guard = 0
    while result.needs_input and guard < 8:
        guard += 1
        kind = result.needs_input.get("type", "text")
        supplied = (
            {"password": password, "value": password} if kind == "password"
            else {"confirm": "YES", "value": "YES"}
        )
        result = handler.execute(command, inputs=supplied)
    return result


@pytest.fixture
def handler(tmp_path):
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        yield CommandHandler(core), core
    finally:
        core.release_instance_lock()


# ==========================================================================
# 1. Domain allow/block lists: naming domains restricts, an empty value clears,
#    and `policy create` and `policy edit` agree. There is no "all"/"none" word.
# ==========================================================================

def test_readme_documents_domains_with_no_magic_words():
    """The README documents empty-to-clear, not an "all"/"none" keyword, and
    the removed sentinels are gone from it."""
    assert '--allow-domains D,D  Merchant domains to allow (empty = allow any)' in README, \
        "README no longer documents --allow-domains with the empty=allow-any rule"
    assert '--block-domains D,D  Merchant domains to block (empty = block none)' in README, \
        "README no longer documents --block-domains with the empty=block-none rule"
    assert '"all" for any' not in README, "the removed 'all' sentinel is still documented"
    assert '"none" for clear' not in README, "the removed 'none' sentinel is still documented"


def test_no_allow_list_allows_any_merchant(handler):
    """A policy with no allowlist restricts nothing - that is how "any domain"
    is expressed, and it is the default when the flag is omitted."""
    h, core = handler
    result = _drive(h, "policy create standard --day 100 --txn 50")
    assert result.success, result.error

    policy = next(p for p in core.get_all_policies() if p.name == "standard")
    allowed, reason = policy.check_domain_allowed("https://api.example.com/resource")
    assert allowed, f"an ordinary merchant was refused with no allowlist set: {reason!r}"


def test_policy_create_and_edit_treat_a_domain_value_identically(handler):
    """The bug this replaces: `create` stored the value literally while `edit`
    read "all"/"none" as sentinels, so one flag meant two things across the two
    commands. With the sentinels gone, a named domain is a named domain on both,
    and an empty value clears on both."""
    h, core = handler
    assert _drive(h, "policy create p1 --day 100 --txn 50 --allow-domains api.example.com").success
    p1 = next(p for p in core.get_all_policies() if p.name == "p1")
    assert p1.allowed_domains == ["api.example.com"], p1.allowed_domains

    # edit to a different domain: replaces, no sentinel magic
    assert _drive(h, "policy edit p1 --allow-domains other.example.com").success
    p1 = next(p for p in core.get_all_policies() if p.name == "p1")
    assert p1.allowed_domains == ["other.example.com"], p1.allowed_domains

    # an empty value clears, on both commands - here via edit
    assert _drive(h, 'policy edit p1 --allow-domains ""').success
    p1 = next(p for p in core.get_all_policies() if p.name == "p1")
    assert p1.allowed_domains == [], p1.allowed_domains
    allowed, _ = p1.check_domain_allowed("https://anything.example.com/x")
    assert allowed


def test_empty_block_list_leaves_domain_restrictions_off(handler):
    """No blocklist (the default, and what an empty value restores) means a
    path-only resource - the common x402 shape - is not refused for want of a
    checkable domain, and a junk 'none' entry must not flip it on."""
    h, core = handler
    result = _drive(h, "policy create standard --day 100 --txn 50")
    assert result.success, result.error

    policy = next(p for p in core.get_all_policies() if p.name == "standard")
    assert not policy.has_domain_restrictions(), policy.blocked_domains
    allowed, reason = policy.check_domain_allowed("/paywalled/asset.bin")
    assert allowed, f"path-only resource refused with no restrictions set: {reason!r}"


# ==========================================================================
# 2. The first command the README tells a reader to run needs a wallet the
#    README has not told them to make yet
# ==========================================================================

def test_the_readme_creates_the_wallet_before_it_tells_you_to_open_it():
    """README's first runnable block after the Quick Start is Agent Management:

        primer-vault --cli
        > wallet open main
        ...

    The note under it explains where `standard` and `A001` come from, but not
    `main`. `wallet create main` appears ~90 lines further down, under Wallet
    Security. A reader following the file in order runs `wallet open main`
    first and gets `Wallet not found: <path>\\main.wallet`, which names no next
    step.
    """
    open_at = README.find("> wallet open main")
    assert open_at != -1, "README no longer contains the '> wallet open main' example"

    create_at = README.find("wallet create")
    assert create_at != -1 and create_at < open_at, (
        "README tells the reader to open wallet 'main' at character "
        f"{open_at} but does not show how to create a wallet until character "
        f"{create_at}"
    )


# ==========================================================================
# 3. "a full 0x address works too" for `agent commission`
# ==========================================================================

def test_readme_says_a_full_0x_address_works_for_commission():
    assert "a full `0x` address works too" in README, \
        "README no longer promises a full 0x address works for agent commission"


def test_agent_commission_accepts_a_full_address_in_any_case(handler):
    """README, Agent Management:

        `A001` is the address ID shown by `address list`; a full `0x` address
        works too.

    An Ethereum address is case-insensitive - the mixed case is an optional
    checksum, and explorers, agent config files and logs all hand out the
    lowercase form. `agent commission` compares the string exactly, so the
    lowercase form of an address that IS in the wallet comes back
    "Address not found". The address commands (`address balance`, `rename`,
    `delete`, `export`) all fold case; only the agent commands do not.
    """
    h, core = handler
    _drive(h, "wallet create main")
    _drive(h, "policy create standard --day 100 --txn 50")
    _drive(h, "agent register MyAgent --auth bearer")

    full = core.get_wallet_addresses()[0]["address"]
    assert full != full.lower(), "expected a checksummed address to test against"

    result = _drive(h, f"agent commission MyAgent standard {full.lower()}")
    assert result.success, (
        "the lowercase form of an address that is in the wallet was refused: "
        f"{result.error!r}"
    )


# ==========================================================================
# 3. The AP2 receipt the README prints is not the receipt Vault produces
# ==========================================================================

def test_the_ap2_receipt_example_in_the_readme_matches_the_receipt_vault_emits():
    """README, Transaction Receipts, shows a JSON body under

        Every payment is logged with AP2-formatted receipts:

    and that body is what an integrator writes a parser against. It is served
    by `history receipt <id>` and by GET /receipt/{id}. Every field checked
    below is taken from the README's own example.
    """
    from primer_vault.models.transaction import (
        Transaction, STATUS_SETTLED, TYPE_X402,
    )

    tx = Transaction(
        id="tx-1",
        timestamp="2026-01-15T14:32:00Z",
        agent_id="ABC123",
        agent_name="MyAgent",
        agent_code="agent-code-1",
        amount_micro=1_500_000,
        recipient="0x8ba1f109551bD432803012645Ac136ddd64DBA72",
        network="eip155:4663",
        status=STATUS_SETTLED,
        auto_approved=False,
        type=TYPE_X402,
        wallet_address="0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0",
        wallet_id="W001",
        tx_hash="0x3a1b2c3d4e5f",
        signed_at="2026-01-15T14:32:01Z",
        settled_at="2026-01-15T14:32:30Z",
        verification_status="verified",
        verification_block=12847293,
    )

    receipt = tx.to_ap2_receipt(policy_name="standard")

    match = re.search(
        r'```json\n(\{\s*\n\s*"type": "AP2Receipt".*?\n\})\n```', README, re.S)
    assert match, "README no longer contains the AP2Receipt example"
    example = json.loads(match.group(1))

    missing = []
    for section, fields in example.items():
        if not isinstance(fields, dict):
            continue
        got = receipt.get(section)
        if not isinstance(got, dict):
            missing.append(f"{section} (absent or not an object)")
            continue
        for key in fields:
            if key not in got:
                missing.append(f"{section}.{key}")

    assert not missing, (
        "fields the README's receipt example documents but the emitted receipt "
        f"does not carry: {missing}. Emitted shape: "
        + json.dumps({k: (sorted(v) if isinstance(v, dict) else type(v).__name__)
                      for k, v in receipt.items()}, indent=2)
    )
