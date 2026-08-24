"""
A seed phrase must never appear in an
exception message.

Vault validates seed phrases with the `mnemonic` package against the ENGLISH
wordlist only (crypto.py add_seed). Derivation later goes through
eth_account's seed_from_mnemonic, whose language auto-detection raises
ValidationError with the FULL RAW MNEMONIC embedded in the message when the
words are valid in more than one BIP-39 language (100 words are shared
between the English and French wordlists).

A phrase built entirely from shared words, with a valid English checksum,
therefore passes add_seed but makes every later derivation raise an
exception that carries the whole seed phrase. That exception surfaces in
`str(e)` handlers: core/vault.py logs it and returns it to API clients, and
services/signing.py logs it and puts it in the activity feed (persisted to
disk when log retention is on).

These tests fail if a seed phrase can reach an exception message.
"""

import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from primer_vault.wallet.crypto import VaultWallet

TEST_PASSWORD = "review-pass-1"


def _shared_wordlist_phrase() -> str:
    """Build a 12-word phrase, valid English checksum, all words in en AND fr."""
    from mnemonic import Mnemonic as PyMnemonic
    from eth_account.hdaccount.mnemonic import Mnemonic as EaMnemonic, Language

    en = set(EaMnemonic(Language.ENGLISH).wordlist)
    fr = set(EaMnemonic(Language.FRENCH).wordlist)
    shared = sorted(en & fr)
    assert len(shared) >= 12, "the wordlists do not overlap"

    checker = PyMnemonic("english")
    base = shared[:11]
    for last in shared:
        phrase = " ".join(base + [last])
        if checker.check(phrase):
            return phrase
    pytest.skip("could not construct a checksum-valid shared-wordlist phrase")


def test_derivation_error_does_not_contain_seed_phrase():
    """The exception raised by address derivation must not embed the phrase."""
    phrase = _shared_wordlist_phrase()

    wallet = VaultWallet.create(TEST_PASSWORD)

    # Vault's own validation accepts this phrase (english-only check).
    seed_id = wallet.add_seed(phrase)

    try:
        wallet.derive_address_at_index(seed_id, 0)
    except Exception as e:
        # If derivation fails, the message must not reveal the secret.
        assert phrase not in str(e), (
            "Seed phrase appears verbatim in the exception message; this "
            "string reaches logger.error / activity log / API error responses"
        )


def test_core_add_address_error_response_does_not_contain_seed_phrase(tmp_path):
    """The error dict Core returns to callers must not embed the phrase."""
    from primer_vault.core import Vault

    phrase = _shared_wordlist_phrase()

    core = Vault(data_dir=tmp_path)
    created = core.create_wallet(
        wallet_path=str(tmp_path / "wallets" / "t.wallet"),
        password=TEST_PASSWORD,
        seed_phrase=phrase,
        address_indices=[],  # skip derivation at creation so add_seed succeeds
        unlock=True,
    )
    if not created.get("success"):
        # Creation may itself trip the derivation; its error is equally a leak.
        assert phrase not in created.get("error", ""), (
            "Seed phrase appears in create_wallet error returned to callers"
        )
        return

    result = core.add_address_from_seed("S001", 0)
    if not result.get("success"):
        assert phrase not in result.get("error", ""), (
            "Seed phrase appears in the error string Core returns to the "
            "GUI/CLI/admin API"
        )
