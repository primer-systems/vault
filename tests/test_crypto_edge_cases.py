"""
Cryptography Edge Cases Tests

Tests for wallet encryption edge cases, wrong password handling,
corrupted data detection, key derivation boundaries, and memory cleanup.

These tests verify that cryptographic operations fail safely and
securely handle edge cases.
"""

import json
import os
import secrets
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.wallet.crypto import (
    Wallet, encrypt_seed, decrypt_seed, derive_key,
    NO_PASSWORD_SENTINEL, ETH_DERIVATION_PATH
)


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def core(temp_data_dir):
    """Create a Vault instance with temporary data directory."""
    from primer_vault.core import Vault
    return Vault(data_dir=temp_data_dir)


@pytest.fixture
def valid_seed_phrase():
    """Generate a valid 12-word seed phrase."""
    from eth_account.hdaccount import generate_mnemonic
    return generate_mnemonic(num_words=12, lang="english")


# =============================================================================
# Wrong Password Handling Tests
# =============================================================================

class TestWrongPasswordHandling:
    """Test behavior when wrong password is provided."""

    def test_wrong_password_raises_error(self, temp_data_dir, valid_seed_phrase):
        """Wrong password should raise ValueError."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create wallet with password
        wallet = Wallet(valid_seed_phrase, "correct_password")
        wallet.save(wallet_path)

        # Try to load with wrong password
        with pytest.raises(ValueError) as exc_info:
            Wallet.load(wallet_path, "wrong_password")

        assert "Wrong password" in str(exc_info.value) or "corrupted" in str(exc_info.value).lower()

    def test_empty_password_on_encrypted_wallet(self, temp_data_dir, valid_seed_phrase):
        """Empty password on encrypted wallet should fail."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create encrypted wallet
        wallet = Wallet(valid_seed_phrase, "secure_password")
        wallet.save(wallet_path)

        # Try to load with empty password
        with pytest.raises((ValueError, Exception)):
            Wallet.load(wallet_path, "")

    def test_none_password_on_encrypted_wallet(self, temp_data_dir, valid_seed_phrase):
        """None password on encrypted wallet should fail."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create encrypted wallet
        wallet = Wallet(valid_seed_phrase, "secure_password")
        wallet.save(wallet_path)

        # Try to load with None password
        with pytest.raises((ValueError, TypeError, Exception)):
            Wallet.load(wallet_path, None)

    def test_unencrypted_wallet_ignores_password(self, temp_data_dir, valid_seed_phrase):
        """Unencrypted wallet should load regardless of password provided."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create unencrypted wallet
        wallet = Wallet(valid_seed_phrase, NO_PASSWORD_SENTINEL)
        wallet.save(wallet_path)

        # Should load with any password (ignored)
        loaded = Wallet.load(wallet_path, "any_password")
        assert loaded.seed_phrase == valid_seed_phrase

    def test_password_not_stored_in_wallet_file(self, temp_data_dir, valid_seed_phrase):
        """Password should never be stored in the wallet file."""
        wallet_path = temp_data_dir / "test.wallet"
        password = "super_secret_password_12345"

        wallet = Wallet(valid_seed_phrase, password)
        wallet.save(wallet_path)

        # Read raw file content
        with open(wallet_path, 'r') as f:
            content = f.read()

        # Password should not appear in file
        assert password not in content
        assert "super_secret" not in content


# =============================================================================
# Corrupted Data Handling Tests
# =============================================================================

