"""Hardware wallet prompts for the terminal.

The device work is not here. Talking to a Ledger - discovery, derivation,
EIP-712 signing, mapping its error codes onto ours - is all in
`wallet/ledger.py`, is Qt-free, and is shared by both editions. What was only
ever in the desktop is the *asking*: four dialogs that say plug it in, choose a
derivation path, check the screen, that was rejected.

So this file is the same four prompts, printed. The engine already asks for a
handler rather than assuming one - `services/signing.py` and
`services/trading.py` return LEDGER_SIGN_NOT_AVAILABLE when none is registered -
which is the right shape and stays: shared code asks whether a capability is
present, never which edition it is running in.

One limit is physical and cannot be handled here: signing on a Ledger requires
somebody to press its buttons. An unattended Vault has nobody, so a request
against a hardware-held address will sit until the device times out. That is
true of the desktop too, and it is why unattended machines want software-held
keys.
"""

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import Vault

logger = logging.getLogger(__name__)


def _attended() -> bool:
    """Is there a terminal to print prompts to and read a reply from."""
    return (sys.stdin is not None and sys.stdout is not None
            and sys.stdin.isatty())


def _announce(operation: str, details: str) -> None:
    print("")
    print(f"  {operation}")
    for line in details.splitlines():
        print(f"    {line}")
    print("  Check the details on your Ledger and confirm there to sign,")
    print("  or reject on the device to refuse.")
    print("", flush=True)


def _sign_typed_data(typed_data: dict, device_path: str, expected_address: str) -> str:
    """Sign an EIP-712 payment authorisation on the device.

    Blocks until the person presses a button. The signing service calls this on
    the thread handling the agent's request, which is the same place the desktop
    blocks on its dialog.
    """
    from ..wallet.ledger import LedgerDevice, LedgerError

    message = typed_data.get("message", {}) or {}
    value = message.get("value", 0)
    amount = value / 1_000_000 if isinstance(value, (int, float)) else 0
    details = (f"To:     {message.get('to', 'unknown')}\n"
               f"Amount: {amount:.6f} USDG\n"
               f"Device: {device_path}")

    if _attended():
        _announce("Payment authorisation - confirm on your Ledger", details)
    else:
        logger.warning("A Ledger signature is needed but nobody is at the "
                       "terminal to press the device's buttons: %s",
                       details.replace("\n", "; "))

    device = LedgerDevice.discover()
    if device is None:
        raise LedgerError(
            "No Ledger found. Plug it in, unlock it, and open the Ethereum app.")
    if not device.verify_address(device_path, expected_address):
        # Refuse rather than sign with whatever the device offered. A mismatch
        # means this is not the wallet the policy was checked against.
        raise LedgerError(
            f"The Ledger at {device_path} holds a different address than the "
            f"one this payment is for ({expected_address}). Refusing to sign.")
    return device.sign_typed_data(device_path, typed_data)


def _sign_transaction(tx_dict: dict, device_path: str, expected_address: str,
                      description: str = "Transaction") -> str:
    """Sign a raw transaction on the device (trades, transfers)."""
    from ..wallet.ledger import LedgerDevice, LedgerError

    details = (f"To:     {tx_dict.get('to', 'unknown')}\n"
               f"Chain:  {tx_dict.get('chainId', '?')}\n"
               f"Device: {device_path}")

    if _attended():
        _announce(f"{description} - confirm on your Ledger", details)
    else:
        logger.warning("A Ledger signature is needed but nobody is at the "
                       "terminal: %s", details.replace("\n", "; "))

    device = LedgerDevice.discover()
    if device is None:
        raise LedgerError(
            "No Ledger found. Plug it in, unlock it, and open the Ethereum app.")
    if not device.verify_address(device_path, expected_address):
        raise LedgerError(
            f"The Ledger at {device_path} holds a different address than the "
            f"one this transaction is for ({expected_address}). Refusing to sign.")
    return device.sign_transaction(device_path, tx_dict)


def register_hardware_handlers(core: "Vault") -> None:
    """Give the engine somewhere to send a hardware-signing request."""
    core.set_hardware_sign_handler(_sign_typed_data)
    core.set_hardware_tx_sign_handler(_sign_transaction)
