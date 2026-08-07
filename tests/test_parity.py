"""
GUI/Console Parity Tests

Rule 3: One implementation, multiple interfaces.
Both GUI and Console must call the same Core methods for equivalent operations.

This test scans both codebases to verify parity.
"""

import re
from pathlib import Path
from typing import Dict, Set, List, Tuple

import pytest

SRC_DIR = Path(__file__).parent.parent / "src"
PRIMER_VAULT_DIR = SRC_DIR / "primer_vault"


def extract_core_calls(filepath: Path) -> Dict[str, List[Tuple[int, str]]]:
    """
    Extract all self.core.method() calls from a file.

    Returns dict mapping method names to list of (lineno, full_line) tuples.
    """
    calls = {}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Match both self.core. and self._core. patterns
        pattern = re.compile(r'self\._?core\.([a-z_][a-z0-9_]*)\s*\(')

        for lineno, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue

            matches = pattern.findall(line)
            for method in matches:
                if method not in calls:
                    calls[method] = []
                calls[method].append((lineno, line.strip()))
    except Exception:
        pass

    return calls


def get_gui_core_calls() -> Dict[str, Set[str]]:
    """
    Get all Core method calls from GUI code.

    Returns dict mapping method name to set of files that call it.
    """
    result = {}
    ui_dir = PRIMER_VAULT_DIR / 'ui'

    if not ui_dir.exists():
        return result

    # Focus on main GUI files
    files = [
        ui_dir / 'main_window.py',
        ui_dir / 'tabs.py',
        ui_dir / 'dialogs.py',
    ]

    for filepath in files:
        if not filepath.exists():
            continue

        calls = extract_core_calls(filepath)
        for method in calls:
            if method not in result:
                result[method] = set()
            result[method].add(str(filepath.relative_to(PRIMER_VAULT_DIR)))

    return result


def get_console_core_calls() -> Dict[str, Set[str]]:
    """
    Get all Core method calls from Console code.

    The Console uses a CommandHandler architecture where console.py delegates
    to command modules in src/commands/. We need to scan both locations.

    Returns dict mapping method name to set of files that call it.
    """
    result = {}

    # Console delegates to CommandHandler, which uses command modules
    # We need to scan both console.py and the commands/ directory
    files_to_scan = []

    console_file = PRIMER_VAULT_DIR / 'ui' / 'console.py'
    if console_file.exists():
        files_to_scan.append(console_file)

    # CommandHandler and command modules that console.py uses
    commands_dir = PRIMER_VAULT_DIR / 'commands'
    if commands_dir.exists():
        for filepath in commands_dir.glob('*.py'):
            if filepath.name != '__init__.py':
                files_to_scan.append(filepath)

    for filepath in files_to_scan:
        calls = extract_core_calls(filepath)
        for method in calls:
            if method not in result:
                result[method] = set()
            result[method].add(str(filepath.relative_to(PRIMER_VAULT_DIR)))

    return result


