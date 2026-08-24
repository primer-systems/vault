"""
Architectural Compliance Tests

These tests verify the codebase follows ARCHITECTURE.md rules:
- Rule 1: Core is the single source of truth
- Rule 2: No Qt in core
- Rule 3: One implementation, multiple interfaces
- Rule 4: No duplication
- Rule 5: Changes flow through events

Run with: pytest tests/test_architecture.py -v
"""

import ast
import os
import re
from pathlib import Path
from typing import Set, List, Tuple

import pytest

# Get the src directory and package root
SRC_DIR = Path(__file__).parent.parent / "src"
PRIMER_VAULT_DIR = SRC_DIR / "primer_vault"


def get_python_files(directory: Path) -> List[Path]:
    """Get all Python files in a directory recursively."""
    return list(directory.rglob("*.py"))


def get_imports_from_file(filepath: Path) -> List[Tuple[str, int, str]]:
    """
    Parse a Python file and return all imports.

    Returns list of (module_name, line_number, full_line) tuples.
    """
    imports = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')

        tree = ast.parse(content)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((alias.name, node.lineno, lines[node.lineno - 1].strip()))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                imports.append((module, node.lineno, lines[node.lineno - 1].strip()))
    except SyntaxError:
        pass  # Skip files with syntax errors

    return imports


def get_attribute_access(filepath: Path) -> List[Tuple[str, int, str]]:
    """
    Find attribute access patterns like 'self.core._signing_service'.

    Returns list of (attribute_chain, line_number, full_line) tuples.
    """
    accesses = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Pattern to find private attribute access on core or self
        pattern = re.compile(r'(?:self\.)?core\._\w+|self\._(?:signing_service|policy_store|wallet)')

        for lineno, line in enumerate(lines, 1):
            matches = pattern.findall(line)
            for match in matches:
                accesses.append((match, lineno, line.strip()))
    except Exception:
        pass

    return accesses


class TestNoQtInCore:
    """Rule 2: No Qt imports in core, services, models, daemon, or client."""

    FORBIDDEN_DIRS = ['core', 'services', 'models', 'daemon', 'client']
    QT_MODULES = ['PyQt6', 'PyQt5', 'PySide6', 'PySide2']

    def get_qt_violations(self) -> List[Tuple[Path, str, int, str]]:
        """Find all Qt imports in forbidden directories."""
        violations = []

        for dirname in self.FORBIDDEN_DIRS:
            dirpath = PRIMER_VAULT_DIR / dirname
            if not dirpath.exists():
                continue

            for filepath in get_python_files(dirpath):
                imports = get_imports_from_file(filepath)
                for module, lineno, line in imports:
                    if any(qt in module for qt in self.QT_MODULES):
                        violations.append((filepath.relative_to(PRIMER_VAULT_DIR), module, lineno, line))

        return violations

    def test_no_qt_in_core(self):
        """Core layer must not import Qt."""
        violations = self.get_qt_violations()

        if violations:
            msg = "Qt imports found in core layer (violates Rule 2):\n"
            for filepath, _module, lineno, line in violations:
                msg += f"  {filepath}:{lineno}: {line}\n"
            pytest.fail(msg)

class TestUIUsesPublicAPI:
    """Rule 1 & 3: UI must use Core's public methods, not internal attributes."""

    PRIVATE_PATTERNS = [
        r'core\._signing_service',
        r'core\._policy_store',
        r'core\._wallet',
        r'core\._event_bus',
        r'core\._approval_handler',
        r'core\._agent_server',
    ]

    def get_private_access_violations(self) -> List[Tuple[Path, str, int, str]]:
        """Find UI code accessing Core's private attributes."""
        violations = []
        ui_dir = PRIMER_VAULT_DIR / 'ui'

        if not ui_dir.exists():
            return violations

        pattern = re.compile('|'.join(self.PRIVATE_PATTERNS))

        for filepath in get_python_files(ui_dir):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                for lineno, line in enumerate(lines, 1):
                    # Skip comments
                    if line.strip().startswith('#'):
                        continue

                    matches = pattern.findall(line)
                    for match in matches:
                        violations.append((
                            filepath.relative_to(PRIMER_VAULT_DIR),
                            match,
                            lineno,
                            line.strip()
                        ))
            except Exception:
                pass

        return violations

    def test_no_private_core_access(self):
        """UI must not access Core's private attributes."""
        violations = self.get_private_access_violations()

        if violations:
            msg = "UI accessing Core private attributes (violates Rules 1 & 3):\n"
            for filepath, attr, lineno, line in violations:
                msg += f"  {filepath}:{lineno}: {attr}\n    {line}\n"
            pytest.fail(msg)


