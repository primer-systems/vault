"""
Ledger Hardware Wallet Tests

Covers the device layer (paths, discovery, derivation, signing) with ledgereth
mocked out, plus the integration points where Ledger addresses change behaviour:
x402 payment signing and trade execution.

The device itself is never touched. What these tests pin down is that we call
ledgereth with exactly the arguments a real device needs - in particular that
derivation paths reach the library without the `m/` prefix, and that the EIP-712
hashes we hand the device produce the same signature a software wallet would.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.wallet.ledger import (  # noqa: E402
    LedgerAddress,
    LedgerAppNotOpenError,
    LedgerBlindSigningDisabledError,
    LedgerDevice,
    LedgerDisconnectedError,
    LedgerError,
    LedgerLockedError,
    LedgerPathType,
    LedgerRejectedError,
    get_derivation_path,
    to_device_path,
)

LEDGER_ADDR = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_ADDR = "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
DEFAULT_PATH = "m/44'/60'/0'/0/0"


def _discovered_device():
    """Return a LedgerDevice with discovery mocked as successful."""
    with patch("ledgereth.accounts.get_accounts",
               return_value=[MagicMock(address=LEDGER_ADDR)]):
        return LedgerDevice.discover()


# =============================================================================
# Derivation Paths
# =============================================================================

class TestDerivationPaths:
    """Test derivation path generation."""

    def test_ledger_live_path(self):
        """Ledger Live uses m/44'/60'/x'/0/0 format."""
        assert get_derivation_path(LedgerPathType.LEDGER_LIVE, 0) == "m/44'/60'/0'/0/0"
        assert get_derivation_path(LedgerPathType.LEDGER_LIVE, 5) == "m/44'/60'/5'/0/0"

    def test_bip44_path(self):
        """BIP44 standard uses m/44'/60'/0'/0/x format."""
        assert get_derivation_path(LedgerPathType.BIP44, 0) == "m/44'/60'/0'/0/0"
        assert get_derivation_path(LedgerPathType.BIP44, 5) == "m/44'/60'/0'/0/5"

    def test_legacy_mew_path(self):
        """Legacy MEW uses m/44'/60'/0'/x format."""
        assert get_derivation_path(LedgerPathType.LEGACY_MEW, 0) == "m/44'/60'/0'/0"
        assert get_derivation_path(LedgerPathType.LEGACY_MEW, 5) == "m/44'/60'/0'/5"

    def test_custom_path(self):
        """Custom path uses the user-provided template."""
        assert get_derivation_path(
            LedgerPathType.CUSTOM, 0, custom_path="m/44'/60'/1'/{}") == "m/44'/60'/1'/0"
        assert get_derivation_path(
            LedgerPathType.CUSTOM, 3, custom_path="m/44'/60'/1'/{}") == "m/44'/60'/1'/3"

    def test_custom_path_requires_template(self):
        """Custom path without a template raises."""
        with pytest.raises(ValueError, match="custom_path"):
            get_derivation_path(LedgerPathType.CUSTOM, 0)


class TestDevicePathNormalization:
    """The `m/` prefix must be stripped before paths reach ledgereth."""

    def test_strips_m_prefix(self):
        assert to_device_path("m/44'/60'/0'/0/0") == "44'/60'/0'/0/0"

    def test_leaves_bare_path_alone(self):
        assert to_device_path("44'/60'/0'/0/0") == "44'/60'/0'/0/0"

    @pytest.mark.parametrize("path_type", [
        LedgerPathType.LEDGER_LIVE, LedgerPathType.BIP44, LedgerPathType.LEGACY_MEW,
    ])
    def test_every_path_type_is_valid_bip32_for_ledgereth(self, path_type):
        """ledgereth's own parser must accept what we generate."""
        from ledgereth.utils import is_bip32_path

        device_path = to_device_path(get_derivation_path(path_type, 2))
        assert is_bip32_path(device_path)

    @pytest.mark.parametrize("bad", ["", "   ", "m", "m/"])
    def test_rejects_empty_or_incomplete(self, bad):
        with pytest.raises(ValueError):
            to_device_path(bad)


# =============================================================================
# Discovery
# =============================================================================

class TestLedgerDiscovery:
    """Test Ledger device discovery with mocked USB interactions."""

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_success(self, mock_get_accounts):
        """Successful discovery returns a LedgerDevice."""
        mock_get_accounts.return_value = [MagicMock(address=LEDGER_ADDR)]
        device = LedgerDevice.discover()
        assert isinstance(device, LedgerDevice)

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_no_device(self, mock_get_accounts):
        """No device connected returns None rather than raising."""
        from ledgereth.exceptions import LedgerNotFound

        mock_get_accounts.side_effect = LedgerNotFound("No Ledger device found")
        assert LedgerDevice.discover() is None

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_device_locked(self, mock_get_accounts):
        """A locked device is reported distinctly so the user can act on it."""
        from ledgereth.exceptions import LedgerLocked

        mock_get_accounts.side_effect = LedgerLocked("Device is locked")
        with pytest.raises(LedgerLockedError):
            LedgerDevice.discover()

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_eth_app_not_open(self, mock_get_accounts):
        """Ethereum app closed maps to LedgerAppNotOpenError."""
        from ledgereth.exceptions import LedgerAppNotOpened

        mock_get_accounts.side_effect = LedgerAppNotOpened("app is not open")
        with pytest.raises(LedgerAppNotOpenError):
            LedgerDevice.discover()

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_empty_account_list(self, mock_get_accounts):
        """A device that returns no accounts is treated as no device."""
        mock_get_accounts.return_value = []
        assert LedgerDevice.discover() is None


# =============================================================================
# Low-level USB failures
# =============================================================================

