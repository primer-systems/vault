"""
Settings persistence and cross-process synchronization.

Handles loading/saving settings to JSON file and watching for
external changes using watchdog.
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from ..models.store import write_json_atomic

logger = logging.getLogger(__name__)

# Canonical default port for Vault (RHC mainnet chain ID)
DEFAULT_PORT = 4663

#: Requests per minute one caller may make to the agent API. 0 means no ceiling.
#:
#: Lives here rather than in the GUI's own settings file because the ceiling
#: protects the agent server, which the daemon and the CLI also run - a limit
#: only the GUI could set would not exist in headless mode, which is the mode
#: most exposed to a LAN.
DEFAULT_RATE_LIMIT_PER_MINUTE = 300

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
        "allow_lan": False,
        "rate_limit_per_minute": DEFAULT_RATE_LIMIT_PER_MINUTE,
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
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE


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
                "rate_limit_per_minute": self.server.rate_limit_per_minute,
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
            settings.server.rate_limit_per_minute = s.get(
                "rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE)

        if "security" in data:
            s = data["security"]
            raw_mode = s.get("admin_api_mode", ADMIN_API_MODE_GUI_ONLY)
            # Validate on the way in. The admin-API gate is the only thing
            # standing between a local process and the wallet, and it treats any
            # value other than "open" as locked-down - but a hand-edited
            # settings.json can carry a near-miss of the *safe* value ("GUI_ONLY",
            # "gui-only") that is neither, and an unvalidated one would sail
            # through. Anything not exactly one of the two known modes falls back
            # to the safe one, the same way an unreadable file does.
            if raw_mode not in (ADMIN_API_MODE_OPEN, ADMIN_API_MODE_GUI_ONLY):
                logger.warning(
                    "Unrecognised admin_api_mode %r in settings.json; using the "
                    "safe default %r. Valid values are %r and %r.",
                    raw_mode, ADMIN_API_MODE_GUI_ONLY,
                    ADMIN_API_MODE_OPEN, ADMIN_API_MODE_GUI_ONLY)
                raw_mode = ADMIN_API_MODE_GUI_ONLY
            settings.security.admin_api_mode = raw_mode

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
        on_change: Optional[Callable[[AppSettings], None]] = None,
        on_save_error: Optional[Callable[[str], None]] = None
    ):
        """
        Initialize settings manager.

        Args:
            data_dir: Directory for settings file
            on_change: Callback when settings change (from file or programmatic)
            on_save_error: Callback with a user-readable message when a save
                fails. The in-memory settings stay in effect after a failed
                save, so the person who made the change is the one who needs
                to hear that it will not survive a restart - a log line does
                not reach them.
        """
        self._data_dir = data_dir
        self._settings_file = data_dir / "settings.json"
        self._settings = AppSettings()
        self._on_change = on_change
        self._on_save_error = on_save_error
        self._observer = None
        self._write_lock = threading.Lock()
        self._last_written_content: Optional[str] = None  # To detect our own writes
        # Set when settings.json exists but could not be READ at startup, and no
        # copy could be kept - so the file on disk is the only copy of the user's
        # settings. A save writes defaults over it, so saving is refused until a
        # restart reads it cleanly. See _load and _save.
        self._protected = False

        # Load existing settings
        self._load()

        # Start file watcher
        self._start_watcher()

    def _load(self) -> None:
        """Load settings from file."""
        if not self._settings_file.exists():
            logger.info("No settings file found, using defaults")
            self._save()  # Create with defaults
            return

        try:
            with open(self._settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._settings = AppSettings.from_dict(data)
            logger.info(f"Loaded settings from {self._settings_file}")
        except json.JSONDecodeError as e:
            # Readable but not valid JSON. Keep a copy so the user's settings
            # stay recoverable, then fall back to defaults. If even the copy
            # cannot be made, protect the file from the next save's overwrite.
            aside = self._set_aside()
            if aside is None:
                self._protected = True
            self._settings = AppSettings()
            logger.error("Invalid settings file, using defaults: %s. A copy "
                         "has been kept as %s.", e,
                         aside or "(copy failed; the original is untouched)")
        except Exception as e:
            # Could not open the file at all - it is the only copy of the user's
            # settings, so the next save must not overwrite it with defaults.
            self._protected = True
            self._settings = AppSettings()
            logger.error("Failed to load settings: %s. The file will be left "
                         "untouched, not overwritten, so it stays recoverable "
                         "until a restart can read it.", e)

    def _set_aside(self) -> Optional[str]:
        """Copy settings.json aside before a save can replace it. Best effort;
        returns the copy's name, or None if the copy could not be made."""
        aside = self._settings_file.with_name(self._settings_file.name + ".unreadable")
        try:
            import shutil
            shutil.copy2(self._settings_file, aside)
        except OSError:
            return None
        return aside.name

    def _save(self) -> None:
        """Save settings to file.

        Through the same atomic writer the policy store uses. An in-place
        write truncates the file first, so a save that fails midway destroys
        the previous settings, which the next start then silently
        replaces with defaults - re-enabling networks the user had disabled
        and discarding a custom RPC endpoint.
        """
        with self._write_lock:
            if self._protected:
                message = ("Not saving settings: settings.json could not be "
                           "read at startup and no copy was kept, so it is "
                           "left untouched to stay recoverable. Your change is "
                           "active now but will be lost when Vault restarts; "
                           "restart while the file is readable to save it.")
                logger.error(message)
                if self._on_save_error:
                    try:
                        self._on_save_error(message)
                    except Exception:
                        logger.exception("Settings save-error callback failed")
                return
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                data = self._settings.to_dict()
                # Remembered before the write so the watcher recognises the
                # resulting file as our own when its event arrives.
                self._last_written_content = json.dumps(data, indent=2)
                write_json_atomic(self._settings_file, data)
                logger.debug(f"Saved settings to {self._settings_file}")
            except Exception as e:
                # The change is already live in memory; only the file is
                # stale. Whoever made the change needs to hear that, not
                # just the log.
                message = (f"Could not save settings ({e}). Your change is "
                           "active now but will be lost when Vault restarts.")
                logger.error(message)
                if self._on_save_error:
                    try:
                        self._on_save_error(message)
                    except Exception:
                        logger.exception("Settings save-error callback failed")

    def _start_watcher(self) -> None:
        """Start watching the settings file for changes."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class SettingsFileHandler(FileSystemEventHandler):
                """React to any event that can land new contents at
                settings.json. An in-place editor fires on_modified; an
                atomic save - ours, or an editor doing write-temp-then-
                rename - arrives as on_moved or on_created depending on
                platform. Handling only on_modified missed renames."""

                def __init__(handler_self, manager):
                    handler_self.manager = manager

                def _maybe_reload(handler_self, path):
                    if Path(path).name == "settings.json":
                        handler_self.manager._on_file_changed()

                def on_modified(handler_self, event):
                    if not event.is_directory:
                        handler_self._maybe_reload(event.src_path)

                def on_created(handler_self, event):
                    if not event.is_directory:
                        handler_self._maybe_reload(event.src_path)

                def on_moved(handler_self, event):
                    if not event.is_directory:
                        handler_self._maybe_reload(event.dest_path)

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
        """Handle a settings-file change seen by the watcher.

        Our own saves land here too - a rename raises a watcher event just as
        an external edit does. They are recognised by content: whatever we
        last wrote is not news. Content comparison is the right recogniser here:
        a save can raise no event or several, so anything that counts events
        (a "skip the next reload" flag) can swallow a real external change or
        reload our own write. Content cannot drift that way.

        It is not airtight either: `_last_written_content` records our own
        saves and nothing else, so an external edit that happens to restore
        exactly what we last wrote - a reverted file, a restored backup - is
        indistinguishable from our own write and is ignored. Vault then holds
        settings the file no longer shows, until its next save.

        The comparison must run under the write lock. Watcher events arrive
        late: an event from save N can land while save N+1 is underway, read
        the not-yet-replaced file, see it differ from what save N+1 just
        recorded, and "reload" stale settings over the newer in-memory ones.
        Holding the lock means this only ever compares a settled file against
        the record of the save that produced it.
        """
        changed = None
        try:
            with self._write_lock:
                with open(self._settings_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Our own write, arriving back via the watcher
                if content == self._last_written_content:
                    return

                # Parse and update
                data = json.loads(content)
                self._settings = AppSettings.from_dict(data)
                changed = self._settings
                logger.info("Settings reloaded from external change")

        except FileNotFoundError:
            # Events can arrive for a path that is gone again already - the
            # file is being replaced this instant. The event for the version
            # that lands will find it.
            pass
        except Exception as e:
            logger.error(f"Failed to reload settings: {e}")

        # The listener runs outside the lock: it may react by changing a
        # setting itself, which saves, which takes the lock again.
        if changed is not None and self._on_change:
            self._on_change(changed)

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

    def get_rate_limit(self) -> int:
        """Requests per minute allowed from one caller. 0 means no ceiling."""
        return self._settings.server.rate_limit_per_minute

    def set_rate_limit(self, requests_per_minute: int) -> None:
        """Set the agent API's per-caller ceiling. 0 means no ceiling."""
        value = max(0, int(requests_per_minute))
        if self._settings.server.rate_limit_per_minute != value:
            self._settings.server.rate_limit_per_minute = value
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
