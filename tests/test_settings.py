"""
Tests for settings persistence and config commands.

Run with: pytest tests/test_settings.py -v
"""

import json
import pytest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core.settings import SettingsManager, AppSettings
from primer_vault.commands import CommandHandler


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create a temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def settings_manager(temp_data_dir):
    """Create a SettingsManager instance."""
    return SettingsManager(temp_data_dir)




@pytest.fixture
def handler(core):
    """Create a CommandHandler instance."""
    return CommandHandler(core)


class TestAppSettings:
    """Test AppSettings dataclass."""

    def test_default_values(self):
        """AppSettings has correct defaults."""
        settings = AppSettings()
        assert settings.version == 1
        assert settings.signing.verify_settlements is True
        assert settings.signing.max_request_age_seconds == 300
        assert settings.server.default_port == 4663
        assert settings.server.allow_lan is False

    def test_to_dict(self):
        """AppSettings converts to dict correctly."""
        settings = AppSettings()
        d = settings.to_dict()
        assert d["version"] == 1
        assert d["signing"]["verify_settlements"] is True
        assert d["server"]["default_port"] == 4663

    def test_from_dict(self):
        """AppSettings creates from dict correctly."""
        data = {
            "version": 1,
            "signing": {
                "verify_settlements": True,
                "max_request_age_seconds": 600,
                "enabled_networks": {"4663": True}
            },
            "server": {
                "default_port": 8080,
                "allow_lan": True
            }
        }
        settings = AppSettings.from_dict(data)
        assert settings.signing.verify_settlements is True
        assert settings.signing.max_request_age_seconds == 600
        assert settings.server.default_port == 8080
        assert settings.server.allow_lan is True


class TestSettingsManager:
    """Test SettingsManager class."""

    def test_creates_settings_file(self, temp_data_dir):
        """SettingsManager creates settings.json on init."""
        manager = SettingsManager(temp_data_dir)
        settings_file = temp_data_dir / "settings.json"
        assert settings_file.exists()
        manager.stop()

    def test_loads_existing_settings(self, temp_data_dir):
        """SettingsManager loads existing settings.json."""
        settings_file = temp_data_dir / "settings.json"
        settings_file.write_text(json.dumps({
            "version": 1,
            "signing": {"verify_settlements": True}
        }))

        manager = SettingsManager(temp_data_dir)
        assert manager.get_verify_settlements() is True
        manager.stop()

    def test_set_verify_settlements(self, settings_manager):
        """set_verify_settlements updates and persists."""
        settings_manager.set_verify_settlements(True)
        assert settings_manager.get_verify_settlements() is True

        # Check file was updated
        settings_file = settings_manager._settings_file
        data = json.loads(settings_file.read_text())
        assert data["signing"]["verify_settlements"] is True
        settings_manager.stop()

    def test_set_max_request_age(self, settings_manager):
        """set_max_request_age updates and persists."""
        settings_manager.set_max_request_age(600)
        assert settings_manager.get_max_request_age() == 600
        settings_manager.stop()

    def test_set_max_request_age_minimum(self, settings_manager):
        """set_max_request_age enforces 30s minimum."""
        settings_manager.set_max_request_age(10)
        assert settings_manager.get_max_request_age() == 30
        settings_manager.stop()

    def test_set_network_enabled(self, settings_manager):
        """set_network_enabled updates and persists."""
        settings_manager.set_network_enabled(4663, True)
        assert settings_manager.is_network_enabled(4663) is True

        settings_manager.set_network_enabled(4663, False)
        assert settings_manager.is_network_enabled(4663) is False
        settings_manager.stop()

    def test_get_enabled_networks(self, settings_manager):
        """get_enabled_networks returns dict with int keys."""
        networks = settings_manager.get_enabled_networks()
        assert isinstance(networks, dict)
        for key in networks.keys():
            assert isinstance(key, int)
        settings_manager.stop()

    def test_set_default_port(self, settings_manager):
        """set_default_port updates and persists."""
        settings_manager.set_default_port(8080)
        assert settings_manager.get_default_port() == 8080
        settings_manager.stop()

    def test_set_allow_lan(self, settings_manager):
        """set_allow_lan updates and persists."""
        settings_manager.set_allow_lan(True)
        assert settings_manager.get_allow_lan() is True
        settings_manager.stop()

    def test_set_rpc_endpoint(self, settings_manager):
        """set_rpc_endpoint updates and persists."""
        settings_manager.set_rpc_endpoint(4663, "https://custom.rpc.com")
        assert settings_manager.get_rpc_endpoint(4663) == "https://custom.rpc.com"

        # Reset to default
        settings_manager.set_rpc_endpoint(4663, None)
        assert settings_manager.get_rpc_endpoint(4663) is None
        settings_manager.stop()

    def test_on_change_callback(self, temp_data_dir):
        """on_change callback is called when settings change."""
        callback_called = []

        def on_change(settings):
            callback_called.append(settings)

        manager = SettingsManager(temp_data_dir, on_change=on_change)
        # Default is True, so set to False to trigger a change
        manager.set_verify_settlements(False)

        assert len(callback_called) == 1
        assert callback_called[0].signing.verify_settlements is False
        manager.stop()


