"""
Wallet, Seed, and Address command implementations.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..wallet.crypto import MIN_PASSWORD_LENGTH
from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


def _extract_flag(args: list[str], flag: str) -> tuple[Optional[str], list[str]]:
    """Extract a --flag <value> from args. Returns (value_or_None, remaining_args)."""
    if flag not in args:
        return None, list(args)
    idx = args.index(flag)
    value = args[idx + 1] if idx + 1 < len(args) else ""
    remaining = [a for i, a in enumerate(args) if i != idx and i != idx + 1]
    return value, remaining


# --------------------------------------------------------------------- ledger

#: Path types `address ledger` accepts, in the order the desktop's connect
#: dialog lists them, with the same default. A device enrolled from either
#: edition therefore lands on the same addresses - which matters, because the
#: path is what the wallet stores and what every later signature is checked
#: against.
LEDGER_PATH_TYPES = ("ledger_live", "bip44", "legacy_mew", "custom")
DEFAULT_LEDGER_PATH_TYPE = "ledger_live"

#: Ceiling on how many addresses one `list` may sweep. Each index is a separate
#: USB round trip - the desktop hides that behind a worker thread and a Load
#: More button, and a terminal has neither, so an unbounded --count would look
#: like a hang for as long as it took.
LEDGER_MAX_PAGE = 50

#: What to check when the device will not answer. The last item only bites the
#: terminal edition: an engine started by systemd runs as its own user, and USB
#: access to the device is a permission that user may not have been granted.
_LEDGER_CHECKLIST = (
    "  - Plug the Ledger into this machine and unlock it with your PIN\n"
    "  - Open the Ethereum app on the device\n"
    "  - Close Ledger Live, or anything else holding the device\n"
    "  - If Vault runs as a system service, that service's user needs USB\n"
    "    access to the device (on Linux, a udev rule)"
)


def _parse_ledger_path_type(name: Optional[str], custom_path: Optional[str]):
    """Resolve --path-type/--path into a LedgerPathType. Returns (type, error)."""
    from ..wallet.ledger import LedgerPathType

    chosen = (name or DEFAULT_LEDGER_PATH_TYPE).strip().lower().replace("-", "_")
    if chosen not in LEDGER_PATH_TYPES:
        return None, (
            f"Unknown path type: {name}\n"
            f"Choose one of: {', '.join(LEDGER_PATH_TYPES)}"
        )

    path_type = LedgerPathType(chosen)
    if path_type == LedgerPathType.CUSTOM and not custom_path:
        return None, (
            "--path-type custom also needs the template to use:\n"
            "  address ledger list --path-type custom --path \"m/44'/60'/0'/0/{index}\""
        )
    return path_type, None


def _parse_count(value: Optional[str], flag: str, default: int,
                 minimum: int = 0, maximum: Optional[int] = None):
    """Parse a numeric flag. Returns (number, error)."""
    if value is None:
        return default, None
    try:
        number = int(value)
    except ValueError:
        return None, f"Invalid {flag} '{value}': expected a number"
    if number < minimum:
        return None, f"Invalid {flag} {number}: must be {minimum} or more"
    if maximum is not None and number > maximum:
        return None, f"Invalid {flag} {number}: {maximum} is the most one call may read"
    return number, None


class WalletCommands:
    """Wallet-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route wallet subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "status":
            return self._status()
        elif subcmd == "lock":
            if "--help" in args or "-h" in args:
                return CommandResult.ok(
                    "wallet lock - Lock the wallet, clearing its keys from memory.")
            return self._lock()
        elif subcmd == "create":
            if "--help" in args or "-h" in args:
                return self._create_help()
            # Parse --password flag for non-interactive mode
            password_flag, name_args = _extract_flag(args[1:], "--password")
            if not name_args:
                return CommandResult.need_input("text", "Enter wallet name:")
            if password_flag is not None:
                return self._create_noninteractive(name_args[0], password_flag)
            return self._create(name_args[0])
        elif subcmd == "open":
            if "--help" in args or "-h" in args:
                return self._open_help()
            password_flag, name_args = _extract_flag(args[1:], "--password")
            if not name_args:
                return CommandResult.need_input("text", "Enter wallet name:")
            if password_flag is not None:
                return self._open_noninteractive(name_args[0], password_flag)
            return self._open(name_args[0])
        elif subcmd == "detach":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("wallet detach - Unload wallet without deleting file\n\nUsage: wallet detach")
            return self._detach()
        elif subcmd == "delete":
            if "--help" in args or "-h" in args:
                return self._delete_help()
            return self._delete(force=("--yes" in args or "-y" in args))
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show wallet command help."""
        help_text = """wallet - Manage wallet

Subcommands:
  status        - Show wallet status
  create <name> - Create new wallet
  open <path>   - Open/unlock wallet
  lock          - Lock wallet
  detach        - Unload wallet without deleting
  delete        - Delete current wallet file"""
        return CommandResult.ok(help_text)

    def _status(self) -> CommandResult:
        """Show wallet status."""
        if self.core.is_wallet_unlocked():
            addresses = self.core.get_wallet_addresses()
            return CommandResult.ok(
                f"Wallet unlocked. {len(addresses)} address(es).",
                data={"unlocked": True, "address_count": len(addresses)}
            )
        else:
            # The tracked wallet path - set on load, cleared on detach, kept on
            # lock - is what distinguishes "locked" from "no wallet loaded".
            has_wallets = self.core.get_wallet_path() is not None
            msg = "Wallet locked." if has_wallets else "No wallet loaded."
            return CommandResult.ok(msg, data={"unlocked": False, "has_wallet": has_wallets})

    def _lock(self) -> CommandResult:
        """Lock the wallet."""
        was_unlocked = self.core.is_wallet_unlocked()
        self.core.lock_wallet()
        if was_unlocked:
            return CommandResult.ok("Wallet locked.")
        else:
            return CommandResult.ok("No wallet loaded (already locked).")

    def _detach(self) -> CommandResult:
        """Detach (unload) wallet without deleting the file."""
        if not self.core.detach_wallet():
            return CommandResult.ok("No wallet is currently loaded.")
        return CommandResult.ok("Wallet detached.")

    def _create_help(self) -> CommandResult:
        """Help for wallet create."""
        return CommandResult.ok("""wallet create - Create a new HD wallet

Usage: wallet create <name> [--password <pass>]

Arguments:
  <name>              Required. Name for the new wallet file.

Options:
  --password <pass>   Set password non-interactively (at least 8 characters)

Creates a new wallet file in the wallets directory.
Without --password you will be prompted to set a password.
A 12-word recovery phrase will be shown - save it!""")

    def _create_noninteractive(self, name: str, password: str) -> CommandResult:
        """Create a wallet without interactive prompts."""
        if not name or not name.strip():
            return CommandResult.fail("Wallet name cannot be empty.")
        if any(c in name for c in r'/\:*?"<>|'):
            return CommandResult.fail("Wallet name contains invalid characters.")

        wallet_dir = self.core.get_wallet_dir()
        wallet_path = wallet_dir / f"{name}.wallet"
        if wallet_path.exists():
            return CommandResult.fail(f"Wallet already exists: {name}")
        result = self.core.create_wallet(
            wallet_path=str(wallet_path),
            password=password,
            seed_phrase=None,
            unlock=True
        )
        if result.get("success"):
            seed_phrase = result.get("seed_phrase", "")
            addresses = result.get("addresses", [])
            lines = [
                "Wallet created!",
                "",
                "IMPORTANT: Write down this recovery phrase:",
                "",
                f"  {seed_phrase}",
                "",
                "Store it safely. It cannot be recovered!",
                "",
                f"Wallet saved to: {wallet_path.name}",
                f"Addresses: {len(addresses)}",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "seed_phrase": seed_phrase,
                "wallet_path": str(wallet_path),
                "addresses": addresses,
            })
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _create(self, name: str, inputs: dict = None) -> CommandResult:
        """Create a new wallet with password prompts."""
        if not name or not name.strip():
            return CommandResult.fail("Wallet name cannot be empty.")
        if any(c in name for c in r'/\:*?"<>|'):
            return CommandResult.fail("Wallet name contains invalid characters.")

        wallet_dir = self.core.get_wallet_dir()
        wallet_path = wallet_dir / f"{name}.wallet"

        if wallet_path.exists():
            return CommandResult.fail(f"Wallet already exists: {name}")

        # Password flow: need password and confirmation
        if inputs is None:
            # First call - ask for password
            self.handler._set_pending(
                lambda inp, **ctx: self._create(ctx["name"], inp),
                name=name
            )
            return CommandResult.need_input(
                "password",
                f"Enter password (at least {MIN_PASSWORD_LENGTH} characters):",
                step="password"
            )

        if inputs.get("step") == "password" or "password" in inputs and "confirm" not in inputs:
            # Got password, now need confirmation
            password = inputs.get("password", inputs.get("value", ""))
            self.handler._set_pending(
                lambda inp, **ctx: self._create(ctx["name"], {"password": ctx["password"], "confirm": inp.get("value", inp.get("password", ""))}),
                name=name,
                password=password
            )
            return CommandResult.need_input(
                "password",
                "Confirm password:",
                step="confirm"
            )

        # Got both password and confirmation
        password = inputs.get("password", "")
        confirm = inputs.get("confirm", "")

        if confirm != password:
            return CommandResult.fail("Passwords do not match.")

        # Create the wallet
        result = self.core.create_wallet(
            wallet_path=str(wallet_path),
            password=password,
            seed_phrase=None,
            unlock=True
        )

        if result.get("success"):
            seed_phrase = result.get("seed_phrase", "")
            addresses = result.get("addresses", [])

            lines = [
                "Wallet created!",
                "",
                "IMPORTANT: Write down this recovery phrase:",
                "",
                f"  {seed_phrase}",
                "",
                "Store it safely. It cannot be recovered!",
                "",
                f"Wallet saved to: {wallet_path.name}",
                f"Addresses: {len(addresses)}",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "seed_phrase": seed_phrase,
                "wallet_path": str(wallet_path),
                "addresses": addresses,
            })
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _open_help(self) -> CommandResult:
        """Help for wallet open."""
        return CommandResult.ok("""wallet open - Open and unlock a wallet file

Usage: wallet open <name> [--password <pass>]

Arguments:
  <name>              Required. Wallet name or path to open.

Options:
  --password <pass>   Unlock non-interactively

The name/path can be:
  - Wallet name (e.g., 'mywallet')
  - Relative path (e.g., 'mywallet.wallet')
  - Absolute path""")

    def _open_noninteractive(self, path: str, password: str) -> CommandResult:
        """Open a wallet without interactive prompts."""
        wallet_dir = self.core.get_wallet_dir()
        wallet_path = Path(path)
        if not wallet_path.is_absolute():
            wallet_path = wallet_dir / path
        if not wallet_path.suffix:
            wallet_path = wallet_path.with_suffix('.wallet')
        if not wallet_path.exists():
            return CommandResult.fail(f"Wallet not found: {wallet_path}")
        result = self.core.load_wallet(str(wallet_path), password)
        if result.get("success"):
            addresses = result.get("addresses", [])
            return CommandResult.ok(f"Wallet unlocked. {len(addresses)} address(es).")
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _open(self, path: str, inputs: dict = None) -> CommandResult:
        """Open/unlock a wallet with password prompt."""
        wallet_dir = self.core.get_wallet_dir()

        # Resolve path
        wallet_path = Path(path)
        if not wallet_path.is_absolute():
            wallet_path = wallet_dir / path
        if not wallet_path.suffix:
            wallet_path = wallet_path.with_suffix('.wallet')

        if not wallet_path.exists():
            return CommandResult.fail(f"Wallet not found: {wallet_path}")

        # Need password
        if inputs is None:
            self.handler._set_pending(
                lambda inp, **ctx: self._open(ctx["path"], inp),
                path=path
            )
            return CommandResult.need_input(
                "password",
                "Enter password (or press Enter for none):",
            )

        password = inputs.get("password", inputs.get("value", ""))
        result = self.core.load_wallet(str(wallet_path), password)

        if result.get("success"):
            addresses = result.get("addresses", [])
            return CommandResult.ok(f"Wallet unlocked. {len(addresses)} address(es).")
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _delete_help(self) -> CommandResult:
        """Help for wallet delete."""
        return CommandResult.ok("""wallet delete - Delete the current wallet file