class TestUIImportRestrictions:
    """UI should not import certain things directly from models/utils."""

    # Functions that should only be called via Core, not imported directly
    FORBIDDEN_MODEL_IMPORTS = [
        'generate_agent_token',
        'generate_agent_id',
        'encrypt_agent_secret',
        'hash_bearer_token',
        'generate_intent_mandate',
        'PolicyStore',  # UI should never access store directly
    ]

    def get_forbidden_imports(self) -> List[Tuple[Path, str, int, str]]:
        """Find forbidden imports in UI code."""
        violations = []
        ui_dir = PRIMER_VAULT_DIR / 'ui'

        if not ui_dir.exists():
            return violations

        for filepath in get_python_files(ui_dir):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    lines = content.split('\n')

                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if node.module and 'models' in node.module:
                            for alias in node.names:
                                if alias.name in self.FORBIDDEN_MODEL_IMPORTS:
                                    violations.append((
                                        filepath.relative_to(PRIMER_VAULT_DIR),
                                        alias.name,
                                        node.lineno,
                                        lines[node.lineno - 1].strip()
                                    ))
            except Exception:
                pass

        return violations

    def test_no_forbidden_model_imports(self):
        """UI must not import model functions that bypass Core."""
        violations = self.get_forbidden_imports()

        if violations:
            msg = "UI importing forbidden model functions (violates architecture):\n"
            for filepath, name, lineno, line in violations:
                msg += f"  {filepath}:{lineno}: {name}\n    {line}\n"
            pytest.fail(msg)


class TestCoreMethodsExist:
    """Verify Core methods called by UI actually exist."""

    def get_core_method_calls(self) -> Set[str]:
        """Find all method calls on 'self.core' in UI code."""
        methods = set()
        ui_dir = PRIMER_VAULT_DIR / 'ui'

        if not ui_dir.exists():
            return methods

        # Pattern: self.core.method_name( or self.core.property
        pattern = re.compile(r'self\.core\.([a-z_][a-z0-9_]*)')

        for filepath in get_python_files(ui_dir):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                matches = pattern.findall(content)
                methods.update(matches)
            except Exception:
                pass

        return methods

    def get_core_public_methods(self) -> Set[str]:
        """Get all public methods and properties on Vault class."""
        methods = set()
        core_file = PRIMER_VAULT_DIR / 'core' / 'vault.py'

        if not core_file.exists():
            return methods

        try:
            with open(core_file, 'r', encoding='utf-8') as f:
                content = f.read()

            tree = ast.parse(content)

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == 'Vault':
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            if not item.name.startswith('_'):
                                methods.add(item.name)
                        elif isinstance(item, ast.AsyncFunctionDef):
                            if not item.name.startswith('_'):
                                methods.add(item.name)
        except Exception:
            pass

        # Also add property names
        methods.add('event_bus')  # Known property

        return methods

    def test_ui_calls_existing_methods(self):
        """All Core methods called by UI must exist on Vault."""
        called = self.get_core_method_calls()
        available = self.get_core_public_methods()

        # Filter out private method calls (shouldn't happen, caught by other test)
        called_public = {m for m in called if not m.startswith('_')}

        missing = called_public - available

        if missing:
            msg = "UI calls Core methods that don't exist:\n"
            for method in sorted(missing):
                msg += f"  - core.{method}()\n"
            msg += f"\nAvailable public methods: {sorted(available)}"
            pytest.fail(msg)