class TestCoreSettingsIntegration:
    """Test Core integration with SettingsManager."""

    def test_core_has_settings_manager(self, core):
        """Core initializes SettingsManager."""
        assert hasattr(core, 'settings_manager')
        assert core.settings_manager is not None

    def test_core_set_verify_settlements(self, core):
        """Core.set_verify_settlements persists settings."""
        core.set_verify_settlements(True)
        assert core.get_verify_settlements() is True

    def test_core_set_max_request_age(self, core):
        """Core.set_max_request_age persists settings."""
        core.set_max_request_age(600)
        assert core.get_max_request_age() == 600

    def test_core_set_network_enabled(self, core):
        """Core.set_network_enabled persists settings."""
        core.set_network_enabled(4663, True)
        assert core.is_network_enabled(4663) is True

    def test_core_get_enabled_networks(self, core):
        """Core.get_enabled_networks returns dict."""
        networks = core.get_enabled_networks()
        assert isinstance(networks, dict)


class TestConfigCommands:
    """Test config CLI commands."""

    def test_config_show(self, handler):
        """config show displays all settings."""
        result = handler.execute("config show")
        assert result.success
        assert "Current Settings" in result.output
        assert "verify-settlements" in result.output
        assert "replay-window" in result.output
        assert "Networks" in result.output

    def test_config_no_args(self, handler):
        """config with no args shows settings."""
        result = handler.execute("config")
        assert result.success
        assert "Current Settings" in result.output

    def test_config_get_verify_settlements(self, handler):
        """config get verify-settlements works."""
        result = handler.execute("config get verify-settlements")
        assert result.success
        assert "verify-settlements" in result.output

    def test_config_get_replay_window(self, handler):
        """config get replay-window works."""
        result = handler.execute("config get replay-window")
        assert result.success
        assert "replay-window" in result.output

    def test_config_set_verify_settlements_on(self, handler):
        """config set verify-settlements on works."""
        result = handler.execute("config set verify-settlements on")
        assert result.success
        assert "enabled" in result.output.lower()

        # Verify it was set
        result = handler.execute("config get verify-settlements")
        assert "on" in result.output

    def test_config_set_verify_settlements_off(self, handler):
        """config set verify-settlements off works."""
        result = handler.execute("config set verify-settlements off")
        assert result.success
        assert "disabled" in result.output.lower()

    def test_config_set_replay_window(self, handler):
        """config set replay-window works."""
        result = handler.execute("config set replay-window 600")
        assert result.success
        assert "600" in result.output

    def test_config_set_replay_window_minimum(self, handler):
        """config set replay-window enforces minimum."""
        result = handler.execute("config set replay-window 10")
        assert not result.success
        assert "30" in result.error

    def test_config_set_network(self, handler):
        """config set network works."""
        result = handler.execute("config set network 4663 on")
        assert result.success
        assert "enabled" in result.output.lower()

        result = handler.execute("config set network 4663 off")
        assert result.success
        assert "disabled" in result.output.lower()

    def test_config_set_default_port(self, handler):
        """config set default-port works."""
        result = handler.execute("config set default-port 8080")
        assert result.success
        assert "8080" in result.output

    def test_config_set_allow_lan(self, handler):
        """config set allow-lan works."""
        result = handler.execute("config set allow-lan on")
        assert result.success
        assert "allowed" in result.output.lower()

    def test_config_set_rpc(self, handler):
        """config set rpc works."""
        result = handler.execute("config set rpc 4663 https://custom.rpc.com")
        assert result.success
        assert "4663" in result.output

        result = handler.execute("config set rpc 4663 default")
        assert result.success
        assert "default" in result.output.lower()

    def test_config_set_invalid_setting(self, handler):
        """config set with invalid setting fails."""
        result = handler.execute("config set foobar value")
        assert not result.success
        assert "Unknown setting" in result.error

    def test_config_set_no_value(self, handler):
        """config set without value shows help."""
        result = handler.execute("config set")
        assert not result.success
        assert "Usage" in result.error


class TestSettingsEventEmission:
    """Test that settings changes emit events."""

    def test_settings_changed_event(self, core):
        """SETTINGS_CHANGED event is emitted on change."""
        from primer_vault.core.events import EventType

        events_received = []

        def on_event(event):
            events_received.append(event)

        core.event_bus.subscribe(EventType.SETTINGS_CHANGED, on_event)

        # Change a setting (default is True, so set to False to trigger change)
        core.set_verify_settlements(False)

        # Event should be emitted
        assert len(events_received) == 1
        assert events_received[0].type == EventType.SETTINGS_CHANGED
        assert events_received[0].data["signing"]["verify_settlements"] is False
