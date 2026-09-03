"""Enrolling a Ledger address from the terminal edition.

Signing with a hardware address already worked in both editions; *adding* one
did not. `core.add_hardware_address` had exactly one caller, in `ui/tabs.py`,
so a machine with no screen could use a Ledger only if somebody had first
enrolled it on a desktop.

The property that matters here is not that the command runs. It is that both
editions put the *same* bytes in the wallet: the address, the derivation path
and the path-type tag are what every later signature is checked against, so a
terminal that derived even slightly differently would produce addresses the
desktop could not sign for, and vice versa. `test_both_editions_agree_on_the_path`
is the test that would catch that.

No real device is involved. A Ledger is a USB dongle that needs a person to
press its buttons, which CI does not have, so these drive a stand-in with the
same surface as `LedgerDevice`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands import CommandHandler  # noqa: E402
from primer_vault.wallet.ledger import (  # noqa: E402
    LedgerAddress,
    LedgerError,
    LedgerPathType,
    get_derivation_path,
)


#: How far the stub will search when asked to invert a derivation path. Well
#: past anything these tests enrol.
_STUB_INDEX_RANGE = 64


# Deterministic stand-in addresses. Content is irrelevant; only that an index
# maps to a stable, distinguishable address.
def _address_for(index: int) -> str:
    return "0x" + f"{index:040x}"


class FakeLedger:
    """A stand-in with LedgerDevice's surface and none of its USB.

    `derived` records every path asked for, which is how the path-agreement
    test compares the two editions without a device.
    """

    def __init__(self, fail_after: int = None, wrong_address: bool = False):
        self.derived = []
        self._fail_after = fail_after
        self._wrong_address = wrong_address

    def get_address(self, path_type, index, custom_path=None) -> LedgerAddress:
        path = get_derivation_path(path_type, index, custom_path)
        self.derived.append(path)
        if self._fail_after is not None and len(self.derived) > self._fail_after:
            raise LedgerError("Device stopped answering")
        return LedgerAddress(path=path, address=_address_for(index),
                             path_type=path_type.value, index=index)

    def get_addresses(self, path_type, start_index=0, count=5, custom_path=None):
        found = []
        for i in range(start_index, start_index + count):
            try:
                found.append(self.get_address(path_type, i, custom_path))
            except LedgerError:
                if not found:
                    raise
                break
        return found

    def verify_address(self, path, expected_address) -> bool:
        """True when this device really would derive `expected_address` at `path`.

        Answering a flat True would let a stored path and a stored address drift
        apart without any test noticing, which is the one thing verification
        exists to catch.
        """
        if self._wrong_address:
            return False
        for path_type in LedgerPathType:
            if path_type is LedgerPathType.CUSTOM:
                continue
            for index in range(_STUB_INDEX_RANGE):
                if get_derivation_path(path_type, index) == path:
                    return _address_for(index).lower() == expected_address.lower()
        return False


@pytest.fixture
def temp_data_dir(tmp_path):
    """What conftest's `core` fixture builds its data directory from."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def handler(core):
    return CommandHandler(core)