class TestBareBaseExceptionHandling:
    """
    ledgerblue raises bare BaseException for USB write failures
    (ledgerblue/comm.py: `raise BaseException("Error while writing")`).

    `except Exception` does not catch that, so before this was handled the
    error escaped every boundary: it killed the worker thread without emitting
    a result, hung the dialog on "Detecting...", and crashed the app.
    """

    WRITE_FAILURE = BaseException("Error while writing")

    @patch("ledgereth.accounts.get_accounts")
    def test_discover_converts_write_failure(self, mock_get_accounts):
        from primer_vault.wallet.ledger import LedgerBusyError

        mock_get_accounts.side_effect = self.WRITE_FAILURE
        with pytest.raises(LedgerBusyError):
            LedgerDevice.discover()

    @patch("ledgereth.accounts.get_accounts")
    def test_write_failure_message_is_actionable(self, mock_get_accounts):
        """On Windows this is nearly always Ledger Live holding the device."""
        mock_get_accounts.side_effect = self.WRITE_FAILURE
        with pytest.raises(LedgerError) as exc_info:
            LedgerDevice.discover()

        assert "Ledger Live" in str(exc_info.value)

    @patch("ledgereth.transactions.create_transaction")
    def test_signing_converts_write_failure(self, mock_create):
        mock_create.side_effect = BaseException("Error while writing")
        device = _discovered_device()

        with pytest.raises(LedgerError):
            device.sign_transaction(DEFAULT_PATH, EIP1559_TX)

    @patch("ledgereth.messages.sign_typed_data_draft")
    def test_typed_data_converts_write_failure(self, mock_sign):
        mock_sign.side_effect = BaseException("Error while writing")
        device = _discovered_device()

        with pytest.raises(LedgerError):
            device.sign_typed_data(DEFAULT_PATH, TestTypedDataSigning.TYPED_DATA)

    @patch("ledgereth.accounts.get_account_by_path")
    def test_address_listing_survives_write_failure(self, mock_by_path):
        """A bare BaseException mid-sweep must not escape as a crash."""
        def side_effect(path):
            if path.endswith(("/1", "/2")):
                raise BaseException("Error while writing")
            return MagicMock(address=LEDGER_ADDR)

        mock_by_path.side_effect = side_effect
        device = _discovered_device()

        addresses = device.get_addresses(LedgerPathType.BIP44, start_index=0, count=3)
        assert [a.index for a in addresses] == [0]

    @pytest.mark.parametrize("fatal", [KeyboardInterrupt, SystemExit])
    @patch("ledgereth.accounts.get_accounts")
    def test_fatal_exceptions_still_propagate(self, mock_get_accounts, fatal):
        """Ctrl-C and interpreter shutdown must never be swallowed."""
        mock_get_accounts.side_effect = fatal()
        with pytest.raises(fatal):
            LedgerDevice.discover()


# =============================================================================
# Connection handling
# =============================================================================

class TestConnectionReset:
    """
    ledgereth caches one dongle in a module global and never invalidates it, so
    a broken connection stays broken for the life of the process. Dropping the
    cache is the only way to recover without restarting the app.
    """

    def test_reset_clears_the_cached_dongle(self):
        import ledgereth.comms as comms
        from primer_vault.wallet.ledger import reset_connection

        dongle = MagicMock()
        comms.DONGLE_CACHE = dongle
        comms.DONGLE_CONFIG_CACHE = b"config"

        reset_connection()

        assert comms.DONGLE_CACHE is None
        assert comms.DONGLE_CONFIG_CACHE is None
        dongle.close.assert_called_once()

    def test_reset_survives_a_dongle_that_wont_close(self):
        """Closing a dead handle usually throws; that must not mask the reset."""
        import ledgereth.comms as comms
        from primer_vault.wallet.ledger import reset_connection

        dongle = MagicMock()
        dongle.close.side_effect = BaseException("already gone")
        comms.DONGLE_CACHE = dongle

        reset_connection()
        assert comms.DONGLE_CACHE is None

    def test_reset_with_no_connection_is_harmless(self):
        import ledgereth.comms as comms
        from primer_vault.wallet.ledger import reset_connection

        comms.DONGLE_CACHE = None
        reset_connection()
        assert comms.DONGLE_CACHE is None

    @patch("ledgereth.accounts.get_account_by_path")
    def test_retry_reconnects_before_trying_again(self, mock_by_path):
        """The retry is pointless unless the dead dongle is dropped first."""
        import ledgereth.comms as comms
        from primer_vault.wallet.ledger import LedgerDevice

        comms.DONGLE_CACHE = MagicMock()
        seen_cache = []

        def side_effect(path):
            seen_cache.append(comms.DONGLE_CACHE)
            if len(seen_cache) == 1:
                raise Exception("stale handle")
            return MagicMock(address=LEDGER_ADDR)

        mock_by_path.side_effect = side_effect
        with patch("ledgereth.accounts.get_accounts",
                   return_value=[MagicMock(address=LEDGER_ADDR)]):
            device = LedgerDevice.discover()

        assert device.derive_address(DEFAULT_PATH) == LEDGER_ADDR
        # First attempt saw the stale dongle; the retry saw a cleared cache.
        assert seen_cache[1] is None


