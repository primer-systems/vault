"""
Settings persistence and cross-process synchronization.

Handles loading/saving settings to JSON file and watching for
external changes using watchdog.
"""

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Canonical default port for Vault (RHC mainnet chain ID)
DEFAULT_PORT = 4663

# Admin API access modes
ADMIN_API_MODE_OPEN = "open"          # Any local process can access (current behavior)
ADMIN_API_MODE_GUI_ONLY = "gui_only"  # Reject all external HTTP requests

# Default settings values
DEFAULT_SETTINGS = {
    "version": 1,
    "signing": {
        "verify_settlements": True,
        "max_request_age_seconds": 300,
        "enabled_networks": {
            "4663": True,    # Robinhood Chain - enabled by default
        }
    },
    "server": {
        "default_port": DEFAULT_PORT,
        "allow_lan": False
    },
    "security": {
        "admin_api_mode": ADMIN_API_MODE_GUI_ONLY,
    },
    "display": {
        "default_network": 4663
    },
    "rpc": {
        "4663": None
    }
}


@dataclass
class SigningSettings:
    """Signing-related settings."""
    verify_settlements: bool = True
    max_request_age_seconds: int = 300
    enabled_networks: dict = field(default_factory=lambda: {"4663": True})


@dataclass
class ServerSettings:
    """Server-related settings."""
    default_port: int = DEFAULT_PORT
    allow_lan: bool = False


@dataclass
class SecuritySettings:
    """Security-related settings."""
    admin_api_mode: str = ADMIN_API_MODE_GUI_ONLY  # "open" or "gui_only"


@dataclass
class DisplaySettings:
    """Display-related settings."""
    default_network: int = 4663


@dataclass
class RpcSettings:
    """Custom RPC endpoint settings."""
    endpoints: dict = field(default_factory=lambda: {"4663": None})


