"""
Single-instance integration tests.

Verifies that CoreClient can connect to an AdminAPIServer and that
CommandHandler works identically whether given a direct core or a CoreClient.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.daemon.admin_api import AdminAPIServer
from primer_vault.client.core_client import CoreClient, CoreClientError
from primer_vault.commands import CommandHandler

TEST_PORT = 19403  # Use non-standard port to avoid conflicts with real instances


@pytest.fixture(scope="module")
def core_and_server(tmp_path_factory):
    """Start a Vault core + AdminAPIServer for the test module."""
    data_dir = tmp_path_factory.mktemp("single_instance_data")
    (data_dir / "wallets").mkdir(exist_ok=True)
    core = Vault(data_dir=data_dir)
    # Set admin API mode to "open" for testing (default is "gui_only")
    core.settings_manager.set_admin_api_mode("open")
    # Create and unlock a wallet for agent operations
    wallet_path = str(data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass")
    core.load_wallet(wallet_path, "testpass")
    server = AdminAPIServer(core, port=TEST_PORT)
    server.start()
    time.sleep(0.1)  # Let server thread start
    yield core, server
    server.stop()


@pytest.fixture
def client(core_and_server):
    """CoreClient connected to the test server."""
    from primer_vault.client.core_client import ClientConfig
    cfg = ClientConfig(admin_port=TEST_PORT, timeout=5.0, retry_count=1)
    return CoreClient(cfg)


@pytest.fixture
def direct_core(core_and_server):
    """The direct Vault core (same instance the server wraps)."""
    core, _ = core_and_server
    return core


# -------------------------------------------------------------------------
# Connection
# -------------------------------------------------------------------------

class TestConnection:

    def test_try_connect_finds_server(self, core_and_server):
        from primer_vault.client.core_client import ClientConfig
        cfg = ClientConfig(admin_port=TEST_PORT, timeout=2.0, retry_count=1)
        c = CoreClient(cfg)
        assert c.is_daemon_running()

    def test_try_connect_returns_none_on_wrong_port(self):
        from primer_vault.client.core_client import ClientConfig
        cfg = ClientConfig(admin_port=19999, timeout=0.5, retry_count=1, retry_delay=0.0)
        c = CoreClient(cfg)
        assert not c.is_daemon_running()

    def test_status(self, client):
        status = client.get_status()
        assert status["status"] == "ok"
        assert "wallet_unlocked" in status
        assert "server_running" in status


# -------------------------------------------------------------------------
# Agents via CoreClient
# -------------------------------------------------------------------------

class TestAgentsRemote:

    def test_create_and_list_agent(self, client):
        agent, secret = client.create_agent("RemoteBot", "hmac")
        assert agent.name == "RemoteBot"
        assert agent.id
        assert secret

        agents = client.get_all_agents()
        names = [a.name for a in agents]
        assert "RemoteBot" in names

    def test_get_agent_by_id(self, client):
        agent, _ = client.create_agent("LookupBot")
        found = client.get_agent_by_id(agent.id)
        assert found is not None
        assert found.name == "LookupBot"

    def test_get_agent_by_name(self, client):
        client.create_agent("NamedBot")
        found = client.get_agent_by_id("NamedBot")
        assert found is not None
        assert found.name == "NamedBot"

    def test_get_agent_not_found(self, client):
        result = client.get_agent_by_id("NONEXISTENT")
        assert result is None

    def test_suspend_agent(self, client):
        agent, _ = client.create_agent("SuspendBot")
        assert agent.status in ("active", "uncommissioned")
        client.suspend_agent(agent.code)
        refreshed = client.get_agent_by_id(agent.id)
        assert refreshed.status == "suspended"

    def test_delete_agent(self, client):
        agent, _ = client.create_agent("DeleteBot")
        client.delete_agent(agent.code)
        assert client.get_agent_by_id(agent.id) is None


# -------------------------------------------------------------------------
# Policies via CoreClient
# -------------------------------------------------------------------------

class TestPoliciesRemote:

    def test_create_policy(self, client):
        policy = client.create_policy(
            name="RemotePolicy",
            daily_limit_micro=50_000_000,
            per_request_max_micro=5_000_000,
        )
        assert policy.name == "RemotePolicy"
        assert policy.daily_limit_micro == 50_000_000
        assert policy.id

    def test_list_policies(self, client):
        client.create_policy("ListPolicy", daily_limit_micro=10_000_000, per_request_max_micro=1_000_000)
        policies = client.get_all_policies()
        names = [p.name for p in policies]
        assert "ListPolicy" in names

    def test_update_policy(self, client):
        policy = client.create_policy("EditPolicy", daily_limit_micro=10_000_000, per_request_max_micro=1_000_000)
        policy.daily_limit_micro = 20_000_000
        client.update_policy(policy)
        refreshed = client.get_policy(policy.id)
        assert refreshed.daily_limit_micro == 20_000_000

    def test_delete_policy(self, client):
        policy = client.create_policy("DeletePolicy")
        client.delete_policy(policy.id)
        assert client.get_policy(policy.id) is None


# -------------------------------------------------------------------------
# CommandHandler works identically with CoreClient vs direct core
# -------------------------------------------------------------------------

class TestCommandHandlerParity:

    def test_agent_list_via_client(self, client):
        client.create_agent("ParityBot")
        handler = CommandHandler(client)
        result = handler.execute("agent list")
        assert result.success
        assert "ParityBot" in result.output

    def test_policy_list_via_client(self, client):
        client.create_policy("ParityPolicy", daily_limit_micro=50_000_000, per_request_max_micro=5_000_000)
        handler = CommandHandler(client)
        result = handler.execute("policy list")
        assert result.success
        assert "ParityPolicy" in result.output

    def test_status_via_client(self, client):
        handler = CommandHandler(client)
        result = handler.execute("status")
        assert result.success
        assert "Wallet:" in result.output

    def test_changes_reflected_in_direct_core(self, client, direct_core):
        """Create agent via client; verify it appears on direct core."""
        agent, _ = client.create_agent("CrossBot")
        direct_agents = direct_core.get_all_agents()
        names = [a.name for a in direct_agents]
        assert "CrossBot" in names


# -------------------------------------------------------------------------
# Settings via CoreClient
# -------------------------------------------------------------------------

class TestSettingsRemote:

    def test_get_settings(self, client):
        sm = client.settings_manager
        settings = sm.settings
        assert hasattr(settings, "signing")
        assert hasattr(settings, "server")
        assert hasattr(settings.signing, "verify_settlements")

    def test_set_and_get_setting(self, client):
        sm = client.settings_manager
        original = sm.get_verify_settlements()
        sm.set_verify_settlements(not original)
        assert sm.get_verify_settlements() == (not original)
        # Restore
        sm.set_verify_settlements(original)


# -------------------------------------------------------------------------
# Wallet status via CoreClient
# -------------------------------------------------------------------------

class TestWalletRemote:

    def test_wallet_status(self, client):
        assert isinstance(client.is_wallet_unlocked(), bool)

    def test_wallet_dir_resolvable_via_client(self, client):
        """get_wallet_dir() should return a Path derived from daemon's data_dir."""
        from pathlib import Path
        wallet_dir = client.get_wallet_dir()
        assert isinstance(wallet_dir, Path)
        assert wallet_dir.name == "wallets"

    def test_wallet_create_not_supported(self, client):
        with pytest.raises(CoreClientError):
            client.create_wallet(wallet_path="/tmp/x.wallet", password="")