Usage: wallet delete

Permanently deletes the currently open wallet file.
All seeds, addresses, and keys will be lost.
Make sure you have backed up your seed phrase!""")

    def _delete(self, inputs: dict = None, force: bool = False) -> CommandResult:
        """Delete the current wallet file."""
        if not self.core.get_wallet_path():
            return CommandResult.fail("No wallet is currently open.")

        wallet_path = self.core.get_wallet_path()
        if not wallet_path:
            return CommandResult.fail("No wallet file to delete.")

        if force or (inputs and inputs.get("confirm") == "YES"):
            if self.core.delete_wallet_file():
                return CommandResult.ok("Wallet file deleted.")
            else:
                return CommandResult.fail("Failed to delete wallet file.")

        wallet_name = Path(wallet_path).name
        self.handler._set_pending(
            lambda inp, **ctx: self._delete(inp),
        )
        return CommandResult.need_input(
            "confirm",
            f"DELETE wallet '{wallet_name}'? This CANNOT be undone!\nMake sure you have backed up your seed phrase!\nType YES to confirm:",
        )


class SeedCommands:
    """Seed-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route seed subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "list":
            return self._list()
        elif subcmd == "create":
            if "--help" in args or "-h" in args:
                return self._create_help()
            word_count = 12
            if "--words" in args:
                try:
                    idx = args.index("--words")
                    word_count = int(args[idx + 1])
                except (IndexError, ValueError):
                    return CommandResult.fail("--words requires 12 or 24")
            return self._create(word_count)
        elif subcmd == "import":
            if "--help" in args or "-h" in args:
                return self._import_help()
            if len(args) > 1:
                phrase = " ".join(args[1:])
                return self._import(phrase)
            else:
                return self._import_prompted()
        elif subcmd == "delete":
            if "--help" in args or "-h" in args:
                return self._delete_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: seed delete <seed>")
            return self._delete(args[1], force=("--yes" in args or "-y" in args))
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show seed command help."""
        help_text = """seed - Manage wallet seeds

Subcommands:
  list                    - List all seeds
  create [--words 12|24]  - Generate new BIP-39 seed
  import [phrase]         - Import existing seed phrase
  delete <seed>           - Delete seed and its addresses"""
        return CommandResult.ok(help_text)

    def _list(self) -> CommandResult:
        """List all seeds in the wallet."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        seeds = self.core.get_wallet_seeds()
        if not seeds:
            return CommandResult.ok("No seeds in wallet.")

        lines = ["Seeds:"]
        for seed in seeds:
            addresses = self.core.get_wallet_addresses_for_seed(seed["id"])
            lines.append(f"  {seed['id']}  path: {seed['derivation_path']}  ({len(addresses)} addresses)")

        return CommandResult.ok("\n".join(lines), data={"seeds": seeds})

    def _create_help(self) -> CommandResult:
        """Help for seed create."""
        return CommandResult.ok("""seed create - Generate a new BIP-39 seed phrase

Usage: seed create [--words 12|24]

Options:
  --words    Number of words (12 or 24, default: 12)

The seed phrase will be displayed once. Back it up securely!""")

    def _create(self, word_count: int = 12) -> CommandResult:
        """Generate a new BIP-39 seed phrase."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        result = self.core.create_seed(word_count=word_count)
        if result.get("success"):
            seed_id = result["seed_id"]
            seed_phrase = result["seed_phrase"]
            lines = [
                f"Created new seed: {seed_id}",
                "",
                "IMPORTANT: Back up this seed phrase securely!",
                "It will not be shown again.",
                "",
                seed_phrase,
                "",
                f"Use 'address create {seed_id} <index>' to derive addresses.",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "seed_id": seed_id,
                "seed_phrase": seed_phrase,
            })
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _import_help(self) -> CommandResult:
        """Help for seed import."""
        return CommandResult.ok("""seed import - Import an existing BIP-39 seed phrase

Usage: seed import ["phrase"]

If no phrase is provided, you will be prompted to enter it.

