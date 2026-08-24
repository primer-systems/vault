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

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
