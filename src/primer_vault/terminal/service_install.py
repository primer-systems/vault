"""Registering Vault with the operating system's service manager.

Run once, ever. It does not start Vault - it tells the OS that Vault is
something to start at boot, writes the file that says so, and exits. This is an
install action, not a runtime mode: after it, `primer-vault` is still the same
one command, and typing it attaches to whatever the OS started.

What it writes runs the plain `primer-vault` command. There is no second
version of the program and no flag distinguishing the boot copy - when a
service manager starts it there is no terminal attached, which the program
notices for itself and logs instead of prompting.

We do not build the service manager. systemd, launchd and Windows all ship one;
this writes the ten lines each of them wants.
"""

import getpass
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "primer-vault"


def _executable() -> str:
    """The command the service manager should run.

    `sys.argv[0]` is the console script pip installed, which is what the user
    typed and what they will expect to see in the unit file. Falling back to
    `python -m primer_vault` covers a checkout being run in place.
    """
    candidate = Path(sys.argv[0])
    if candidate.name.startswith("primer-vault"):
        resolved = shutil.which(candidate.name) or str(candidate.resolve())
        return resolved
    return f"{sys.executable} -m primer_vault"


def _systemd_unit(command: str, user: str) -> str:
    return f"""[Unit]
Description=Primer Vault
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
ExecStart={command}
Restart=on-failure
RestartSec=5
# The wallet password, if this machine unlocks one at boot. Keep the file
# 0600 and outside the Vault data directory - a password stored beside the
# wallet defeats the wallet's own encryption.
# EnvironmentFile=/etc/primer-vault.env

[Install]
WantedBy=multi-user.target
"""


def _install_systemd(command: str) -> int:
    user = getpass.getuser()
    unit = _systemd_unit(command, user)
    target = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

    try:
        target.write_text(unit, encoding="utf-8")
    except PermissionError:
        print(f"Writing {target} needs root. Either re-run with sudo, or save "
              f"this yourself:\n", file=sys.stderr)
        print(unit)
        return 1
    except OSError as e:
        print(f"Could not write {target}: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {target}")
    for argv in (["systemctl", "daemon-reload"],
                 ["systemctl", "enable", "--now", SERVICE_NAME]):
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"`{' '.join(argv)}` failed: {result.stderr.strip()}",
                  file=sys.stderr)
            print("Run it yourself once the unit file looks right.", file=sys.stderr)
            return 1

    print(f"Vault now starts at boot. Check it with:  systemctl status {SERVICE_NAME}")
    print("Type `primer-vault` at any time to attach to it.")
    print("")
    print("To have it unlock a wallet on its own at boot:")
    print("  1. primer-vault config set startup-wallet <name>")
    print("  2. put  PRIMER_VAULT_PASSWORD=<password>  in /etc/primer-vault.env")
    print("     and  chmod 600 /etc/primer-vault.env")
    print(f"  3. uncomment the EnvironmentFile= line in {target}")
    print(f"  4. systemctl restart {SERVICE_NAME}")
    return 0


def _install_windows(command: str) -> int:
    """Register with Task Scheduler rather than the Service Control Manager.

    Windows will not accept an arbitrary program as a service - a real service
    has to answer the OS's start/stop messages, which means either teaching the
    program that protocol or shipping a wrapper, and both are weight this does
    not need. Task Scheduler runs a plain program at boot, is built in, and
    needs no extra dependency.
    """
    argv = [
        "schtasks", "/Create",
        "/TN", SERVICE_NAME,
        "/TR", f'"{command}"',
        "/SC", "ONSTART",
        "/RL", "HIGHEST",
        "/F",
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        print(f"Could not register the scheduled task: {message}", file=sys.stderr)
        if "denied" in message.lower():
            print("Run this from an Administrator terminal.", file=sys.stderr)
        return 1

    print(f"Vault now starts at boot as scheduled task '{SERVICE_NAME}'.")
    print("Type `primer-vault` at any time to attach to it.")
    print("")
    print("To have it unlock a wallet on its own at boot:")
    print("  1. primer-vault config set startup-wallet <name>")
    print("  2. System Properties > Environment Variables > System variables >")
    print("     New, named PRIMER_VAULT_PASSWORD")
    print("     (Windows has no equivalent of systemd's EnvironmentFile, so the")
    print("      value is set on the machine rather than kept in a file.)")
    return 0


def install_service(args: list[str]) -> int:
    """Entry point for `primer-vault install-service`."""
    if args and args[0] in ("--help", "-h"):
        print("""install-service - run Vault at boot

Usage: primer-vault install-service

Registers the `primer-vault` command with this machine's service manager, so
Vault starts at boot and keeps running when you log out. Run once. It does not
start Vault now, and it changes nothing about how the command behaves.

Linux needs root (use sudo); Windows needs an Administrator terminal.

To have Vault unlock a wallet on its own at boot, set the wallet with
`config set startup-wallet <name>`, then put its password in
PRIMER_VAULT_PASSWORD - on Linux via /etc/primer-vault.env and the
EnvironmentFile= line in the unit file, on Windows as a system environment
variable. Never in settings.json, which sits beside the wallet file itself.""")
        return 0

    command = _executable()

    if sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            print("No systemd on this machine. Vault has nothing to register "
                  "with; start it yourself, or add it to whatever supervisor "
                  "this system uses.", file=sys.stderr)
            return 1
        return _install_systemd(command)

    if sys.platform == "win32":
        return _install_windows(command)

    if sys.platform == "darwin":
        print("macOS (launchd) is not wired up yet. Vault runs fine in a "
              "terminal; it just will not come back on its own after a reboot.",
              file=sys.stderr)
        return 1

    print(f"No service manager is known for this platform ({sys.platform}).",
          file=sys.stderr)
    return 1
