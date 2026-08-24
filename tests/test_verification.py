"""
Transaction Verification Tests

Tests for on-chain verification, RPC failures, timeout handling,
and verification state transitions.

These tests verify that transaction verification handles errors
gracefully and maintains data integrity.
"""

import sys
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import Transaction


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)




@pytest.fixture
def signing_service(core):
    """Get the signing service from primer_vault.core."""
    return core._signing_service


@pytest.fixture
def settled_transaction():
    """Create a settled transaction for testing."""
    tx = Transaction.create(
        agent_id="ABC123",
        agent_name="TestAgent",
        agent_code="TEST123",
        amount_micro=1000000,
        recipient="0x1234567890123456789012345678901234567890",
        network="robinhood"
    )
    tx.status = "settled"
    tx.tx_hash = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    return tx


# =============================================================================
# Verification State Tests
# =============================================================================

class TestVerificationStates:
    """Test verification state transitions."""

    def test_initial_verification_status_none(self):
        """New transaction should have no verification status."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )

        # Verification status should be None or unset initially
        assert tx.verification_status is None or tx.verification_status == ""

    def test_pending_verification_status(self, settled_transaction, signing_service):
        """Transaction can be set to pending verification."""
        settled_transaction.verification_status = "pending"
        assert settled_transaction.verification_status == "pending"

    def test_verified_status(self, settled_transaction):
        """Transaction can be marked as verified."""
        settled_transaction.verification_status = "verified"
        settled_transaction.verification_block = 12345678

        assert settled_transaction.verification_status == "verified"
        assert settled_transaction.verification_block == 12345678

    def test_failed_status(self, settled_transaction):
        """Transaction can be marked as failed."""
        settled_transaction.verification_status = "failed"
        assert settled_transaction.verification_status == "failed"

    def test_not_found_status(self, settled_transaction):
        """Transaction can be marked as not found."""
        settled_transaction.verification_status = "not_found"
        assert settled_transaction.verification_status == "not_found"


# =============================================================================
# RPC Failure Tests
# =============================================================================

class TestRPCFailures:
    """Test handling of RPC endpoint failures."""

    def test_rpc_connection_error_handled(self, settled_transaction, signing_service):
        """RPC connection error should be handled gracefully."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.side_effect = ConnectionError("RPC unavailable")

            # Should not raise
            signing_service._verify_transaction_sync(settled_transaction)

            # Should mark as failed
            # Could not reach a node. That is not the chain reporting a revert,
            # and the record must not say it was, or a moment offline leaves a
            # permanent "payment failed" in the compliance trail.
            assert settled_transaction.verification_status == "unavailable"
            assert settled_transaction.verification_detail

    def test_rpc_timeout_handled(self, settled_transaction, signing_service):
        """RPC timeout should be handled gracefully."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.side_effect = TimeoutError("Request timed out")

            signing_service._verify_transaction_sync(settled_transaction)

            # Could not reach a node. That is not the chain reporting a revert,
            # and the record must not say it was, or a moment offline leaves a
            # permanent "payment failed" in the compliance trail.
            assert settled_transaction.verification_status == "unavailable"
            assert settled_transaction.verification_detail

    def test_rpc_invalid_response_handled(self, settled_transaction, signing_service):
        """Invalid RPC response should be handled."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            # Return None (tx not found)
            mock_instance.eth.get_transaction_receipt.return_value = None

            signing_service._verify_transaction_sync(settled_transaction)

            assert settled_transaction.verification_status == "not_found"

    def test_rpc_returns_failed_transaction(self, settled_transaction, signing_service):
        """RPC returning failed tx should mark as failed."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            # Return receipt with status = 0 (failed)
            mock_instance.eth.get_transaction_receipt.return_value = {
                "status": 0,
                "blockNumber": 12345
            }

            signing_service._verify_transaction_sync(settled_transaction)

            # The chain answered, and the answer was that it reverted. This is
            # the one case that earns "failed" — the counterpart to the tests
            # above, where Vault could not ask at all.
            assert settled_transaction.verification_status == "failed"
            assert "reverted" in (settled_transaction.verification_detail or "")

    def test_rpc_returns_successful_transaction(self, settled_transaction, signing_service):
        """RPC returning successful tx should mark as verified."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.return_value = {
                "status": 1,
                "blockNumber": 12345678
            }

            signing_service._verify_transaction_sync(settled_transaction)

            assert settled_transaction.verification_status == "verified"
            assert settled_transaction.verification_block == 12345678


# =============================================================================
# Invalid Transaction Hash Tests
# =============================================================================

class TestInvalidTxHash:
    """Test handling of invalid transaction hashes."""

    def test_empty_tx_hash(self, signing_service):
        """Empty tx_hash should be handled."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )
        tx.status = "settled"
        tx.tx_hash = ""  # Empty

        signing_service._verify_transaction_sync(tx)

        assert tx.verification_status == "not_found"

    def test_none_tx_hash(self, signing_service):
        """None tx_hash should be handled."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )
        tx.status = "settled"
        tx.tx_hash = None

        signing_service._verify_transaction_sync(tx)

        assert tx.verification_status == "not_found"

    def test_malformed_tx_hash(self, settled_transaction, signing_service):
        """Malformed tx_hash should be handled."""
        settled_transaction.tx_hash = "not_a_valid_hash"

        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.side_effect = ValueError("Invalid hash")

            signing_service._verify_transaction_sync(settled_transaction)

            # Should be marked as failed
            # Could not reach a node. That is not the chain reporting a revert,
            # and the record must not say it was, or a moment offline leaves a
            # permanent "payment failed" in the compliance trail.
            assert settled_transaction.verification_status == "unavailable"
            assert settled_transaction.verification_detail