Example:
  seed import "abandon abandon abandon ... about\"""")

    def _import(self, phrase: str) -> CommandResult:
        """Import an existing BIP-39 seed phrase."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        # Strip quotes if present
        phrase = phrase.strip().strip('"').strip("'")

        result = self.core.add_seed(phrase)
        if result.get("success"):
            seed_id = result["seed_id"]
            return CommandResult.ok(
                f"Imported seed: {seed_id}\nUse 'address create {seed_id} <index>' to derive addresses.",
                data={"seed_id": seed_id}
            )
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _import_prompted(self, inputs: dict = None) -> CommandResult:
        """Prompt user for seed phrase and import it."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        if inputs is None:
            self.handler._set_pending(
                lambda inp, **ctx: self._import_prompted(inp),
            )
            return CommandResult.need_input(
                "text",
                "Enter your BIP-39 seed phrase (12 or 24 words):",
            )

        phrase = inputs.get("value", inputs.get("text", ""))
        if phrase.strip():
            return self._import(phrase)
        else:
            return CommandResult.ok("Cancelled.")

    def _delete_help(self) -> CommandResult:
        """Help for seed delete."""
        return CommandResult.ok("""seed delete - Remove a seed and its derived addresses

Usage: seed delete <seed>

Arguments:
  <seed>   Seed ID (e.g., S001)

Warning: All addresses derived from this seed will be deleted.
Agents using those addresses will be decommissioned.""")

    def _delete(self, seed_id: str, inputs: dict = None, force: bool = False) -> CommandResult:
        """Delete a seed and its derived addresses."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        seed_id = seed_id.upper()
        seeds = self.core.get_wallet_seeds()
        if not any(s["id"] == seed_id for s in seeds):
            return CommandResult.fail(f"Seed not found: {seed_id}")

        addresses = self.core.get_wallet_addresses_for_seed(seed_id)

        if force or (inputs and inputs.get("confirm") == "YES"):
            result = self.core.remove_seed(seed_id, remove_addresses=True)
            if result.get("success"):
                lines = [f"Seed {seed_id} deleted."]
                removed = result.get("removed_addresses", [])
                for address in removed:
                    decommissioned = self.core.decommission_agents_for_address(address)
                    for agent_name in decommissioned:
                        lines.append(f"Decommissioned agent: {agent_name}")
                return CommandResult.ok("\n".join(lines))
            else:
                return CommandResult.fail(result.get("error", "Unknown error"))

        self.handler._set_pending(
            lambda inp, **ctx: self._delete(ctx["seed_id"], inp),
            seed_id=seed_id
        )
        return CommandResult.need_input(
            "confirm",
            f"Delete seed {seed_id} and {len(addresses)} address(es)? This cannot be undone.\nType YES to confirm:",
        )


class AddressCommands:
    """Address-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route address subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "list":
            return self._list()
        elif subcmd == "create":
            if "--help" in args or "-h" in args:
                return self._create_help()
            return self._create(args[1:])
        elif subcmd == "import":
            if "--help" in args or "-h" in args:
                return self._import_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: address import <private_key> [name]")
            private_key = args[1]
            name = " ".join(args[2:]) if len(args) > 2 else None
            return self._import(private_key, name)
        elif subcmd == "ledger":
            return self._ledger(args[1:])
        elif subcmd == "delete":
            if "--help" in args or "-h" in args:
                return self._delete_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: address delete <address>")
            return self._delete(args[1], force=("--yes" in args or "-y" in args))
        elif subcmd == "rename":
            if "--help" in args or "-h" in args:
                return self._rename_help()
            if len(args) < 3:
                return CommandResult.fail("Usage: address rename <address> <name>")
            return self._rename(args[1], " ".join(args[2:]))
        elif subcmd == "export":
            if "--help" in args or "-h" in args:
                return self._export_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: address export <address>")
            # Parse --wallet and --password flags for standalone mode
            wallet_flag, remaining = _extract_flag(args[1:], "--wallet")
            password_flag, remaining = _extract_flag(remaining, "--password")
            force = "--yes" in remaining or "-y" in remaining
            # Filter out flags to get the address identifier
            identifier = None
            for arg in remaining:
                if not arg.startswith("-"):
                    identifier = arg
                    break
            if not identifier:
                return CommandResult.fail("Usage: address export <address>")
            return self._export(identifier, force=force, wallet_name=wallet_flag, password=password_flag)
        elif subcmd == "balance":
            if "--help" in args or "-h" in args:
                return self._balance_help()
            return self._balance(args[1:])
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show address command help."""
        help_text = """address - Manage wallet addresses

Subcommands:
  list                              - List all addresses
  create [seed] [index] [name]      - Derive new address
  import <key> [name]               - Import private key
  ledger <list|add|verify>          - Addresses held on a Ledger device
  delete <address>                  - Delete address
  rename <address> <name>           - Rename address
  export <address>                  - Export private key
  balance [address]                 - Check on-chain balance"""
        return CommandResult.ok(help_text)

    def _find_address(self, identifier: str) -> Optional[dict]:
        """Find address by ID or full address."""
        addresses = self.core.get_wallet_addresses()
        for addr in addresses:
            if addr["id"].upper() == identifier.upper() or addr["address"].lower() == identifier.lower():
                return addr
        return None

    def _list(self) -> CommandResult:
        """List addresses in wallet."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        addresses = self.core.get_wallet_addresses()
        if not addresses:
            return CommandResult.ok("No addresses in wallet.")

        lines = ["Addresses:"]
        for addr in addresses:
            seed_id = addr.get("seed_id")
            index = addr.get("index")
            is_hardware = addr.get("is_hardware", False)

            if is_hardware:
                derivation = f"  [{addr.get('device_label') or 'Hardware'}]"
            elif seed_id is not None and index is not None:
                derivation = f"  {seed_id}/#{index}"
            else:
                derivation = "  (imported)"

            name = addr.get("name") or ""
            # Suppress the GUI's auto-generated default name (e.g. "S001 #0") — it
            # duplicates the derivation column in a different format.
            if seed_id is not None and index is not None and name == f"{seed_id} #{index}":
                name = ""
            name_part = f"  {name}" if name else ""
            lines.append(f"  {addr['id']}  {addr['address'][:16]}...{addr['address'][-8:]}{derivation}{name_part}")

        return CommandResult.ok("\n".join(lines), data={"addresses": addresses})

    def _create_help(self) -> CommandResult:
        """Help for address create."""
        return CommandResult.ok("""address create - Derive a new address from a seed

Usage: address create [seed] [index] [name]

Arguments:
  [seed]    Seed ID (e.g., S001). Defaults to first seed.
  [index]   Derivation index. Defaults to next available.
  [name]    Optional name for the address

Examples:
  address create
  address create S001 5
  address create S001 5 "My Agent\"""")

    def _create(self, args: list[str]) -> CommandResult:
        """Derive a new address from a seed."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        seeds = self.core.get_wallet_seeds()
        if not seeds:
            return CommandResult.fail("No seeds in wallet. Cannot derive address.")

        # Parse arguments: [seed] [index] [name]
        seed_id = None
        index = None
        name = None

        if len(args) >= 1:
            if args[0].upper().startswith("S") and len(args[0]) == 4:
                seed_id = args[0].upper()
                if len(args) >= 2:
                    try:
                        index = int(args[1])
                        if len(args) >= 3:
                            name = " ".join(args[2:])
                    except ValueError:
                        name = " ".join(args[1:])
            else:
                name = " ".join(args)

        # Default seed to first available
        if seed_id is None:
            seed_id = seeds[0]["id"]
        else:
            if not any(s["id"] == seed_id for s in seeds):
                return CommandResult.fail(f"Seed not found: {seed_id}")

        # Validate or auto-select derivation index
        existing = self.core.get_wallet_addresses_for_seed(seed_id)
        used_indices = {addr["index"] for addr in existing if addr.get("index") is not None}
        if index is None:
            index = 0
            while index in used_indices:
                index += 1
        elif index in used_indices:
            return CommandResult.fail(
                f"Derivation index {index} is already used for seed {seed_id}. "
                f"Choose a different index."
            )

        result = self.core.add_address_from_seed(seed_id, index, name or None)

        if result.get("success"):
            addr = result.get("address", "")
            return CommandResult.ok(
                f"Created address: {addr}\n  Seed: {seed_id}, Index: {index}",
                data={"address": addr, "seed_id": seed_id, "index": index}
            )
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _import_help(self) -> CommandResult:
        """Help for address import."""
        return CommandResult.ok("""address import - Import a private key

