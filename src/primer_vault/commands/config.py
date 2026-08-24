"""
Config commands - CLI interface for settings management.
"""

from typing import TYPE_CHECKING

from ..core.settings import ADMIN_API_MODE_GUI_ONLY, ADMIN_API_MODE_OPEN
from .result import CommandResult
from ..networks import NETWORKS

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


# Known networks with their display names (derived from the central registry)
KNOWN_NETWORKS = {chain_id: cfg.display_name for chain_id, cfg in NETWORKS.items()}


class ConfigCommands:
    """Handles config commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Execute a config subcommand."""
        if not args:
            return self._cmd_show([])

        subcmd = args[0].lower()
        subargs = args[1:]

        if subcmd in ("--help", "-h", "help"):
            return self._cmd_help()
        elif subcmd == "show":
            return self._cmd_show(subargs)
        elif subcmd == "set":
            return self._cmd_set(subargs)
        elif subcmd == "get":
            return self._cmd_get(subargs)
        else:
            return CommandResult.fail(f"Unknown config command: {subcmd}")

    def _cmd_help(self) -> CommandResult:
        """Help for the config command."""
        return CommandResult.ok(
            "config - View and change Vault settings\n\n"
            "Usage:\n"
            "  config show                 Show all settings\n"
            "  config get <setting>        Read one setting\n"
            "  config set <setting> <val>  Change one setting\n\n"
            "Settings:\n"
            "  admin-api           open | gui-only  (who may drive a running instance)\n"
            "  verify-settlements  on | off\n"
            "  replay-window       seconds\n"
            "  default-port        agent API port\n"
            "  allow-lan           on | off  (expose the agent API on the LAN)\n"
            "  rate-limit          requests per minute\n"
            "  default-network     chain id\n"
            "  network <id>        on | off\n"
            "  rpc <id> <url|default>  set a chain's RPC endpoint, or 'default' to clear\n\n"
            "Examples:\n"
            "  config set admin-api open\n"
            "  config get admin-api")

    def _cmd_show(self, args: list[str]) -> CommandResult:
        """Show current settings."""
        settings = self.core.settings_manager.settings

        lines = ["Current Settings:"]
        lines.append("")
        lines.append("Signing:")
        lines.append(f"  verify-settlements: {'on' if settings.signing.verify_settlements else 'off'}")
        lines.append(f"  replay-window: {settings.signing.max_request_age_seconds}s")

        lines.append("")
        lines.append("Networks:")
        for chain_id_str, enabled in sorted(settings.signing.enabled_networks.items()):
            try:
                chain_id = int(chain_id_str)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                status = "enabled" if enabled else "disabled"
                lines.append(f"  {chain_id} ({name}): {status}")
            except ValueError:
                pass

        lines.append("")
        lines.append("Server:")
        lines.append(f"  default-port: {settings.server.default_port}")
        lines.append(f"  allow-lan: {'on' if settings.server.allow_lan else 'off'}")
        limit = settings.server.rate_limit_per_minute
        lines.append(f"  rate-limit: {limit if limit else 'unlimited'}"
                     f"{' req/min' if limit else ''}")

        lines.append("")
        lines.append("Security:")
        mode = settings.security.admin_api_mode
        lines.append(f"  admin-api: {mode}")
        if mode == ADMIN_API_MODE_OPEN:
            lines.append("    any local process can drive the Admin API")

        lines.append("")
        lines.append("Display:")
        default_net = settings.display.default_network
        net_name = KNOWN_NETWORKS.get(default_net, f"Chain {default_net}")
        lines.append(f"  default-network: {default_net} ({net_name})")

        lines.append("")
        lines.append("RPC Endpoints:")
        for chain_id, name in sorted(KNOWN_NETWORKS.items()):
            endpoint = settings.rpc.endpoints.get(str(chain_id))
            value = endpoint if endpoint else "(default)"
            lines.append(f"  {chain_id} ({name}): {value}")

        return CommandResult.ok("\n".join(lines))

    def _cmd_get(self, args: list[str]) -> CommandResult:
        """Get a specific setting value."""
        if not args:
            return CommandResult.fail("Usage: config get <setting>")

        setting = args[0].lower()
        settings = self.core.settings_manager

        if setting == "admin-api":
            return CommandResult.ok(f"admin-api: {settings.get_admin_api_mode()}")

        if setting == "verify-settlements":
            value = "on" if settings.get_verify_settlements() else "off"
            return CommandResult.ok(f"verify-settlements: {value}")

        elif setting == "replay-window":
            value = settings.get_max_request_age()
            return CommandResult.ok(f"replay-window: {value}s")

        elif setting == "default-port":
            value = settings.get_default_port()
            return CommandResult.ok(f"default-port: {value}")

        elif setting == "allow-lan":
            value = "on" if settings.get_allow_lan() else "off"
            return CommandResult.ok(f"allow-lan: {value}")

        elif setting == "rate-limit":
            limit = settings.get_rate_limit()
            return CommandResult.ok(
                f"rate-limit: {limit} req/min" if limit else "rate-limit: unlimited")

        elif setting == "default-network":
            value = settings.get_default_network()
            name = KNOWN_NETWORKS.get(value, "")
            return CommandResult.ok(f"default-network: {value} ({name})")

        elif setting.startswith("network"):
            # config get network 4663
            if len(args) < 2:
                return CommandResult.fail("Usage: config get network <chain_id>")
            try:
                chain_id = int(args[1])
                enabled = settings.is_network_enabled(chain_id)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                status = "enabled" if enabled else "disabled"
                return CommandResult.ok(f"{chain_id} ({name}): {status}")
            except ValueError:
                return CommandResult.fail(f"Invalid chain ID: {args[1]}")

        elif setting.startswith("rpc"):
            # config get rpc 4663
            if len(args) < 2:
                return CommandResult.fail("Usage: config get rpc <chain_id>")
            try:
                chain_id = int(args[1])
                endpoint = settings.get_rpc_endpoint(chain_id)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                value = endpoint if endpoint else "(default)"
                return CommandResult.ok(f"rpc {chain_id} ({name}): {value}")
            except ValueError:
                return CommandResult.fail(f"Invalid chain ID: {args[1]}")

        else:
            return CommandResult.fail(f"Unknown setting: {setting}")

    def _cmd_set(self, args: list[str]) -> CommandResult:
        """Set a configuration value."""
        if len(args) < 2:
            return CommandResult.fail(
                "Usage: config set <setting> <value>\n"
                "\n"
                "Settings:\n"
                "  verify-settlements on|off    - Enable/disable settlement verification\n"
                "  replay-window <seconds>      - Set max request age (min 30)\n"
                "  network <chain_id> on|off    - Enable/disable a network\n"
                "  default-port <port>          - Set default server port\n"
                "  allow-lan on|off             - Allow LAN connections\n"
                "  default-network <chain_id>   - Set default network for display\n"
                "  rpc <chain_id> <url|default> - Set custom RPC endpoint\n"
                "  admin-api open|gui-only      - Who may drive the Admin API"
            )

        setting = args[0].lower()
        value = args[1]
        settings = self.core.settings_manager

        if setting == "verify-settlements":
            if value.lower() in ("on", "true", "1", "yes"):
                settings.set_verify_settlements(True)
                return CommandResult.ok("Settlement verification enabled")
            elif value.lower() in ("off", "false", "0", "no"):
                settings.set_verify_settlements(False)
                return CommandResult.ok("Settlement verification disabled")
            else:
                return CommandResult.fail("Value must be 'on' or 'off'")

        elif setting == "replay-window":
            try:
                seconds = int(value)
                if seconds < 30:
                    return CommandResult.fail("Replay window must be at least 30 seconds")
                settings.set_max_request_age(seconds)
                return CommandResult.ok(f"Replay window set to {seconds}s")
            except ValueError:
                return CommandResult.fail("Value must be a number of seconds")

        elif setting == "network":
            # config set network 4663 on
            if len(args) < 3:
                return CommandResult.fail("Usage: config set network <chain_id> on|off")
            try:
                chain_id = int(value)
            except ValueError:
                return CommandResult.fail(f"Invalid chain ID: {value}")

            enable_value = args[2].lower()
            if enable_value in ("on", "true", "1", "yes", "enabled"):
                settings.set_network_enabled(chain_id, True)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                if chain_id not in KNOWN_NETWORKS:
                    return CommandResult.ok(
                        f"Warning: Network {chain_id} is not a recognized chain. "
                        f"Balance checking and transaction verification will not work.\n"
                        f"Network {chain_id} enabled."
                    )
                return CommandResult.ok(f"Network {chain_id} ({name}) enabled")
            elif enable_value in ("off", "false", "0", "no", "disabled"):
                settings.set_network_enabled(chain_id, False)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                return CommandResult.ok(f"Network {chain_id} ({name}) disabled")
            else:
                return CommandResult.fail("Value must be 'on' or 'off'")

        elif setting == "default-port":
            try:
                port = int(value)
                if port < 1 or port > 65535:
                    return CommandResult.fail("Port must be between 1 and 65535")
                settings.set_default_port(port)
                return CommandResult.ok(f"Default port set to {port}")
            except ValueError:
                return CommandResult.fail("Value must be a port number")

        elif setting == "rate-limit":
            # Reachable without a screen on purpose: the ceiling matters most on
            # a headless box, which is the one most likely to be exposed.
            try:
                per_minute = int(value)
            except ValueError:
                return CommandResult.fail("Value must be a number of requests per minute")
            if per_minute < 0:
                return CommandResult.fail("Value must be 0 or more (0 = no limit)")
            settings.set_rate_limit(per_minute)
            return CommandResult.ok(
                f"Agent API rate limit set to {per_minute} req/min" if per_minute
                else "Agent API rate limit removed")

        elif setting == "allow-lan":
            if value.lower() in ("on", "true", "1", "yes"):
                settings.set_allow_lan(True)
                return CommandResult.ok("LAN connections allowed")
            elif value.lower() in ("off", "false", "0", "no"):
                settings.set_allow_lan(False)
                return CommandResult.ok("LAN connections disabled")
            else:
                return CommandResult.fail("Value must be 'on' or 'off'")

        elif setting == "admin-api":
            # Deliberately opt-in, and deliberately reachable without a screen.
            # The point of the default was that opening this port is a conscious
            # act, not that the act happens in the GUI - and on a headless box
            # the GUI is not somewhere the operator can go.
            choice = value.lower().replace("_", "-")
            if choice == "open":
                settings.set_admin_api_mode(ADMIN_API_MODE_OPEN)
                return CommandResult.ok(
                    "Admin API opened to local processes.\n\n"
                    "Any program running as you can now create agents, read wallet\n"
                    "addresses and approve requests through port "
                    "4664. "
                    "Only do this on a machine you trust.")
            elif choice in ("gui-only", "guionly"):
                settings.set_admin_api_mode(ADMIN_API_MODE_GUI_ONLY)
                return CommandResult.ok(
                    "Admin API restricted to the Vault window (the default).")
            else:
                return CommandResult.fail("Value must be 'open' or 'gui-only'")

        elif setting == "default-network":
            try:
                chain_id = int(value)
                settings.set_default_network(chain_id)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                return CommandResult.ok(f"Default network set to {chain_id} ({name})")
            except ValueError:
                return CommandResult.fail("Value must be a chain ID")

        elif setting == "rpc":
            # config set rpc 4663 https://example.com
            if len(args) < 3:
                return CommandResult.fail("Usage: config set rpc <chain_id> <url|default>")
            try:
                chain_id = int(value)
            except ValueError:
                return CommandResult.fail(f"Invalid chain ID: {value}")

            endpoint = args[2]
            if endpoint.lower() == "default":
                settings.set_rpc_endpoint(chain_id, None)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                return CommandResult.ok(f"RPC endpoint for {chain_id} ({name}) reset to default")
            else:
                settings.set_rpc_endpoint(chain_id, endpoint)
                name = KNOWN_NETWORKS.get(chain_id, f"Chain {chain_id}")
                return CommandResult.ok(f"RPC endpoint for {chain_id} ({name}) set to {endpoint}")

        else:
            return CommandResult.fail(f"Unknown setting: {setting}")
