"""
The Admin API must be openable on a machine that has no GUI.

The default is `gui_only`, and that default is worth keeping: in open mode the
Admin API has no authentication at all, so any local process could create an
agent, receive its token, commission it to a funded address and spend. Requiring
a conscious act before opening that door is right.

The act must not require a window, though. A headless server has none, so a
GUI-only route would queue approvals for an operator with no channel to approve
them through - the daemon announces that approvals arrive "via CLI or admin
API".

The property being protected is *deliberate opt-in*, not *GUI-mediated*. These
tests pin that: the default stays closed, and there is a way to open it that does
not assume a screen.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core.settings import ADMIN_API_MODE_GUI_ONLY, ADMIN_API_MODE_OPEN


@pytest.fixture
def temp_data_dir(tmp_path):
    return tmp_path


class TestTheDefaultStaysClosed:

    def test_a_fresh_install_is_gui_only(self, core):
        assert core.settings_manager.get_admin_api_mode() == ADMIN_API_MODE_GUI_ONLY

    def test_headless_does_not_open_it_by_itself(self, temp_data_dir):
        """Starting a daemon must not be the thing that opens the door."""
        from primer_vault.core import Vault

        vault = Vault(data_dir=temp_data_dir)
        assert vault.settings_manager.get_admin_api_mode() == ADMIN_API_MODE_GUI_ONLY


class TestItCanBeOpenedWithoutAScreen:

    def _run(self, core, line):
        from primer_vault.commands.handler import CommandHandler
        return CommandHandler(core).execute(line)

    def test_the_cli_can_open_it(self, core):
        result = self._run(core, "config set admin-api open")
        assert result.success, result.error
        assert core.settings_manager.get_admin_api_mode() == ADMIN_API_MODE_OPEN

    def test_the_cli_can_close_it_again(self, core):
        core.settings_manager.set_admin_api_mode(ADMIN_API_MODE_OPEN)
        result = self._run(core, "config set admin-api gui-only")
        assert result.success, result.error
        assert core.settings_manager.get_admin_api_mode() == ADMIN_API_MODE_GUI_ONLY

    def test_opening_it_says_what_that_means(self, core):
        """Someone typing this deserves to know what they just allowed."""
        result = self._run(core, "config set admin-api open")
        assert "trust" in result.output.lower() or "any program" in result.output.lower()

    def test_a_nonsense_value_is_refused(self, core):
        result = self._run(core, "config set admin-api maybe")
        assert not result.success
        assert core.settings_manager.get_admin_api_mode() == ADMIN_API_MODE_GUI_ONLY

    def test_the_current_setting_can_be_read_back(self, core):
        result = self._run(core, "config get admin-api")
        assert result.success and "gui_only" in result.output

    def test_the_setting_appears_in_the_full_listing(self, core):
        """It is the most security-relevant setting Vault has; it should not be
        invisible to someone reviewing their configuration."""
        result = self._run(core, "config show")
        assert "admin-api" in result.output

    def test_the_listing_says_what_open_means(self, core):
        core.settings_manager.set_admin_api_mode(ADMIN_API_MODE_OPEN)
        result = self._run(core, "config show")
        assert "any local process" in result.output

    def test_the_flag_exists_on_headless(self):
        """--admin-open has to reach the daemon, not just be accepted by argparse."""
        import inspect

        from primer_vault.app import run_headless
        from primer_vault.daemon.app import Daemon, run_daemon

        assert "admin_open" in inspect.signature(run_headless).parameters
        assert "admin_open" in inspect.signature(run_daemon).parameters
        assert "admin_open" in inspect.signature(Daemon.__init__).parameters


class TestTheRefusalNamesAReachableRemedy:
    """A server being told to open a GUI is being told to do something it cannot."""

    def _messages(self):
        root = Path(__file__).parent.parent / "src" / "primer_vault"
        return [
            (root / "daemon" / "admin_api.py").read_text(encoding="utf-8"),
            (root / "client" / "core_client.py").read_text(encoding="utf-8"),
        ]

    def test_both_messages_offer_a_command(self):
        for text in self._messages():
            # A headless server has no window, so each message must name the
            # remedy that reaches a running server: restarting with --admin-open.
            assert "--admin-open" in text

    def test_neither_message_sends_a_server_to_a_window_alone(self):
        for text in self._messages():
            start = text.find("GUI-only mode")
            assert start != -1
            message = text[start:start + 500]
            assert "--admin-open" in message or "config set admin-api" in message

    def test_the_two_messages_agree_on_the_gui_path(self):
        """They disagreed: one said Settings > Security, the other
        Settings > Network > Security. One of them was sending people nowhere."""
        for text in self._messages():
            assert "Settings > Network > Security" not in text
