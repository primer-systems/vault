"""
Shared utility functions for Vault.

Contains path helpers and common utilities used across packages.
"""

import sys
from pathlib import Path

from platformdirs import user_data_dir


# ============================================
# Name Validation
# ============================================

class NameValidationError(ValueError):
    """Raised when a name fails validation."""
    pass


def validate_name(name: str, field: str = "Name") -> str:
    """
    Validate a display name for agents, policies, or addresses.

    Names must:
    - Be non-empty (after stripping whitespace)
    - Contain only printable ASCII characters (codes 32-126)
    - Be 100 characters or less

    Args:
        name: The name to validate
        field: Field name for error messages (e.g., "Policy name", "Agent name")

    Returns:
        The validated name (stripped of leading/trailing whitespace)

    Raises:
        NameValidationError: If name is invalid
    """
    if not name:
        raise NameValidationError(f"{field} cannot be empty")

    name = name.strip()

    if not name:
        raise NameValidationError(f"{field} cannot be empty or whitespace only")

    if len(name) > 100:
        raise NameValidationError(f"{field} must be 100 characters or less")

    invalid = [c for c in name if not (32 <= ord(c) <= 126)]
    if invalid:
        raise NameValidationError(
            f"{field} contains invalid characters. Only printable ASCII is allowed."
        )

    return name


# ============================================
# Agent Onboarding Snippet
# ============================================

def agent_config_snippet(agent_id: str, token: str, auth_mode: str,
                         url: str = "http://localhost:4663") -> str:
    """The paste-into-system-prompt block handed to an agent at commissioning.

    Single source of truth for this copy (GUI dialogs + CLI both use it), so it
    stays consistent everywhere. Covers both capabilities and sets the key-custody
    boundary and the approval/pending expectation.
    """
    return (
        "I've set up a local service called Primer Vault for you to use. It holds "
        "my crypto keys and enforces my limits, so you never handle keys yourself. "
        "It does two things:\n"
        "\n"
        "- Trading: execute token swaps on Uniswap (Robinhood Chain). Use it "
        "whenever I ask for a trade or your strategy calls for one.\n"
        "- x402 payments: pay for paid APIs. Use it whenever you get an HTTP 402 "
        "(Payment Required) response.\n"
        "\n"
        f"Read the instructions at {url}/agent now — they cover both, including how "
        "to structure requests and that some actions need my approval (they come "
        "back \"pending\", so poll for the result).\n"
        "\n"
        "Your credentials:\n"
        f"PRIMER_VAULT_AGENT_ID={agent_id}\n"
        f"PRIMER_VAULT_AGENT_TOKEN={token}\n"
        f"PRIMER_VAULT_AUTH_MODE={auth_mode}\n"
        f"PRIMER_VAULT_URL={url}\n"
        "\n"
        "Keep the token secret — never print, log, or share it."
    )


def get_app_dir() -> Path:
    """Get the application data directory."""
    if getattr(sys, 'frozen', False):
        # Running as compiled (PyInstaller) — keep data next to the executable
        app_dir = Path(sys.executable).parent / "data"
    else:
        # Pip-installed or dev mode — use platform-standard location
        # Windows: %APPDATA%/Primer/Vault, Linux: ~/.local/share/Primer/Vault, macOS: ~/Library/Application Support/Primer/Vault
        app_dir = Path(user_data_dir("Vault", appauthor="Primer"))

    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_wallet_dir() -> Path:
    """Get the wallet storage directory."""
    return get_app_dir() / "wallets"


def get_default_wallet_path() -> Path:
    """Get path to default wallet file."""
    return get_wallet_dir() / "default.json"


def get_assets_dir() -> Path:
    """Get the assets directory."""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile mode extracts to temp dir
        if hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS) / "assets"
        # Onedir mode - assets next to exe
        return Path(sys.executable).parent / "assets"
    # Installed as pip package — assets bundled inside package
    pkg_assets = Path(__file__).parent / "assets"
    if pkg_assets.exists():
        return pkg_assets
    # Dev mode fallback (running directly from repo without installing)
    return Path(__file__).parent.parent.parent / "assets"


def get_settings_path() -> Path:
    """Get path to settings file."""
    return get_app_dir() / "settings.json"


def get_logs_dir() -> Path:
    """Get the logs directory."""
    logs_dir = get_app_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir
