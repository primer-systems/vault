"""
Ledger Hardware Wallet Support.

Provides secure signing via Ledger devices. Private keys never leave the device.

Derivation paths supported:
- Ledger Live:     m/44'/60'/x'/0/0   (account index at position 3)
- BIP44 Standard:  m/44'/60'/0'/0/x   (address index at position 5)
- Legacy MEW:      m/44'/60'/0'/x     (older MyEtherWallet style)
- Custom:          user-specified path
"""

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ledgereth keeps ONE process-global dongle (ledgereth.comms.DONGLE_CACHE) and
# the USB HID handle under it is not safe to share. Two overlapping operations
# interleave their APDUs, the device answers with status 0x6F00, and ledgereth
# reports that - misleadingly - as "Unable to find Ledger device". Every device
# call is therefore serialized through this lock.
_DEVICE_LOCK = threading.RLock()


class LedgerPathType(Enum):
    """Standard derivation path types for Ledger."""
    LEDGER_LIVE = "ledger_live"      # m/44'/60'/x'/0/0
    BIP44 = "bip44"                  # m/44'/60'/0'/0/x
    LEGACY_MEW = "legacy_mew"        # m/44'/60'/0'/x
    CUSTOM = "custom"


# Path templates for each type
PATH_TEMPLATES = {
    LedgerPathType.LEDGER_LIVE: "m/44'/60'/{account}'/0/0",
    LedgerPathType.BIP44: "m/44'/60'/0'/0/{index}",
    LedgerPathType.LEGACY_MEW: "m/44'/60'/0'/{index}",
}


def to_device_path(path: str) -> str:
    """
    Normalize a derivation path for the ledgereth library.

    Vault stores and displays paths in the conventional `m/44'/60'/0'/0/0` form,
    but ledgereth's BIP-32 parser rejects the leading `m/` and expects
    `44'/60'/0'/0/0`. Every device call must go through this.

    Args:
        path: Derivation path, with or without the `m/` prefix

    Returns:
        Path without the `m/` prefix

    Raises:
        ValueError: If the path is empty or is a bare `m`
    """
    if not path or not path.strip():
        raise ValueError("Derivation path is empty")

    cleaned = path.strip()
    if cleaned in ("m", "m/"):
        raise ValueError(f"Incomplete derivation path: {path!r}")
    if cleaned.startswith("m/"):
        cleaned = cleaned[2:]

    return cleaned


def get_derivation_path(path_type: LedgerPathType, index: int, custom_path: str = None) -> str:
    """
    Get the full derivation path for a given type and index.

    Args:
        path_type: The path type (Ledger Live, BIP44, etc.)
        index: Address/account index
        custom_path: Required if path_type is CUSTOM

    Returns:
        Full BIP-44 derivation path string
    """
    if path_type == LedgerPathType.CUSTOM:
        if not custom_path:
            raise ValueError("custom_path required for CUSTOM path type")
        # Replace placeholder with index
        if "{}" in custom_path:
            return custom_path.format(index)
        elif "{index}" in custom_path:
            return custom_path.replace("{index}", str(index))
        elif "{account}" in custom_path:
            return custom_path.replace("{account}", str(index))
        else:
            # Assume path is complete as-is
            return custom_path

    template = PATH_TEMPLATES[path_type]
    if "{account}" in template:
        return template.replace("{account}", str(index))
    else:
        return template.replace("{index}", str(index))


@dataclass
class LedgerAddress:
    """An address derived from a Ledger device."""
    path: str               # Full derivation path
    address: str            # 0x... address
    path_type: str          # LedgerPathType value (for display)
    index: int              # Index used in derivation


class LedgerError(Exception):
    """Base exception for Ledger operations."""
    pass


class LedgerNotFoundError(LedgerError):
    """No Ledger device found."""
    pass


class LedgerLockedError(LedgerError):
    """Ledger is locked (needs PIN)."""
    pass


class LedgerAppNotOpenError(LedgerError):
    """Ethereum app not open on Ledger."""
    pass


class LedgerRejectedError(LedgerError):
    """User rejected the operation on the device."""
    pass


class LedgerDisconnectedError(LedgerError):
    """Device was disconnected during operation."""
    pass


class LedgerBlindSigningDisabledError(LedgerError):
    """Blind signing is disabled in Ledger Ethereum app settings."""
    pass