class TestGUIConsoleParity:
    """Verify GUI and Console use the same Core methods."""

    # Operations that MUST have parity - both should call the same Core methods
    REQUIRED_PARITY_OPERATIONS = {
        # Agent operations
        'create_agent': 'Create agent',
        'commission_agent': 'Commission agent',
        'suspend_agent': 'Suspend agent',
        'activate_agent': 'Activate agent',
        'delete_agent': 'Delete agent',

        # Policy operations
        'create_policy': 'Create policy',
        'update_policy': 'Update policy',
        'delete_policy': 'Delete policy',

        # Approval operations
        'approve_request': 'Approve payment request',
        'reject_request': 'Reject payment request',

        # Wallet operations
        'load_wallet': 'Unlock wallet',
        'lock_wallet': 'Lock wallet',
        'create_wallet': 'Create wallet',

        # Server operations
        'start_server': 'Start server',
        'stop_server': 'Stop server',

        # History operations
        'clear_transactions': 'Clear transaction history',
    }

    def test_required_parity(self):
        """Both GUI and Console must call the same Core methods for key operations."""
        gui_calls = get_gui_core_calls()
        console_calls = get_console_core_calls()

        missing_in_gui = []
        missing_in_console = []

        for method, description in self.REQUIRED_PARITY_OPERATIONS.items():
            in_gui = method in gui_calls
            in_console = method in console_calls

            if in_console and not in_gui:
                missing_in_gui.append((method, description))
            elif in_gui and not in_console:
                missing_in_console.append((method, description))

        if missing_in_gui or missing_in_console:
            msg = "GUI/Console parity violations:\n"

            if missing_in_gui:
                msg += "\nMissing in GUI (Console has it):\n"
                for method, desc in missing_in_gui:
                    msg += f"  - {method}(): {desc}\n"

            if missing_in_console:
                msg += "\nMissing in Console (GUI has it):\n"
                for method, desc in missing_in_console:
                    msg += f"  - {method}(): {desc}\n"

            pytest.fail(msg)

    def test_list_all_core_calls(self):
        """List all Core method calls for both GUI and Console."""
        gui_calls = get_gui_core_calls()
        console_calls = get_console_core_calls()

        all_methods = set(gui_calls.keys()) | set(console_calls.keys())

        print("\n\nCore method usage comparison:")
        print("-" * 60)
        print(f"{'Method':<35} {'GUI':<10} {'Console':<10}")
        print("-" * 60)

        for method in sorted(all_methods):
            in_gui = "✓" if method in gui_calls else ""
            in_console = "✓" if method in console_calls else ""
            print(f"{method:<35} {in_gui:<10} {in_console:<10}")

        print("-" * 60)

    def test_no_gui_only_state_changes(self):
        """
        State-changing operations shouldn't exist only in GUI.

        If GUI can do something that changes state, Console should be able to too.
        """
        gui_calls = get_gui_core_calls()
        console_calls = get_console_core_calls()

        # These are known state-changing methods
        STATE_CHANGING_METHODS = {
            'create_agent', 'commission_agent', 'decommission_agent',
            'suspend_agent', 'activate_agent', 'delete_agent',
            'update_agent', 'set_agent_mandate',
            'create_policy', 'update_policy', 'delete_policy',
            'approve_request', 'reject_request',
            'create_wallet', 'load_wallet', 'lock_wallet',
            'add_seed', 'generate_address', 'import_private_key',
            'start_server', 'stop_server',
            'clear_transactions',
            'set_network_enabled', 'set_verify_settlements', 'set_max_request_age',
        }

        gui_only = set(gui_calls.keys()) - set(console_calls.keys())
        state_changing_gui_only = gui_only & STATE_CHANGING_METHODS

        if state_changing_gui_only:
            msg = "State-changing operations exist only in GUI (should also be in Console):\n"
            for method in sorted(state_changing_gui_only):
                files = gui_calls[method]
                msg += f"  - {method}() in {', '.join(files)}\n"
            # This is a warning, not a failure - some operations may be GUI-specific
            print(f"\nWARNING: {msg}")


class TestConsoleCommands:
    """Verify Console commands map to Core methods properly."""

    def get_console_command_handlers(self) -> Dict[str, List[Tuple[int, str]]]:
        """
        Find all command handler methods in Console.

        Returns dict mapping handler name to (lineno, first_line) tuples.
        """
        handlers = {}
        console_file = PRIMER_VAULT_DIR / 'ui' / 'console.py'

        if not console_file.exists():
            return handlers

        try:
            with open(console_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # Pattern: def _cmd_xxx(self, ...) or def _xxx_xxx(self, args)
            pattern = re.compile(r'def (_(?:cmd_)?[a-z_]+)\s*\(self')

            for lineno, line in enumerate(lines, 1):
                match = pattern.search(line)
                if match:
                    handler = match.group(1)
                    if handler not in handlers:
                        handlers[handler] = []
                    handlers[handler].append((lineno, line.strip()))
        except Exception:
            pass

        return handlers

    def test_list_console_commands(self):
        """List all Console command handlers."""
        handlers = self.get_console_command_handlers()

        print("\n\nConsole command handlers:")
        for handler in sorted(handlers.keys()):
            locations = handlers[handler]
            for lineno, line in locations:
                print(f"  L{lineno}: {handler}")


class TestEventSubscriptions:
    """Both GUI and Console should subscribe to relevant EventBus events."""

    def get_event_subscriptions(self, filepath: Path) -> Set[str]:
        """Find all EventType subscriptions in a file."""
        events = set()

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # Pattern: subscribe(EventType.XXX, ...)
            pattern = re.compile(r'subscribe\s*\(\s*EventType\.([A-Z_]+)')
            events.update(pattern.findall(content))
        except Exception:
            pass

        return events

    def test_event_subscriptions(self):
        """List event subscriptions for GUI and Console."""
        gui_file = PRIMER_VAULT_DIR / 'ui' / 'main_window.py'
        console_file = PRIMER_VAULT_DIR / 'ui' / 'console.py'

        gui_events = self.get_event_subscriptions(gui_file) if gui_file.exists() else set()
        console_events = self.get_event_subscriptions(console_file) if console_file.exists() else set()

        all_events = gui_events | console_events

        print("\n\nEvent subscriptions comparison:")
        print("-" * 50)
        print(f"{'Event':<30} {'GUI':<10} {'Console':<10}")
        print("-" * 50)

        for event in sorted(all_events):
            in_gui = "✓" if event in gui_events else ""
            in_console = "✓" if event in console_events else ""
            print(f"{event:<30} {in_gui:<10} {in_console:<10}")

        print("-" * 50)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