class TestDeviceSerialization:
    """
    The device is a single serial resource. Overlapping APDU exchanges get
    interleaved and the device answers 0x6F00, which ledgereth reports as
    "Unable to find Ledger device" even though it is plugged in and fine.
    """

    @patch("ledgereth.accounts.get_account_by_path")
    def test_concurrent_derivations_do_not_overlap(self, mock_by_path):
        import threading
        import time

        from primer_vault.wallet.ledger import LedgerDevice

        in_flight = []
        overlaps = []

        def side_effect(path):
            in_flight.append(path)
            if len(in_flight) > 1:
                overlaps.append(tuple(in_flight))
            time.sleep(0.01)          # widen the window for a race
            in_flight.pop()
            return MagicMock(address=LEDGER_ADDR)

        mock_by_path.side_effect = side_effect
        with patch("ledgereth.accounts.get_accounts",
                   return_value=[MagicMock(address=LEDGER_ADDR)]):
            device = LedgerDevice.discover()

        threads = [
            threading.Thread(target=device.get_addresses,
                             args=(LedgerPathType.BIP44,), kwargs={"count": 3})
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert overlaps == [], f"device calls overlapped: {overlaps}"


# =============================================================================
# Address retrieval
# =============================================================================

class TestAddressRetrieval:
    """Test address retrieval from the device."""

    @patch("ledgereth.accounts.get_account_by_path")
    def test_get_addresses_ledger_live(self, mock_by_path):
        """Returns one LedgerAddress per index, in order."""
        mock_by_path.side_effect = lambda p: MagicMock(address=LEDGER_ADDR)
        device = _discovered_device()

        addresses = device.get_addresses(LedgerPathType.LEDGER_LIVE, start_index=0, count=2)

        assert len(addresses) == 2
        assert all(isinstance(a, LedgerAddress) for a in addresses)
        assert [a.index for a in addresses] == [0, 1]
        assert addresses[0].path == "m/44'/60'/0'/0/0"

    @patch("ledgereth.accounts.get_account_by_path")
    def test_device_receives_path_without_m_prefix(self, mock_by_path):
        """Regression: ledgereth rejects `m/`-prefixed paths outright."""
        mock_by_path.side_effect = lambda p: MagicMock(address=LEDGER_ADDR)
        device = _discovered_device()

        device.get_addresses(LedgerPathType.BIP44, start_index=0, count=3)

        sent = [call.args[0] for call in mock_by_path.call_args_list]
        assert sent == ["44'/60'/0'/0/0", "44'/60'/0'/0/1", "44'/60'/0'/0/2"]
        assert not any(p.startswith("m/") for p in sent)

    @patch("ledgereth.accounts.get_account_by_path")
    def test_transient_failure_is_retried_and_the_sweep_continues(self, mock_by_path):
        """A single hiccup reconnects and carries on - no gap in the list."""
        failed_once = []

        def side_effect(path):
            if path.endswith("/1") and not failed_once:
                failed_once.append(path)
                raise Exception("transient glitch")
            return MagicMock(address=LEDGER_ADDR)

        mock_by_path.side_effect = side_effect
        device = _discovered_device()

        addresses = device.get_addresses(LedgerPathType.BIP44, start_index=0, count=3)
        assert [a.index for a in addresses] == [0, 1, 2]

    @patch("ledgereth.accounts.get_account_by_path")
    def test_persistent_failure_stops_the_sweep(self, mock_by_path):
        """
        A failure that survives a reconnect means the link is down, so every
        remaining index would fail identically. Return what we have rather than
        filling the list with ten copies of the same error.
        """
        def side_effect(path):
            if path.endswith(("/1", "/2")):
                raise Exception("derivation blew up")
            return MagicMock(address=LEDGER_ADDR)

        mock_by_path.side_effect = side_effect
        device = _discovered_device()

        addresses = device.get_addresses(LedgerPathType.BIP44, start_index=0, count=3)
        assert [a.index for a in addresses] == [0]

    @patch("ledgereth.accounts.get_account_by_path")
    def test_failure_on_the_very_first_index_raises(self, mock_by_path):
        """With nothing to show, the caller needs the reason, not an empty list."""
        mock_by_path.side_effect = Exception("derivation blew up")
        device = _discovered_device()

        with pytest.raises(LedgerError):
            device.get_addresses(LedgerPathType.BIP44, start_index=0, count=3)


class TestAddressVerification:
    """Test address verification before signing."""

    @patch("ledgereth.accounts.get_account_by_path")
    def test_verify_address_match(self, mock_by_path):
        mock_by_path.return_value = MagicMock(address=LEDGER_ADDR)
        device = _discovered_device()
        assert device.verify_address(DEFAULT_PATH, LEDGER_ADDR) is True

    @patch("ledgereth.accounts.get_account_by_path")
    def test_verify_address_mismatch(self, mock_by_path):
        mock_by_path.return_value = MagicMock(address=LEDGER_ADDR)
        device = _discovered_device()
        assert device.verify_address(DEFAULT_PATH, OTHER_ADDR) is False

    @patch("ledgereth.accounts.get_account_by_path")
    def test_verify_address_is_case_insensitive(self, mock_by_path):
        mock_by_path.return_value = MagicMock(address=LEDGER_ADDR.lower())
        device = _discovered_device()
        assert device.verify_address(DEFAULT_PATH, LEDGER_ADDR.upper()) is True

    @patch("ledgereth.accounts.get_account_by_path")
    def test_derive_address_surfaces_the_reason(self, mock_by_path):
        """derive_address explains failures instead of returning False."""
        from ledgereth.exceptions import LedgerLocked

        mock_by_path.side_effect = LedgerLocked("locked")
        device = _discovered_device()

        with pytest.raises(LedgerLockedError):
            device.derive_address(DEFAULT_PATH)


# =============================================================================
# EIP-712 signing (x402 payments)
# =============================================================================

class TestTypedDataSigning:
    """Test EIP-712 signing, used for x402 payment authorization."""

    TYPED_DATA = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "content", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {
            "name": "Test", "version": "1", "chainId": 1,
            "verifyingContract": "0x" + "00" * 20,
        },
        "message": {"content": "Hello"},
    }

    @patch("ledgereth.messages.sign_typed_data_draft")
    def test_sign_typed_data_success(self, mock_sign):
        mock_sign.return_value = MagicMock(signature="0x" + "ab" * 65)
        device = _discovered_device()

        signature = device.sign_typed_data(DEFAULT_PATH, self.TYPED_DATA)
        assert signature == "0x" + "ab" * 65

    @patch("ledgereth.messages.sign_typed_data_draft")
    def test_sends_two_32_byte_hashes_and_clean_path(self, mock_sign):
        """ledgereth signs hashes, not the document - check what we hand it."""
        mock_sign.return_value = MagicMock(signature="0x" + "ab" * 65)
        device = _discovered_device()

        device.sign_typed_data(DEFAULT_PATH, self.TYPED_DATA)

        kwargs = mock_sign.call_args.kwargs
        assert len(kwargs["domain_hash"]) == 32
        assert len(kwargs["message_hash"]) == 32
        assert kwargs["sender_path"] == "44'/60'/0'/0/0"

    def test_hashes_match_software_wallet_digest(self):
        """
        The decisive check: the hashes we send the Ledger must reconstruct the
        exact digest eth_account signs locally. If this holds, a device signature
        is byte-identical to a software one and the x402 facilitator will accept it.
        """
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak

        from primer_vault.services.eip3009 import build_transfer_authorization_typed_data

        typed_data = build_transfer_authorization_typed_data(
            chain_id=8453, token_address="0x" + "11" * 20,
            token_name="Global Dollar", token_version="1",
            from_address="0x" + "22" * 20, to_address="0x" + "33" * 20,
            value=5_000_000, valid_after=1, valid_before=99_999_999_999,
            nonce=b"\x04" * 32,
        )

        account = Account.from_key("0x" + "11" * 32)
        signable = encode_typed_data(full_message=typed_data)

        # What the software wallet produces.
        software_sig = account.sign_message(signable).signature.hex()

        # What a device would produce from the hashes we send it.
        captured = {}

        def fake_draft(domain_hash, message_hash, sender_path=None, dongle=None):
            captured["digest"] = keccak(b"\x19\x01" + domain_hash + message_hash)
            return MagicMock(signature="0x00")

        device = _discovered_device()
        with patch("ledgereth.messages.sign_typed_data_draft", side_effect=fake_draft):
            device.sign_typed_data(DEFAULT_PATH, typed_data)

        device_sig = account.unsafe_sign_hash(captured["digest"]).signature.hex()
        assert device_sig == software_sig

    @patch("ledgereth.messages.sign_typed_data_draft")
    def test_sign_typed_data_rejected(self, mock_sign):
        from ledgereth.exceptions import LedgerCancel

        mock_sign.side_effect = LedgerCancel("user said no")
        device = _discovered_device()

        with pytest.raises(LedgerRejectedError):
            device.sign_typed_data(DEFAULT_PATH, self.TYPED_DATA)

    @patch("ledgereth.messages.sign_typed_data_draft")
    def test_blind_signing_disabled_is_actionable(self, mock_sign):
        mock_sign.side_effect = Exception("Blind signing must be enabled")
        device = _discovered_device()

        with pytest.raises(LedgerBlindSigningDisabledError, match="[Bb]lind signing"):
            device.sign_typed_data(DEFAULT_PATH, self.TYPED_DATA)

    def test_invalid_typed_data_is_rejected_before_the_device(self):
        device = _discovered_device()
        with pytest.raises(LedgerError, match="Invalid EIP-712"):
            device.sign_typed_data(DEFAULT_PATH, {"not": "typed data"})


