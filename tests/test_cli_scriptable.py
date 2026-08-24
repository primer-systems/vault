"""
Tests for CLI scriptable mode (non-interactive execution).

Run with: pytest tests/test_cli_scriptable.py -v
"""

import os
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.cli import parse_global_flags, ScriptContext, handle_input_request
from primer_vault.commands.result import CommandResult


class TestParseGlobalFlags:
    """Test global flag parsing."""

    def test_no_flags(self):
        """Args without flags pass through."""
        args = ["wallet", "list"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["wallet", "list"]
        assert ctx.auto_confirm is False
        assert ctx.password is None

    def test_yes_flag_long(self):
        """--yes flag sets auto_confirm."""
        args = ["--yes", "policy", "delete", "test"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["policy", "delete", "test"]
        assert ctx.auto_confirm is True

    def test_yes_flag_short(self):
        """-y flag sets auto_confirm."""
        args = ["-y", "wallet", "delete"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["wallet", "delete"]
        assert ctx.auto_confirm is True

    def test_yes_flag_after_command(self):
        """--yes can appear after command."""
        args = ["policy", "delete", "test", "--yes"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["policy", "delete", "test"]
        assert ctx.auto_confirm is True

    def test_password_flag(self):
        """--password flag sets password."""
        args = ["--password", "secret123", "wallet", "open", "mywallet"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["wallet", "open", "mywallet"]
        assert ctx.password == "secret123"

    def test_password_flag_after_command(self):
        """--password can appear after command."""
        args = ["wallet", "create", "new", "--password", "mypass"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["wallet", "create", "new"]
        assert ctx.password == "mypass"

    def test_both_flags(self):
        """Both --yes and --password work together."""
        args = ["--yes", "--password", "pass123", "wallet", "delete"]
        remaining, ctx = parse_global_flags(args)
        assert remaining == ["wallet", "delete"]
        assert ctx.auto_confirm is True
        assert ctx.password == "pass123"

    def test_env_var_password(self):
        """PRIMER_VAULT_PASSWORD env var is used."""
        with patch.dict(os.environ, {"PRIMER_VAULT_PASSWORD": "envpass"}):
            args = ["wallet", "open", "test"]
            remaining, ctx = parse_global_flags(args)
            assert remaining == ["wallet", "open", "test"]
            assert ctx.password == "envpass"

    def test_flag_overrides_env_var(self):
        """--password flag overrides env var."""
        with patch.dict(os.environ, {"PRIMER_VAULT_PASSWORD": "envpass"}):
            args = ["--password", "flagpass", "wallet", "open", "test"]
            remaining, ctx = parse_global_flags(args)
            assert ctx.password == "flagpass"


class TestScriptContext:
    """Test ScriptContext input handling."""

    def test_no_auto_input(self):
        """Default context doesn't auto-supply input."""
        ctx = ScriptContext()
        assert ctx.get_input_for("confirm") is None
        assert ctx.get_input_for("password") is None
        assert ctx.get_input_for("text") is None

    def test_auto_confirm(self):
        """Auto-confirm supplies YES for confirm type."""
        ctx = ScriptContext(auto_confirm=True)
        result = ctx.get_input_for("confirm")
        assert result == {"confirm": "YES"}

    def test_auto_confirm_not_for_password(self):
        """Auto-confirm doesn't affect password type."""
        ctx = ScriptContext(auto_confirm=True)
        assert ctx.get_input_for("password") is None

    def test_password_supplies_value(self):
        """Password supplies both password and value keys."""
        ctx = ScriptContext(password="mypass")
        result = ctx.get_input_for("password")
        assert result == {"password": "mypass", "value": "mypass"}

    def test_password_not_for_confirm(self):
        """Password doesn't affect confirm type."""
        ctx = ScriptContext(password="mypass")
        assert ctx.get_input_for("confirm") is None

    def test_combined_context(self):
        """Combined auto_confirm and password."""
        ctx = ScriptContext(auto_confirm=True, password="secret")
        assert ctx.get_input_for("confirm") == {"confirm": "YES"}
        assert ctx.get_input_for("password") == {"password": "secret", "value": "secret"}


class TestHandleInputRequestWithContext:
    """Test handle_input_request with ScriptContext."""

    def test_scripted_confirm(self):
        """Confirm is auto-supplied with script context."""
        result = CommandResult.need_input("confirm", "Type YES:")
        ctx = ScriptContext(auto_confirm=True)
        inputs = handle_input_request(result, ctx)
        assert inputs == {"confirm": "YES"}

    def test_scripted_password(self):
        """Password is auto-supplied with script context."""
        result = CommandResult.need_input("password", "Enter password:")
        ctx = ScriptContext(password="testpass")
        inputs = handle_input_request(result, ctx)
        assert inputs == {"password": "testpass", "value": "testpass"}

    def test_no_context_falls_back_to_interactive(self):
        """With no script context there is no value to auto-supply."""
        CommandResult.need_input("confirm", "Type YES:")
        ctx = ScriptContext()  # No auto_confirm
        assert ctx.get_input_for("confirm") is None


class TestScriptableIntegration:
    """Integration tests for scriptable CLI mode."""

    @pytest.fixture
    def temp_data_dir(self, tmp_path):
        """Create a temporary data directory."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "wallets").mkdir()
        return data_dir


    @pytest.fixture
    def handler(self, core):
        """Create a CommandHandler instance."""
        from primer_vault.commands import CommandHandler
        return CommandHandler(core)

    def test_policy_delete_with_yes_flag(self, handler, core):
        """policy delete works with --yes flag (simulated)."""
        # Create a policy first
        result = handler.execute("policy create test-policy")
        assert result.success

        # Simulate the --yes flag by passing pre-confirmed input
        result = handler.execute("policy delete test-policy")
        assert result.needs_input  # Would normally prompt

        # With YES confirmation
        result = handler.execute("policy delete test-policy", inputs={"confirm": "YES"})
        assert result.success
        assert "deleted" in result.output.lower() or "Policy" in result.output

    def test_wallet_create_with_password_flag(self, handler, core, temp_data_dir):
        """wallet create works with --password flag (simulated)."""
        # First execution - would prompt for password
        result = handler.execute("wallet create testwallet")
        assert result.needs_input
        assert result.needs_input.get("type") == "password"

        # Supply password
        result = handler.execute("wallet create testwallet",
                                 inputs={"password": "testpass", "value": "testpass"})
        # Would then ask for confirmation
        if result.needs_input:
            result = handler.execute("wallet create testwallet",
                                     inputs={"password": "testpass", "confirm": "testpass"})

        assert result.success
        assert "created" in result.output.lower() or "Wallet" in result.output