class LedgerBusyError(LedgerError):
    """The device is connected but cannot be talked to (usually claimed by another app)."""
    pass


# ledgerblue raises bare BaseException for low-level USB failures (see
# ledgerblue/comm.py "Error while writing"), which `except Exception` does not
# catch. Every boundary that talks to the device catches BaseException instead
# and re-raises only the ones that must never be swallowed.
FATAL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def reset_connection() -> None:
    """
    Drop ledgereth's cached dongle so the next call opens a fresh one.

    ledgereth caches the dongle in a module global and never invalidates it, so
    once a connection breaks every later call reuses the dead handle and fails
    instantly - the device appears permanently gone until the app restarts.
    Clearing the cache is the only way back.
    """
    import ledgereth.comms as comms

    with _DEVICE_LOCK:
        dongle = comms.DONGLE_CACHE
        comms.DONGLE_CACHE = None
        comms.DONGLE_CONFIG_CACHE = None

        if dongle is not None:
            try:
                dongle.close()
            except BaseException:  # noqa: BLE001 - closing a dead handle often throws
                logger.debug("Ignoring error while closing stale Ledger dongle", exc_info=True)


def _map_device_error(exc: BaseException, operation: str) -> LedgerError:
    """
    Translate a ledgereth/USB exception into a Vault LedgerError subclass.

    ledgereth raises typed exceptions for the common cases; anything else
    (USB layer, ledgerblue CommException) only carries a message, so we fall
    back to matching on the text.

    Args:
        exc: The exception raised by ledgereth
        operation: Short description used in the fallback message

    Returns:
        The appropriate LedgerError subclass (not raised)
    """
    from ledgereth.exceptions import (
        LedgerAppNotOpened,
        LedgerCancel,
        LedgerLocked,
        LedgerNotFound,
    )

    if isinstance(exc, LedgerCancel):
        return LedgerRejectedError("Rejected on device.")
    if isinstance(exc, LedgerNotFound):
        # Do not trust this one's wording. ledgereth maps status 0x6F00 -
        # ledgerblue's catch-all "unknown error" - onto LedgerNotFound, whose
        # message claims the device is missing. In practice it usually means a
        # confused exchange with a device that is plugged in and working.
        return LedgerDisconnectedError(
            "Lost contact with the Ledger.\n\n"
            "Check it is unlocked with the Ethereum app open, then try again."
        )
    if isinstance(exc, LedgerLocked):
        return LedgerLockedError("Ledger is locked. Please unlock with your PIN.")
    if isinstance(exc, LedgerAppNotOpened):
        return LedgerAppNotOpenError("Please open the Ethereum app on your Ledger.")

    error_str = str(exc).lower()

    # Raw USB failure: the handle is bad or someone else owns the device.
    # On Windows this is nearly always Ledger Live holding it open.
    if "error while writing" in error_str or "error while communicating" in error_str:
        return LedgerBusyError(
            "Cannot communicate with the Ledger.\n\n"
            "Close Ledger Live (and any other wallet app using the device), "
            "then make sure the Ledger is unlocked with the Ethereum app open "
            "and try again."
        )
    if "denied" in error_str or "reject" in error_str or "cancel" in error_str:
        return LedgerRejectedError("Rejected on device.")
    if "blind" in error_str or "6a80" in error_str:
        return LedgerBlindSigningDisabledError(
            "Blind signing is disabled. Enable it in the Ledger Ethereum app "
            "settings (Settings > Blind signing)."
        )
    if "disconnected" in error_str or "not found" in error_str or "no device" in error_str:
        return LedgerDisconnectedError("Ledger disconnected. Please reconnect and try again.")

    return LedgerError(f"{operation} failed: {exc}")