@pytest.fixture
def device(monkeypatch):
    """Install a FakeLedger as whatever `LedgerDevice.discover()` returns."""
    fake = FakeLedger()
    from primer_vault.wallet import ledger

    monkeypatch.setattr(ledger.LedgerDevice, "discover", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def no_device(monkeypatch):
    """discover() finds nothing, which is what an unplugged machine looks like."""
    from primer_vault.wallet import ledger

    monkeypatch.setattr(ledger.LedgerDevice, "discover", classmethod(lambda cls: None))


# ---------------------------------------------------------------------------
# The command exists and is discoverable
# ---------------------------------------------------------------------------

class TestDiscoverable:
    """A command nobody can find is a command nobody has."""

    def test_address_help_lists_ledger(self, handler):
        result = handler.execute("address")
        assert result.success
        assert "ledger" in result.output

    def test_ledger_help_lists_every_subcommand(self, handler):
        result = handler.execute("address ledger")
        assert result.success
        for subcommand in ("list", "add", "verify"):
            assert f"address ledger {subcommand}" in result.output

    def test_ledger_help_lists_every_path_type(self, handler):
        """All four, or a device on an unlisted convention is unreachable."""
        result = handler.execute("address ledger --help")
        assert result.success
        for path_type in ("ledger_live", "bip44", "legacy_mew", "custom"):
            assert path_type in result.output

    def test_unknown_subcommand_is_refused(self, handler):
        result = handler.execute("address ledger frobnicate")
        assert not result.success
        assert "frobnicate" in result.error


# ---------------------------------------------------------------------------
# Reading the device
# ---------------------------------------------------------------------------

class TestList:

    def test_list_shows_addresses_and_paths(self, handler, device):
        result = handler.execute("address ledger list")
        assert result.success, result.error
        assert _address_for(0) in result.output
        assert get_derivation_path(LedgerPathType.LEDGER_LIVE, 0) in result.output
        assert len(result.data["addresses"]) == 5

    def test_list_defaults_to_the_same_path_type_as_the_desktop(self, handler, device):
        """The desktop's connect dialog pre-selects Ledger Live. If the terminal
        defaulted elsewhere, `address ledger add 0` on the two editions would
        enrol two different addresses under the same instruction."""
        handler.execute("address ledger list --count 1")
        assert device.derived == [get_derivation_path(LedgerPathType.LEDGER_LIVE, 0)]

    def test_start_and_count_choose_the_window(self, handler, device):
        result = handler.execute("address ledger list --start 3 --count 2")
        assert result.success, result.error
        assert [row["index"] for row in result.data["addresses"]] == [3, 4]

    def test_list_marks_addresses_already_in_the_wallet(self, handler, device):
        handler.execute("address ledger add 1")
        result = handler.execute("address ledger list")
        assert result.success, result.error
        rows = {row["index"]: row for row in result.data["addresses"]}
        assert rows[1]["address_id"], "an enrolled address should say so"
        assert rows[0]["address_id"] is None

    def test_list_enrols_nothing(self, handler, core, device):
        handler.execute("address ledger list")
        assert not [a for a in core.get_wallet_addresses() if a["is_hardware"]]

    def test_a_short_sweep_says_it_was_short(self, handler, monkeypatch, core):
        """A device that stops answering half way must not read as 'that is all
        there is' - the operator would conclude the remaining indices are empty."""
        from primer_vault.wallet import ledger
        fake = FakeLedger(fail_after=2)
        monkeypatch.setattr(ledger.LedgerDevice, "discover", classmethod(lambda cls: fake))

        result = handler.execute("address ledger list --count 5")
        assert result.success, result.error
        assert "Stopped after 2 of 5" in result.output


# ---------------------------------------------------------------------------
# Enrolling
# ---------------------------------------------------------------------------

class TestAdd:

    def test_add_puts_a_hardware_address_in_the_wallet(self, handler, core, device):
        result = handler.execute("address ledger add 0")
        assert result.success, result.error

        hardware = [a for a in core.get_wallet_addresses() if a["is_hardware"]]
        assert len(hardware) == 1
        assert hardware[0]["address"].lower() == _address_for(0).lower()

    def test_the_stored_path_is_the_one_that_was_derived(self, handler, core, device):
        """The path is what every later signature is routed to. Storing one the
        device did not derive would produce an address that can never sign."""
        handler.execute("address ledger add 4")
        stored = [a for a in core.get_wallet_addresses() if a["is_hardware"]][0]
        assert stored["device_path"] == get_derivation_path(LedgerPathType.LEDGER_LIVE, 4)
        assert stored["device_path"] in device.derived

    def test_no_private_key_is_stored(self, handler, core, device):
        """The whole point of a hardware address: the key stays on the device."""
        handler.execute("address ledger add 0")
        wallet = core.get_wallet()
        entry = [a for a in wallet.addresses if a.is_hardware][0]
        assert entry.encrypted_pkey is None
        assert entry.seed_id is None

    def test_a_single_address_can_be_named(self, handler, core, device):
        handler.execute('address ledger add 0 "Trading desk"')
        stored = [a for a in core.get_wallet_addresses() if a["is_hardware"]][0]
        assert stored["name"] == "Trading desk"

    def test_several_indices_in_one_call(self, handler, core, device):
        result = handler.execute("address ledger add 0,1,2")
        assert result.success, result.error
        hardware = [a for a in core.get_wallet_addresses() if a["is_hardware"]]
        assert len(hardware) == 3

    def test_a_name_with_several_indices_is_refused(self, handler, device):
        """One name cannot describe three addresses, and silently applying it to
        the first is the kind of surprise that gets noticed months later."""
        result = handler.execute('address ledger add 0,1 "Trading desk"')
        assert not result.success
        assert "single address" in result.error

    def test_adding_the_same_index_twice_does_not_duplicate(self, handler, core, device):
        handler.execute("address ledger add 0")
        result = handler.execute("address ledger add 0")
        assert not result.success
        assert "already in wallet" in result.error
        assert len([a for a in core.get_wallet_addresses() if a["is_hardware"]]) == 1

    def test_a_partial_batch_keeps_what_worked(self, handler, core, monkeypatch):
        """Three indices, a device that dies after the second: the first two are
        real addresses on a real device and there is no reason to discard them."""
        from primer_vault.wallet import ledger
        fake = FakeLedger(fail_after=2)
        monkeypatch.setattr(ledger.LedgerDevice, "discover", classmethod(lambda cls: fake))

        result = handler.execute("address ledger add 0,1,2")
        assert result.success, result.error
        assert len([a for a in core.get_wallet_addresses() if a["is_hardware"]]) == 2
        assert "could not be read" in result.output

    def test_add_without_an_index_explains_itself(self, handler, device):
        result = handler.execute("address ledger add")
        assert not result.success
        assert "address ledger list" in result.error

    def test_a_non_numeric_index_is_refused(self, handler, device):
        result = handler.execute("address ledger add zero")
        assert not result.success
        assert "zero" in result.error


# ---------------------------------------------------------------------------
# Path types
# ---------------------------------------------------------------------------

class TestPathTypes:

    @pytest.mark.parametrize("path_type", ["ledger_live", "bip44", "legacy_mew"])
    def test_every_standard_convention_derives_its_own_path(self, handler, device,
                                                            path_type):
        handler.execute(f"address ledger list --path-type {path_type} --count 1")
        assert device.derived == [
            get_derivation_path(LedgerPathType(path_type), 0)
        ]

    def test_custom_needs_a_template(self, handler, device):
        result = handler.execute("address ledger list --path-type custom")
        assert not result.success
        assert "--path" in result.error

    def test_custom_uses_the_template_given(self, handler, device):
        result = handler.execute(
            "address ledger list --path-type custom --path \"m/44'/60'/7'/0/{index}\" "
            "--count 1")
        assert result.success, result.error
        assert device.derived == ["m/44'/60'/7'/0/0"]

    def test_an_unknown_path_type_is_refused(self, handler, device):
        result = handler.execute("address ledger list --path-type bip99")
        assert not result.success
        assert "bip99" in result.error

    def test_the_path_type_is_recorded_with_the_address(self, handler, core, device):
        """Stored so a later browse can be resumed on the same convention."""
        handler.execute("address ledger add 0 --path-type bip44")
        wallet = core.get_wallet()
        entry = [a for a in wallet.addresses if a.is_hardware][0]
        assert entry.device_path_type == "bip44"


# ---------------------------------------------------------------------------
# The two editions must agree
# ---------------------------------------------------------------------------

def test_both_editions_agree_on_the_path(device):
    """`address ledger add N` and the desktop's picker must enrol the same key.

    Both go through `get_derivation_path`, so this asserts the terminal has not
    grown a second opinion about what index N means - which would produce
    addresses one edition could sign for and the other could not.
    """
    from primer_vault.wallet.address_source import LedgerAddressSource

    for path_type in (LedgerPathType.LEDGER_LIVE, LedgerPathType.BIP44,
                      LedgerPathType.LEGACY_MEW):
        source = LedgerAddressSource(wallet=None, device=FakeLedger(),
                                     path_type=path_type)
        source.prepare(0, 3)

        for index in range(3):
            desktop_path = source.path_for(index)
            terminal_path = get_derivation_path(path_type, index)
            assert desktop_path == terminal_path, (
                f"{path_type.value} #{index}: desktop enrols {desktop_path}, "
                f"terminal enrols {terminal_path}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

class TestVerify:

    def test_a_matching_device_verifies(self, handler, core, device):
        handler.execute("address ledger add 0")
        stored = [a for a in core.get_wallet_addresses() if a["is_hardware"]][0]

        result = handler.execute(f"address ledger verify {stored['id']}")
        assert result.success, result.error
        assert result.data["verified"] is True

    def test_a_different_device_fails_the_check(self, handler, core, device, monkeypatch):
        handler.execute("address ledger add 0")
        stored = [a for a in core.get_wallet_addresses() if a["is_hardware"]][0]

        from primer_vault.wallet import ledger
        monkeypatch.setattr(ledger.LedgerDevice, "discover",
                            classmethod(lambda cls: FakeLedger(wrong_address=True)))

        result = handler.execute(f"address ledger verify {stored['id']}")
        assert not result.success
        assert "Mismatch" in result.error

    def test_a_software_address_cannot_be_verified(self, handler, core, device):
        software = core.get_wallet_addresses()[0]
        result = handler.execute(f"address ledger verify {software['id']}")
        assert not result.success
        assert "not a hardware address" in result.error

    def test_an_unknown_address_is_refused(self, handler, device):
        result = handler.execute("address ledger verify A999")
        assert not result.success
        assert "not found" in result.error


# ---------------------------------------------------------------------------
# When there is no device
# ---------------------------------------------------------------------------

class TestNoDevice:
    """The common case on a server, and the one where a bad message costs the
    most: the operator is remote and cannot see whether anything is plugged in."""

    def test_list_says_what_to_check(self, handler, no_device):
        result = handler.execute("address ledger list")
        assert not result.success
        assert "No Ledger found" in result.error
        assert "Ethereum app" in result.error

    def test_the_service_user_case_is_named(self, handler, no_device):
        """Only the terminal edition hits this: an engine started by systemd runs
        as its own user, and USB access is a permission that user may not have.
        Nothing else in the message would lead anyone to look at udev."""
        result = handler.execute("address ledger list")
        assert "udev" in result.error

    def test_add_says_the_same_thing(self, handler, no_device):
        result = handler.execute("address ledger add 0")
        assert not result.success
        assert "No Ledger found" in result.error

    def test_nothing_is_written_when_the_device_is_absent(self, handler, core, no_device):
        handler.execute("address ledger add 0")
        assert not [a for a in core.get_wallet_addresses() if a["is_hardware"]]


# ---------------------------------------------------------------------------
# A locked wallet
# ---------------------------------------------------------------------------

class TestLockedWallet:

    @pytest.mark.parametrize("command", [
        "address ledger list",
        "address ledger add 0",
        "address ledger verify L001",
    ])
    def test_every_subcommand_needs_an_unlocked_wallet(self, core_no_wallet,
                                                       device, command):
        """There is nowhere to put an address until the wallet is open, and the
        refusal must come before the device is touched - a USB prompt for an
        operation that cannot complete is worse than no prompt."""
        result = CommandHandler(core_no_wallet).execute(command)
        assert not result.success
        assert "unlocked" in result.error.lower()
        assert device.derived == []


@pytest.fixture
def core_no_wallet(tmp_path):
    from primer_vault.core import Vault
    vault = Vault(data_dir=tmp_path / "locked")
    yield vault
    vault.release_instance_lock()
