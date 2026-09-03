"""
Shared utility functions for Vault.

Contains path helpers and common utilities used across packages.
"""

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir


# ============================================
# Durable writes
# ============================================


def fsync_dir(directory: Path) -> None:
    """Flush a directory-entry change - a rename - all the way to disk.

    os.replace makes a rename atomic, but atomic is not durable: the new
    directory entry sits in the filesystem journal until its next commit, and
    a power loss before then reboots to the previous file. For most files
    that is a harmless few-seconds rollback; for the spend ledger it can
    revive allowance whose signed authorization is already in an agent's
    hands. Fsyncing the directory after the rename
    closes that window. A process crash never needs this - only power loss
    does.

    POSIX only. Windows cannot fsync a directory handle this way; there,
    os.replace plus the file-content fsync the writers already do is the
    ceiling, and NTFS's own metadata journaling is the remaining guarantee.

    Best effort by design: the rename has already succeeded when this runs,
    so a filesystem that refuses the directory open (some network mounts do)
    costs only the durability nicety, never the write.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ============================================
# HTTP request origin
# ============================================


def is_browser_request(headers) -> bool:
    """True if a web page told the browser to send this request.

    Vault's servers listen on loopback, where a request from a browser tab and
    one from a local program look identical at the socket. But a browser stamps
    `Origin` on anything a page initiates, and Vault's own callers never do:
    agents use ordinary HTTP clients, the CLI uses urlopen, and the GUI holds
    the core directly. So the header separates the two reliably here.

    Both servers refuse these. Without that, a site the user has open could
    drive Vault in the background - and against endpoints that move money or
    write the payment record, a request that succeeds is damaging even if the
    page is never allowed to read the reply.
    """
    return bool(headers.get("Origin"))


# Loopback names a request may legitimately arrive under. Anything else, on a
# loopback-bound socket, did not resolve here by accident.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_rebound_host(headers, bound_address: str) -> bool:
    """True if the Host header names something that is not this machine.

    `is_browser_request` closes the ordinary browser route, but not DNS
    rebinding: a page on attacker.tld whose DNS is re-pointed at 127.0.0.1
    becomes same-origin with the server, and a same-origin GET carries no
    `Origin` header at all. The Host header is what still gives it away -
    the browser sends the name the user typed, and that name is not ours.

    Judged against the address the socket is actually bound to, so the two
    cannot drift apart:

    - Bound to loopback: only loopback names are legitimate. Reject the rest.
    - Bound to 0.0.0.0 (`--allow-lan`): the request is meant to arrive under
      this machine's LAN address or name, which Vault does not know and cannot
      enumerate. There is nothing to check against, so this returns False and
      the operator's other protections carry it.

    A missing Host is allowed through: HTTP/1.0 clients may omit it, and no
    browser does - so demanding one would only break Vault's own callers.
    """
    if bound_address not in _LOOPBACK_HOSTS:
        return False

    host = (headers.get("Host") or "").strip().lower()
    if not host:
        return False

    # Strip the port. Bracketed IPv6 ("[::1]:4663") keeps its brackets so it
    # matches the set above; a bare IPv6 host has no port to strip.
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    elif host.count(":") == 1:
        host = host.split(":")[0]

    return host not in _LOOPBACK_HOSTS


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


class DataDirectoryError(RuntimeError):
    """The data directory could not be created, or cannot be written to.

    Carries a message written for the user rather than for a log, because the
    interfaces surface it directly: the GUI in a dialog, the CLI on stderr.
    """

    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = reason
        super().__init__(self.user_message())

    def user_message(self) -> str:
        """The full explanation shown to the user."""
        portable = getattr(sys, 'frozen', False)
        where = (
            "Vault keeps its data next to the application, so it can run from a "
            "USB stick or any folder without installing."
            if portable else
            "Vault keeps its data in the standard location for your platform."
        )
        advice = (
            "Move Vault to a folder you can write to - your Desktop or Documents "
            "folder will work - and start it again."
            if portable else
            "Check that you have permission to write to that folder."
        )
        return (
            f"Vault cannot write to its data folder, so it cannot start.\n\n"
            f"Folder:  {self.path}\n"
            f"Problem: {self.reason}\n\n"
            f"{where}\n\n{advice}"
        )


def get_app_dir() -> Path:
    """Get the application data directory, creating it if needed.

    Three locations, in order:

    - `PRIMER_VAULT_DATA_DIR`, if set. An environment variable rather than a
      flag because the thing that starts Vault on a server is a service
      manager, which cannot type one - and because everything a machine must
      remember across a reboot has to live somewhere the reboot cannot lose.
      This is also how one machine runs two independent Vaults: the instance
      lock, the wallet and the control channel are all scoped to this folder.
    - Frozen builds keep data in `data/` beside the executable. Nothing was
      installed, so nothing is left behind on the machine, and the whole thing
      travels on a USB stick. The path is resolved at run time, so the drive
      letter does not matter.
    - Pip installs use the platform-standard directory, which is what someone
      who ran an installer expects.

    Raises:
        DataDirectoryError: if the folder cannot be created or written to. This
            is called during startup, before any window exists, and a windowed
            build has no console to print to - so the failure has to arrive as
            something an interface can show, not as a bare OSError.
    """
    override = os.environ.get("PRIMER_VAULT_DATA_DIR")
    if override:
        app_dir = Path(override).expanduser()
    elif getattr(sys, 'frozen', False):
        # Running as compiled (PyInstaller) — keep data next to the executable
        app_dir = Path(sys.executable).parent / "data"
    else:
        # Pip-installed or dev mode — use platform-standard location
        # Windows: %LOCALAPPDATA%/Primer/Vault, Linux: ~/.local/share/Primer/Vault, macOS: ~/Library/Application Support/Primer/Vault
        app_dir = Path(user_data_dir("Vault", appauthor="Primer"))

    try:
        app_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DataDirectoryError(app_dir, e.strerror or str(e)) from e

    # Creating the folder does not prove it can be written to: it may already
    # exist as read-only, or sit on a mount that rejects writes. The wallet is
    # written on the very next step, so find out now, with a path to name.
    probe = app_dir / ".write-test"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise DataDirectoryError(app_dir, e.strerror or str(e)) from e

    return app_dir


def get_wallet_dir() -> Path:
    """Get the wallet storage directory."""
    return get_app_dir() / "wallets"


def get_assets_dir() -> Path:
    """Get the assets directory."""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile mode extracts to temp dir
        if hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS) / "assets"
        # Onedir mode - assets next to exe
        return Path(sys.executable).parent / "assets"
    # Assets live inside the package, so this is the same path whether Vault was
    # pip-installed or is running from a clone. One copy, one path - a second
    # location with a fallback between them is how the two drift apart.
    return Path(__file__).parent / "assets"


def get_settings_path() -> Path:
    """Get path to settings file."""
    return get_app_dir() / "settings.json"


def get_logs_dir() -> Path:
    """Get the logs directory."""
    logs_dir = get_app_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def redact_urls(text: str) -> str:
    """Strip any endpoint reference from `text` with a placeholder.

    Error text from web3 / requests names the RPC endpoint that failed, and a
    hosted node's URL carries the account's API key. That text reaches API
    responses (a failed trade's reason, a settlement receipt), so it must be
    stripped before it crosses the process boundary. Local logs keep the
    original - it is the user's own endpoint and useful for debugging there.

    Two shapes are redacted: a full `http(s)://…` URL, and urllib3's
    `url: <target>` form, which prints only the path (`url: /v2/<key>`) because
    it names the host separately - so the key can appear without an `http://`.
    """
    import re
    return re.sub(r"https?://\S+|\burl:\s*\S+", "[endpoint removed]", str(text))
