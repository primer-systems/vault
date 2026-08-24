"""
Cryptography Edge Cases Tests

Wallet encryption edge cases, wrong password handling, corrupted data
detection, key derivation boundaries, and memory cleanup.

These verify that cryptographic operations fail safely: a wallet that cannot be
proved authentic must not open, and a locked wallet must retain nothing.
"""

import json
import os
import secrets
import sys
import tempfile
import shutil
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.wallet.crypto import (
    VaultWallet, UnsupportedWalletVersion, CorruptedWalletFile,
    WALLET_FORMAT_VERSION, derive_key, encrypt_with_key, decrypt_with_key,
)

PASSWORD = "a sufficiently long password"


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def valid_seed_phrase():
    """Generate a valid 12-word seed phrase."""
    from eth_account.hdaccount import generate_mnemonic
    return generate_mnemonic(num_words=12, lang="english")


def make_wallet(seed_phrase, password=PASSWORD):
    """A wallet holding one seed and one derived address."""
    wallet = VaultWallet.create(password)
    seed_id = wallet.add_seed(seed_phrase)
    wallet.add_address_from_seed(seed_id, 0, "Primary")
    return wallet, seed_id


def saved_wallet(path, seed_phrase, password=PASSWORD):
    """A wallet written to `path`, returned with its seed id."""
    wallet, seed_id = make_wallet(seed_phrase, password)
    wallet.save(path)
    return wallet, seed_id


def stored_strings(node):
    """Every string *value* in a wallet file, at any depth.

    Values only - the keys are Vault's own field names, not stored data. That
    distinction matters here: six BIP-39 words ("address", "index", "name",
    "salt", "tag", "version") are also keys in this file, so a scan of the raw
    text finds them in about one run in twenty-nine no matter how sound the
    encryption is.
    """
    if isinstance(node, dict):
        for value in node.values():
            yield from stored_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from stored_strings(value)
    elif isinstance(node, str):
        yield node


# =============================================================================
# Wrong Password Handling Tests
# =============================================================================

class TestWrongPasswordHandling:
    """Test behavior when wrong password is provided."""

    def test_wrong_password_raises_error(self, temp_data_dir, valid_seed_phrase):
        """Wrong password should raise ValueError."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, "wrong_password")

    def test_empty_password_on_encrypted_wallet(self, temp_data_dir, valid_seed_phrase):
        """Empty password should fail on an encrypted wallet."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, "")

    def test_none_password_on_encrypted_wallet(self, temp_data_dir, valid_seed_phrase):
        """None password should fail rather than being treated as empty."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        with pytest.raises((ValueError, TypeError, AttributeError)):
            VaultWallet.load(wallet_path, None)

    def test_verify_password_does_not_open_on_a_wrong_one(self, temp_data_dir,
                                                          valid_seed_phrase):
        """The check answers from the stored wrapping, not a retained password."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)
        wallet = VaultWallet.load(wallet_path, PASSWORD)

        assert wallet.verify_password(PASSWORD) is True
        assert wallet.verify_password("wrong_password") is False

    def test_password_not_stored_in_wallet_file(self, temp_data_dir, valid_seed_phrase):
        """Neither the password nor the seed may appear anywhere in the file.

        The password and the whole phrase are checked against the raw text -
        both are long and distinctive, so finding either is conclusive.

        Individual seed words are checked against the stored *values* instead,
        and as whole whitespace-delimited tokens. Two reasons, and both are
        needed: scanning the raw text matches Vault's own field names, six of
        which are BIP-39 words; and matching a bare substring would hit the
        encrypted hex blobs, where a short word made of hex letters turns up by
        chance. Tokens-within-values catches what actually matters - a phrase
        stored as a list, or a word left in a name or a label - without either
        false positive.
        """
        wallet_path = temp_data_dir / "test.wallet"
        password = "my_secret_password_123"
        saved_wallet(wallet_path, valid_seed_phrase, password)

        content = wallet_path.read_text(encoding="utf-8")
        assert password not in content
        assert valid_seed_phrase not in content

        values = list(stored_strings(json.loads(content)))
        assert values, "the file should contain stored values to check"

        words = set(valid_seed_phrase.split())
        for value in values:
            assert password not in value
            assert valid_seed_phrase not in value
            leaked = words.intersection(value.split())
            assert not leaked, f"seed word(s) {sorted(leaked)} stored in {value!r}"


# =============================================================================
# Corrupted Data Handling Tests
# =============================================================================

class TestCorruptedDataHandling:
    """Every one of these must fail closed: no wallet, not a partial one."""

    def test_truncated_json_rejected(self, temp_data_dir, valid_seed_phrase):
        """A half-written file must not open."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        content = wallet_path.read_text(encoding="utf-8")
        wallet_path.write_text(content[:len(content) // 2], encoding="utf-8")

        with pytest.raises((ValueError, json.JSONDecodeError, KeyError)):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_corrupted_wrapped_key_rejected(self, temp_data_dir, valid_seed_phrase):
        """Tampering with the wrapped master key must be caught by GCM."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        data = json.loads(wallet_path.read_text(encoding="utf-8"))
        data["wrapped_key"]["ciphertext"] = secrets.token_bytes(
            len(data["wrapped_key"]["ciphertext"]) // 2).hex()
        wallet_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_modified_auth_tag_rejected(self, temp_data_dir, valid_seed_phrase):
        """A flipped authentication tag must not decrypt."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        data = json.loads(wallet_path.read_text(encoding="utf-8"))
        tag = bytearray.fromhex(data["wrapped_key"]["tag"])
        tag[0] ^= 0xFF
        data["wrapped_key"]["tag"] = tag.hex()
        wallet_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_modified_iv_causes_decryption_failure(self, temp_data_dir, valid_seed_phrase):
        """A changed IV must not silently yield different plaintext."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        data = json.loads(wallet_path.read_text(encoding="utf-8"))
        iv = bytearray.fromhex(data["wrapped_key"]["iv"])
        iv[0] ^= 0xFF
        data["wrapped_key"]["iv"] = iv.hex()
        wallet_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_tampered_seed_rejected(self, temp_data_dir, valid_seed_phrase):
        """A seed entry altered under an intact master key is still caught."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        data = json.loads(wallet_path.read_text(encoding="utf-8"))
        blob = bytearray.fromhex(data["seeds"][0]["encrypted_phrase"])
        blob[0] ^= 0xFF
        data["seeds"][0]["encrypted_phrase"] = blob.hex()
        wallet_path.write_text(json.dumps(data), encoding="utf-8")

        # Caught as damage (the password was correct), not surfaced as the raw
        # crypto exception, so the caller can name the fault and the backup.
        with pytest.raises(CorruptedWalletFile):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_invalid_json_rejected(self, temp_data_dir):
        """Not JSON at all."""
        wallet_path = temp_data_dir / "test.wallet"
        wallet_path.write_text("this is not json", encoding="utf-8")

        with pytest.raises((ValueError, json.JSONDecodeError)):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_missing_required_fields_rejected(self, temp_data_dir):
        """A wallet marked encrypted but carrying no key wrapping."""
        wallet_path = temp_data_dir / "test.wallet"
        wallet_path.write_text(
            json.dumps({"version": WALLET_FORMAT_VERSION, "encrypted": True}),
            encoding="utf-8")

        with pytest.raises((ValueError, KeyError)):
            VaultWallet.load(wallet_path, PASSWORD)

    def test_wrong_version_rejected(self, temp_data_dir, valid_seed_phrase):
        """An unreadable format is refused distinctly from a bad password.

        The remedy differs and no password will ever open the file, so saying
        "wrong password" would send the user after the wrong problem.
        """
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase)

        data = json.loads(wallet_path.read_text(encoding="utf-8"))
        data["version"] = 99
        wallet_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(UnsupportedWalletVersion):
            VaultWallet.load(wallet_path, PASSWORD)


# =============================================================================
# Key Derivation Edge Cases
# =============================================================================

class TestKeyDerivationEdgeCases:
    """Test edge cases in HD key derivation."""

    def test_derive_address_index_zero(self, valid_seed_phrase):
        """Index 0 should derive successfully."""
        wallet, seed_id = make_wallet(valid_seed_phrase)
        address = wallet.derive_address_at_index(seed_id, 0)

        assert address.startswith("0x")
        assert len(address) == 42

    def test_derive_address_index_large(self, valid_seed_phrase):
        """A large index should still derive."""
        wallet, seed_id = make_wallet(valid_seed_phrase)
        address = wallet.derive_address_at_index(seed_id, 1000)

        assert address.startswith("0x")
        assert len(address) == 42

    def test_derive_address_index_max_safe(self, valid_seed_phrase):
        """The top of the non-hardened range should derive."""
        wallet, seed_id = make_wallet(valid_seed_phrase)
        address = wallet.derive_address_at_index(seed_id, 2**31 - 1)

        assert address.startswith("0x")

    def test_negative_address_index_rejected(self, valid_seed_phrase):
        """A negative index is not a derivation path."""
        wallet, seed_id = make_wallet(valid_seed_phrase)

        # eth_account refuses to build the path, which is the right refusal —
        # the point is that a negative index never reaches key derivation.
        with pytest.raises(Exception, match="not valid"):
            wallet.derive_address_at_index(seed_id, -1)

    def test_consistent_derivation(self, valid_seed_phrase):
        """The same seed and index must always give the same address."""
        wallet_a, seed_a = make_wallet(valid_seed_phrase)
        wallet_b, seed_b = make_wallet(valid_seed_phrase, "a different password")

        for index in (0, 1, 5):
            assert (wallet_a.derive_address_at_index(seed_a, index)
                    == wallet_b.derive_address_at_index(seed_b, index))

    def test_different_seeds_different_addresses(self):
        """Different seeds must not collide."""
        from eth_account.hdaccount import generate_mnemonic
        wallet_a, seed_a = make_wallet(generate_mnemonic(num_words=12, lang="english"))
        wallet_b, seed_b = make_wallet(generate_mnemonic(num_words=12, lang="english"))

        assert (wallet_a.derive_address_at_index(seed_a, 0)
                != wallet_b.derive_address_at_index(seed_b, 0))

    def test_unknown_seed_rejected(self, valid_seed_phrase):
        """Deriving from a seed the wallet does not hold."""
        wallet, _ = make_wallet(valid_seed_phrase)

        with pytest.raises(ValueError):
            wallet.derive_address_at_index("S999", 0)


# =============================================================================
# Seed Phrase Validation
# =============================================================================

class TestSeedPhraseValidation:
    """Only a valid BIP-39 mnemonic may enter the wallet."""

    @pytest.mark.parametrize("phrase, why", [
        ("these are not valid bip39 words at all", "not in the wordlist"),
        ("abandon " * 11, "11 words"),
        ("abandon " * 13, "13 words"),
        ("", "empty"),
        ("   \t\n  ", "whitespace only"),
    ])
    def test_invalid_seed_phrase_rejected(self, phrase, why):
        wallet = VaultWallet.create(PASSWORD)

        with pytest.raises(ValueError):
            wallet.add_seed(phrase.strip() if phrase.strip() else phrase)

    def test_24_word_seed_accepted(self):
        """24-word seeds are supported alongside 12."""
        from eth_account.hdaccount import generate_mnemonic
        phrase = generate_mnemonic(num_words=24, lang="english")

        wallet = VaultWallet.create(PASSWORD)
        seed_id = wallet.add_seed(phrase)

        assert wallet.get_seed_phrase(seed_id) == phrase

    def test_same_seed_added_twice_is_one_seed(self, valid_seed_phrase):
        """Adding a seed already held returns the existing id."""
        wallet = VaultWallet.create(PASSWORD)
        first = wallet.add_seed(valid_seed_phrase)
        second = wallet.add_seed(valid_seed_phrase)

        assert first == second
        assert len(wallet.seeds) == 1


# =============================================================================
# Memory Security
# =============================================================================

class TestMemorySecurity:
    """Locking must leave nothing usable behind."""

    def test_lock_clears_decrypted_seeds(self, valid_seed_phrase):
        wallet, seed_id = make_wallet(valid_seed_phrase)
        assert wallet.get_seed_phrase(seed_id) == valid_seed_phrase

        wallet.lock()

        with pytest.raises(ValueError):
            wallet.get_seed_phrase(seed_id)

    def test_lock_clears_the_master_key(self, valid_seed_phrase):
        """Without the master key nothing else in the wallet can be read."""
        wallet, _ = make_wallet(valid_seed_phrase)
        assert wallet.data_key is not None

        wallet.lock()

        with pytest.raises(ValueError):
            _ = wallet.data_key

    def test_lock_prevents_signing(self, valid_seed_phrase):
        """A locked wallet must not produce a private key."""
        wallet, seed_id = make_wallet(valid_seed_phrase)
        addr_id = wallet.addresses[0].id

        wallet.lock()

        with pytest.raises(ValueError, match="locked"):
            wallet.get_private_key(addr_id)

    def test_del_calls_lock(self, valid_seed_phrase):
        """Destruction locks, so a dropped reference does not leave secrets."""
        wallet, seed_id = make_wallet(valid_seed_phrase)
        wallet.__del__()

        assert wallet._data_key is None
        assert wallet._decrypted_seeds == {}


# =============================================================================
# Encryption / Decryption
# =============================================================================

class TestEncryptionDecryption:
    """Round trips under the master key, and passwords that stress the KDF."""

    def test_encrypt_decrypt_round_trip(self):
        key = secrets.token_bytes(32)
        original = "the quick brown fox jumps over the lazy dog"

        ciphertext, iv, tag = encrypt_with_key(key, original)

        assert decrypt_with_key(key, ciphertext, iv, tag) == original

    def test_wrong_key_rejected(self):
        ciphertext, iv, tag = encrypt_with_key(secrets.token_bytes(32), "secret")

        with pytest.raises(InvalidTag):
            decrypt_with_key(secrets.token_bytes(32), ciphertext, iv, tag)

    def test_unicode_password_supported(self, valid_seed_phrase, temp_data_dir):
        """Nothing is stripped or normalised, so a passphrase arrives as typed."""
        wallet_path = temp_data_dir / "test.wallet"
        password = "пароль密码🔐passphrase"
        saved_wallet(wallet_path, valid_seed_phrase, password)

        reloaded = VaultWallet.load(wallet_path, password)

        assert reloaded.get_seed_phrase(reloaded.seeds[0].id) == valid_seed_phrase

    def test_very_long_password(self, valid_seed_phrase, temp_data_dir):
        wallet_path = temp_data_dir / "test.wallet"
        password = "x" * 1000
        saved_wallet(wallet_path, valid_seed_phrase, password)

        reloaded = VaultWallet.load(wallet_path, password)

        assert reloaded.get_seed_phrase(reloaded.seeds[0].id) == valid_seed_phrase

    def test_whitespace_in_password_is_significant(self, valid_seed_phrase, temp_data_dir):
        """A pasted passphrase must not be quietly trimmed."""
        wallet_path = temp_data_dir / "test.wallet"
        saved_wallet(wallet_path, valid_seed_phrase, "  spaced password  ")

        with pytest.raises(ValueError):
            VaultWallet.load(wallet_path, "spaced password")

    def test_derive_key_is_deterministic(self):
        """Same password and salt, same key - or nothing would ever reopen."""
        salt = secrets.token_bytes(16)

        assert derive_key("a password", salt) == derive_key("a password", salt)

    def test_derive_key_varies_with_salt(self):
        """Two wallets with one password must not share a key."""
        assert (derive_key("a password", secrets.token_bytes(16))
                != derive_key("a password", secrets.token_bytes(16)))


# =============================================================================
# Agent Secret Encryption
# =============================================================================

class TestAgentSecretEncryption:
    """Agent credentials are encrypted under the wallet's master key, with the
    agent id bound in as associated data."""

    def test_agent_secret_encryption_round_trip(self):
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        data_key = secrets.token_bytes(32)
        secret = secrets.token_bytes(32).hex()

        encrypted, iv, tag = encrypt_agent_secret(secret, data_key, "ABC123")

        assert decrypt_agent_secret(encrypted, iv, tag, data_key, "ABC123") == secret

    def test_agent_secret_wrong_key_rejected(self):
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        secret = secrets.token_bytes(32).hex()
        encrypted, iv, tag = encrypt_agent_secret(secret, secrets.token_bytes(32), "ABC123")

        with pytest.raises(InvalidTag):
            decrypt_agent_secret(encrypted, iv, tag, secrets.token_bytes(32), "ABC123")

    def test_agent_secret_wrong_agent_id_rejected(self):
        """The binding is what stops a credential being moved between agents."""
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        data_key = secrets.token_bytes(32)
        secret = secrets.token_bytes(32).hex()
        encrypted, iv, tag = encrypt_agent_secret(secret, data_key, "ABC123")

        with pytest.raises(InvalidTag):
            decrypt_agent_secret(encrypted, iv, tag, data_key, "XYZ789")

class TestSavingTheWalletFile:
    """A save must not create a moment where the wallet is exposed or lost.

    The wallet holds every seed and key, so the two windows an ordinary atomic
    write closes matter more here than anywhere else: the file must never exist
    at its real path with default permissions, and it must be on disk before the
    directory entry points at it. PolicyStore has written this sequence down for
    a while; the wallet was chmod-ing after the move and never flushing.
    """

    def test_a_save_leaves_no_temporary_file_behind(self, temp_data_dir,
                                                    valid_seed_phrase):
        path = temp_data_dir / "test.wallet"
        saved_wallet(path, valid_seed_phrase)
        assert [f.name for f in temp_data_dir.iterdir()] == ["test.wallet"]

    def test_repeated_saves_leave_no_litter(self, temp_data_dir, valid_seed_phrase):
        """The old temp name was fixed, so two saves shared one path.

        Two files are the expected steady state: the wallet, and the
        `.previous` copy each save keeps of the version it replaces (the
        file-level recovery path for a damaged wallet). Litter means
        anything beyond those two - orphaned temp files in particular.
        """
        path = temp_data_dir / "test.wallet"
        wallet, _ = saved_wallet(path, valid_seed_phrase)
        wallet.save(path)
        wallet.save(path)
        assert sorted(f.name for f in temp_data_dir.iterdir()) == [
            "test.wallet", "test.wallet.previous"]

    def test_a_dotted_wallet_name_survives_the_save(self, temp_data_dir,
                                                    valid_seed_phrase):
        """`with_suffix` replaces the extension rather than appending, so a
        name like `my.backup.wallet` needs its temporary file built by
        appending."""
        path = temp_data_dir / "my.backup.wallet"
        saved_wallet(path, valid_seed_phrase)

        assert path.exists()
        assert [f.name for f in temp_data_dir.iterdir()] == ["my.backup.wallet"]
        assert VaultWallet.load(path, PASSWORD) is not None

    def test_a_failed_save_leaves_the_previous_wallet_intact(self, temp_data_dir,
                                                             valid_seed_phrase,
                                                             monkeypatch):
        """The failure mode that matters. Writing over the target directly would
        destroy a good wallet to produce a broken one."""
        import json as json_module

        path = temp_data_dir / "test.wallet"
        wallet, _ = saved_wallet(path, valid_seed_phrase)
        before = path.read_text(encoding="utf-8")

        def out_of_space(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(json_module, "dump", out_of_space)
        with pytest.raises(OSError):
            wallet.save(path)
        monkeypatch.undo()

        assert path.read_text(encoding="utf-8") == before
        assert VaultWallet.load(path, PASSWORD) is not None
        assert [f.name for f in temp_data_dir.iterdir()] == ["test.wallet"], (
            "a half-written wallet was left in the data directory")

    @pytest.mark.skipif(os.name == "nt", reason="Unix permissions only")
    def test_the_saved_wallet_is_owner_only(self, temp_data_dir, valid_seed_phrase):
        path = temp_data_dir / "test.wallet"
        saved_wallet(path, valid_seed_phrase)
        assert os.stat(path).st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="Unix permissions only")
    def test_permissions_are_set_before_the_file_takes_its_name(
            self, temp_data_dir, valid_seed_phrase, monkeypatch):
        """The ordering, asserted directly rather than through its result.

        Checking the mode afterwards passes either way - chmod-after-replace
        ends in the same state, having been briefly world-readable at the real
        path on the way. What has to be true is that the file is already 0600
        when os.replace runs.
        """
        import os as os_module

        path = temp_data_dir / "test.wallet"
        modes = []
        real_replace = os_module.replace

        def watched(src, dst, *args, **kwargs):
            modes.append(os_module.stat(src).st_mode & 0o777)
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os_module, "replace", watched)
        saved_wallet(path, valid_seed_phrase)
        monkeypatch.undo()

        assert modes, "the save did not go through os.replace"
        assert all(m == 0o600 for m in modes), (
            f"the wallet was moved into place with mode(s) {[oct(m) for m in modes]}")