# =============================================================================
# Transaction signing (trading)
# =============================================================================

EIP1559_TX = {
    "to": "0x" + "cc" * 20,
    "value": 0,
    "gas": 210000,
    "nonce": 7,
    "chainId": 8453,
    "data": "0xdeadbeef",
    "maxFeePerGas": 2_000_000_000,
    "maxPriorityFeePerGas": 1_000_000_000,
}


class TestTransactionSigning:
    """Test whole-transaction signing, used for trades."""

    @patch("ledgereth.transactions.create_transaction")
    def test_sign_transaction_returns_raw_hex(self, mock_create):
        mock_create.return_value = MagicMock(raw_transaction="0x02f8...")
        device = _discovered_device()

        raw = device.sign_transaction(DEFAULT_PATH, EIP1559_TX)
        assert raw == "0x02f8..."

    @patch("ledgereth.transactions.create_transaction")
    def test_eip1559_fields_are_mapped(self, mock_create):
        """web3 tx keys must land on ledgereth's differently-named parameters."""
        mock_create.return_value = MagicMock(raw_transaction="0x")
        device = _discovered_device()

        device.sign_transaction(DEFAULT_PATH, EIP1559_TX)

        kw = mock_create.call_args.kwargs
        assert kw["destination"] == EIP1559_TX["to"]
        assert kw["amount"] == 0
        assert kw["gas"] == 210000
        assert kw["nonce"] == 7
        assert kw["chain_id"] == 8453
        # web3 hands calldata over as a hex string; ledgereth needs bytes and
        # will not convert one itself, so the mapping has to.
        assert kw["data"] == bytes.fromhex("deadbeef")
        assert kw["max_fee_per_gas"] == 2_000_000_000
        assert kw["max_priority_fee_per_gas"] == 1_000_000_000
        assert kw["sender_path"] == "44'/60'/0'/0/0"
        # ledgereth raises if gas_price accompanies EIP-1559 fields.
        assert kw["gas_price"] is None

    @patch("ledgereth.transactions.create_transaction")
    def test_legacy_gas_price_is_passed_through(self, mock_create):
        mock_create.return_value = MagicMock(raw_transaction="0x")
        device = _discovered_device()

        legacy = {k: v for k, v in EIP1559_TX.items()
                  if k not in ("maxFeePerGas", "maxPriorityFeePerGas")}
        legacy["gasPrice"] = 3_000_000_000

        device.sign_transaction(DEFAULT_PATH, legacy)

        kw = mock_create.call_args.kwargs
        assert kw["gas_price"] == 3_000_000_000
        assert kw["max_fee_per_gas"] is None

    @patch("ledgereth.transactions.create_transaction")
    def test_eip1559_wins_over_stray_gas_price(self, mock_create):
        """web3 sometimes includes both; sending both makes ledgereth raise."""
        mock_create.return_value = MagicMock(raw_transaction="0x")
        device = _discovered_device()

        device.sign_transaction(DEFAULT_PATH, {**EIP1559_TX, "gasPrice": 5})

        kw = mock_create.call_args.kwargs
        assert kw["gas_price"] is None
        assert kw["max_fee_per_gas"] == 2_000_000_000

    @pytest.mark.parametrize("missing", ["to", "gas", "nonce", "chainId"])
    def test_missing_required_field_is_caught_locally(self, missing):
        """Fail before prompting the user rather than deep inside ledgereth."""
        device = _discovered_device()
        tx = {k: v for k, v in EIP1559_TX.items() if k != missing}

        with pytest.raises(ValueError, match=missing):
            device.sign_transaction(DEFAULT_PATH, tx)

    def test_missing_gas_pricing_is_caught_locally(self):
        device = _discovered_device()
        tx = {k: v for k, v in EIP1559_TX.items()
              if k not in ("maxFeePerGas", "maxPriorityFeePerGas")}

        with pytest.raises(ValueError, match="gas pricing"):
            device.sign_transaction(DEFAULT_PATH, tx)

    @patch("ledgereth.transactions.create_transaction")
    def test_rejection_on_device(self, mock_create):
        from ledgereth.exceptions import LedgerCancel

        mock_create.side_effect = LedgerCancel("nope")
        device = _discovered_device()

        with pytest.raises(LedgerRejectedError):
            device.sign_transaction(DEFAULT_PATH, EIP1559_TX)

    @patch("ledgereth.transactions.create_transaction")
    def test_disconnect_during_signing(self, mock_create):
        from ledgereth.exceptions import LedgerNotFound

        mock_create.side_effect = LedgerNotFound("gone")
        device = _discovered_device()

        with pytest.raises(LedgerDisconnectedError):
            device.sign_transaction(DEFAULT_PATH, EIP1559_TX)


