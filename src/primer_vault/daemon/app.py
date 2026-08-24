"""
Daemon entry point.

Starts the Vault core, admin API, and agent server.
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from ..core.settings import ADMIN_API_MODE_OPEN
from ..core import Vault
from ..core.interfaces import HeadlessApprovalHandler
from ..instance_lock import InstanceAlreadyRunning
from ..utils import DataDirectoryError

logger = logging.getLogger(__name__)


class Daemon:
    """
    Vault daemon process.

    Owns:
    - Vault core instance
    - Admin API server (for GUI/Console connections)
    - Agent HTTP server (for AI agents)
    """

    def __init__(
        self,
        data_dir: Path = None,
        admin_port: int = 4664,
        agent_port: int = 4663,
        allow_lan: bool = False,
        admin_open: bool = False
    ):
        self._data_dir = data_dir
        self._admin_open = admin_open
        self._admin_port = admin_port
        self._agent_port = agent_port
        self._allow_lan = allow_lan

        self._core: Vault = None
        self._admin_server = None
        self._running = False
        self._shutdown_event = threading.Event()

    def start(self):
        """Start the daemon."""
        logger.info("Starting Vault daemon...")

        # Initialize core with headless approval handler
        self._core = Vault(data_dir=self._data_dir)

        # There is no window here to enable the Admin API from, so a daemon
        # started with --admin-open records the choice now. Without it the
        # operator can start the daemon and then find that the queued approvals
        # below have no channel to be approved through.
        if self._admin_open:
            self._core.settings_manager.set_admin_api_mode(ADMIN_API_MODE_OPEN)
        # Queue requests rather than auto-rejecting — operator can approve/reject via CLI or admin API
        self._core.set_approval_handler(HeadlessApprovalHandler(self._core, auto_reject=False))

        # Start admin API server
        from .admin_api import AdminAPIServer
        self._admin_server = AdminAPIServer(self._core, port=self._admin_port)
        self._admin_server.start()

        # Start agent HTTP server. start_server returns False on a bind failure
        # (the port is taken) rather than raising, so it has to be checked - the
        # daemon must not announce an agent API that never opened, since a
        # headless operator has no window to notice the difference and their
        # agents would be talking to whatever else holds the port.
        if not self._core.start_server(self._agent_port, self._allow_lan):
            logger.error(
                "Agent API could not bind port %d - it is already in use. The "
                "daemon has not started; free the port or use a different "
                "--agent-port.", self._agent_port)
            if self._admin_server:
                try:
                    self._admin_server.stop()
                except Exception:
                    pass
                self._admin_server = None
            return

        self._running = True
        logger.info(f"Daemon started - Admin API on :{self._admin_port}, Agent API on :{self._agent_port}")

    def stop(self):
        """Stop the daemon gracefully."""
        logger.info("Stopping Vault daemon...")

        self._running = False

        # Stop agent server
        if self._core and self._core.is_server_running():
            self._core.stop_server()

        if self._admin_server:
            self._admin_server.stop()

        # Lock wallet on shutdown
        if self._core:
            self._core.lock_wallet()
            self._core.release_instance_lock()

        self._shutdown_event.set()
        logger.info("Daemon stopped")

    def wait(self):
        """Wait for shutdown signal."""
        self._shutdown_event.wait()

    def is_running(self) -> bool:
        """Check if daemon is running."""
        return self._running


def run_daemon(
    data_dir: Path = None,
    admin_port: int = 4664,
    agent_port: int = 4663,
    allow_lan: bool = False,
    interactive: bool = True,
    startup_wallet: str = None,
    startup_password: str = None,
    admin_open: bool = False,
):
    """
    Run the daemon.

    Args:
        data_dir: Data directory (defaults to the standard location for this
            build - see utils.get_app_dir)
        admin_port: Port for admin API (default 4664)
        agent_port: Port for agent API (default 4663)
        allow_lan: Allow LAN access to agent API
        interactive: If True, prompt for wallet password
        admin_open: Allow local processes to drive the Admin API. There is no
            window here to enable it from, so a headless operator who wants CLI
            control has to say so at startup or beforehand.
    """
    from ..services.logging import configure_logging
    configure_logging()

    daemon = Daemon(
        data_dir=data_dir,
        admin_port=admin_port,
        agent_port=agent_port,
        allow_lan=allow_lan,
        admin_open=admin_open,
    )

    # Handle shutdown signals
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        daemon.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        daemon.start()

        # start() returns without raising when the agent port is taken; it logs
        # the reason but does not mark itself running. Do not go on to announce a
        # daemon that is not up.
        if not daemon.is_running():
            print(f"Could not start the agent API: port {agent_port} is already "
                  f"in use. Free it or pass a different --agent-port.",
                  file=sys.stderr)
            sys.exit(1)

        # Auto-unlock wallet if credentials provided
        if startup_wallet and startup_password is not None:
            wallet_dir = daemon._core.get_wallet_dir()
            wallet_path = wallet_dir / startup_wallet
            if not wallet_path.suffix:
                wallet_path = wallet_path.with_suffix(".wallet")
            result = daemon._core.load_wallet(str(wallet_path), startup_password)
            if result.get("success"):
                logger.info(f"Wallet '{startup_wallet}' unlocked on startup")
                if interactive:
                    print(f"  Wallet: {startup_wallet} (unlocked)")
            else:
                logger.warning(f"Failed to unlock wallet '{startup_wallet}': {result.get('error')}")
                if interactive:
                    print(f"  Wallet: failed to unlock '{startup_wallet}' — {result.get('error')}")

        if interactive:
            print("\nVault daemon running")
            print(f"  Admin API: http://localhost:{admin_port}")
            print(f"  Agent API: http://localhost:{agent_port}")
            print("\nPress Ctrl+C to stop\n")

        daemon.wait()

    except InstanceAlreadyRunning as e:
        print(e.user_message(), file=sys.stderr)
        sys.exit(1)
    except DataDirectoryError as e:
        print(e.user_message(), file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        # A port bind failure (most often the admin port already in use). The
        # GUI handles the same fault by carrying on without the admin API;
        # headless has no window to fall back to, so it stops - but with a
        # sentence naming the ports, not a raw traceback.
        print(f"Could not start Vault's servers: {e}. A port may already be in "
              f"use - the admin API uses --admin-port ({admin_port}) and the "
              f"agent API uses --agent-port ({agent_port}).", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()


def main():
    """CLI entry point for daemon."""
    parser = argparse.ArgumentParser(description="Vault Daemon")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Data directory (default: beside the executable for a downloaded "
             "build, or the platform-standard location for a pip install)"
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=4664,
        help="Admin API port (default: 4664)"
    )
    parser.add_argument(
        "--agent-port",
        type=int,
        default=4663,
        help="Agent API port (default: 4663)"
    )
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="Allow LAN access to agent API"
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Don't prompt for input (for background service)"
    )

    args = parser.parse_args()

    run_daemon(
        data_dir=args.data_dir,
        admin_port=args.admin_port,
        agent_port=args.agent_port,
        allow_lan=args.allow_lan,
        interactive=not args.no_interactive
    )


if __name__ == "__main__":
    main()
