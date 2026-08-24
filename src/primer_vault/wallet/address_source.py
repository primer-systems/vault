"""
Address sources for the shared address picker.

DerivationBrowserDialog renders rows from one of these, so software seeds and
hardware wallets go through the same UI. Deliberately Qt-free: this is pure
derivation/lookup logic, and keeping it free of UI imports also keeps it out of
the wallet <-> ui import cycle.
"""

from .crypto import VaultWallet


class AddressSource:
    """
    Supplies rows to DerivationBrowserDialog.

    Lets one picker serve both software seeds and hardware wallets. Seed
    derivation is local BIP-32 maths and effectively instant; Ledger derivation
    is a USB round trip per address, so slow sources declare themselves via
    is_ready()/prepare() and the dialog loads them on a worker thread.
    """

    #: Dialog title
    title = "Select Addresses"
    #: Whether to offer the destructive "Delete Seed" action
    supports_delete = False
    #: Status text shown while prepare() runs
    loading_text = "Loading addresses..."

    def default_name(self, index: int) -> str:
        """Placeholder name for a not-yet-added address."""
        raise NotImplementedError

    def is_ready(self, start: int, count: int) -> bool:
        """True if derive() can serve this range without blocking."""
        return True

    def prepare(self, start: int, count: int) -> None:
        """Fetch a range so derive() can serve it. May block; called off the UI thread."""
        return None

    def derive(self, index: int) -> str:
        """Return the address at index. Must be fast once is_ready() is True."""
        raise NotImplementedError

    def existing_entry(self, index: int, address: str):
        """Return the wallet's AddressEntry for this row, or None if not added yet."""
        raise NotImplementedError


class SeedAddressSource(AddressSource):
    """Addresses derived locally from a seed phrase."""

    supports_delete = True

    def __init__(self, wallet: VaultWallet, seed_id: str):
        self.wallet = wallet
        self.seed_id = seed_id
        self.title = f"Derive Addresses from {seed_id}"

    def default_name(self, index: int) -> str:
        return f"{self.seed_id} #{index}"

    def derive(self, index: int) -> str:
        return self.wallet.derive_address_at_index(self.seed_id, index)

    def existing_entry(self, index: int, address: str):
        # Matched by index, since a seed's addresses are defined by their index.
        for addr in self.wallet.get_addresses_for_seed(self.seed_id):
            if addr.index == index:
                return addr
        return None


class LedgerAddressSource(AddressSource):
    """Addresses derived on a connected Ledger device.

    Each derivation is a USB round trip, so a whole page is fetched in one
    prepare() call on a worker thread and cached for derive() to serve.
    """

    supports_delete = False
    loading_text = "Reading addresses from your Ledger..."

    def __init__(self, wallet: VaultWallet, device, path_type, custom_path: str = None):
        self.wallet = wallet
        self.device = device
        self.path_type = path_type
        self.custom_path = custom_path
        self._cache: dict[int, str] = {}
        self._paths: dict[int, str] = {}

        from .ledger import get_path_type_display_name
        self.title = f"Derive Addresses from Ledger ({get_path_type_display_name(path_type.value)})"

    def default_name(self, index: int) -> str:
        return f"Ledger #{index}"

    def is_ready(self, start: int, count: int) -> bool:
        return all(i in self._cache for i in range(start, start + count))

    def prepare(self, start: int, count: int) -> None:
        missing = [i for i in range(start, start + count) if i not in self._cache]
        if not missing:
            return
        # Contiguous fetch from the lowest missing index keeps the device chatter
        # to one sweep rather than one call per gap.
        first, last = min(missing), max(missing)
        for addr in self.device.get_addresses(
            self.path_type, start_index=first, count=last - first + 1,
            custom_path=self.custom_path,
        ):
            self._cache[addr.index] = addr.address
            self._paths[addr.index] = addr.path

    def derive(self, index: int) -> str:
        if index not in self._cache:
            raise RuntimeError(f"Address at index {index} has not been read from the device")
        return self._cache[index]

    def path_for(self, index: int) -> str:
        """Full derivation path for an index, for storing alongside the address."""
        return self._paths[index]

    def existing_entry(self, index: int, address: str):
        # Matched by address: a Ledger address may have been added under a
        # different path type, and the same key would still be the same account.
        for addr in self.wallet.get_hardware_addresses():
            if addr.address.lower() == address.lower():
                return addr
        return None
