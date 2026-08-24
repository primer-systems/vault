#!/usr/bin/env python3
"""
Vault CLI - Command-line interface for Vault.

This is a thin wrapper around CommandHandler that handles:
- REPL loop for interactive mode
- Single command execution for scripting
- Password prompts via stdin or --password flag
- Confirmation prompts via stdin or --yes flag

Scripting Mode:
  In single-command mode, use flags to avoid interactive prompts:
    --yes, -y           Auto-confirm destructive actions (supplies YES)
    --password <pw>     Provide password for wallet operations

  Or use PRIMER_VAULT_PASSWORD environment variable for passwords.
"""

import sys
import os
import getpass
import subprocess

from .core import Vault
from .commands import CommandHandler, CommandResult
from .utils import get_app_dir, DataDirectoryError
from .version import __version__


BANNER = f"""
█ █ ▄▀█ █ █ █   ▀█▀
▀▄▀ █▀█ █▄█ █▄▄  █
v{__version__} - Type 'help' for commands, 'exit' to quit
"""


class ScriptContext:
    """Context for scripted (non-interactive) command execution."""

    def __init__(self, auto_confirm: bool = False, password: str = None):
        self.auto_confirm = auto_confirm
        self.password = password

    def get_input_for(self, input_type: str) -> dict:
        """Get pre-filled input based on type, or None if interactive needed."""
        if input_type == "confirm" and self.auto_confirm:
            return {"confirm": "YES"}
        elif input_type == "password" and self.password is not None:
            return {"password": self.password, "value": self.password}
        return None


def parse_global_flags(args: list[str]) -> tuple[list[str], ScriptContext]:
    """
    Parse global flags from command-line arguments.

    Returns:
        (remaining_args, script_context)
    """
    auto_confirm = False
    password = os.environ.get("PRIMER_VAULT_PASSWORD")
    remaining = []

    i = 0
    while i < len(args):
        if args[i] in ("--yes", "-y"):
            auto_confirm = True
            i += 1
        elif args[i] == "--password":
            if i + 1 < len(args):
                password = args[i + 1]
                i += 2
            else:
                print("Error: --password requires a value", file=sys.stderr)
                sys.exit(1)
        else:
            remaining.append(args[i])
            i += 1

    return remaining, ScriptContext(auto_confirm=auto_confirm, password=password)


def handle_input_request(result: CommandResult, script_ctx: ScriptContext = None) -> dict:
    """Handle a command that needs additional input."""
    needs = result.needs_input
    input_type = needs.get("type", "text")
    prompt = needs.get("prompt", "> ")

    # Check if script context can provide the input
    if script_ctx:
        scripted_input = script_ctx.get_input_for(input_type)
        if scripted_input:
            return scripted_input

    # Fall back to interactive input
    if input_type == "password":
        try:
            value = getpass.getpass(prompt + " ")
        except EOFError:
            value = ""
    elif input_type == "confirm":
        print(prompt)
        try:
            value = input().strip()
        except EOFError:
            value = ""
        return {"confirm": value}
    else:
        print(prompt)
        try:
            value = input().strip()
        except EOFError:
            value = ""
        return {"value": value}

    return {"password": value, "value": value}


def run_command(handler: CommandHandler, command: str, script_ctx: ScriptContext = None) -> bool:
    """
    Run a single command and print output.

    Args:
        handler: Command handler
        command: Command string to execute
        script_ctx: Optional script context for non-interactive mode

    Returns True if should continue, False if should exit.
    """
    result = handler.execute(command)

    # Handle multi-step commands (passwords, confirmations)
    while result.needs_input:
        inputs = handle_input_request(result, script_ctx)
        result = handler.execute(command, inputs=inputs)

    # Print output
    if result.output:
        print(result.output)
    elif result.error:
        print(f"Error: {result.error}", file=sys.stderr)

    # Check for exit
    if result.data and result.data.get("action") == "exit":
        return False

    # Check for clear (in terminal, clear screen)
    if result.data and result.data.get("action") == "clear":
        subprocess.run('cls' if os.name == 'nt' else 'clear', shell=True, check=False)

    return True


def piped_mode(handler: CommandHandler):
    """Process commands from piped stdin, one per line. No prompts or banner."""
    for line in sys.stdin:
        command = line.strip()
        if not command:
            continue
        if not run_command(handler, command):
            break


def interactive_mode(handler: CommandHandler):
    """Run interactive REPL mode."""
    print(BANNER)

    while True:
        try:
            command = input("> ").strip()
            if not command:
                continue

            if not run_command(handler, command):
                print("Goodbye!")
                break

        except KeyboardInterrupt:
            print("\nUse 'exit' to quit.")
        except EOFError:
            print("\nGoodbye!")
            break