class TestEventBusUsage:
    """Rule 5: Changes flow through events - verify Core emits events."""

    EXPECTED_EVENTS = [
        'AGENT_CREATED',
        'AGENT_UPDATED',
        'AGENT_DELETED',
        'POLICY_CREATED',
        'POLICY_UPDATED',
        'POLICY_DELETED',
        'TRANSACTION_CREATED',
        'TRANSACTION_UPDATED',
        'WALLET_LOCKED',
        'WALLET_UNLOCKED',
        'SERVER_STARTED',
        'SERVER_STOPPED',
        'APPROVAL_NEEDED',
        'APPROVAL_RESOLVED',
        'ACTIVITY',
    ]

    def get_emitted_events(self) -> Set[str]:
        """Find all event types emitted by Core."""
        events = set()

        # Check both primer_vault.py and events.py (emit_activity is in events.py)
        files_to_check = [
            PRIMER_VAULT_DIR / 'core' / 'vault.py',
            PRIMER_VAULT_DIR / 'core' / 'events.py',
        ]

        for core_file in files_to_check:
            if not core_file.exists():
                continue

            try:
                with open(core_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Pattern: EventType.EVENT_NAME
                pattern = re.compile(r'EventType\.([A-Z_]+)')
                events.update(pattern.findall(content))
            except Exception:
                pass

        return events

    def test_core_emits_expected_events(self):
        """Core should emit all standard event types."""
        emitted = self.get_emitted_events()
        expected = set(self.EXPECTED_EVENTS)

        missing = expected - emitted

        if missing:
            msg = "Core doesn't emit expected events:\n"
            for event in sorted(missing):
                msg += f"  - {event}\n"
            pytest.fail(msg)

class TestWalletDirectAccess:
    """UI should not create/restore wallets directly - must go through Core.

    "UI" is every file that draws a window, not just the ui/ package. The wallet
    dialogs live under wallet/ for import reasons - core code imports
    wallet.crypto and must not pull Qt in with it - but they are UI and the same
    rule applies to them.
    """

    FORBIDDEN_PATTERNS = [
        r'VaultWallet\.create\s*\(',
    ]

    #: Files that draw windows, wherever they sit in the tree.
    UI_PATHS = ['ui', os.path.join('wallet', 'dialogs.py')]

    #: Lines exempt from the rule, each with the reason it is allowed.
    #:
    #: A scratch wallet built to preview derivations is not the wallet being
    #: created: it is never saved, never unlocked, and never becomes the user's.
    #: The rule exists to stop the UI writing a real wallet behind Core's back,
    #: which these do not do.
    ALLOWED = {
        (os.path.join('wallet', 'dialogs.py'), '_show_derivation_dialog'),
        (os.path.join('wallet', 'dialogs.py'), '_pick_ledger_addresses'),
    }

    def get_direct_wallet_creation(self) -> List[Tuple[Path, str, int, str]]:
        """Find direct wallet creation in UI code."""
        violations = []
        pattern = re.compile('|'.join(self.FORBIDDEN_PATTERNS))

        targets = []
        for entry in self.UI_PATHS:
            path = PRIMER_VAULT_DIR / entry
            if path.is_dir():
                targets.extend(get_python_files(path))
            elif path.is_file():
                targets.append(path)

        for filepath in targets:
            rel = str(filepath.relative_to(PRIMER_VAULT_DIR))
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            except Exception:
                continue

            enclosing = ''
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith('def ') or stripped.startswith('async def '):
                    enclosing = stripped.split('(')[0].split()[-1]
                if stripped.startswith('#'):
                    continue
                if (rel, enclosing) in self.ALLOWED:
                    continue

                for match in pattern.findall(line):
                    violations.append((
                        filepath.relative_to(PRIMER_VAULT_DIR), match, lineno, stripped))

        return violations

    def test_no_direct_wallet_creation(self):
        """UI must not create wallets directly - use Core methods."""
        violations = self.get_direct_wallet_creation()

        if violations:
            msg = "UI creating wallets directly (violates architecture):\n"
            for filepath, _pattern, lineno, line in violations:
                msg += f"  {filepath}:{lineno}: {line}\n"
            pytest.fail(msg)


class TestStylingDiscipline:
    """Rule 4 (no duplication) applied to styling: every color flows through the
    centralized design system. No inline stylesheets, no raw brand constants, no
    literal hex outside the single source of truth.

    See docs/styling-remediation/ for the rationale.
    """

    # The only two places allowed to set the application stylesheet.
    ALLOWED_SETSTYLESHEET = {
        os.path.join("app.py"),
        os.path.join("ui", "main_window.py"),
    }
    # Color data lives only here.
    COLOR_SOURCE_FILES = {
        os.path.join("design_tokens.py"),
        os.path.join("ui", "theme.py"),
    }
    # Non-color Theme.* constants that are allowed anywhere.
    ALLOWED_THEME_ATTRS = {
        "MONO_FONT", "PLACEHOLDER", "OPTIONAL",
        "MIN_DIALOG_WIDTH", "MIN_POPUP_WIDTH", "RUST",
    }

    def _code_lines(self):
        """Yield (relpath, lineno, line) for non-comment source lines."""
        for filepath in get_python_files(PRIMER_VAULT_DIR):
            rel = str(filepath.relative_to(PRIMER_VAULT_DIR))
            try:
                lines = filepath.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                yield rel, lineno, line

    def test_no_widget_level_setstylesheet(self):
        """Widgets must be styled via QSS + semantic properties, never inline."""
        violations = []
        for rel, lineno, line in self._code_lines():
            if ".setStyleSheet(" not in line and "setStyleSheet(f" not in line:
                # allow bare `app.setStyleSheet(` too
                if "setStyleSheet(" not in line:
                    continue
            if "setStyleSheet(" not in line:
                continue
            # theme.py references setStyleSheet only in docstrings/comments (skipped
            # by the docstring heuristic below), and the two allowed app-level calls.
            if rel in self.ALLOWED_SETSTYLESHEET:
                continue
            if rel == os.path.join("ui", "theme.py"):
                continue
            violations.append(f"  {rel}:{lineno}: {line.strip()}")
        assert not violations, (
            "Inline setStyleSheet() defeats the theme and never restyles on "
            "theme switch. Use a semantic property + a QSS rule instead:\n"
            + "\n".join(violations)
        )

    def test_no_raw_theme_color_constants(self):
        """No raw Theme.<COLOR> usage - use palette tokens / status helpers."""
        pattern = re.compile(r"Theme\.([A-Z_]+)")
        violations = []
        for rel, lineno, line in self._code_lines():
            for attr in pattern.findall(line):
                if attr in self.ALLOWED_THEME_ATTRS:
                    continue
                violations.append(f"  {rel}:{lineno}: Theme.{attr}  ->  {line.strip()}")
        assert not violations, (
            "Raw Theme.* color constants bypass the per-theme palette. Use a "
            "palette token via active()/status_color(), or a semantic property:\n"
            + "\n".join(violations)
        )

    def test_no_literal_hex_outside_color_source(self):
        """Literal hex colors may only appear in the color-source files."""
        pattern = re.compile(r"#[0-9a-fA-F]{3,6}\b")
        violations = []
        for rel, lineno, line in self._code_lines():
            if rel in self.COLOR_SOURCE_FILES:
                continue
            if pattern.search(line):
                violations.append(f"  {rel}:{lineno}: {line.strip()}")
        assert not violations, (
            "Literal hex colors must live only in design_tokens.py (or be a CSS "
            "var / CONSOLE token). Move the color into the palette:\n"
            + "\n".join(violations)
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestRulesAreDocumented:
    """ARCHITECTURE.md and this file describe the same rules.

    The document is what a contributor reads; this file is what actually stops
    them. A rule enforced here but absent there is a rule people break in good
    faith — and the reference was dangling entirely until it was written.
    """

    def _doc(self) -> str:
        path = Path(__file__).parent.parent / "ARCHITECTURE.md"
        assert path.exists(), "ARCHITECTURE.md is referenced by this file and by src/"
        return path.read_text(encoding="utf-8")

    def test_the_document_exists_and_is_substantial(self):
        assert len(self._doc()) > 2000

    def test_every_enforcing_test_is_named_in_the_document(self):
        doc = self._doc()
        source = Path(__file__).read_text(encoding="utf-8")
        enforcing = [
            name for name in re.findall(r"def (test_\w+)", source)
            # listing helpers assert nothing, and this class documents itself
            if not name.startswith("test_list") and "documented" not in name
            and "document" not in name
        ]
        undocumented = [name for name in enforcing if name not in doc]
        assert not undocumented, (
            "These rules are enforced but not written down in ARCHITECTURE.md:\n  "
            + "\n  ".join(undocumented))