Usage: address import <private_key> [name]

Arguments:
  <private_key>   Hex-encoded private key (with or without 0x prefix)
  [name]          Optional name for the address

Example:
  address import 0x1234...abcd "Imported Key\"""")

    def _import(self, private_key: str, name: Optional[str] = None) -> CommandResult:
        """Import a private key."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        # Strip 0x prefix if present
        if private_key.startswith("0x") or private_key.startswith("0X"):
            private_key = private_key[2:]

        # Validate hex format
        if len(private_key) != 64:
            return CommandResult.fail("Invalid private key: must be 64 hex characters (32 bytes)")

        try:
            int(private_key, 16)
        except ValueError:
            return CommandResult.fail("Invalid private key: not valid hex")

        result = self.core.add_imported_key(private_key, name)

        if result.get("success"):
            addr = result.get("address", "")
            return CommandResult.ok(f"Imported address: {addr}", data={"address": addr})
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    # -------------------------------------------------------------- ledger
    #
    # The desktop enrols a Ledger through two dialogs: connect and choose a path
    # type, then the same paged, tick-box browser the software seeds use. None of
    # that shape survives a terminal - a picker is inherently several round trips
    # and a CommandResult is one - so the terminal splits it the way it already
    # splits seed derivation: `list` to look, `add` to commit an index.
    # Scriptable, and it works unchanged over the control channel.
    #
    # Nothing below talks to the device itself. Discovery, path templates,
    # derivation and error mapping all live in `wallet/ledger.py`, are Qt-free,
    # and are the same code the desktop calls.

    def _ledger(self, args: list[str]) -> CommandResult:
        """Route `address ledger` subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._ledger_help()

        subcmd = args[0].lower()
        rest = args[1:]

        if "--help" in rest or "-h" in rest:
            return self._ledger_help()

        if subcmd == "list":
            return self._ledger_list(rest)
        elif subcmd == "add":
            return self._ledger_add(rest)
        elif subcmd == "verify":
            if not rest:
                return CommandResult.fail("Usage: address ledger verify <address>")
            return self._ledger_verify(rest[0])
        else:
            return CommandResult.fail(
                f"Unknown subcommand: {subcmd}\n"
                "Usage: address ledger <list|add|verify>"
            )

    def _ledger_help(self) -> CommandResult:
        """Help for address ledger."""
        from ..wallet.ledger import LedgerPathType, get_path_type_description

        rows = []
        for name in LEDGER_PATH_TYPES:
            marker = "  (default)" if name == DEFAULT_LEDGER_PATH_TYPE else ""
            rows.append(f"  {name:<12} "
                        f"{get_path_type_description(LedgerPathType(name))}{marker}")
        path_types = "\n".join(rows)

        return CommandResult.ok(f"""address ledger - Addresses held on a Ledger device

The private keys never leave the device. Vault stores the address and its
derivation path, and every signature is produced on the Ledger itself.

Usage:
  address ledger list [--path-type <type>] [--start <n>] [--count <n>]
  address ledger add <index[,index...]> [name] [--path-type <type>]
  address ledger verify <address>

Path types:
{path_types}

  For --path-type custom, supply the template with --path, writing {{index}}
  where the number goes.

Flags:
  --path-type <type>   Derivation convention (default: {DEFAULT_LEDGER_PATH_TYPE})
  --path <template>    Template, required when --path-type is custom
  --start <n>          First index to read (list only, default 0)
  --count <n>          How many to read (list only, default 5, max {LEDGER_MAX_PAGE})

Examples:
  address ledger list
  address ledger list --path-type bip44 --start 0 --count 10
  address ledger add 0 "Trading desk"
  address ledger add 0,1,2
  address ledger verify L001

The device must be plugged into the machine running Vault, unlocked, with the
Ethereum app open. Enrolment needs somebody physically present to press the
device's buttons, so a machine meant to run unattended wants software-held keys.""")

    def _ledger_connect(self):
        """Open the connected Ledger. Returns (device, failure_result)."""
        from ..wallet.ledger import FATAL_EXCEPTIONS, LedgerDevice, LedgerError

        try:
            device = LedgerDevice.discover()
        except FATAL_EXCEPTIONS:
            raise
        except LedgerError as e:
            return None, CommandResult.fail(f"{e}\n{_LEDGER_CHECKLIST}")
        except BaseException as e:
            # ledgerblue raises bare BaseException for low-level USB failures,
            # which `except Exception` would walk straight past.
            return None, CommandResult.fail(
                f"Could not talk to the Ledger: {e}\n{_LEDGER_CHECKLIST}")

        if device is None:
            return None, CommandResult.fail(f"No Ledger found.\n{_LEDGER_CHECKLIST}")
        return device, None

    def _ledger_existing(self) -> dict:
        """Addresses already in the wallet, keyed by lowercased 0x address.

        Matched by address rather than by index, for the reason the desktop's
        picker matches the same way: the same key can be reached under more than
        one path convention, and it is still the same key.
        """
        return {a["address"].lower(): a for a in self.core.get_wallet_addresses()}

    def _ledger_list(self, args: list[str]) -> CommandResult:
        """Read a page of addresses off the device without enrolling any."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        path_type_name, args = _extract_flag(args, "--path-type")
        custom_path, args = _extract_flag(args, "--path")
        start_value, args = _extract_flag(args, "--start")
        count_value, args = _extract_flag(args, "--count")

        path_type, error = _parse_ledger_path_type(path_type_name, custom_path)
        if error:
            return CommandResult.fail(error)

        start, error = _parse_count(start_value, "--start", default=0)
        if error:
            return CommandResult.fail(error)
        count, error = _parse_count(count_value, "--count", default=5,
                                    minimum=1, maximum=LEDGER_MAX_PAGE)
        if error:
            return CommandResult.fail(error)

        device, failure = self._ledger_connect()
        if failure:
            return failure

        from ..wallet.ledger import (FATAL_EXCEPTIONS, LedgerError,
                                     get_path_type_display_name)
        try:
            found = device.get_addresses(path_type, start_index=start, count=count,
                                         custom_path=custom_path)
        except FATAL_EXCEPTIONS:
            raise
        except LedgerError as e:
            return CommandResult.fail(f"{e}\n{_LEDGER_CHECKLIST}")
        except BaseException as e:
            return CommandResult.fail(f"Could not read addresses: {e}")

        if not found:
            return CommandResult.fail("The device returned no addresses.")

        existing = self._ledger_existing()
        label = get_path_type_display_name(path_type.value)
        lines = [f"Ledger addresses ({label}):", ""]
        rows = []
        for entry in found:
            already = existing.get(entry.address.lower())
            note = f"  in wallet as {already['id']}" if already else ""
            lines.append(f"  #{entry.index:<4} {entry.address}  {entry.path}{note}")
            rows.append({
                "index": entry.index,
                "address": entry.address,
                "path": entry.path,
                "path_type": entry.path_type,
                "address_id": already["id"] if already else None,
            })

        # A sweep stops short of `count` when the link drops part-way through.
        # Say so, rather than letting a short page read as "that is all there is".
        if len(found) < count:
            lines.append("")
            lines.append(f"  Stopped after {len(found)} of {count} - the device "
                         f"stopped answering.")

        hint = "Add one with:  address ledger add <index>"
        if path_type.value != DEFAULT_LEDGER_PATH_TYPE:
            hint += f" --path-type {path_type.value}"
        lines.extend(["", hint])

        return CommandResult.ok("\n".join(lines), data={"addresses": rows})

    def _ledger_add(self, args: list[str]) -> CommandResult:
        """Enrol one or more device addresses into the wallet."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        path_type_name, args = _extract_flag(args, "--path-type")
        custom_path, args = _extract_flag(args, "--path")

        if not args:
            return CommandResult.fail(
                "Usage: address ledger add <index[,index...]> [name]\n"
                "Run `address ledger list` first to see what is on the device.")

        indices = []
        for piece in args[0].split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except ValueError:
                return CommandResult.fail(
                    f"Invalid index '{piece}': expected a number\n"
                    "Usage: address ledger add <index[,index...]> [name]")
            if value < 0:
                return CommandResult.fail(f"Invalid index {value}: must be 0 or more")
            indices.append(value)

        if not indices:
            return CommandResult.fail("No index given. Usage: address ledger add <index>")
        if len(indices) > LEDGER_MAX_PAGE:
            return CommandResult.fail(
                f"Too many indices at once: {LEDGER_MAX_PAGE} is the maximum")

        name = " ".join(args[1:]) if len(args) > 1 else None
        if name and len(indices) > 1:
            # One name cannot describe several addresses, and quietly applying it
            # to the first would be a surprise. The wallet's own default naming
            # ("Ledger #1", "Ledger #2") covers the batch case.
            return CommandResult.fail(
                "A name can only be given when adding a single address.\n"
                "Add them one at a time to name each, or omit the name.")

        path_type, error = _parse_ledger_path_type(path_type_name, custom_path)
        if error:
            return CommandResult.fail(error)

        device, failure = self._ledger_connect()
        if failure:
            return failure

        from ..wallet.ledger import FATAL_EXCEPTIONS

        existing = self._ledger_existing()
        lines = []
        added = []
        for index in indices:
            try:
                entry = device.get_address(path_type, index, custom_path=custom_path)
            except FATAL_EXCEPTIONS:
                raise
            except BaseException as e:
                lines.append(f"  #{index}  could not be read: {e}")
                continue

            already = existing.get(entry.address.lower())
            if already:
                lines.append(f"  #{index}  {entry.address}  already in wallet "
                             f"as {already['id']}")
                continue

            result = self.core.add_hardware_address(
                entry.address, entry.path, entry.path_type, name)
            if result.get("success"):
                address_id = result.get("address_id") or ""
                suffix = f" as {address_id}" if address_id else ""
                lines.append(f"  #{index}  {entry.address}  added{suffix}")
                added.append({
                    "index": index,
                    "address": entry.address,
                    "path": entry.path,
                    "path_type": entry.path_type,
                    "id": address_id or None,
                })
                existing[entry.address.lower()] = {"id": address_id}
            else:
                lines.append(f"  #{index}  {entry.address}  not added: "
                             f"{result.get('error', 'unknown error')}")

        if not added:
            return CommandResult.fail("No addresses were added.\n" + "\n".join(lines))

        heading = f"Added {len(added)} Ledger address{'es' if len(added) != 1 else ''}:"
        return CommandResult.ok("\n".join([heading] + lines), data={"added": added})

    def _ledger_verify(self, identifier: str) -> CommandResult:
        """Confirm the connected device still derives a stored address."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        addr = self._find_address(identifier)
        if not addr:
            return CommandResult.fail(f"Address not found: {identifier}")
        if not addr.get("is_hardware"):
            return CommandResult.fail(
                f"{addr['id']} is not a hardware address - there is no device to "
                f"check it against.")

        path = addr.get("device_path")
        if not path:
            return CommandResult.fail(
                f"{addr['id']} has no derivation path recorded, so it cannot be "
                f"checked against a device.")

        device, failure = self._ledger_connect()
        if failure:
            return failure

        from ..wallet.ledger import FATAL_EXCEPTIONS
        try:
            matches = device.verify_address(path, addr["address"])
        except FATAL_EXCEPTIONS:
            raise
        except BaseException as e:
            return CommandResult.fail(f"Could not check the address: {e}")

        if matches:
            return CommandResult.ok(
                f"Verified: the connected Ledger derives {addr['address']}\n"
                f"  at {path}",
                data={"verified": True, "address": addr["address"], "path": path})

        # The check ran and came back no, which is worth failing on: it means a
        # different device, or the same one restored from a different phrase.
        return CommandResult.fail(
            f"Mismatch: the connected Ledger does not derive {addr['address']} at "
            f"{path}.\nThis is a different device, or one restored from a "
            f"different recovery phrase.")

    def _delete_help(self) -> CommandResult:
        """Help for address delete."""
        return CommandResult.ok("""address delete - Remove an address from the wallet

