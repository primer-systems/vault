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

from ..core import Vault
from ..core.interfaces import HeadlessApprovalHandler

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
        allow_lan: bool = False
    ):
        self._data_dir = data_dir
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
        # Queue requests rather than auto-rejecting — operator can approve/reject via CLI or admin API
        self._core.set_approval_handler(HeadlessApprovalHandler(self._core, auto_reject=False))

        # Start admin API server
        from .admin_api import AdminAPIServer
        self._admin_server = AdminAPIServer(self._core, port=self._admin_port)
        self._admin_server.start()

        # Start agent HTTP server
        self._core.start_server(self._agent_port, self._allow_lan)

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
):
    """
    Run the daemon.

    Args:
        data_dir: Data directory (defaults to ~/.primer_vault/)
        admin_port: Port for admin API (default 4664)
        agent_port: Port for agent API (default 4663)
        allow_lan: Allow LAN access to agent API
        interactive: If True, prompt for wallet password
    """
    from ..services.logging import configure_logging
    configure_logging()

    daemon = Daemon(
        data_dir=data_dir,
        admin_port=admin_port,
        agent_port=agent_port,
        allow_lan=allow_lan
    )

    # Handle shutdown signals
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        daemon.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        daemon.start()

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
            print(f"\nVault daemon running")
            print(f"  Admin API: http://localhost:{admin_port}")
            print(f"  Agent API: http://localhost:{agent_port}")
            print(f"\nPress Ctrl+C to stop\n")

        daemon.wait()

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
        help="Data directory (default: ~/.primer_vault/)"
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