# =============================================================================
# Wallet integration
# =============================================================================

class TestWalletIntegration:
    """Test Ledger address integration with VaultWallet."""

    @staticmethod
    def _wallet():
        from primer_vault.wallet.crypto import VaultWallet
        return VaultWallet.create("test-password")

    def test_add_hardware_address(self):
        wallet = self._wallet()
        wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH,
            path_type="ledger_live", name="My Ledger")

        entry = wallet.get_address_by_address(LEDGER_ADDR)
        assert entry.is_hardware
        assert entry.address == LEDGER_ADDR
        assert entry.device_path == DEFAULT_PATH
        assert entry.device_path_type == "ledger_live"
        assert entry.name == "My Ledger"

    def test_ledger_ids_are_distinct_from_software_ids(self):
        wallet = self._wallet()
        addr_id = wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH, path_type="ledger_live")
        assert addr_id.startswith("L")

    def test_ledger_address_has_no_private_key(self):
        """The whole point: Vault must never be able to produce these keys."""
        wallet = self._wallet()
        addr_id = wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH, path_type="ledger_live")

        with pytest.raises(ValueError, match="[Ll]edger"):
            wallet.get_private_key(addr_id)

    def test_get_hardware_addresses(self):
        wallet = self._wallet()
        wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH,
            path_type="ledger_live", name="Ledger 1")

        ledger_addrs = wallet.get_hardware_addresses()
        assert len(ledger_addrs) == 1
        assert ledger_addrs[0].is_hardware

    def test_hardware_flag_is_brand_neutral(self):
        """Routing asks 'does this need a device', not 'is this a Ledger'."""
        wallet = self._wallet()
        wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH, path_type="ledger_live")

        entry = wallet.get_address_by_address(LEDGER_ADDR)
        assert entry.is_hardware
        assert not entry.is_software
        assert entry.device_label == "Ledger"

    def test_software_addresses_are_not_hardware(self):
        from primer_vault.wallet.crypto import AddressEntry

        entry = AddressEntry(id="A001", name="Main", address=LEDGER_ADDR)
        assert not entry.is_hardware
        assert entry.is_software
        assert entry.device_label == ""

    def test_ledger_address_survives_a_save_load_round_trip(self):
        """Ledger metadata must persist - it's all we have to sign with later."""
        import json
        import tempfile

        wallet = self._wallet()
        wallet.add_hardware_address(
            address=LEDGER_ADDR, path=DEFAULT_PATH,
            path_type="ledger_live", name="Persisted")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.json"
            wallet.save(path)
            raw = json.loads(path.read_text())

            reloaded = type(wallet).load(path, password="test-password")

        entry = reloaded.get_address_by_address(LEDGER_ADDR)
        assert entry.is_hardware
        assert entry.device_path == DEFAULT_PATH
        assert entry.device_path_type == "ledger_live"
        # No secret material should have been written for a hardware address.
        assert "encrypted_key" not in json.dumps(raw["addresses"])


# =============================================================================
# Shared address picker
# =============================================================================

class _FakeDevice:
    """Device stand-in that records how many sweeps the picker asks for."""

    def __init__(self):
        self.calls = []

    def get_addresses(self, path_type, start_index=0, count=5, custom_path=None):
        self.calls.append((start_index, count))
        return [
            LedgerAddress(
                path=get_derivation_path(path_type, i, custom_path),
                address=f"0x{i:040x}",
                path_type=path_type.value,
                index=i,
            )
            for i in range(start_index, start_index + count)
        ]


