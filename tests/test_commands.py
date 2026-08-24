"""
Tests for CommandHandler and command modules.

These tests verify command behavior without Qt dependencies.
Run with: pytest tests/test_commands.py -v
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


class TestBasicCommands:
    """Test basic commands (help, status, etc.)."""

    def test_help(self, handler):
        """help command shows help text."""
        result = handler.execute("help")
        assert result.success
        assert "AGENTS" in result.output
        assert "POLICIES" in result.output
        assert "WALLET" in result.output

    def test_status(self, handler):
        """status command shows system status."""
        result = handler.execute("status")
        assert result.success
        assert "Wallet:" in result.output
        assert "Server:" in result.output

    def test_unknown_command(self, handler):
        """Unknown command returns error."""
        result = handler.execute("foobar")
        assert not result.success
        assert "Unknown command" in result.error

    def test_empty_command(self, handler):
        """Empty command returns success with no output."""
        result = handler.execute("")
        assert result.success
        assert result.output == ""


class TestMandatePublishing:
    """Publishing a mandate is not finished when the upload returns.

    The registry files the mandate under an id and hands it back, and that id has
    to be stored on the agent - it is what `/mandate` reports as
    `mandate_registry_id`, and what an agent gives a merchant who wants to verify
    the authorization. Uploading without recording it leaves the field null
    forever while every visible sign says the upload worked.

    The window path always did this. The command path did not, so an agent
    commissioned from a terminal could never be verified.
    """

    def _an_address(self, core):
        """A real address in the wallet - `agent commission` will not accept one
        the wallet does not hold."""
        from eth_account.hdaccount import generate_mnemonic

        seed = core.add_seed(generate_mnemonic(num_words=12, lang="english"))
        entry = core.add_address_from_seed(seed["seed_id"], 0, "Primary")
        return entry["address"]

    def _agent_with_a_policy(self, core, name="pubbot"):
        agent, _ = core.create_agent(name=name, auth_mode="bearer")
        policy = core.create_policy(name="pub-" + name, daily_limit_micro=100_000_000)
        core.commission_agent(
            agent_code=agent.code, policy_id=policy.id,
            wallet_address=self._an_address(core))
        return core.get_agent_by_code(agent.code)

    def _registry(self, monkeypatch, *, ok=True):
        """Stand in for the AP2 registry, and record what was sent to it."""
        from primer_vault.commands import agent as agent_cmd

        posted = []

        class Response:
            status_code = 201 if ok else 500

            def json(self):
                return {"id": "reg-9f3c"}

        def post(url, json=None, headers=None, timeout=None):
            posted.append(json)
            return Response()

        monkeypatch.setattr(agent_cmd._requests, "post", post)
        return posted

    def test_the_registry_id_is_stored_on_the_agent(self, core, handler, monkeypatch):
        self._registry(monkeypatch)
        agent = self._agent_with_a_policy(core)

        result = handler.execute(f"agent mandate {agent.name} --upload")
        assert result.success, result.output

        stored = core.get_agent_by_code(agent.code).intent_mandate
        assert stored["registryId"] == "reg-9f3c"
        assert stored["registryUrl"].endswith("id=reg-9f3c")

    def test_the_id_reaches_the_field_agents_read(self, core, handler, monkeypatch):
        """`/mandate` reports it as mandate_registry_id. That is the whole point
        of storing it, so assert the value at the end of the journey."""
        self._registry(monkeypatch)
        agent = self._agent_with_a_policy(core)
        handler.execute(f"agent mandate {agent.name} --upload")

        stored = core.get_agent_by_code(agent.code).intent_mandate
        assert stored.get("registryId") == "reg-9f3c"

    def test_commissioning_with_upload_stores_it_too(self, core, handler, monkeypatch):
        """The other command that publishes. Both go through one helper, so
        this guards against them drifting apart."""
        self._registry(monkeypatch)
        agent, _ = core.create_agent(name="combot", auth_mode="bearer")
        policy = core.create_policy(name="com", daily_limit_micro=100_000_000)
        address = self._an_address(core)

        result = handler.execute(
            f"agent commission {agent.name} {policy.name} {address} "
            f"--mandate --upload")
        assert result.success, result.output

        stored = core.get_agent_by_code(agent.code).intent_mandate
        assert stored["registryId"] == "reg-9f3c"

    def test_nothing_is_stamped_when_the_upload_fails(self, core, handler, monkeypatch):
        """A mandate that was never published must not claim a registry id."""
        self._registry(monkeypatch, ok=False)
        agent = self._agent_with_a_policy(core)

        handler.execute(f"agent mandate {agent.name} --upload")

        stored = core.get_agent_by_code(agent.code).intent_mandate
        assert stored is not None, "the mandate itself is still generated"
        assert "registryId" not in stored

    def test_generating_without_upload_stores_no_registry_id(self, core, handler,
                                                             monkeypatch):
        self._registry(monkeypatch)
        agent = self._agent_with_a_policy(core)

        handler.execute(f"agent mandate {agent.name}")

        stored = core.get_agent_by_code(agent.code).intent_mandate
        assert "registryId" not in stored


class TestAgentCommands:
    """Test agent commands."""

    def test_agent_help(self, handler):
        """agent --help shows help."""
        result = handler.execute("agent --help")
        assert result.success
        assert "register" in result.output
        assert "commission" in result.output

    def test_agent_list_empty(self, handler):
        """agent list with no agents."""
        result = handler.execute("agent list")
        assert result.success
        assert "No agents" in result.output

    def test_agent_register(self, handler):
        """agent register creates an agent."""
        result = handler.execute("agent register testbot")
        assert result.success
        assert "testbot" in result.output
        assert "PRIMER_VAULT_AGENT_ID" in result.output
        assert result.data is not None
        assert "token" in result.data

    def test_agent_register_with_auth(self, handler):
        """agent register with --auth flag."""
        result = handler.execute("agent register mybot --auth bearer")
        assert result.success
        assert "bearer" in result.output.lower()

    def test_agent_register_duplicate(self, handler):
        """agent register with duplicate name fails."""
        handler.execute("agent register dupbot")
        result = handler.execute("agent register dupbot")
        assert not result.success
        # Error should mention already exists or duplicate

    def test_agent_list_after_register(self, handler):
        """agent list shows registered agents."""
        handler.execute("agent register listedbot")
        result = handler.execute("agent list")
        assert result.success
        assert "listedbot" in result.output
        assert result.data is not None
        assert len(result.data.get("agents", [])) >= 1

    def test_agent_show(self, handler):
        """agent show displays agent details."""
        handler.execute("agent register showbot")
        result = handler.execute("agent show showbot")
        assert result.success
        assert "showbot" in result.output
        assert "Auth Mode:" in result.output

    def test_agent_show_not_found(self, handler):
        """agent show with invalid name fails."""
        result = handler.execute("agent show nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()


class TestPolicyCommands:
    """Test policy commands."""

    def test_policy_help(self, handler):
        """policy --help shows help."""
        result = handler.execute("policy --help")
        assert result.success
        assert "create" in result.output
        assert "edit" in result.output

    def test_policy_list_empty(self, handler):
        """policy list with no policies."""
        result = handler.execute("policy list")
        assert result.success
        assert "No policies" in result.output

    def test_policy_create(self, handler):
        """policy create creates a policy."""
        result = handler.execute("policy create standard")
        assert result.success
        assert "created" in result.output.lower()

    def test_policy_create_with_limits(self, handler):
        """policy create with custom limits."""
        result = handler.execute("policy create premium --day 500 --txn 50 --auto 5")
        assert result.success
        assert "premium" in result.output.lower()

    def test_policy_list_after_create(self, handler):
        """policy list shows created policies."""
        handler.execute("policy create listpolicy")
        result = handler.execute("policy list")
        assert result.success
        assert "listpolicy" in result.output

    def test_policy_show(self, handler):
        """policy show displays policy details."""
        handler.execute("policy create showpolicy --day 200")
        result = handler.execute("policy show showpolicy")
        assert result.success
        assert "200" in result.output
        assert "Daily Limit" in result.output

    def test_policy_edit(self, handler):
        """policy edit modifies a policy."""
        handler.execute("policy create editpolicy")
        result = handler.execute("policy edit editpolicy --day 300")
        assert result.success
        assert "updated" in result.output.lower()


class TestServerCommands:
    """Test server commands."""

    def test_server_help(self, handler):
        """server --help shows help."""
        result = handler.execute("server --help")
        assert result.success
        assert "start" in result.output
        assert "stop" in result.output

    def test_server_status_stopped(self, handler):
        """server status when not running."""
        result = handler.execute("server status")
        assert result.success
        assert "not running" in result.output.lower()


class TestHistoryCommands:
    """Test history commands."""

    def test_history_help(self, handler):
        """history --help shows help."""
        result = handler.execute("history --help")
        assert result.success
        assert "show" in result.output
        assert "clear" in result.output

    def test_history_empty(self, handler):
        """history with no transactions."""
        result = handler.execute("history")
        assert result.success
        assert "No transactions" in result.output


class TestWalletCommands:
    """Test wallet commands (without actual wallet operations)."""

    def test_wallet_help(self, handler):
        """wallet --help shows help."""
        result = handler.execute("wallet --help")
        assert result.success
        assert "create" in result.output
        assert "open" in result.output

    def test_wallet_status_no_wallet(self, handler_no_wallet):
        """wallet status when no wallet is loaded."""
        result = handler_no_wallet.execute("wallet status")
        assert result.success
        # No wallet file exists in test environment — should say "No wallet loaded"
        assert "wallet" in result.output.lower()
        assert result.data["unlocked"] is False


class TestApprovalCommands:
    """Test approval commands."""

    def test_pending_empty(self, handler):
        """pending with no requests."""
        result = handler.execute("pending")
        assert result.success
        assert "No pending" in result.output

    def test_approve_not_found(self, handler):
        """approve with invalid ID."""
        result = handler.execute("approve nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()


class TestSeedCommands:
    """Test seed commands."""

    def test_seed_help(self, handler):
        """seed --help shows help."""
        result = handler.execute("seed --help")
        assert result.success
        assert "create" in result.output
        assert "import" in result.output

    def test_seed_list_wallet_locked(self, handler_no_wallet):
        """seed list when wallet locked."""
        result = handler_no_wallet.execute("seed list")
        assert not result.success
        assert "unlocked" in result.error.lower()


class TestAddressCommands:
    """Test address commands."""

    def test_address_help(self, handler):
        """address --help shows help."""
        result = handler.execute("address --help")
        assert result.success
        assert "create" in result.output
        assert "import" in result.output

    def test_address_list_wallet_locked(self, handler_no_wallet):
        """address list when wallet locked."""
        result = handler_no_wallet.execute("address list")
        assert not result.success
        assert "unlocked" in result.error.lower()


class TestCommandResult:
    """Test CommandResult class."""

    def test_ok(self):
        """CommandResult.ok creates successful result."""
        result = CommandResult.ok("Success!")
        assert result.success
        assert result.output == "Success!"
        assert result.error is None

    def test_fail(self):
        """CommandResult.fail creates failed result."""
        result = CommandResult.fail("Something went wrong")
        assert not result.success
        assert result.error == "Something went wrong"

    def test_need_input(self):
        """CommandResult.need_input creates input request."""
        result = CommandResult.need_input("password", "Enter password:")
        assert result.success  # Not a failure
        assert result.needs_input is not None
        assert result.needs_input["type"] == "password"
        assert result.needs_input["prompt"] == "Enter password:"
