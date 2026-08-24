"""One home per setting.

gui_settings.json is for how the window looks and behaves. Everything the
daemon or the CLI also has to know lives in the core's settings.json. The two
files had drifted into holding copies of the same facts, and each copy went
wrong in its own way: a server port the window used but a headless run did not,
a rate limit that reached nothing, a replay window shown as one number while
another was enforced, three Uniswap addresses that could be edited and were
then ignored.

These tests hold the line: no core-owned key can come back into the GUI's file,
and each formerly-dead control now reaches the thing it claims to control.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


#: Settings the core owns. None of these may be written to gui_settings.json.
CORE_OWNED = {
    "server_port", "custom_port_enabled",   # settings.json: server.default_port
    "rate_limit",                           # settings.json: server.rate_limit_per_minute
    "replay_window_seconds",                # settings.json: signing.max_request_age_seconds
    "admin_api_mode",                       # settings.json: security.admin_api_mode
    "allow_lan", "verify_settlements",      # settings.json: server / signing
    "rhc_rpc",                              # settings.json: rpc.<chain>
    "wallet_path",                          # the core's wallet_path.txt
    "uniswap_factory", "uniswap_quoter", "uniswap_router",  # networks.py, not a setting
}


@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def offline(monkeypatch):
    """No live RPC calls from a test. The network dialog kicks off two
    connectivity checks in background threads on construction, which reach a
    public endpoint and then emit into a dialog the test has already closed."""
    from primer_vault.ui.dialogs import NetworkSettingsDialog

    monkeypatch.setattr(NetworkSettingsDialog, "_check_rhc_connection", lambda self: None)
    monkeypatch.setattr(NetworkSettingsDialog, "_check_dex_connection", lambda self: None)


@pytest.fixture
def core(tmp_path):
    from primer_vault.core import Vault
    c = Vault(data_dir=tmp_path)
    yield c
    c.release_instance_lock()


# ---------------------------------------------------------------------------
# The GUI's file holds only GUI-owned keys
# ---------------------------------------------------------------------------

def test_no_core_owned_setting_is_allowed_in_the_gui_file():
    from primer_vault.ui.main_window import MainWindow

    leaked = MainWindow.GUI_OWNED_SETTINGS & CORE_OWNED
    assert not leaked, f"core-owned settings listed as GUI-owned: {sorted(leaked)}"


def test_a_dialog_answer_is_filtered_before_it_is_stored(tmp_path, monkeypatch):
    """The filter, not each call site, is what keeps the file clean."""
    from primer_vault.ui import main_window as mw

    monkeypatch.setattr(mw, "get_app_dir", lambda: tmp_path)
    window = SimpleNamespace(
        _settings={},
        GUI_OWNED_SETTINGS=mw.MainWindow.GUI_OWNED_SETTINGS,
        update_activity=lambda *a, **k: None,
    )
    window._save_settings = lambda: mw.MainWindow._save_settings(window)

    # Exactly what the two dialogs return, GUI-owned and core-owned mixed.
    mw.MainWindow._remember_gui_settings(window, {
        "theme": "dark",
        "auto_lock_minutes": 5,
        "auto_start_server": False,
        "server_port": 5000,
        "rate_limit": 60,
        "replay_window_seconds": 120,
        "admin_api_mode": "open",
        "rhc_rpc": "https://example.invalid",
        "verify_settlements": False,
    })

    stored = set(window._settings)
    assert stored == {"theme", "auto_lock_minutes", "auto_start_server"}
    assert not stored & CORE_OWNED


# ---------------------------------------------------------------------------
# Every control reaches the core
# ---------------------------------------------------------------------------

def test_the_rate_limit_the_user_sets_is_the_one_enforced(core):
    from primer_vault.services.server import rate_limiter

    core.settings_manager.set_rate_limit(7)
    core._apply_rate_limit()

    assert rate_limiter.requests_per_minute == 7
    rate_limiter.configure(300)   # leave the shared limiter as we found it


def test_a_rate_limit_of_zero_means_unlimited_not_blocked():
    """The control has always said "0 = unlimited". Before, 0 refused every
    request - the setting would have locked the user out of their own agent
    API the moment it was wired up."""
    from primer_vault.services.server import RateLimiter

    limiter = RateLimiter(requests_per_minute=0)
    assert not any(limiter.is_rate_limited("1.2.3.4") for _ in range(50))


def test_the_gui_starts_the_server_on_the_port_the_core_records(core, qt_app,
                                                                monkeypatch):
    """The window and a headless run must agree on the port."""
    from primer_vault.ui.main_window import MainWindow

    core.settings_manager.set_default_port(5123)

    started = {}
    window = SimpleNamespace(
        core=SimpleNamespace(
            settings_manager=core.settings_manager,
            start_server=lambda port, allow_lan: started.update(port=port),
            is_server_running=lambda: False,
            server_port=5123,
        ),
        _settings={},
        update_status=lambda: None,
        update_status_indicators=lambda: None,
        update_activity=lambda *a, **k: None,
    )
    MainWindow._auto_start_server(window)

    assert started.get("port") == 5123


def test_the_uniswap_addresses_are_not_settings(qt_app, core, offline):
    """They come from the network registry and are shown, not edited - so the
    dialog must not offer them back as something to save."""
    from primer_vault.ui.dialogs import NetworkSettingsDialog

    dialog = NetworkSettingsDialog(core=core, settings={})
    try:
        assert dialog.uniswap_factory_input.isReadOnly()
        assert dialog.uniswap_quoter_input.isReadOnly()
        assert dialog.uniswap_router_input.isReadOnly()
        assert not set(dialog.get_settings()) & {
            "uniswap_factory", "uniswap_quoter", "uniswap_router"}
    finally:
        dialog.close()


def test_the_dialog_shows_the_replay_window_actually_in_force(qt_app, core):
    """It read the GUI's copy, so it could display 300 while 60 was enforced -
    and pressing OK would then widen the window back to what it displayed."""
    from primer_vault.ui.dialogs import SettingsDialog

    core.set_max_request_age(60)

    dialog = SettingsDialog({"replay_window_seconds": 300}, core=core)
    try:
        assert dialog.replay_window_input.value() == 60
    finally:
        dialog.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