class TestLedgerAddressSource:
    """The Ledger source feeds the same picker the software seeds use."""

    @staticmethod
    def _source(wallet=None):
        from primer_vault.wallet.address_source import LedgerAddressSource

        wallet = wallet or SimpleNamespace(get_hardware_addresses=lambda: [])
        return LedgerAddressSource(wallet, _FakeDevice(), LedgerPathType.BIP44)

    def test_not_ready_before_prepare(self):
        """Device reads are slow, so the picker must load them off the UI thread."""
        source = self._source()
        assert source.is_ready(0, 10) is False

    def test_ready_after_prepare(self):
        source = self._source()
        source.prepare(0, 10)
        assert source.is_ready(0, 10) is True
        assert source.derive(3) == f"0x{3:040x}"

    def test_prepare_uses_one_sweep_per_page(self):
        """One USB sweep per page, not one round trip per address."""
        source = self._source()
        source.prepare(0, 10)
        assert source.device.calls == [(0, 10)]

    def test_prepare_only_fetches_what_is_missing(self):
        source = self._source()
        source.prepare(0, 10)
        source.prepare(0, 20)      # Load More
        assert source.device.calls == [(0, 10), (10, 10)]

    def test_prepare_is_a_noop_when_cached(self):
        source = self._source()
        source.prepare(0, 10)
        source.prepare(0, 10)
        assert len(source.device.calls) == 1

    def test_derive_before_prepare_is_an_error_not_a_blank(self):
        source = self._source()
        with pytest.raises(RuntimeError):
            source.derive(0)

    def test_path_is_kept_for_storage(self):
        """The path is what Vault stores - it's how signing finds the key later."""
        source = self._source()
        source.prepare(0, 3)
        assert source.path_for(2) == "m/44'/60'/0'/0/2"

    def test_existing_addresses_are_matched_by_address(self):
        """An address already added under another path type is still the same key."""
        existing = SimpleNamespace(id="L001", name="Mine", address=f"0x{1:040X}")
        source = self._source(
            wallet=SimpleNamespace(get_hardware_addresses=lambda: [existing]))
        source.prepare(0, 3)

        assert source.existing_entry(1, source.derive(1)) is existing
        assert source.existing_entry(2, source.derive(2)) is None

    def test_default_name(self):
        assert self._source().default_name(4) == "Ledger #4"

    def test_delete_seed_is_not_offered(self):
        """There is no seed to delete for a hardware wallet."""
        assert self._source().supports_delete is False


class TestSeedAddressSource:
    """The seed source must keep the existing picker behaviour exactly."""

    @staticmethod
    def _source():
        from primer_vault.wallet.address_source import SeedAddressSource

        entries = [SimpleNamespace(id="A001", name="First", index=0)]
        wallet = SimpleNamespace(
            derive_address_at_index=lambda seed_id, i: f"0x{i:040x}",
            get_addresses_for_seed=lambda seed_id: entries)
        return SeedAddressSource(wallet, "S001"), entries

    def test_always_ready_so_rendering_stays_synchronous(self):
        source, _ = self._source()
        assert source.is_ready(0, 10) is True

    def test_default_name_matches_previous_format(self):
        source, _ = self._source()
        assert source.default_name(3) == "S001 #3"

    def test_existing_entries_are_matched_by_index(self):
        source, entries = self._source()
        assert source.existing_entry(0, "0x0") is entries[0]
        assert source.existing_entry(5, "0x5") is None

    def test_delete_seed_is_offered(self):
        source, _ = self._source()
        assert source.supports_delete is True


# =============================================================================
# x402 payment integration
# =============================================================================

X402_DATA = {
    "x402Version": 2,
    "accepts": [{
        "scheme": "exact",
        "network": "eip155:4663",
        "amount": "1000",
        "asset": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "payTo": "0x00000000000000000000000000000000000c0De0",
        "resource": "http://api.example.com/thing",
        "maxTimeoutSeconds": 60,
        "extra": {"name": "Global Dollar", "version": "1"},
    }],
}


def _ledger_signing_service():
    """A SigningService whose agent is backed by a Ledger address."""
    from primer_vault.services.signing import SigningService

    addr_entry = SimpleNamespace(
        id="L001", address=LEDGER_ADDR, is_hardware=True,
        device_path=DEFAULT_PATH, device_path_type="ledger_live")
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: addr_entry,
        get_private_key=lambda _id: pytest.fail("must not read a key for a Ledger address"))

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", wallet_address=LEDGER_ADDR,
        spent_today_micro=0, status="active", intent_mandate=None)
    # A real limit: 0 means a cap of zero and refuses everything (these tests
    # sign a 1000-micro payment, which must fit under the day's allowance).
    policy = SimpleNamespace(daily_limit_micro=10_000_000)

    svc = SigningService()
    svc.set_wallet_provider(lambda addr: wallet)
    svc.set_stores(SimpleNamespace(
        update_agent=lambda a: None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None,
        get_all_transactions=lambda: []))
    return svc, agent, policy


class TestX402PaymentIntegration:
    """Ledger addresses sign x402 payment authorizations on the device."""

    def test_payment_is_signed_via_the_handler(self):
        svc, agent, policy = _ledger_signing_service()
        calls = []

        def handler(typed_data, path, expected_address):
            calls.append((typed_data, path, expected_address))
            return "0x" + "ab" * 65

        svc.set_callbacks(on_hardware_sign_needed=handler)
        result = svc._sign_payment(agent, policy, X402_DATA, amount_micro=1000)

        assert result["status"] == "success", result
        assert len(calls) == 1

        typed_data, path, expected_address = calls[0]
        assert path == DEFAULT_PATH
        assert expected_address == LEDGER_ADDR
        # The device signs a TransferWithAuthorization, not a raw transaction.
        assert typed_data["primaryType"] == "TransferWithAuthorization"
        # Checksummed by the builder, so compare case-insensitively.
        assert typed_data["message"]["from"].lower() == LEDGER_ADDR.lower()

    def test_no_handler_is_reported_not_silently_skipped(self):
        """Headless has no device prompt; the agent must get a clear error."""
        svc, agent, policy = _ledger_signing_service()
        result = svc._sign_payment(agent, policy, X402_DATA, amount_micro=1000)
        assert result["code"] == "LEDGER_SIGN_NOT_AVAILABLE"

    @pytest.mark.parametrize("error,expected_code", [
        (LedgerRejectedError("Rejected on device."), "LEDGER_REJECTED"),
        (LedgerDisconnectedError("Ledger disconnected."), "LEDGER_DISCONNECTED"),
        (LedgerError("something else broke"), "LEDGER_ERROR"),
    ])
    def test_device_errors_map_to_agent_facing_codes(self, error, expected_code):
        svc, agent, policy = _ledger_signing_service()

        def handler(typed_data, path, expected_address):
            raise error

        svc.set_callbacks(on_hardware_sign_needed=handler)
        result = svc._sign_payment(agent, policy, X402_DATA, amount_micro=1000)

        assert result["status"] == "error"
        assert result["code"] == expected_code

    def test_spending_is_not_recorded_when_signing_fails(self):
        """A rejected payment must not consume the agent's daily allowance."""
        svc, agent, policy = _ledger_signing_service()

        def handler(typed_data, path, expected_address):
            raise LedgerRejectedError("Rejected on device.")

        svc.set_callbacks(on_hardware_sign_needed=handler)
        svc._sign_payment(agent, policy, X402_DATA, amount_micro=1000)

        assert agent.spent_today_micro == 0