class LedgerDevice:
    """
    Interface to a connected Ledger hardware wallet.

    Uses ledgereth library for USB HID communication with the device.

    Usage:
        device = LedgerDevice.discover()
        if device:
            addresses = device.get_addresses(LedgerPathType.LEDGER_LIVE, count=5)
            signature = device.sign_transaction(path, tx_dict)
    """

    def __init__(self):
        """Initialize device connection."""
        self._check_ledgereth_available()

    def _check_ledgereth_available(self):
        """Check if ledgereth is installed and importable."""
        try:
            import ledgereth
            self._ledgereth = ledgereth
        except ImportError as e:
            raise LedgerError(
                "ledgereth library not installed. "
                "Install with: pip install ledgereth"
            ) from e

    @classmethod
    def discover(cls) -> Optional["LedgerDevice"]:
        """
        Discover and connect to a Ledger device.

        Returns:
            LedgerDevice instance if found, None otherwise

        Raises:
            LedgerLockedError: If device is locked
            LedgerAppNotOpenError: If Ethereum app is not open
        """
        try:
            device = cls()
            # Try to get an address to verify connection and app status
            device._verify_connection()
            return device
        except LedgerNotFoundError:
            return None

    def _verify_connection(self):
        """Verify device is connected and Ethereum app is open.

        Every device operation starts here, so this is where a dead cached
        dongle has to be caught. ledgereth holds one process-global dongle and
        never invalidates it: once a handle is closed, every later call fails
        instantly with something like "not open", and the device looks
        permanently gone until Vault restarts.

        That state is reached routinely. A single trade needs two signatures -
        the ERC-20 approval, then the swap - and the handle can be left dead
        between them, so the second confirmation fails on a device the user is
        holding and has already approved with.

        Retrying is safe here specifically because this only reads an address.
        The signing methods deliberately do not retry: a second attempt there
        would ask the user to confirm again, and a confirmation must never be
        requested twice for one action.
        """
        from ledgereth.accounts import get_accounts
        from ledgereth.exceptions import LedgerNotFound

        try:
            with _DEVICE_LOCK:
                try:
                    accounts = get_accounts(count=1)
                except (LedgerNotFound, *FATAL_EXCEPTIONS):
                    raise
                except BaseException as e:
                    logger.warning(
                        "Ledger connection check failed (%s); dropping the cached "
                        "dongle and retrying once", e)
                    reset_connection()
                    accounts = get_accounts(count=1)
            if not accounts:
                raise LedgerNotFoundError("No accounts returned from device")
        except LedgerNotFound as e:
            # Distinct from a mid-operation disconnect: nothing was ever found,
            # so discover() can translate this into "no device" rather than an error.
            raise LedgerNotFoundError("No Ledger device found. Please connect your Ledger.") from e
        except LedgerError:
            # Already one of ours (including the empty-account-list case above) -
            # it carries a user-facing message, so don't re-wrap it.
            raise
        except FATAL_EXCEPTIONS:
            raise
        except BaseException as e:
            if "busy" in str(e).lower():
                raise LedgerBusyError(
                    "The Ledger is in use by another application.\n\n"
                    "Close Ledger Live and try again."
                ) from e
            raise _map_device_error(e, "Ledger connection") from e

    def get_addresses(
        self,
        path_type: LedgerPathType,
        start_index: int = 0,
        count: int = 5,
        custom_path: str = None
    ) -> list[LedgerAddress]:
        """
        Get multiple addresses from the device.

        Args:
            path_type: Derivation path type
            start_index: Starting index (default 0)
            count: Number of addresses to fetch (default 5)
            custom_path: Custom path template (required if path_type is CUSTOM)

        Returns:
            List of LedgerAddress objects
        """
        addresses = []
        with _DEVICE_LOCK:
            for i in range(start_index, start_index + count):
                path = get_derivation_path(path_type, i, custom_path)
                try:
                    address = self._derive_once(path, retry=True)
                except FATAL_EXCEPTIONS:
                    raise
                except LedgerError:
                    # A mid-sweep failure that survives a reconnect is the link
                    # going down, not one bad index - every remaining index would
                    # fail identically. Stop, rather than filling the list with
                    # ten copies of the same error.
                    logger.warning(
                        "Stopping address sweep at %s after %d address(es)", path, len(addresses))
                    if not addresses:
                        raise
                    break

                addresses.append(LedgerAddress(
                    path=path,
                    address=address,
                    path_type=path_type.value,
                    index=i
                ))

        return addresses

    def _derive_once(self, path: str, retry: bool = False) -> str:
        """
        Derive one address, optionally reconnecting once on failure.

        The retry exists because ledgereth's cached dongle can be left dead by a
        single bad exchange; without dropping and reopening it, every subsequent
        call fails instantly no matter what the user does.
        """
        from ledgereth.accounts import get_account_by_path

        device_path = to_device_path(path)

        with _DEVICE_LOCK:
            try:
                return get_account_by_path(device_path).address
            except FATAL_EXCEPTIONS:
                raise
            except BaseException as e:
                if not retry:
                    raise _map_device_error(e, f"Address derivation at {path}") from e

                logger.warning("Deriving %s failed (%s); reconnecting and retrying", path, e)
                reset_connection()

                try:
                    return get_account_by_path(device_path).address
                except FATAL_EXCEPTIONS:
                    raise
                except BaseException as retry_error:
                    raise _map_device_error(
                        retry_error, f"Address derivation at {path}") from retry_error

    def get_address(
        self,
        path_type: LedgerPathType,
        index: int,
        custom_path: str = None
    ) -> LedgerAddress:
        """
        Get a single address from the device.

        Args:
            path_type: Derivation path type
            index: Address index
            custom_path: Custom path template (required if path_type is CUSTOM)

        Returns:
            LedgerAddress object
        """
        path = get_derivation_path(path_type, index, custom_path)
        return LedgerAddress(
            path=path,
            address=self._derive_once(path, retry=True),
            path_type=path_type.value,
            index=index
        )

    def verify_address(self, path: str, expected_address: str) -> bool:
        """
        Verify that the connected device can derive the expected address.

        Used before signing to ensure the correct Ledger is connected.

        Args:
            path: Full derivation path
            expected_address: Expected 0x address

        Returns:
            True if addresses match, False otherwise
        """
        try:
            return self._derive_once(path, retry=True).lower() == expected_address.lower()
        except FATAL_EXCEPTIONS:
            raise
        except LedgerError as e:
            logger.warning(f"Failed to verify address at {path}: {e}")
            return False

    def derive_address(self, path: str) -> str:
        """
        Derive the address at a path, raising on failure.

        Unlike verify_address(), this surfaces *why* derivation failed (locked
        device, wrong app) instead of collapsing everything to False, so callers
        can show the user an actionable message before signing.

        Args:
            path: Full derivation path

        Returns:
            The 0x address held at that path on the connected device
        """
        return self._derive_once(path, retry=True)

    def sign_transaction(self, path: str, tx_dict: dict) -> str:
        """
        Sign a transaction on the Ledger device.

        The user reviews and confirms on the device screen. Contract calls
        (swaps, approvals) show as raw calldata unless the Ethereum app has a
        plugin for the contract, so blind signing usually must be enabled.

        Args:
            path: Derivation path for the signing key
            tx_dict: Transaction dictionary as produced by web3's
                `build_transaction()`, with keys:
                - to: recipient address (required)
                - value: amount in wei
                - gas: gas limit (required)
                - nonce: transaction nonce (required)
                - chainId: chain ID (required)
                - data: contract calldata (optional)
                - maxFeePerGas + maxPriorityFeePerGas (EIP-1559), or gasPrice (legacy)

        Returns:
            The signed transaction, RLP-encoded as a 0x-prefixed hex string,
            ready to hand to `eth_sendRawTransaction`.

        Raises:
            LedgerRejectedError: If the user rejected on the device
            LedgerDisconnectedError: If the device was disconnected
            LedgerBlindSigningDisabledError: If blind signing is required but off
            ValueError: If the transaction is missing required fields
        """
        from ledgereth.transactions import create_transaction

        # ledgereth requires these outright - it cannot fill them in from a node.
        missing = [k for k in ("to", "gas", "nonce", "chainId") if tx_dict.get(k) is None]
        if missing:
            raise ValueError(
                f"Transaction is missing required field(s) for Ledger signing: "
                f"{', '.join(missing)}"
            )

        # EIP-1559 and legacy pricing are mutually exclusive in ledgereth.
        max_fee = tx_dict.get("maxFeePerGas")
        max_priority = tx_dict.get("maxPriorityFeePerGas")
        gas_price = tx_dict.get("gasPrice")
        if max_fee is not None or max_priority is not None:
            gas_price = None
        elif gas_price is None:
            raise ValueError(
                "Transaction is missing gas pricing: supply either "
                "maxFeePerGas/maxPriorityFeePerGas or gasPrice"
            )

        # web3's build_transaction() returns calldata as a 0x-prefixed string,
        # which is what every contract call arrives here as - an approval, a
        # swap, an unwrap. ledgereth wants bytes and will not convert a string
        # itself, so it has to be done here.
        data = tx_dict.get("data") or b""
        if isinstance(data, str):
            try:
                data = bytes.fromhex(data[2:] if data.startswith(("0x", "0X")) else data)
            except ValueError as e:
                raise ValueError(f"Transaction data is not valid hex: {data[:20]}...") from e
        elif isinstance(data, (bytes, bytearray)):
            data = bytes(data)

        with _DEVICE_LOCK:
            try:
                signed_tx = create_transaction(
                    destination=tx_dict["to"],
                    amount=int(tx_dict.get("value", 0)),
                    gas=int(tx_dict["gas"]),
                    nonce=int(tx_dict["nonce"]),
                    data=data,
                    gas_price=int(gas_price) if gas_price is not None else None,
                    max_fee_per_gas=int(max_fee) if max_fee is not None else None,
                    max_priority_fee_per_gas=(
                        int(max_priority) if max_priority is not None else None),
                    chain_id=int(tx_dict["chainId"]),
                    sender_path=to_device_path(path),
                )
                # `raw_transaction` is already a 0x-prefixed hex string.
                return signed_tx.raw_transaction
            except FATAL_EXCEPTIONS:
                raise
            except BaseException as e:
                raise _map_device_error(e, "Transaction signing") from e

    def sign_typed_data(self, path: str, typed_data: dict) -> str:
        """
        Sign EIP-712 typed data on the Ledger device.

        Used for x402 payment authorization (TransferWithAuthorization).

        ledgereth signs the *hashes*, not the structured document, so the
        domain separator and message hash are computed here with eth_account
        and handed to the device. This is the device's "blind" EIP-712 path,
        which requires blind signing enabled in the Ethereum app.

        Args:
            path: Derivation path for the signing key
            typed_data: EIP-712 typed data dictionary with:
                - types: type definitions
                - primaryType: main type name
                - domain: domain separator data
                - message: the data to sign

        Returns:
            65-byte signature (r + s + v) as a 0x-prefixed hex string

        Raises:
            LedgerRejectedError: If the user rejected on the device
            LedgerBlindSigningDisabledError: If blind signing is disabled
        """
        from eth_account.messages import encode_typed_data
        from ledgereth.messages import sign_typed_data_draft

        try:
            signable = encode_typed_data(full_message=typed_data)
        except FATAL_EXCEPTIONS:
            raise
        except BaseException as e:
            raise LedgerError(f"Invalid EIP-712 typed data: {e}") from e

        # SignableMessage carries version b'\x01', header=domain hash, body=message hash.
        domain_hash = signable.header
        message_hash = signable.body

        with _DEVICE_LOCK:
            try:
                signed = sign_typed_data_draft(
                    domain_hash=domain_hash,
                    message_hash=message_hash,
                    sender_path=to_device_path(path),
                )
                return signed.signature
            except FATAL_EXCEPTIONS:
                raise
            except BaseException as e:
                raise _map_device_error(e, "EIP-712 signing") from e

    def sign_message(self, path: str, message: bytes) -> str:
        """
        Sign a personal message (EIP-191) on the Ledger device.

        Args:
            path: Derivation path for the signing key
            message: Message bytes to sign

        Returns:
            65-byte signature (r + s + v) as a 0x-prefixed hex string

        Raises:
            LedgerRejectedError: If the user rejected on the device
        """
        from ledgereth.messages import sign_message

        with _DEVICE_LOCK:
            try:
                signed = sign_message(message, sender_path=to_device_path(path))
                return signed.signature
            except FATAL_EXCEPTIONS:
                raise
            except BaseException as e:
                raise _map_device_error(e, "Message signing") from e


def get_path_type_display_name(path_type: str) -> str:
    """Get human-readable name for a path type."""
    names = {
        "ledger_live": "Ledger Live",
        "bip44": "BIP44 Standard",
        "legacy_mew": "Legacy (MEW)",
        "custom": "Custom",
    }
    return names.get(path_type, path_type)


def get_path_type_description(path_type: LedgerPathType) -> str:
    """Get description with path template for a path type."""
    descriptions = {
        LedgerPathType.LEDGER_LIVE: "m/44'/60'/x'/0/0 (Ledger Live default)",
        LedgerPathType.BIP44: "m/44'/60'/0'/0/x (MetaMask, most wallets)",
        LedgerPathType.LEGACY_MEW: "m/44'/60'/0'/x (MyEtherWallet legacy)",
        LedgerPathType.CUSTOM: "Custom derivation path",
    }
    return descriptions.get(path_type, "")
