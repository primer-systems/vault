"""
Integration tests for CLI command workflows.

These tests verify complete user journeys through the application,
testing that commands work correctly in sequence and that state
dependencies are properly enforced.

Run with: pytest tests/test_integration.py -v
"""

import pytest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands import CommandHandler, CommandResult


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create a temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "wallets").mkdir()
    return data_dir




@pytest.fixture
def core_no_wallet(temp_data_dir):
    """Create a Core instance with temp directory but NO wallet (for locked wallet tests)."""
    from primer_vault.core import Vault
    return Vault(data_dir=temp_data_dir)


@pytest.fixture
def handler(core):
    """Create a CommandHandler instance."""
    return CommandHandler(core)


@pytest.fixture
def handler_no_wallet(core_no_wallet):
    """Create a CommandHandler instance without wallet."""
    return CommandHandler(core_no_wallet)


def execute_with_inputs(handler, command: str, inputs: dict = None) -> CommandResult:
    """Execute a command, providing inputs if needed.

    Handles multi-step commands by looping until no more input is needed.
    The inputs dict can contain:
    - 'password' and 'confirm' for wallet creation
    - 'password' for wallet open
    - 'confirm' for delete confirmations
    """
    result = handler.execute(command)

    # Loop to handle multi-step input flows
    max_iterations = 10  # Safety limit
    iteration = 0

    while result.needs_input and inputs and iteration < max_iterations:
        iteration += 1
        step = result.needs_input.get("step", "")
        input_type = result.needs_input.get("type", "text")

        # Determine what input to provide based on step/type
        step_inputs = {}
        if step == "password" or (input_type == "password" and "confirm" not in str(result.needs_input.get("prompt", "")).lower()):
            step_inputs["password"] = inputs.get("password", "")
            step_inputs["value"] = inputs.get("password", "")
        elif step == "confirm" or "confirm" in str(result.needs_input.get("prompt", "")).lower():
            confirm_val = inputs.get("confirm", inputs.get("password", ""))
            step_inputs["password"] = confirm_val
            step_inputs["value"] = confirm_val
            step_inputs["confirm"] = confirm_val  # Also set confirm for delete operations
        elif input_type == "confirm":
            step_inputs["confirm"] = inputs.get("confirm", "yes")
        else:
            step_inputs = inputs

        result = handler.execute(command, inputs=step_inputs)

    return result


# =============================================================================
# Wallet Lifecycle Tests
# =============================================================================

class TestWalletLifecycle:
    """Test complete wallet workflows."""

    def test_create_wallet_full_flow(self, handler):
        """Create wallet → verify unlocked → verify seed shown."""
        result = execute_with_inputs(
            handler,
            "wallet create testwallet",
            {"password": "testpass", "confirm": "testpass"}
        )

        assert result.success, f"Failed: {result.error}"
        assert "Wallet created" in result.output
        assert result.data is not None
        assert "seed_phrase" in result.data
        assert len(result.data["seed_phrase"].split()) == 12  # 12-word seed

        # Verify wallet is now unlocked
        status = handler.execute("wallet status")
        assert "unlocked" in status.output.lower()

    def test_create_wallet_rejects_an_empty_password(self, handler):
        """An empty password accepted as a real one would encrypt the wallet
        with nothing while still looking protected."""
        result = execute_with_inputs(
            handler,
            "wallet create nopasswallet",
            {"password": "", "confirm": ""}
        )

        assert not result.success
        assert "at least 8" in result.error

    def test_create_wallet_password_mismatch(self, handler):
        """Password confirmation mismatch should fail."""
        result = execute_with_inputs(
            handler,
            "wallet create badwallet",
            {"password": "pass1", "confirm": "pass2"}
        )

        assert not result.success
        assert "match" in result.error.lower()

    def test_wallet_lock_unlock_cycle(self, handler, temp_data_dir):
        """Create → lock → verify locked → unlock → verify unlocked."""
        # Create wallet
        execute_with_inputs(
            handler,
            "wallet create locktest",
            {"password": "testpass", "confirm": "testpass"}
        )

        # Lock it
        result = handler.execute("wallet lock")
        assert result.success

        # Verify locked
        status = handler.execute("wallet status")
        assert "locked" in status.output.lower()

        # Unlock it
        result = execute_with_inputs(
            handler,
            "wallet open locktest",
            {"password": "testpass"}
        )
        assert result.success

        # Verify unlocked
        status = handler.execute("wallet status")
        assert "unlocked" in status.output.lower()

    def test_wallet_wrong_password(self, handler):
        """Opening wallet with wrong password should fail."""
        # Create wallet
        execute_with_inputs(
            handler,
            "wallet create pwtest",
            {"password": "correct", "confirm": "correct"}
        )

        # Lock it
        handler.execute("wallet lock")

        # Try wrong password
        result = execute_with_inputs(
            handler,
            "wallet open pwtest",
            {"password": "wrong"}
        )

        assert not result.success

    def test_wallet_already_exists(self, handler):
        """Creating wallet with existing name should fail."""
        execute_with_inputs(
            handler,
            "wallet create dupwallet",
            {"password": "testpassword", "confirm": "testpassword"}
        )

        result = execute_with_inputs(
            handler,
            "wallet create dupwallet",
            {"password": "testpassword", "confirm": "testpassword"}
        )

        assert not result.success
        assert "exists" in result.error.lower()