# =============================================================================
# Trading integration
# =============================================================================

USDG = "0x" + "d1" * 20
WETH = "0x" + "e2" * 20


class _FakeExecAdapter:
    """Stand-in DexAdapter that records what gets broadcast."""

    DEC = {USDG.lower(): 6, WETH.lower(): 18}
    SYM = {USDG.lower(): "USDG", WETH.lower(): "WETH"}

    def __init__(self):
        self.sent_raw = []
        self.signed_locally = []

    # -- quoting ------------------------------------------------------------
    def token_metadata(self, token):
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "9" * 40

    def quote_exact_input_single(self, ti, to, amt, fee, tick_spacing=None, hooks=None):
        return {"amount_out": 5 * 10**15, "sqrt_after": 0,
                "ticks_crossed": 1, "gas_estimate": 90000}

    # -- execution ----------------------------------------------------------
    def router_address(self):
        return "0x" + "7" * 40

    def approval_steps(self, token, owner, amount, token_label=""):
        """Mirror DexAdapter.approval_steps: nothing to approve if covered."""
        if self.allowance(token, owner, self.router_address()) >= amount:
            return []
        return [(self.build_approve_tx(token, self.router_address(), amount, owner),
                 f"approve {token_label or 'the input token'} for the router")]

    def allowance(self, token, owner, spender):
        return 0  # force the approval step, so a trade needs two signatures

    def build_approve_tx(self, token, spender, amount, sender, gas=None):
        return {**EIP1559_TX, "to": token, "nonce": 1}

    def build_swap_tx(self, *a, **kw):
        return {**EIP1559_TX, "to": self.router_address(), "nonce": 2}

    def simulate_swap(self, *a, **kw):
        return 5 * 10**15

    def send_raw(self, raw):
        self.sent_raw.append(raw)
        return f"0xhash{len(self.sent_raw)}"

    def sign_and_send(self, tx, private_key, before_send=None):
        if before_send:
            before_send()
        self.signed_locally.append(tx)
        return f"0xhash{len(self.signed_locally)}"

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        """The fill, which a real adapter reads out of the receipt's logs.

        These fakes hand back a bare status, so there is nothing to read and the
        honest answer is "unknown" - the same answer the real adapter gives for a
        receipt it cannot parse. That makes every trade here exercise the branch
        where the fill is unavailable, which is worth having: a trade that
        settled must still be recorded as settled when its fill cannot be read.
        Reading a real receipt is covered in test_trade_fill.py.
        """
        return None


def _ledger_trading_service(monkeypatch, adapter):
    """A TradingService whose wallet holds a single Ledger-backed address."""
    from primer_vault.services.trading import TradingService

    addr_entry = SimpleNamespace(
        id="L001", address=LEDGER_ADDR, is_hardware=True,
        device_path=DEFAULT_PATH, device_path_type="ledger_live")
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: addr_entry,
        get_private_key=lambda _id: pytest.fail("must not read a key for a Ledger address"))

    # execute_trade re-checks that the address belongs to the agent, so the
    # service needs a store to resolve it - the same check a real trade gets.
    agent = SimpleNamespace(id="A1", name="Bot", code="A1-code", wallet_address=LEDGER_ADDR)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None)

    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc


def _trade(svc, adapter):
    """Build and execute a USDG->WETH trade through the service."""
    from primer_vault.models.trade import TradeRequest

    request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
    request.wallet_address = LEDGER_ADDR
    quote = svc.prepare_trade(request)
    return svc.execute_trade(request, quote)


class TestTradingIntegration:
    """Ledger addresses must be able to trade, signing each tx on the device."""

    def test_trade_signs_approval_and_swap_on_device(self, monkeypatch):
        adapter = _FakeExecAdapter()
        svc = _ledger_trading_service(monkeypatch, adapter)

        calls = []

        def handler(tx, path, address, description):
            calls.append((tx, path, address, description))
            return f"0xsigned{len(calls)}"

        svc.set_hardware_tx_signer(handler)
        result = _trade(svc, adapter)

        assert result["status"] == "executed"
        # Two device confirmations: the ERC-20 approval, then the swap.
        assert len(calls) == 2
        assert adapter.sent_raw == ["0xsigned1", "0xsigned2"]
        # Nothing was signed locally.
        assert adapter.signed_locally == []

    def test_handler_receives_path_and_address_for_verification(self, monkeypatch):
        adapter = _FakeExecAdapter()
        svc = _ledger_trading_service(monkeypatch, adapter)

        calls = []
        svc.set_hardware_tx_signer(
            lambda tx, path, address, desc: calls.append((path, address, desc)) or "0xsig")
        _trade(svc, adapter)

        for path, address, description in calls:
            assert path == DEFAULT_PATH
            assert address == LEDGER_ADDR
            assert description  # each step tells the user what they're approving

        # The descriptions distinguish the two confirmations.
        assert "approve" in calls[0][2].lower()
        assert "swap" in calls[1][2].lower()

    def test_trade_without_a_handler_is_refused(self, monkeypatch):
        """Headless/CLI has no way to prompt, so say so rather than hanging."""
        adapter = _FakeExecAdapter()
        svc = _ledger_trading_service(monkeypatch, adapter)

        result = _trade(svc, adapter)
        assert result["code"] == "LEDGER_SIGN_NOT_AVAILABLE"
        assert adapter.sent_raw == []

    def test_rejection_on_device_fails_the_trade_cleanly(self, monkeypatch):
        adapter = _FakeExecAdapter()
        svc = _ledger_trading_service(monkeypatch, adapter)

        def reject(tx, path, address, description):
            raise LedgerRejectedError("Rejected on device.")

        svc.set_hardware_tx_signer(reject)
        result = _trade(svc, adapter)

        assert result["status"] == "failed"
        assert "Rejected on device" in result["reason"]
        assert adapter.sent_raw == []

    def test_disconnect_mid_trade_is_reported(self, monkeypatch):
        adapter = _FakeExecAdapter()
        svc = _ledger_trading_service(monkeypatch, adapter)

        calls = []

        def flaky(tx, path, address, description):
            calls.append(description)
            if len(calls) == 1:
                return "0xsigned1"          # approval succeeds
            raise LedgerDisconnectedError("Ledger disconnected. Please reconnect and try again.")

        svc.set_hardware_tx_signer(flaky)
        result = _trade(svc, adapter)

        assert result["status"] == "failed"
        assert "disconnected" in result["reason"].lower()
        # The approval did broadcast before the device went away.
        assert adapter.sent_raw == ["0xsigned1"]