Usage: address delete <address>

Arguments:
  <address>   Address ID (e.g., A1) or full 0x address

Warning: Agents using this address will be decommissioned.""")

    def _delete(self, identifier: str, inputs: dict = None, force: bool = False) -> CommandResult:
        """Delete an address from the wallet."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        addr = self._find_address(identifier)
        if not addr:
            return CommandResult.fail(f"Address not found: {identifier}")

        if force or (inputs and inputs.get("confirm") == "YES"):
            result = self.core.remove_address(addr["id"])
            if result.get("success"):
                lines = [f"Address deleted: {addr['address'][:16]}..."]
                # Decommissioning is now handled by core.remove_address()
                decommissioned = result.get("decommissioned", [])
                for agent_name in decommissioned:
                    lines.append(f"Decommissioned agent: {agent_name}")
                return CommandResult.ok("\n".join(lines))
            else:
                return CommandResult.fail(result.get("error", "Unknown error"))

        self.handler._set_pending(
            lambda inp, **ctx: self._delete(ctx["identifier"], inp),
            identifier=identifier
        )
        return CommandResult.need_input(
            "confirm",
            f"Delete address {addr['address'][:16]}...? This cannot be undone.\nType YES to confirm:",
        )

    def _rename_help(self) -> CommandResult:
        """Help for address rename."""
        return CommandResult.ok("""address rename - Rename an address

Usage: address rename <address> <name>

Arguments:
  <address>   Address ID (e.g., A1) or full 0x address
  <name>      New name for the address""")

    def _rename(self, identifier: str, new_name: str) -> CommandResult:
        """Rename an address."""
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        addr = self._find_address(identifier)
        if not addr:
            return CommandResult.fail(f"Address not found: {identifier}")

        if self.core.rename_address(addr["id"], new_name):
            return CommandResult.ok(f"Address renamed to: {new_name}")
        else:
            return CommandResult.fail("Failed to rename address.")

    def _export_help(self) -> CommandResult:
        """Help for address export."""
        return CommandResult.ok("""address export - Export private key for an address

Usage: address export <address> [--wallet <name>] [--password <pw>] [--yes]

Arguments:
  <address>   Address ID (e.g., A001) or full 0x address

Options:
  --wallet <name>     Wallet to export from (defaults to the open one)
  --password <pw>     Wallet password - required (or PRIMER_VAULT_PASSWORD)
  --yes               Skip confirmation prompt

Examples:
  address export A001 --wallet mywallet --password "secret" --yes
  PRIMER_VAULT_PASSWORD="secret" address export A001 --wallet mywallet --yes

Note: the password is required every time, even if the wallet is already open.

WARNING: This will display your private key on screen.
Never share your private key with anyone!""")

    def _export(self, identifier: str, inputs: dict = None, force: bool = False,
                wallet_name: str = None, password: str = None) -> CommandResult:
        """Export private key for an address with confirmation."""
        import os

        # Exporting a key needs the password every time, even when the wallet is
        # already open. This is the one command that hands out a key in the
        # clear, and "the wallet happens to be unlocked" is not proof that the
        # person asking is the owner - an engine left running unattended is
        # unlocked for weeks. Re-deriving the master key with Argon2id is what
        # proves it, and it is the same check that protects the wallet file.
        if password is None:
            password = os.environ.get("PRIMER_VAULT_PASSWORD")
        if password is None:
            return CommandResult.fail(
                "Exporting a private key needs the wallet password, even when "
                "the wallet is already open.\n\n"
                "  address export <addr> --wallet <name> --password <pw> --yes\n\n"
                "Or set PRIMER_VAULT_PASSWORD."
            )

        wallet_path = None
        if wallet_name:
            wallet_path = self.core.get_wallet_dir() / f"{wallet_name}.wallet"
            if not wallet_path.exists():
                return CommandResult.fail(f"Wallet not found: {wallet_name}")
        else:
            current = self.core.get_wallet_path()
            if current is None:
                return CommandResult.fail(
                    "No wallet is open, so there is nothing to export from. "
                    "Name one with --wallet <name>.")
            wallet_path = current

        result = self.core.load_wallet(str(wallet_path), password)
        if not result.get("success"):
            return CommandResult.fail(
                f"Failed to unlock wallet: {result.get('error', 'Unknown error')}")

        addr = self._find_address(identifier)
        if not addr:
            return CommandResult.fail(f"Address not found: {identifier}")

        # Hardware addresses have no private key to export
        if addr.get("is_hardware", False):
            label = addr.get("device_label") or "hardware wallet"
            return CommandResult.fail(
                f"Cannot export private key for {label} address.\n"
                "Hardware wallet keys never leave the device."
            )

        # Check if we have confirmation
        if force or (inputs and inputs.get("confirm") == "YES"):
            private_key = self.core.get_private_key_for_address(addr["id"])
            if not private_key:
                return CommandResult.fail("Failed to retrieve private key.")

            lines = [
                "WARNING: Keep this private key secure!",
                "",
                f"Address: {addr['address']}",
                f"Private Key: 0x{private_key.hex() if isinstance(private_key, bytes) else private_key}",
                "",
                "Never share this key with anyone.",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "address": addr["address"],
                "private_key": private_key
            })

        # Need confirmation. The credentials ride along in the pending context:
        # this command runs twice - once to ask, once with the answer - and the
        # second call has to arrive with the same password the first proved,
        # or it refuses itself.
        self.handler._set_pending(
            lambda inp, **ctx: self._export(
                ctx["identifier"], inp,
                wallet_name=ctx.get("wallet_name"),
                password=ctx.get("password")),
            identifier=identifier,
            wallet_name=wallet_name,
            password=password,
        )
        return CommandResult.need_input(
            "confirm",
            f"Export private key for {addr['address'][:16]}...?\n"
            "WARNING: This will display your private key!\n"
            "Type YES to confirm:",
        )

    def _balance_help(self) -> CommandResult:
        """Help for address balance."""
        return CommandResult.ok("""address balance - Check on-chain balance for address(es)

Usage: address balance [address] [--network <chain_id>]

Arguments:
  [address]     Optional. Address ID (e.g., A001) or full address.
                If omitted, shows balances for all addresses.

Options:
  --network N   Only check balance on network N (chain ID)

Examples:
  address balance
  address balance A001
  address balance A001 --network 4663""")

    def _balance(self, args: list[str]) -> CommandResult:
        """Check on-chain balance for address(es)."""
        from ..networks import BalanceFetcher, NETWORKS

        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked.")

        # Parse arguments
        address_id = None
        network_filter = None
        i = 0
        while i < len(args):
            if args[i] == "--network":
                if i + 1 < len(args):
                    try:
                        network_filter = int(args[i + 1])
                    except ValueError:
                        return CommandResult.fail(f"Invalid network ID: {args[i + 1]}")
                    i += 2
                else:
                    return CommandResult.fail("--network requires a chain ID")
            elif not args[i].startswith("-"):
                if address_id is None:
                    address_id = args[i]
                i += 1
            else:
                return CommandResult.fail(f"Unknown option: {args[i]}")

        # Get addresses to check
        if address_id:
            addr = self._find_address(address_id)
            if not addr:
                return CommandResult.fail(f"Address not found: {address_id}")
            addresses = [addr]
        else:
            addresses = self.core.get_wallet_addresses()
            if not addresses:
                return CommandResult.ok("No addresses in wallet.")

        # Determine which networks to check
        if network_filter:
            if network_filter not in NETWORKS:
                return CommandResult.fail(f"Unknown network: {network_filter}")
            networks = [NETWORKS[network_filter]]
        else:
            # Check enabled networks only
            networks = [
                net for chain_id, net in NETWORKS.items()
                if self.core.is_network_enabled(chain_id)
            ]
            if not networks:
                # Fall back to testnets if none enabled
                networks = [net for net in NETWORKS.values() if net.is_testnet]

        lines = ["Balances:"]
        balance_data = []

        for addr in addresses:
            lines.append(f"\n{addr['id']} ({addr['address'][:10]}...{addr['address'][-6:]}):")

            for network in networks:
                # Get custom RPC if set
                rpc_url = self.core.settings_manager.get_rpc_endpoint(network.chain_id)
                fetcher = BalanceFetcher(network, rpc_url)

                if not fetcher.is_connected:
                    lines.append(f"  {network.display_name}: (connection failed)")
                    continue

                balances = fetcher.get_all_balances(addr["address"])

                for balance in balances:
                    label = f"  [{network.display_name}] {balance.symbol}"
                    if balance.fetch_failed:
                        lines.append(f"{label}: (fetch failed)")
                    elif balance.formatted > 0:
                        lines.append(f"{label}: {balance.formatted:.6f}")
                        balance_data.append({
                            "address": addr["address"],
                            "network": network.chain_id,
                            "network_name": network.display_name,
                            "symbol": balance.symbol,
                            "balance": balance.formatted,
                            "raw": balance.raw
                        })
                    else:
                        lines.append(f"{label}: 0")

        return CommandResult.ok("\n".join(lines), data={"balances": balance_data})