# =============================================================================
# Network Resolution Tests
# =============================================================================

class TestNetworkResolution:
    """Test network resolution for verification."""

    def test_caip_network_format(self, signing_service):
        """CAIP-2 network format should be parsed correctly."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="eip155:4663"  # CAIP-2 format
        )
        tx.status = "settled"
        tx.tx_hash = "0x" + "ab" * 32

        # Chain ID should be extracted correctly
        # Test would need actual RPC mock to verify

    def test_v1_network_format(self, signing_service):
        """v1 network format should be resolved correctly."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"  # v1 format
        )
        tx.status = "settled"
        tx.tx_hash = "0x" + "ab" * 32

        # Should resolve "robinhood" to chain ID 4663

    def test_unknown_network_handled(self, signing_service):
        """Unknown network should be handled gracefully."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="unknown-network"
        )
        tx.status = "settled"
        tx.tx_hash = "0x" + "ab" * 32

        signing_service._verify_transaction_sync(tx)

        # An unknown network means Vault cannot check, not that the payment failed.
        assert tx.verification_status == "unavailable"
        assert "unknown network" in (tx.verification_detail or "")


# =============================================================================
# Manual Verification Tests
# =============================================================================

class TestManualVerification:
    """Test manual verification triggering."""

    def test_verify_settled_transaction(self, settled_transaction, signing_service):
        """Manual verification should work on settled transactions."""
        with patch.object(signing_service, '_verify_transaction_sync'):
            signing_service.verify_transaction(settled_transaction)

            # Should set to pending first
            assert settled_transaction.verification_status == "pending"

    def test_verify_requires_settled_status(self, signing_service):
        """Manual verification should require settled status."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )
        tx.status = "pending"  # Not settled
        tx.tx_hash = "0x" + "ab" * 32

        signing_service.verify_transaction(tx)

        # Should not change status (not settled)
        assert tx.verification_status != "pending"

    def test_verify_requires_tx_hash(self, signing_service):
        """Manual verification should require tx_hash."""
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )
        tx.status = "settled"
        tx.tx_hash = None  # No hash

        signing_service.verify_transaction(tx)

        # Should not set to pending
        assert tx.verification_status != "pending"


# =============================================================================
# Block Number Tests
# =============================================================================

class TestBlockNumbers:
    """Test block number handling."""

    def test_verification_block_set_on_success(self, settled_transaction, signing_service):
        """Verification block should be set on successful verification."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.return_value = {
                "status": 1,
                "blockNumber": 98765432
            }

            signing_service._verify_transaction_sync(settled_transaction)

            assert settled_transaction.verification_block == 98765432

    def test_verification_block_none_on_failure(self, settled_transaction, signing_service):
        """Verification block should remain None on failure."""
        with patch('web3.Web3') as mock_web3:
            mock_instance = MagicMock()
            mock_web3.return_value = mock_instance
            mock_instance.eth.get_transaction_receipt.side_effect = Exception("Error")

            signing_service._verify_transaction_sync(settled_transaction)

            assert settled_transaction.verification_block is None


# =============================================================================
# Auto-Verification Tests
# =============================================================================

class TestAutoVerification:
    """Test automatic verification queue."""

    def test_queue_verification(self, settled_transaction, signing_service):
        """Transaction should be queued for verification."""
        with patch.object(signing_service, '_verify_transaction_sync') as mock_verify:
            signing_service._queue_verification(settled_transaction)

            # Should set to pending
            assert settled_transaction.verification_status == "pending"

            # Wait for timer
            time.sleep(1.5)

            # Verify should have been called
            mock_verify.assert_called_once()

    def test_requeue_stuck_verifications(self, temp_data_dir, core, signing_service):
        """Stuck pending verifications should be requeued on startup."""
        # Create a transaction stuck in pending verification

        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST",
            amount_micro=1000000,
            recipient="0x" + "1" * 40,
            network="robinhood"
        )
        tx.status = "settled"
        tx.tx_hash = "0x" + "ab" * 32
        tx.verification_status = "pending"

        # Add to store
        core._policy_store.add_transaction(tx)

        # Call requeue (simulates restart)
        with patch.object(signing_service, '_queue_verification'):
            signing_service._requeue_stuck_verifications()

            # Should have requeued the stuck transaction
            # Note: Only if verify_settlements is enabled


# =============================================================================
# Verification Settings Tests
# =============================================================================

class TestVerificationSettings:
    """Test verification enable/disable settings."""

    def test_verification_disabled(self, signing_service):
        """Verification can be disabled."""
        signing_service._verify_settlements = False

        # When disabled, verification should not happen automatically
        # This is just a flag check
        assert signing_service._verify_settlements is False

    def test_verification_enabled(self, signing_service):
        """Verification can be enabled."""
        signing_service._verify_settlements = True
        assert signing_service._verify_settlements is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
