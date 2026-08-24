"""
Server command implementations.
"""

from typing import TYPE_CHECKING

from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


class ServerCommands:
    """Server-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route server subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "start":
            if "--help" in args or "-h" in args:
                return self._start_help()
            port = 4663
            if len(args) > 1:
                port_str = args[1]
                # Accept "server start --port 9410" as well as "server start 9410"
                if port_str == "--port":
                    if len(args) < 3:
                        return CommandResult.fail("Missing port value after --port")
                    port_str = args[2]
                elif port_str.startswith("--"):
                    return CommandResult.fail(
                        f"Unknown option: {port_str}\n"
                        "Usage: server start [port]\n"
                        "Example: server start 9410"
                    )
                try:
                    port = int(port_str)
                    if not (1 <= port <= 65535):
                        return CommandResult.fail(f"Invalid port {port_str}: must be between 1 and 65535")
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid port '{port_str}': expected a number\n"
                        "Usage: server start [port]\n"
                        "Example: server start 9410"
                    )
            if port == 4664:
                return CommandResult.fail(
                    "Port 4664 is reserved for the admin API and cannot be used for the agent server."
                )
            return self._start(port)
        elif subcmd == "stop":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("server stop - Stop the agent HTTP server.")
            return self._stop()
        elif subcmd == "status":
            return self._status()
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show server command help."""
        help_text = """server - Manage the agent API server

Subcommands:
  start [port]    - Start server (default port: 4663)
  stop            - Stop server
  status          - Show server status"""
        return CommandResult.ok(help_text)

    def _start_help(self) -> CommandResult:
        """Help for server start."""
        return CommandResult.ok("""server start - Start the agent API server

Usage: server start [port]

Arguments:
  [port]   Port number (default: 4663)

Example:
  server start 8080""")

    def _start(self, port: int) -> CommandResult:
        """Start the server."""
        if self.core.is_server_running():
            return CommandResult.ok(
                f"Server already running on port {self.core.server_port}.",
                data={"running": True, "port": self.core.server_port}
            )
        if self.core.start_server(port):
            # Check if wallet is locked and warn user
            if not self.core.is_wallet_unlocked():
                return CommandResult.ok(
                    f"Server started on port {port}.\n"
                    "Warning: Wallet is locked - signing disabled until unlocked.",
                    data={"running": True, "port": port, "wallet_locked": True}
                )
            return CommandResult.ok(
                f"Server started on port {port}.",
                data={"running": True, "port": port, "wallet_locked": False}
            )
        else:
            return CommandResult.fail("Failed to start server.")

    def _stop(self) -> CommandResult:
        """Stop the server."""
        self.core.stop_server()
        return CommandResult.ok("Server stopped.")

    def _status(self) -> CommandResult:
        """Show server status."""
        if self.core.is_server_running():
            return CommandResult.ok(
                f"Server running on port {self.core.server_port}.",
                data={"running": True, "port": self.core.server_port}
            )
        else:
            return CommandResult.ok("Server not running.", data={"running": False})