class TestStaleDongleRecovery:
    """A dead cached dongle must not end the session.

    ledgereth holds one process-global dongle and never invalidates it, so once a
    handle closes every later call fails instantly and the device looks gone
    until Vault restarts. A single trade needs two confirmations - the ERC-20
    approval, then the swap - so this is reachable in ordinary use, on a device
    the user is holding and has already approved with.

    Recovery belongs in the connection check because that only reads an address.
    The signing paths must not retry: a second attempt would ask the user to
    confirm the same action twice.
    """

    def test_a_dead_handle_is_dropped_and_retried(self, monkeypatch):
        import ledgereth.accounts as accounts
        from primer_vault.wallet import ledger as ledger_mod

        state = {"attempts": 0, "resets": 0}

        class Account:
            address = "0x" + "ab" * 20

        def flaky(count=1):
            state["attempts"] += 1
            if state["resets"] == 0:
                raise ValueError("not open")
            return [Account()]

        monkeypatch.setattr(accounts, "get_accounts", flaky)
        monkeypatch.setattr(ledger_mod, "reset_connection",
                            lambda: state.__setitem__("resets", state["resets"] + 1))

        assert ledger_mod.LedgerDevice.discover() is not None
        assert state["resets"] == 1, "the stale dongle was not dropped"
        assert state["attempts"] == 2, "the call was not retried after the reset"

    def test_a_genuinely_absent_device_is_not_retried(self, monkeypatch):
        """No device is an answer, not a fault - retrying only adds delay."""
        import ledgereth.accounts as accounts
        from ledgereth.exceptions import LedgerNotFound
        from primer_vault.wallet import ledger as ledger_mod

        state = {"attempts": 0}

        def missing(count=1):
            state["attempts"] += 1
            raise LedgerNotFound("no device")

        monkeypatch.setattr(accounts, "get_accounts", missing)

        assert ledger_mod.LedgerDevice.discover() is None
        assert state["attempts"] == 1

    def test_a_persistent_failure_still_surfaces(self, monkeypatch):
        import ledgereth.accounts as accounts
        from primer_vault.wallet import ledger as ledger_mod

        state = {"attempts": 0}

        def always_dead(count=1):
            state["attempts"] += 1
            raise ValueError("not open")

        monkeypatch.setattr(accounts, "get_accounts", always_dead)
        monkeypatch.setattr(ledger_mod, "reset_connection", lambda: None)

        with pytest.raises(ledger_mod.LedgerError):
            ledger_mod.LedgerDevice.discover()
        assert state["attempts"] == 2, "should retry exactly once, then stop"


class TestCalldataReachesTheDeviceAsBytes:
    """Every contract call a trade makes carries calldata, and every one of them
    arrives here as a hex string from web3's build_transaction().

    ledgereth passes it to eth_utils.to_bytes(), which rejects a positional
    string - so a string that is not converted first fails every approval, swap
    and unwrap a hardware wallet attempts.
    """

    @patch("ledgereth.transactions.create_transaction")
    def test_a_hex_string_is_converted(self, mock_create):
        mock_create.return_value = MagicMock(raw_transaction="0x02f8")
        device = _discovered_device()

        tx = dict(EIP1559_TX, data="0xac9650d800000000")
        device.sign_transaction(DEFAULT_PATH, tx)

        passed = mock_create.call_args.kwargs["data"]
        assert isinstance(passed, bytes)
        assert passed == bytes.fromhex("ac9650d800000000")

    @patch("ledgereth.transactions.create_transaction")
    def test_bytes_are_left_alone(self, mock_create):
        mock_create.return_value = MagicMock(raw_transaction="0x02f8")
        device = _discovered_device()

        device.sign_transaction(
            DEFAULT_PATH, dict(EIP1559_TX, data=bytes.fromhex("ac9650d8")))

        assert mock_create.call_args.kwargs["data"] == bytes.fromhex("ac9650d8")

    @patch("ledgereth.transactions.create_transaction")
    def test_no_calldata_becomes_empty_bytes(self, mock_create):
        """A plain ETH transfer has none."""
        mock_create.return_value = MagicMock(raw_transaction="0x02f8")
        device = _discovered_device()

        device.sign_transaction(DEFAULT_PATH, dict(EIP1559_TX, data=None))

        assert mock_create.call_args.kwargs["data"] == b""

    @patch("ledgereth.transactions.create_transaction")
    def test_calldata_that_is_not_hex_is_refused(self, mock_create):
        device = _discovered_device()

        with pytest.raises(ValueError, match="not valid hex"):
            device.sign_transaction(DEFAULT_PATH, dict(EIP1559_TX, data="not-hex"))