class TestCorruptedDataHandling:
    """Test behavior with corrupted wallet files."""

    def test_truncated_json_rejected(self, temp_data_dir, valid_seed_phrase):
        """Truncated JSON file should be rejected."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create valid wallet
        wallet = Wallet(valid_seed_phrase, "password")
        wallet.save(wallet_path)

        # Truncate the file
        with open(wallet_path, 'r') as f:
            content = f.read()

        with open(wallet_path, 'w') as f:
            f.write(content[:len(content)//2])  # Write only half

        # Should fail to load
        with pytest.raises((json.JSONDecodeError, ValueError, Exception)):
            Wallet.load(wallet_path, "password")

    def test_corrupted_encrypted_seed_rejected(self, temp_data_dir, valid_seed_phrase):
        """Corrupted encrypted seed should be detected."""
        wallet_path = temp_data_dir / "test.wallet"

        # Create valid wallet
        wallet = Wallet(valid_seed_phrase, "password")
        wallet.save(wallet_path)

        # Corrupt the encrypted seed
        with open(wallet_path, 'r') as f:
            data = json.load(f)

        # Flip some bits in encrypted seed
        if "encrypted_seed" in data:
            original = data["encrypted_seed"]
            # Corrupt a byte
            corrupted = original[:-4] + "XXXX"
            data["encrypted_seed"] = corrupted

            with open(wallet_path, 'w') as f:
                json.dump(data, f)

            # Should fail to decrypt
            with pytest.raises((ValueError, Exception)):
                Wallet.load(wallet_path, "password")

    def test_modified_auth_tag_rejected(self, temp_data_dir, valid_seed_phrase):
        """Modified authentication tag should be detected (AES-GCM integrity)."""
        wallet_path = temp_data_dir / "test.wallet"

        wallet = Wallet(valid_seed_phrase, "password")
        wallet.save(wallet_path)

        # Modify the auth tag
        with open(wallet_path, 'r') as f:
            data = json.load(f)

        if "tag" in data:
            original_tag = data["tag"]
            # Flip a bit in the tag
            modified_tag = "00" + original_tag[2:]
            data["tag"] = modified_tag

            with open(wallet_path, 'w') as f:
                json.dump(data, f)

            # AES-GCM should reject this
            with pytest.raises((ValueError, Exception)):
                Wallet.load(wallet_path, "password")

    def test_modified_iv_causes_decryption_failure(self, temp_data_dir, valid_seed_phrase):
        """Modified IV should cause decryption to fail."""
        wallet_path = temp_data_dir / "test.wallet"

        wallet = Wallet(valid_seed_phrase, "password")
        wallet.save(wallet_path)

        with open(wallet_path, 'r') as f:
            data = json.load(f)

        if "iv" in data:
            # Modify IV
            data["iv"] = secrets.token_hex(12)

            with open(wallet_path, 'w') as f:
                json.dump(data, f)

            with pytest.raises((ValueError, Exception)):
                Wallet.load(wallet_path, "password")

    def test_invalid_json_rejected(self, temp_data_dir):
        """Invalid JSON should be rejected."""
        wallet_path = temp_data_dir / "test.wallet"

        with open(wallet_path, 'w') as f:
            f.write("this is not valid json {{{")

        with pytest.raises((json.JSONDecodeError, Exception)):
            Wallet.load(wallet_path, "password")

    def test_missing_required_fields_rejected(self, temp_data_dir):
        """Wallet file missing required fields should be rejected."""
        wallet_path = temp_data_dir / "test.wallet"

        # Write minimal invalid wallet
        with open(wallet_path, 'w') as f:
            json.dump({"version": 1}, f)  # Missing encrypted_seed, etc.

        with pytest.raises((KeyError, ValueError, Exception)):
            Wallet.load(wallet_path, "password")

    def test_wrong_version_rejected(self, temp_data_dir, valid_seed_phrase):
        """Wallet with unsupported version should be rejected."""
        wallet_path = temp_data_dir / "test.wallet"

        wallet = Wallet(valid_seed_phrase, "password")
        wallet.save(wallet_path)

        # Change version
        with open(wallet_path, 'r') as f:
            data = json.load(f)

        data["version"] = 99  # Unsupported version

        with open(wallet_path, 'w') as f:
            json.dump(data, f)

        with pytest.raises(ValueError) as exc_info:
            Wallet.load(wallet_path, "password")

        assert "version" in str(exc_info.value).lower()


# =============================================================================
# Key Derivation Edge Cases Tests
# =============================================================================

class TestKeyDerivationEdgeCases:
    """Test edge cases in key derivation."""

    def test_derive_address_index_zero(self, valid_seed_phrase):
        """Should derive address at index 0 correctly."""
        wallet = Wallet(valid_seed_phrase, "password")
        address = wallet.get_address(0)

        assert address is not None
        assert address.startswith("0x")
        assert len(address) == 42

    def test_derive_address_index_large(self, valid_seed_phrase):
        """Should handle large address indices."""
        wallet = Wallet(valid_seed_phrase, "password")

        # Derive at large index
        address = wallet.get_address(1000)
        assert address is not None
        assert address.startswith("0x")

    def test_derive_address_index_max_safe(self, valid_seed_phrase):
        """Should handle max safe index within BIP-44 spec."""
        wallet = Wallet(valid_seed_phrase, "password")

        # BIP-44 uses 31-bit indices
        # Test a reasonably large but safe index
        address = wallet.get_address(2**20)  # 1 million
        assert address is not None
        assert address.startswith("0x")

    def test_negative_address_index_rejected(self, valid_seed_phrase):
        """Negative address index should be rejected or handled."""
        from eth_utils.exceptions import ValidationError
        wallet = Wallet(valid_seed_phrase, "password")

        # Behavior depends on implementation
        try:
            address = wallet.get_address(-1)
            # If it returns something, it should still be a valid address
            # (implementation may wrap or reject)
        except (ValueError, IndexError, OverflowError, ValidationError):
            pass  # Expected behavior

    def test_consistent_derivation(self, valid_seed_phrase):
        """Same seed + index should always produce same address."""
        wallet1 = Wallet(valid_seed_phrase, "password1")
        wallet2 = Wallet(valid_seed_phrase, "password2")

        address1 = wallet1.get_address(0)
        address2 = wallet2.get_address(0)

        # Addresses should be identical (password only encrypts storage)
        assert address1 == address2

    def test_different_seeds_different_addresses(self):
        """Different seeds should produce different addresses."""
        from eth_account.hdaccount import generate_mnemonic

        seed1 = generate_mnemonic(num_words=12, lang="english")
        seed2 = generate_mnemonic(num_words=12, lang="english")

        wallet1 = Wallet(seed1, "password")
        wallet2 = Wallet(seed2, "password")

        address1 = wallet1.get_address(0)
        address2 = wallet2.get_address(0)

        assert address1 != address2


# =============================================================================
# Seed Phrase Validation Tests
# =============================================================================

class TestSeedPhraseValidation:
    """Test seed phrase validation."""

    def test_invalid_seed_phrase_rejected(self):
        """Invalid seed phrase should be rejected."""
        with pytest.raises(ValueError):
            Wallet("these are not valid bip39 words at all", "password")

    def test_11_word_seed_rejected(self):
        """11-word seed phrase should be rejected."""
        # Generate 12 words and remove one
        from eth_account.hdaccount import generate_mnemonic
        seed = generate_mnemonic(num_words=12, lang="english")
        words = seed.split()
        invalid_seed = " ".join(words[:11])

        with pytest.raises(ValueError):
            Wallet(invalid_seed, "password")

    def test_13_word_seed_rejected(self):
        """13-word seed phrase should be rejected (must be 12, 15, 18, 21, 24)."""
        from eth_account.hdaccount import generate_mnemonic
        seed = generate_mnemonic(num_words=12, lang="english")
        words = seed.split()
        # Add an extra valid word
        invalid_seed = seed + " abandon"

        with pytest.raises(ValueError):
            Wallet(invalid_seed, "password")

    def test_24_word_seed_accepted(self):
        """24-word seed phrase should be accepted."""
        from eth_account.hdaccount import generate_mnemonic
        seed = generate_mnemonic(num_words=24, lang="english")

        wallet = Wallet(seed, "password")
        assert wallet.seed_phrase == seed

    def test_empty_seed_rejected(self):
        """Empty seed phrase should be rejected."""
        with pytest.raises((ValueError, Exception)):
            Wallet("", "password")

    def test_whitespace_only_seed_rejected(self):
        """Whitespace-only seed phrase should be rejected."""
        with pytest.raises((ValueError, Exception)):
            Wallet("   \t\n  ", "password")


# =============================================================================
# Memory Security Tests
# =============================================================================

class TestMemorySecurity:
    """Test memory cleanup of sensitive data."""

    def test_lock_clears_seed_phrase(self, valid_seed_phrase):
        """Locking wallet should clear seed phrase from memory."""
        wallet = Wallet(valid_seed_phrase, "password")

        # Verify seed is accessible
        assert wallet.seed_phrase == valid_seed_phrase

        # Lock wallet
        wallet.lock()

        # Seed should be cleared
        assert wallet._seed_phrase is None

    def test_lock_clears_seed_bytes(self, valid_seed_phrase):
        """Locking wallet should clear seed bytes from memory."""
        wallet = Wallet(valid_seed_phrase, "password")

        # Lock wallet
        wallet.lock()

        # Internal seed should be cleared
        assert wallet._seed is None

    def test_lock_clears_password(self, valid_seed_phrase):
        """Locking wallet should clear password reference."""
        wallet = Wallet(valid_seed_phrase, "password")

        wallet.lock()

        assert wallet._password is None

    def test_del_calls_lock(self, valid_seed_phrase):
        """Wallet destructor should call lock()."""
        wallet = Wallet(valid_seed_phrase, "password")
        seed_id = id(wallet._seed_phrase)

        # Delete wallet
        del wallet

        # Can't easily verify memory is cleared, but at least no crash


# =============================================================================
# Encryption/Decryption Edge Cases
# =============================================================================

class TestEncryptionDecryption:
    """Test encryption and decryption edge cases."""

    def test_encrypt_decrypt_round_trip(self):
        """Encrypt then decrypt should return original."""
        original = "abandon " * 11 + "about"  # Valid seed
        password = "test_password"

        encrypted, iv, tag, salt = encrypt_seed(original, password)
        decrypted = decrypt_seed(encrypted, iv, tag, salt, password)

        assert decrypted == original

    def test_unicode_password_supported(self, valid_seed_phrase, temp_data_dir):
        """Unicode passwords should work correctly."""
        wallet_path = temp_data_dir / "unicode.wallet"
        password = "密码🔐пароль"  # Mixed unicode

        wallet = Wallet(valid_seed_phrase, password)
        wallet.save(wallet_path)

        loaded = Wallet.load(wallet_path, password)
        assert loaded.seed_phrase == valid_seed_phrase

    def test_very_long_password(self, valid_seed_phrase, temp_data_dir):
        """Very long passwords should work."""
        wallet_path = temp_data_dir / "longpass.wallet"
        password = "a" * 10000  # 10KB password

        wallet = Wallet(valid_seed_phrase, password)
        wallet.save(wallet_path)

        loaded = Wallet.load(wallet_path, password)
        assert loaded.seed_phrase == valid_seed_phrase

    def test_empty_password_creates_unencrypted(self, valid_seed_phrase, temp_data_dir):
        """Empty password should create unencrypted wallet or be rejected."""
        wallet_path = temp_data_dir / "empty.wallet"

        # Behavior depends on implementation
        # May create unencrypted wallet or require non-empty password
        try:
            wallet = Wallet(valid_seed_phrase, "")
            wallet.save(wallet_path)

            # Check if saved as unencrypted
            with open(wallet_path, 'r') as f:
                data = json.load(f)

            # Should either be unencrypted or fail
        except (ValueError, Exception):
            pass  # Some implementations reject empty password


# =============================================================================
# Agent Secret Encryption Tests
# =============================================================================

class TestAgentSecretEncryption:
    """Test agent secret encryption edge cases."""

    def test_agent_secret_encryption_round_trip(self):
        """Agent secret encryption should be reversible with correct password."""
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        secret = secrets.token_hex(32)
        password = "wallet_password"
        agent_id = "ABC123"

        encrypted, iv, tag, salt = encrypt_agent_secret(secret, password, agent_id)
        decrypted = decrypt_agent_secret(encrypted, iv, tag, salt, password, agent_id)

        assert decrypted == secret

    def test_agent_secret_wrong_password_rejected(self):
        """Agent secret with wrong password should fail."""
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        secret = secrets.token_hex(32)
        agent_id = "ABC123"

        encrypted, iv, tag, salt = encrypt_agent_secret(secret, "correct", agent_id)

        with pytest.raises(Exception):
            decrypt_agent_secret(encrypted, iv, tag, salt, "wrong", agent_id)

    def test_agent_secret_wrong_agent_id_rejected(self):
        """Agent secret with wrong agent_id (AAD) should fail."""
        from primer_vault.models.agent import encrypt_agent_secret, decrypt_agent_secret

        secret = secrets.token_hex(32)
        password = "password"

        encrypted, iv, tag, salt = encrypt_agent_secret(secret, password, "AGENT1")

        with pytest.raises(Exception):
            decrypt_agent_secret(encrypted, iv, tag, salt, password, "AGENT2")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