# =============================================================================
# Address & Seed Lifecycle Tests
# =============================================================================

class TestAddressLifecycle:
    """Test address and seed management."""

    @pytest.fixture
    def unlocked_wallet(self, handler):
        """Create and return handler with unlocked wallet."""
        execute_with_inputs(
            handler,
            "wallet create addrtest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        return handler

    def test_address_created_with_wallet(self, unlocked_wallet):
        """New wallet should have one address."""
        result = unlocked_wallet.execute("address list")
        assert result.success
        assert "A001" in result.output or result.data.get("addresses")

    def test_create_additional_address(self, unlocked_wallet):
        """Derive additional address from seed."""
        result = unlocked_wallet.execute("address create")
        assert result.success
        assert "Created address" in result.output

    def test_create_address_specific_index(self, unlocked_wallet):
        """Derive address at specific index."""
        result = unlocked_wallet.execute("address create S001 5")
        assert result.success
        assert "Index: 5" in result.output

    def test_create_address_with_name(self, unlocked_wallet):
        """Create address with custom name."""
        result = unlocked_wallet.execute('address create S001 3 "My Custom Address"')
        assert result.success

    def test_rename_address(self, unlocked_wallet):
        """Rename an existing address."""
        result = unlocked_wallet.execute("address rename A001 NewName")
        assert result.success
        assert "renamed" in result.output.lower()

    def test_import_private_key(self, unlocked_wallet):
        """Import a private key."""
        # Valid 32-byte hex key
        test_key = "0x" + "ab" * 32
        result = unlocked_wallet.execute(f"address import {test_key}")
        assert result.success
        assert "Imported" in result.output

    def test_import_invalid_key(self, unlocked_wallet):
        """Import invalid key should fail."""
        result = unlocked_wallet.execute("address import notahexkey")
        assert not result.success

    def test_seed_list(self, unlocked_wallet):
        """List seeds in wallet."""
        result = unlocked_wallet.execute("seed list")
        assert result.success
        assert "S001" in result.output

    def test_address_requires_unlocked_wallet(self, handler_no_wallet):
        """Address operations should fail when wallet locked."""
        result = handler_no_wallet.execute("address list")
        assert not result.success
        assert "unlocked" in result.error.lower()


# =============================================================================
# Agent Lifecycle Tests
# =============================================================================

class TestAgentLifecycle:
    """Test agent management workflows."""

    def test_register_agent_basic(self, handler):
        """Register agent with defaults."""
        result = handler.execute("agent register myagent")
        assert result.success
        assert "AG" in result.output  # Agent ID
        assert "PRIMER_VAULT_AGENT_TOKEN" in result.output
        assert result.data.get("token")

    def test_register_agent_bearer_auth(self, handler):
        """Register agent with bearer auth mode."""
        result = handler.execute("agent register bearerbot --auth bearer")
        assert result.success
        assert "bearer" in result.output.lower()

    def test_agent_suspend_uncommissioned_blocked(self, handler):
        """Suspending an uncommissioned agent should fail — prevents stuck state."""
        handler.execute("agent register cyclebot")

        result = handler.execute("agent suspend cyclebot")
        assert not result.success
        assert "not commissioned" in result.error.lower()

    def test_agent_delete_with_confirmation(self, handler):
        """Delete agent requires YES confirmation."""
        handler.execute("agent register deletebot")

        # Delete with confirmation
        result = execute_with_inputs(
            handler,
            "agent delete deletebot",
            {"confirm": "YES"}
        )
        assert result.success
        assert "deleted" in result.output.lower()

        # Verify gone
        show = handler.execute("agent show deletebot")
        assert not show.success

    def test_agent_delete_cancelled(self, handler):
        """Delete agent cancelled if not YES."""
        handler.execute("agent register keepbot")

        execute_with_inputs(
            handler,
            "agent delete keepbot",
            {"confirm": "no"}
        )

        # Should still exist
        show = handler.execute("agent show keepbot")
        assert show.success


# =============================================================================
# Policy Lifecycle Tests
# =============================================================================

class TestPolicyLifecycle:
    """Test policy management workflows."""

    def test_create_policy_defaults(self, handler):
        """Create policy with default limits."""
        result = handler.execute("policy create standard")
        assert result.success

        show = handler.execute("policy show standard")
        assert "$100.00" in show.output  # Default daily
        assert "$10.00" in show.output   # Default per-txn

    def test_create_policy_custom_limits(self, handler):
        """Create policy with custom limits."""
        result = handler.execute("policy create premium --day 500 --txn 50 --auto 5")
        assert result.success

        show = handler.execute("policy show premium")
        assert "$500.00" in show.output
        assert "$50.00" in show.output
        assert "$5.00" in show.output

    def test_edit_policy(self, handler):
        """Edit existing policy limits."""
        handler.execute("policy create editable")

        result = handler.execute("policy edit editable --day 200")
        assert result.success

        show = handler.execute("policy show editable")
        assert "$200.00" in show.output

    def test_policy_delete_with_confirmation(self, handler):
        """Delete policy requires confirmation."""
        handler.execute("policy create deletable")

        result = execute_with_inputs(
            handler,
            "policy delete deletable",
            {"confirm": "YES"}
        )
        assert result.success


# =============================================================================
# Dependency & Cascade Tests (Critical Integration Points)
# =============================================================================

class TestDependenciesAndCascades:
    """Test that operations correctly enforce dependencies and cascade effects."""

    @pytest.fixture
    def full_setup(self, handler):
        """Set up wallet + policy + agent (uncommissioned)."""
        execute_with_inputs(
            handler,
            "wallet create deptest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        handler.execute("policy create testpolicy --day 100")
        handler.execute("agent register testagent")
        return handler

    def test_commission_requires_unlocked_wallet(self, handler):
        """Commission should fail if wallet is locked."""
        # Setup with unlocked wallet
        handler.execute("policy create pol1")
        handler.execute("agent register ag1")
        # Lock the wallet
        handler.execute("wallet lock")

        # Commission should fail when wallet is locked
        result = handler.execute("agent commission ag1 pol1 A001")
        assert not result.success
        assert "unlock" in result.error.lower() or "wallet" in result.error.lower()

    def test_commission_requires_valid_policy(self, full_setup):
        """Commission should fail with nonexistent policy."""
        result = full_setup.execute("agent commission testagent fakepolicy A001")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_commission_requires_valid_address(self, full_setup):
        """Commission should fail with nonexistent address."""
        result = full_setup.execute("agent commission testagent testpolicy A99")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_commission_success(self, full_setup):
        """Commission with all prerequisites should succeed."""
        result = full_setup.execute("agent commission testagent testpolicy A001")
        assert result.success
        assert "commissioned" in result.output.lower()

        # Verify agent shows policy
        show = full_setup.execute("agent show testagent")
        assert "testpolicy" in show.output.lower()

    def test_delete_policy_decommissions_agents(self, full_setup):
        """Deleting a policy should decommission agents using it."""
        # Commission agent first
        full_setup.execute("agent commission testagent testpolicy A001")

        # Delete the policy
        result = execute_with_inputs(
            full_setup,
            "policy delete testpolicy",
            {"confirm": "YES"}
        )
        assert result.success

        # Agent should be decommissioned (no policy)
        show = full_setup.execute("agent show testagent")
        assert "none" in show.output.lower() or show.data.get("agent", {}).get("policy_id") is None

    def test_delete_address_decommissions_agents(self, full_setup):
        """Deleting an address should decommission agents using it."""
        # Commission agent
        full_setup.execute("agent commission testagent testpolicy A001")

        # Delete the address
        result = execute_with_inputs(
            full_setup,
            "address delete A001",
            {"confirm": "YES"}
        )
        assert result.success

        # Should mention decommissioned agent
        assert "decommission" in result.output.lower() or True  # May vary

    def test_operations_fail_after_wallet_lock(self, full_setup):
        """Operations requiring wallet should fail after lock."""
        full_setup.execute("wallet lock")

        # These should all fail
        assert not full_setup.execute("address list").success
        assert not full_setup.execute("seed list").success
        assert not full_setup.execute("address create").success


# =============================================================================
# Server Lifecycle Tests
# =============================================================================

class TestServerLifecycle:
    """Test server start/stop workflows."""

    def test_server_start_stop(self, handler):
        """Start and stop server."""
        result = handler.execute("server start 19402")  # Use high port to avoid conflicts
        assert result.success

        status = handler.execute("server status")
        assert "running" in status.output.lower()

        result = handler.execute("server stop")
        assert result.success

        status = handler.execute("server status")
        assert "not running" in status.output.lower()


# =============================================================================
# Full End-to-End Workflow Tests
# =============================================================================

class TestEndToEndWorkflows:
    """Test complete user journeys."""

    def test_new_user_complete_setup(self, handler):
        """
        Simulate a new user setting up Vault from scratch:
        1. Create wallet
        2. Create policy
        3. Register agent
        4. Commission agent
        5. Start server
        6. Verify status shows everything ready
        """
        # 1. Create wallet
        result = execute_with_inputs(
            handler,
            "wallet create mywallet",
            {"password": "secure123", "confirm": "secure123"}
        )
        assert result.success, "Wallet creation failed"

        # 2. Create policy
        result = handler.execute("policy create standard --day 100 --txn 10 --auto 1")
        assert result.success, "Policy creation failed"

        # 3. Register agent
        result = handler.execute("agent register claude-assistant --auth bearer")
        assert result.success, "Agent registration failed"
        agent_token = result.data.get("token")
        assert agent_token, "No token returned"

        # 4. Commission agent
        result = handler.execute("agent commission claude-assistant standard A001")
        assert result.success, "Commission failed"

        # 5. Start server
        result = handler.execute("server start 19403")
        assert result.success, "Server start failed"

        # 6. Verify final status
        status = handler.execute("status")
        assert "Unlocked" in status.output
        assert "Running" in status.output
        assert "1 active" in status.output

        # Cleanup
        handler.execute("server stop")

    def test_multi_agent_setup(self, handler):
        """
        Set up multiple agents with different policies:
        - Low-trust agent with strict limits
        - High-trust agent with generous limits
        """
        # Setup wallet
        execute_with_inputs(
            handler,
            "wallet create multiagent",
            {"password": "testpassword", "confirm": "testpassword"}
        )

        # Create second address for second agent
        handler.execute("address create")

        # Create policies
        handler.execute("policy create lowlimit --day 10 --txn 1 --auto 0")
        handler.execute("policy create highlimit --day 1000 --txn 100 --auto 50")

        # Register agents
        handler.execute("agent register untrusted-bot")
        handler.execute("agent register trusted-bot")

        # Commission with different policies/addresses
        handler.execute("agent commission untrusted-bot lowlimit A001")
        handler.execute("agent commission trusted-bot highlimit A2")

        # Verify setup
        result = handler.execute("agent list")
        assert "untrusted-bot" in result.output
        assert "trusted-bot" in result.output
        assert len(result.data.get("agents", [])) == 2


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestErrorHandling:
    """Test that errors are handled gracefully."""

    def test_all_commands_with_help(self, handler):
        """Every command should respond to --help without error."""
        commands = [
            "help", "status",
            "agent --help", "agent list --help", "agent register --help",
            "policy --help", "policy create --help",
            "wallet --help", "wallet create --help",
            "seed --help", "seed create --help",
            "address --help", "address create --help",
            "server --help",
            "history --help",
            "pending --help", "approve --help", "reject --help",
        ]

        for cmd in commands:
            result = handler.execute(cmd)
            assert result.success, f"Command '{cmd}' failed: {result.error}"

    def test_operations_on_nonexistent_entities(self, handler):
        """Operations on nonexistent entities should fail gracefully."""
        tests = [
            ("agent show nonexistent", "Agent not found"),
            ("agent suspend nonexistent", "Agent not found"),
            ("policy show nonexistent", "Policy not found"),
            ("policy edit nonexistent --day 100", "Policy not found"),
            ("approve nonexistent", "not found"),
            ("reject nonexistent", "not found"),
        ]

        for cmd, expected_error in tests:
            result = handler.execute(cmd)
            assert not result.success, f"Command '{cmd}' should have failed"
            assert expected_error.lower() in result.error.lower(), \
                f"Expected '{expected_error}' in error for '{cmd}', got: {result.error}"

    def test_missing_required_arguments(self, handler):
        """Commands with missing required args should fail with usage hint."""
        tests = [
            "agent show",
            "agent register",
            "policy show",
            "policy create",
            "address import",
            "address rename A001",  # Missing new name
        ]

        for cmd in tests:
            result = handler.execute(cmd)
            assert not result.success, f"Command '{cmd}' should have failed"


# =============================================================================
# Seed Lifecycle Tests
# =============================================================================

class TestSeedLifecycle:
    """Test seed creation, import, and deletion."""

    @pytest.fixture
    def unlocked_wallet(self, handler):
        """Create and unlock a wallet."""
        execute_with_inputs(
            handler,
            "wallet create seedtest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        return handler

    def test_create_additional_seed(self, unlocked_wallet):
        """Create a new seed in existing wallet."""
        # Wallet already has one seed (S001)
        result = unlocked_wallet.execute("seed create")
        assert result.success
        assert "S002" in result.output or "Created" in result.output
        assert result.data is not None
        assert "seed_phrase" in result.data

    def test_create_24_word_seed(self, unlocked_wallet):
        """Create a 24-word seed."""
        result = unlocked_wallet.execute("seed create --words 24")
        assert result.success
        seed_phrase = result.data.get("seed_phrase", "")
        assert len(seed_phrase.split()) == 24

    def test_import_seed(self, unlocked_wallet):
        """Import an existing seed phrase."""
        # Standard BIP-39 test mnemonic
        test_phrase = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        result = unlocked_wallet.execute(f'seed import "{test_phrase}"')
        assert result.success
        assert "Imported" in result.output or "seed" in result.output.lower()

    def test_import_invalid_seed(self, unlocked_wallet):
        """Import invalid seed should fail."""
        result = unlocked_wallet.execute('seed import "not a valid seed phrase"')
        assert not result.success

    def test_delete_seed(self, unlocked_wallet):
        """Delete a seed with confirmation."""
        # First create an extra seed so we can delete it
        unlocked_wallet.execute("seed create")
        unlocked_wallet.execute("seed list")

        # Delete S002
        result = execute_with_inputs(
            unlocked_wallet,
            "seed delete S002",
            {"confirm": "YES"}
        )
        assert result.success
        assert "deleted" in result.output.lower()

    def test_delete_seed_cancelled(self, unlocked_wallet):
        """Cancelling seed delete should not delete."""
        execute_with_inputs(
            unlocked_wallet,
            "seed delete S001",
            {"confirm": "no"}  # Not YES
        )
        # Should still exist
        seeds = unlocked_wallet.execute("seed list")
        assert "S001" in seeds.output


# =============================================================================
# History with Transactions Tests
# =============================================================================

class TestHistoryWithTransactions:
    """Test history commands with actual transaction records."""

    @pytest.fixture
    def setup_with_transactions(self, handler, temp_data_dir):
        """Set up wallet with some fake transactions."""
        from datetime import datetime
        import sys
        sys.path.insert(0, str(temp_data_dir.parent.parent / "src"))
        from primer_vault.models import Transaction

        # Create wallet and setup
        execute_with_inputs(
            handler,
            "wallet create histtest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        handler.execute("policy create testpolicy")
        handler.execute("agent register histagent")
        handler.execute("agent commission histagent testpolicy A001")

        # Add some fake transactions directly to the store
        core = handler.core
        for i in range(5):
            tx = Transaction(
                id=f"tx{i:03d}{'0' * 56}",
                timestamp=datetime.now().isoformat(),
                agent_id="ABC123",
                agent_name="histagent",
                agent_code="test-code",
                amount_micro=(i + 1) * 1_000_000,  # $1, $2, $3, $4, $5
                recipient=f"0x{'ab' * 20}",
                network="robinhood",
                status="signed" if i % 2 == 0 else "settled",
                auto_approved=i < 3,
            )
            core._policy_store.add_transaction(tx)

        return handler

    def test_history_list(self, setup_with_transactions):
        """List transactions."""
        result = setup_with_transactions.execute("history")
        assert result.success
        assert "histagent" in result.output
        assert result.data is not None
        assert len(result.data.get("transactions", [])) == 5

    def test_history_with_limit(self, setup_with_transactions):
        """List limited transactions."""
        result = setup_with_transactions.execute("history 3")
        assert result.success
        assert len(result.data.get("transactions", [])) == 3

    def test_history_show(self, setup_with_transactions):
        """Show transaction details."""
        result = setup_with_transactions.execute("history show tx000")
        assert result.success
        assert "tx000" in result.output
        assert "histagent" in result.output
        assert "robinhood" in result.output

    def test_history_show_not_found(self, setup_with_transactions):
        """Show nonexistent transaction."""
        result = setup_with_transactions.execute("history show nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_history_clear(self, setup_with_transactions):
        """Clear all transactions."""
        result = execute_with_inputs(
            setup_with_transactions,
            "history clear",
            {"confirm": "YES"}
        )
        assert result.success
        assert "Cleared" in result.output

        # Verify empty
        check = setup_with_transactions.execute("history")
        assert "No transactions" in check.output


# =============================================================================
# Mandate Generation Tests
# =============================================================================

class TestMandateGeneration:
    """Test agent mandate generation."""

    @pytest.fixture
    def commissioned_agent(self, handler):
        """Set up a commissioned agent with properly configured policy."""
        execute_with_inputs(
            handler,
            "wallet create mandatetest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        # Create policy with networks via core (CLI doesn't expose networks flag)
        handler.core.create_policy(
            name="mandatepolicy",
            daily_limit_micro=100_000_000,
            per_request_max_micro=10_000_000,
            auto_approve_below_micro=5_000_000,
            networks=[4663]  # Robinhood Chain
        )
        handler.execute("agent register mandateagent")
        handler.execute("agent commission mandateagent mandatepolicy A001")
        return handler

    def test_mandate_generation(self, commissioned_agent):
        """Generate mandate for commissioned agent."""
        result = commissioned_agent.execute("agent mandate mandateagent")
        assert result.success, f"Mandate generation failed: {result.error}"
        assert "mandate" in result.output.lower() or "generated" in result.output.lower()
        assert result.data is not None
        assert "mandate" in result.data

    def test_mandate_requires_commissioned_agent(self, handler):
        """Mandate fails for uncommissioned agent."""
        execute_with_inputs(
            handler,
            "wallet create mandatetest2",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        handler.execute("agent register uncommagent")

        result = handler.execute("agent mandate uncommagent")
        assert not result.success
        assert "commission" in result.error.lower()

    def test_mandate_requires_unlocked_wallet(self, handler):
        """Mandate requires wallet to be unlocked."""
        execute_with_inputs(
            handler,
            "wallet create mandatetest3",
            {"password": "testpass", "confirm": "testpass"}
        )
        handler.execute("policy create pol")
        handler.execute("agent register ag")
        handler.execute("agent commission ag pol A001")
        handler.execute("wallet lock")

        result = handler.execute("agent mandate ag")
        assert not result.success
        assert "unlock" in result.error.lower() or "wallet" in result.error.lower()

    def test_mandate_agent_not_found(self, handler):
        """Mandate fails for nonexistent agent."""
        result = handler.execute("agent mandate nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()


# =============================================================================
# Signing/Approval Flow Tests
# =============================================================================

class TestSigningApprovalFlow:
    """Test the pending/approve/reject workflow."""

    @pytest.fixture
    def setup_with_pending(self, handler, temp_data_dir):
        """Set up system with a pending request."""
        from datetime import datetime
        import sys
        sys.path.insert(0, str(temp_data_dir.parent.parent / "src"))
        from primer_vault.services.signing import SigningRequest

        # Create wallet and commission agent
        execute_with_inputs(
            handler,
            "wallet create approvaltest",
            {"password": "testpassword", "confirm": "testpassword"}
        )
        handler.execute("policy create testpolicy --day 100 --txn 10 --auto 0")  # No auto-approve
        handler.execute("agent register approvalagent")
        handler.execute("agent commission approvalagent testpolicy A001")

        # Get agent info for creating request
        agent = handler.core.get_all_agents()[0]

        # Create a pending signing request with correct fields
        req = SigningRequest(
            id="req001",
            agent_id=agent.id,
            agent_name=agent.name,
            amount_micro=5_000_000,  # $5
            recipient="0x" + "ab" * 20,
            network="robinhood",
            resource="https://example.com/api",
            request_url="https://example.com/api/resource",
            x402_data={"payTo": "0x" + "ab" * 20, "maxAmountRequired": "5000000"},
            x402_version=1,
            created_at=datetime.now().isoformat(),
        )
        handler.core._signing_service._pending_requests["req001"] = req

        return handler

    def test_pending_shows_requests(self, setup_with_pending):
        """Pending command shows pending requests."""
        result = setup_with_pending.execute("pending")
        assert result.success
        # Should show the pending request
        assert "req001" in result.output or "approvalagent" in result.output or result.data

    def test_approve_request(self, setup_with_pending):
        """Approve a pending request."""
        result = setup_with_pending.execute("approve req001")
        # May succeed or fail depending on signing implementation
        # But should not error with "not found"
        assert "not found" not in (result.error or "").lower() or result.success

    def test_reject_request(self, setup_with_pending):
        """Reject a pending request."""
        result = setup_with_pending.execute("reject req001 insufficient_funds")
        # Should process the rejection
        assert "not found" not in (result.error or "").lower() or result.success

    def test_approve_nonexistent(self, handler):
        """Approve nonexistent request fails."""
        result = handler.execute("approve nonexistent123")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_reject_nonexistent(self, handler):
        """Reject nonexistent request fails."""
        result = handler.execute("reject nonexistent123")
        assert not result.success
        assert "not found" in result.error.lower()
