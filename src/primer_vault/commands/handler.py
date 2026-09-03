"""
CommandHandler - Main command router and executor.

Pure Python, no Qt dependencies. Receives commands as strings,
returns CommandResult objects.
"""

import shlex
from typing import Optional, TYPE_CHECKING

from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault


class CommandHandler:
    """
    Handles command parsing and execution.

    Usage:
        handler = CommandHandler(core)
        result = handler.execute("agent list")
        print(result.output)
    """

    def __init__(self, core: "Vault"):
        self.core = core
        self._pending_command: Optional[dict] = None  # For multi-step commands

        # Command routing table
        self._commands = {
            "help": self._cmd_help,
            "status": self._cmd_status,
            "clear": self._cmd_clear,
            "exit": self._cmd_exit,
            "agent": self._cmd_agent,
            "policy": self._cmd_policy,
            "pending": self._cmd_pending,
            "approve": self._cmd_approve,
            "reject": self._cmd_reject,
            "trade": self._cmd_trade,
            "position": self._cmd_position,
            "venues": self._cmd_venues,
            "server": self._cmd_server,
            "wallet": self._cmd_wallet,
            "seed": self._cmd_seed,
            "address": self._cmd_address,
            "history": self._cmd_history,
            "config": self._cmd_config,
        }

    def execute(self, command: str, inputs: Optional[dict] = None) -> CommandResult:
        """
        Execute a command string.

        Args:
            command: The command string (e.g., "agent register mybot --auth bearer")
            inputs: Optional dict of inputs for commands that need them
                    (e.g., {"password": "secret"} for wallet commands)

        Returns:
            CommandResult with output, success status, and any needed input prompts
        """
        # Handle pending multi-step command
        if self._pending_command and inputs:
            try:
                return self._resume_command(inputs)
            except Exception as e:
                return CommandResult.fail(str(e))

        # Parse command
        try:
            parts = shlex.split(command.strip())
        except ValueError as e:
            return CommandResult.fail(f"Parse error: {e}")

        if not parts:
            return CommandResult.ok()

        cmd = parts[0].lower()
        args = parts[1:]

        # Check for empty command (can happen with empty string input)
        if not cmd:
            return CommandResult.fail("No command provided. Run 'help' for available commands.")

        # Route to handler
        handler = self._commands.get(cmd)
        if handler:
            try:
                return handler(args)
            except Exception as e:
                # No "Error:" prefix here - the CLI and the console each add
                # their own when they print, and a prefix baked into the
                # message shows the user "Error: Error:".
                return CommandResult.fail(str(e))
        else:
            return CommandResult.fail(f"Unknown command: {cmd}")

    def _resume_command(self, inputs: dict) -> CommandResult:
        """Resume a pending multi-step command with provided inputs."""
        if not self._pending_command:
            return CommandResult.fail("No pending command")

        pending = self._pending_command
        self._pending_command = None

        handler = pending.get("handler")
        if handler and callable(handler):
            return handler(inputs, **pending.get("context", {}))

        return CommandResult.fail("Invalid pending command state")

    def _set_pending(self, handler, **context) -> None:
        """Set up a pending command that needs input."""
        self._pending_command = {
            "handler": handler,
            "context": context
        }

    # =========================================================================
    # Basic Commands
    # =========================================================================

    def _cmd_help(self, args: list[str]) -> CommandResult:
        """Show help."""
        help_text = """
CONSOLE
  help                                    - Show this help
  clear                                   - Clear console
  exit                                    - Close console
  status                                  - Show system status

AGENTS
  agent list                              - List all agents
  agent show <agent>                      - Show agent details
  agent register <name> [--auth hmac|bearer] - Register new agent
  agent commission <agent> <policy> <address> [--mandate] [--upload]
  agent edit <agent> [--policy P] [--address A] - Edit agent
  agent suspend <agent>                   - Suspend agent
  agent activate <agent>                  - Activate agent
  agent delete <agent>                    - Delete agent

POLICIES
  policy list                             - List all policies
  policy show <policy>                    - Show policy details
  policy create <name> [options]          - Create policy
  policy edit <policy> [options]          - Edit policy
  policy delete <policy>                  - Delete policy
  (x402: --day N, --txn N, --auto N, --x402, --no-x402)
  (trading: --trading, --trade-max N, --trade-daily N, --trade-auto N)

APPROVALS
  pending                                 - List pending requests
  approve <request>                       - Approve request
  reject <request> [reason]               - Reject request

TRADING
  trade pending                           - List trades awaiting approval
  trade approve <id>                      - Approve and execute a trade
  trade reject <id> [reason]              - Reject a trade

MORPHO LENDING
  venues                                  - Vaults and markets that are permitted
  position pending                        - List lending requests awaiting approval
  position approve <id>                   - Approve and execute a request
  position reject <id> [reason]           - Reject a request

SERVER
  server start [port]                     - Start agent server
  server stop                             - Stop server
  server status                           - Show server status

WALLET
  wallet status                           - Show wallet status
  wallet create <name>                    - Create new wallet
  wallet open [path]                      - Open/unlock wallet
  wallet lock                             - Lock wallet
  wallet detach                           - Unload wallet (keep file)
  wallet delete                           - Delete wallet file

SEEDS
  seed list                               - List all seeds
  seed create [--words 12|24]             - Generate new seed
  seed import [phrase]                    - Import seed phrase
  seed delete <seed>                      - Delete seed and addresses

ADDRESSES
  address list                            - List addresses
  address create [seed] [index] [name]    - Derive new address
  address import <key> [name]             - Import private key
  address delete <address>                - Delete address
  address rename <address> <name>         - Rename address
  address export <address>                - Export private key
  address balance [address]               - Check on-chain balance

HISTORY
  history [limit]                         - List recent transactions
  history show <tx>                       - Show transaction details
  history export [file]                   - Export transactions to CSV
  history verify <tx>                     - Verify transaction on-chain
  history receipt <tx>                    - Get AP2-formatted receipt
  history clear                           - Clear history

CONFIG
  config                                  - Show all settings
  config show                             - Show all settings
  config get <setting>                    - Get a specific setting
  config set <setting> <value>            - Set a setting

Use <command> --help for detailed options.
"""
        return CommandResult.ok(help_text.strip())

    def _cmd_status(self, args: list[str]) -> CommandResult:
        """Show system status."""
        lines = []

        # Wallet status
        if self.core.is_wallet_unlocked():
            addresses = self.core.get_wallet_addresses()
            lines.append(f"Wallet: Unlocked ({len(addresses)} addresses)")
        else:
            try:
                has_wallets = self.core.get_wallet_path() is not None
            except Exception:
                try:
                    wallet_dir = self.core.get_wallet_dir()
                    has_wallets = any(wallet_dir.glob("*.wallet"))
                except Exception:
                    has_wallets = True
            lines.append("Wallet: Locked" if has_wallets else "Wallet: None")

        # Server status
        if self.core.is_server_running():
            lines.append(f"Server: Running on port {self.core.server_port}")
        else:
            lines.append("Server: Stopped")

        # Agents
        agents = self.core.get_all_agents()
        active = sum(1 for a in agents if a.status == "active")
        lines.append(f"Agents: {len(agents)} total, {active} active")

        # Policies
        policies = self.core.get_all_policies()
        lines.append(f"Policies: {len(policies)}")

        # Pending requests
        pending = self.core.get_pending_requests()
        if pending:
            lines.append(f"Pending: {len(pending)} request(s) awaiting approval")

        return CommandResult.ok("\n".join(lines))

    def _cmd_clear(self, args: list[str]) -> CommandResult:
        """Clear screen (no-op for CLI, Console handles this)."""
        return CommandResult.ok("", data={"action": "clear"})

    def _cmd_exit(self, args: list[str]) -> CommandResult:
        """Exit command."""
        return CommandResult.ok("", data={"action": "exit"})

    # =========================================================================
    # Agent Commands
    # =========================================================================

    def _cmd_agent(self, args: list[str]) -> CommandResult:
        """Handle agent commands."""
        from .agent import AgentCommands
        return AgentCommands(self.core, self).execute(args)

    # =========================================================================
    # Policy Commands
    # =========================================================================

    def _cmd_policy(self, args: list[str]) -> CommandResult:
        """Handle policy commands."""
        from .policy import PolicyCommands
        return PolicyCommands(self.core, self).execute(args)

    # =========================================================================
    # Approval Commands
    # =========================================================================

    def _cmd_pending(self, args: list[str]) -> CommandResult:
        """List pending requests."""
        from .approval import ApprovalCommands
        return ApprovalCommands(self.core, self).pending(args)

    def _cmd_approve(self, args: list[str]) -> CommandResult:
        """Approve a request."""
        from .approval import ApprovalCommands
        return ApprovalCommands(self.core, self).approve(args)

    def _cmd_reject(self, args: list[str]) -> CommandResult:
        """Reject a request."""
        from .approval import ApprovalCommands
        return ApprovalCommands(self.core, self).reject(args)

    def _cmd_trade(self, args: list[str]) -> CommandResult:
        """Manage pending agent trades (pending / approve / reject)."""
        from .trade import TradeCommands
        return TradeCommands(self.core, self).execute(args)

    def _cmd_position(self, args: list[str]) -> CommandResult:
        """Manage pending agent lending (pending / approve / reject)."""
        from .position import PositionCommands
        return PositionCommands(self.core, self).execute(args)

    def _cmd_venues(self, args: list[str]) -> CommandResult:
        """List the Morpho venues this Vault permits."""
        from .position import VenuesCommands
        return VenuesCommands(self.core, self).execute(args)

    # =========================================================================
    # Server Commands
    # =========================================================================

    def _cmd_server(self, args: list[str]) -> CommandResult:
        """Handle server commands."""
        from .server import ServerCommands
        return ServerCommands(self.core, self).execute(args)

    # =========================================================================
    # Wallet Commands
    # =========================================================================

    def _cmd_wallet(self, args: list[str]) -> CommandResult:
        """Handle wallet commands."""
        from .wallet import WalletCommands
        return WalletCommands(self.core, self).execute(args)

    def _cmd_seed(self, args: list[str]) -> CommandResult:
        """Handle seed commands."""
        from .wallet import SeedCommands
        return SeedCommands(self.core, self).execute(args)

    def _cmd_address(self, args: list[str]) -> CommandResult:
        """Handle address commands."""
        from .wallet import AddressCommands
        return AddressCommands(self.core, self).execute(args)

    # =========================================================================
    # History Commands
    # =========================================================================

    def _cmd_history(self, args: list[str]) -> CommandResult:
        """Handle history commands."""
        from .history import HistoryCommands
        return HistoryCommands(self.core, self).execute(args)

    # =========================================================================
    # Config Commands
    # =========================================================================

    def _cmd_config(self, args: list[str]) -> CommandResult:
        """Handle config commands."""
        from .config import ConfigCommands
        return ConfigCommands(self.core, self).execute(args)