@dataclass
class AppSettings:
    """Complete application settings."""
    version: int = 1
    signing: SigningSettings = field(default_factory=SigningSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    rpc: RpcSettings = field(default_factory=RpcSettings)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "signing": {
                "verify_settlements": self.signing.verify_settlements,
                "max_request_age_seconds": self.signing.max_request_age_seconds,
                "enabled_networks": self.signing.enabled_networks,
            },
            "server": {
                "default_port": self.server.default_port,
                "allow_lan": self.server.allow_lan,
            },
            "security": {
                "admin_api_mode": self.security.admin_api_mode,
            },
            "display": {
                "default_network": self.display.default_network,
            },
            "rpc": self.rpc.endpoints,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        """Create from dictionary."""
        settings = cls()
        settings.version = data.get("version", 1)

        if "signing" in data:
            s = data["signing"]
            settings.signing.verify_settlements = s.get("verify_settlements", True)
            settings.signing.max_request_age_seconds = s.get("max_request_age_seconds", 300)
            settings.signing.enabled_networks = s.get("enabled_networks", {"4663": True})

        if "server" in data:
            s = data["server"]
            settings.server.default_port = s.get("default_port", DEFAULT_PORT)
            settings.server.allow_lan = s.get("allow_lan", False)

        if "security" in data:
            s = data["security"]
            settings.security.admin_api_mode = s.get("admin_api_mode", ADMIN_API_MODE_GUI_ONLY)

        if "display" in data:
            s = data["display"]
            settings.display.default_network = s.get("default_network", 4663)

        if "rpc" in data:
            settings.rpc.endpoints = data["rpc"]

        return settings


class SettingsManager:
    """
    Manages settings persistence and file watching.

    Features:
    - Load/save settings to JSON file
    - Watch for external file changes using watchdog
    - Notify listeners when settings change
    """

    def __init__(
        self,
        data_dir: Path,
        on_change: Optional[Callable[[AppSettings], None]] = None
    ):
        """
        Initialize settings manager.

        Args:
            data_dir: Directory for settings file
            on_change: Callback when settings change (from file or programmatic)
        """
        self._data_dir = data_dir
        self._settings_file = data_dir / "settings.json"
        self._settings = AppSettings()
        self._on_change = on_change
        self._observer = None
        self._write_lock = threading.Lock()
        self._last_written_content: Optional[str] = None  # To detect our own writes
        self._skip_next_reload = False  # Flag to skip watcher reload after our writes

        # Load existing settings
        self._load()

        # Start file watcher
        self._start_watcher()

    def _load(self) -> None:
        """Load settings from file."""
        if not self._settings_file.exists():
            logger.info(f"No settings file found, using defaults")
            self._save()  # Create with defaults
            return

        try:
            with open(self._settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._settings = AppSettings.from_dict(data)
            logger.info(f"Loaded settings from {self._settings_file}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid settings file, using defaults: {e}")
            self._settings = AppSettings()
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            self._settings = AppSettings()

    def _save(self) -> None:
        """Save settings to file."""
        with self._write_lock:
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                content = json.dumps(self._settings.to_dict(), indent=2)
                self._last_written_content = content
                self._skip_next_reload = True  # Skip watcher reload for our own write
                with open(self._settings_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                logger.debug(f"Saved settings to {self._settings_file}")
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")

    def _start_watcher(self) -> None:
        """Start watching the settings file for changes."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class SettingsFileHandler(FileSystemEventHandler):
                def __init__(handler_self, manager):
                    handler_self.manager = manager

                def on_modified(handler_self, event):
                    if event.is_directory:
                        return
                    if Path(event.src_path).name == "settings.json":
                        handler_self.manager._on_file_changed()

            self._observer = Observer()
            handler = SettingsFileHandler(self)
            self._observer.schedule(handler, str(self._data_dir), recursive=False)
            self._observer.start()
            logger.info("Settings file watcher started")

        except ImportError:
            logger.debug("watchdog not installed, settings file watching disabled")
        except Exception as e:
            logger.error(f"Failed to start settings watcher: {e}")

    def _on_file_changed(self) -> None:
        """Handle external settings file change."""
        # Skip if this was triggered by our own write
        if self._skip_next_reload:
            self._skip_next_reload = False
            return

        try:
            # Read file content
            with open(self._settings_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Skip if content matches what we last wrote (backup check)
            if content == self._last_written_content:
                return

            # Parse and update
            data = json.loads(content)
            self._settings = AppSettings.from_dict(data)
            logger.info("Settings reloaded from external change")

            # Notify listener
            if self._on_change:
                self._on_change(self._settings)

        except Exception as e:
            logger.error(f"Failed to reload settings: {e}")

    def stop(self) -> None:
        """Stop the file watcher."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
            logger.info("Settings file watcher stopped")

    @property
    def settings(self) -> AppSettings:
        """Get current settings (read-only)."""
        return self._settings

    # Convenience accessors

    def get_verify_settlements(self) -> bool:
        """Get settlement verification setting."""
        return self._settings.signing.verify_settlements

    def set_verify_settlements(self, enabled: bool) -> None:
        """Set settlement verification setting."""
        if self._settings.signing.verify_settlements != enabled:
            self._settings.signing.verify_settlements = enabled
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_max_request_age(self) -> int:
        """Get max request age in seconds."""
        return self._settings.signing.max_request_age_seconds

    def set_max_request_age(self, seconds: int) -> None:
        """Set max request age in seconds."""
        if seconds < 30:
            logger.warning(f"Max request age {seconds}s is very short, using 30s minimum")
            seconds = 30
        if self._settings.signing.max_request_age_seconds != seconds:
            self._settings.signing.max_request_age_seconds = seconds
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def is_network_enabled(self, chain_id: int) -> bool:
        """Check if a network is enabled."""
        return self._settings.signing.enabled_networks.get(str(chain_id), False)

    def set_network_enabled(self, chain_id: int, enabled: bool) -> None:
        """Enable or disable a network."""
        key = str(chain_id)
        if self._settings.signing.enabled_networks.get(key) != enabled:
            self._settings.signing.enabled_networks[key] = enabled
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_enabled_networks(self) -> dict[int, bool]:
        """Get all network enabled states as int keys."""
        return {int(k): v for k, v in self._settings.signing.enabled_networks.items()}

    def get_default_port(self) -> int:
        """Get default server port."""
        return self._settings.server.default_port

    def set_default_port(self, port: int) -> None:
        """Set default server port."""
        if self._settings.server.default_port != port:
            self._settings.server.default_port = port
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_allow_lan(self) -> bool:
        """Get allow LAN connections setting."""
        return self._settings.server.allow_lan

    def set_allow_lan(self, allowed: bool) -> None:
        """Set allow LAN connections setting."""
        if self._settings.server.allow_lan != allowed:
            self._settings.server.allow_lan = allowed
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_default_network(self) -> int:
        """Get default network chain ID."""
        return self._settings.display.default_network

    def set_default_network(self, chain_id: int) -> None:
        """Set default network chain ID."""
        if self._settings.display.default_network != chain_id:
            self._settings.display.default_network = chain_id
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_rpc_endpoint(self, chain_id: int) -> Optional[str]:
        """Get custom RPC endpoint for a network."""
        return self._settings.rpc.endpoints.get(str(chain_id))

    def set_rpc_endpoint(self, chain_id: int, endpoint: Optional[str]) -> None:
        """Set custom RPC endpoint for a network. Pass None to reset to default."""
        key = str(chain_id)
        current = self._settings.rpc.endpoints.get(key)
        if current != endpoint:
            if endpoint is None:
                # Remove key to reset to default
                self._settings.rpc.endpoints.pop(key, None)
            else:
                self._settings.rpc.endpoints[key] = endpoint
            self._save()
            if self._on_change:
                self._on_change(self._settings)

    def get_admin_api_mode(self) -> str:
        """Get admin API access mode."""
        return self._settings.security.admin_api_mode

    def set_admin_api_mode(self, mode: str) -> None:
        """Set admin API access mode. Only 'open' or 'gui_only' are valid."""
        if mode not in (ADMIN_API_MODE_OPEN, ADMIN_API_MODE_GUI_ONLY):
            raise ValueError(f"Invalid admin API mode: {mode}")
        if self._settings.security.admin_api_mode != mode:
            self._settings.security.admin_api_mode = mode
            self._save()
            if self._on_change:
                self._on_change(self._settings)
