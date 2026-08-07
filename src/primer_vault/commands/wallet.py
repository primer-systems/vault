"""
Wallet, Seed, and Address command implementations.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
            try:
                # Use the tracked wallet path — set on load, cleared on detach, kept on lock.
                # Correctly distinguishes "locked" from "no wallet loaded".
                has_wallets = self.core.get_wallet_path() is not None
            except Exception:
                # CoreClient blocks get_wallet_path; fall back to directory scan.
                try:
                    wallet_dir = self.core.get_wallet_dir()
                    has_wallets = any(wallet_dir.glob("*.wallet"))
                except Exception:
                    has_wallets = True
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
  --password <pass>   Set password non-interactively (use "" for no password)

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
                "Enter password (or press Enter for none):",
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
  --password <pass>   Unlock non-interactively (use "" for no password)

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
                    return CommandResult.fail("Error: --words requires 12 or 24")
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
            if seed_id is not None and index is not None:
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
  --wallet <name>     Wallet name to open (required in standalone mode)
  --password <pw>     Wallet password (or use PRIMER_VAULT_PASSWORD env var)
  --yes               Skip confirmation prompt

Examples:
  address export A001 --wallet mywallet --password "secret" --yes
  PRIMER_VAULT_PASSWORD="secret" address export A001 --wallet mywallet --yes

Note: Private key export only works in standalone mode (no GUI/daemon running).

WARNING: This will display your private key on screen.
Never share your private key with anyone!""")

    def _export(self, identifier: str, inputs: dict = None, force: bool = False,
                wallet_name: str = None, password: str = None) -> CommandResult:
        """Export private key for an address with confirmation."""
        import os

        # If wallet not unlocked, try to open it with provided credentials
        if not self.core.is_wallet_unlocked():
            # Check for password from env var if not provided via flag
            if password is None:
                password = os.environ.get("PRIMER_VAULT_PASSWORD")

            if wallet_name and password is not None:
                # Try to open the wallet
                wallet_dir = self.core.get_wallet_dir()
                wallet_path = wallet_dir / f"{wallet_name}.wallet"
                if not wallet_path.exists():
                    return CommandResult.fail(f"Wallet not found: {wallet_name}")
                result = self.core.load_wallet(str(wallet_path), password)
                if not result.get("success"):
                    return CommandResult.fail(f"Failed to unlock wallet: {result.get('error', 'Unknown error')}")
            else:
                return CommandResult.fail(
                    "Wallet must be unlocked.\n\n"
                    "In standalone mode, use:\n"
                    "  address export <addr> --wallet <name> --password <pw> --yes\n\n"
                    "Or set PRIMER_VAULT_PASSWORD environment variable."
                )

        addr = self._find_address(identifier)
        if not addr:
            return CommandResult.fail(f"Address not found: {identifier}")

        # Fail before asking for confirmation if key export isn't supported in this mode
        try:
            from primer_vault.client.core_client import CoreClient
            if isinstance(self.core, CoreClient):
                return CommandResult.fail(
                    "Private key export requires running without a GUI or daemon.\n"
                    "Stop the running instance first, then run the command standalone."
                )
        except ImportError:
            pass

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

        # Need confirmation
        self.handler._set_pending(
            lambda inp, **ctx: self._export(ctx["identifier"], inp),
            identifier=identifier
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