def single_command_mode(handler: CommandHandler, args: list[str]):
    """
    Run a single command from arguments (scriptable mode).

    Global flags:
        --yes, -y           Auto-confirm destructive actions
        --password <pw>     Provide password non-interactively

    Environment variables:
        PRIMER_VAULT_PASSWORD  Password for wallet operations
    """
    # Parse global flags
    remaining_args, script_ctx = parse_global_flags(args)

    if not remaining_args:
        print("Error: No command provided", file=sys.stderr)
        sys.exit(1)

    import shlex as _shlex
    command = " ".join(_shlex.quote(a) for a in remaining_args)
    result = handler.execute(command)

    # Handle multi-step commands with script context
    while result.needs_input:
        inputs = handle_input_request(result, script_ctx)
        result = handler.execute(command, inputs=inputs)

    if result.output:
        print(result.output)
    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)

    # Exit with appropriate code
    sys.exit(0 if result.success else 1)


def print_usage():
    """Print usage information."""
    print(f"""Vault CLI v{__version__}

Usage:
  primer-vault                     Start the desktop app (default)
  primer-vault --cli               Interactive terminal
  primer-vault --headless          Run as a daemon, no window
  primer-vault <command> [args]    Run a single command

Mode flags:
  --gui / --cli / --headless   How Vault runs (default: desktop app)
  --wallet <name>              Open this wallet on start
  --agent-port <n>             Agent API port (default 4663)
  --admin-port <n>             Admin API port (default 4664)
  --allow-lan                  Bind the agent API to the LAN
  --admin-open                 Start with the Admin API open (persists)

Global Flags (single-command mode only):
  --yes, -y           Auto-confirm destructive actions (supplies YES)
  --password <pw>     Provide password for wallet operations

Environment Variables:
  PRIMER_VAULT_PASSWORD  Password for wallet operations (alternative to --password)

Examples:
  primer-vault status
  primer-vault wallet open mywallet --password "a-strong-passphrase"
  primer-vault policy delete old-policy --yes
  PRIMER_VAULT_PASSWORD="a-strong-passphrase" primer-vault wallet create newwallet --yes

Note: a password given on the command line is visible to other users on the
machine (via the process list) and is usually kept in your shell history. For
unattended use prefer PRIMER_VAULT_PASSWORD, which is neither.

For command help:
  primer-vault help
  primer-vault <command> --help
""")


def main():
    """Main entry point."""
    # Handle --help at top level
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print_usage()
        sys.exit(0)

    # Handle --version
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"Vault {__version__}")
        sys.exit(0)

    # Resolve data directory. If it is unwritable there is nothing useful to do
    # but say so plainly - every command below needs somewhere to read and write.
    try:
        data_dir = get_app_dir()
    except DataDirectoryError as e:
        print(e.user_message(), file=sys.stderr)
        sys.exit(1)

    # Try to connect to a running instance that shares our data directory
    core = None
    try:
        from primer_vault.client.core_client import CoreClient
        core = CoreClient.try_connect(data_dir=data_dir)
    except Exception:
        pass

    if core is None:
        # No running instance answered the probe — create our own core. The
        # instance lock inside Vault() is the backstop: if an instance IS
        # running but the probe missed it (slow to answer, admin API on a
        # different port, an unrelated program on 4664), this refuses cleanly
        # instead of silently running a second core against the same files.
        from primer_vault.instance_lock import InstanceAlreadyRunning
        try:
            core = Vault(data_dir=data_dir)
        except InstanceAlreadyRunning as e:
            print(e.user_message(), file=sys.stderr)
            print(
                "\nA running Vault normally answers the CLI on port 4664, but "
                "this one did not - it may still be starting, or the port may "
                "be taken by another program. Try again in a moment.",
                file=sys.stderr)
            sys.exit(1)

    handler = CommandHandler(core)

    # Check for command-line arguments
    if len(sys.argv) > 1:
        # Single command mode (scriptable)
        single_command_mode(handler, sys.argv[1:])
    elif sys.stdin is None:
        # A windowed build has no stdin, so there is no terminal to read from.
        # Reached when the desktop window could not open and run_gui() handed
        # over to here; _gui_unavailable has already told the user by then.
        sys.exit(1)
    elif not sys.stdin.isatty():
        # Piped / redirected stdin — process each line as a command
        piped_mode(handler)
    else:
        # Interactive mode
        interactive_mode(handler)


if __name__ == "__main__":
    main()
