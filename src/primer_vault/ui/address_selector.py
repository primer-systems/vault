"""
The address chip and its dropdown.

The wallet tab used to spend a third of its height on a table of addresses, for
a list that is usually two rows long and is read once - to pick one. That space
is worth more to the things the chosen address actually holds.

So the list collapses to a chip on the toolbar row and drops down when clicked.

**Selecting is all the dropdown does.** No double-click, no context menu: a
right-click inside an open popup is fiddly, and a double-click fights with the
single click that picks a row. Everything else acts on whatever is selected,
from buttons beside the chip, where it can be seen without opening anything.

A filter sits at the top because a software wallet has two addresses and a
Ledger can have forty, and the same control has to work for both.
"""

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from .theme import Theme


def _short(address: str) -> str:
    """An address as it appears on the chip."""
    if not address or len(address) < 16:
        return address or ""
    return f"{address[:8]}…{address[-6:]}"


def _source(entry) -> str:
    """Where an address came from: a device, a seed, or an import."""
    if getattr(entry, "is_hardware", False):
        return getattr(entry, "device_label", "Ledger")
    seed_id = getattr(entry, "seed_id", None)
    if seed_id:
        index = getattr(entry, "index", None)
        return f"{seed_id}#{index}" if index is not None else seed_id
    return "—"


class AddressDropdown(QFrame):
    """The panel that drops below the chip. Filter, then a row per address."""

    chosen = pyqtSignal(str)

    #: Above this many addresses the filter is worth showing. Below it, it is a
    #: box to ignore above a list you can already read at a glance.
    FILTER_THRESHOLD = 6

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setProperty("role", "dropdown")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter addresses…")
        self.filter_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_input)

        self.list = QListWidget()
        self.list.setProperty("role", "address-list")
        self.list.itemActivated.connect(self._on_item)
        self.list.itemClicked.connect(self._on_item)
        layout.addWidget(self.list)

    def show_for(self, chip: QWidget, entries, balances: dict,
                 selected: Optional[str]) -> None:
        """Fill the list and drop it below `chip`."""
        self.list.clear()
        for entry in entries:
            address = entry.address
            usd = balances.get(address)
            value = f"  ${usd:,.2f}" if isinstance(usd, (int, float)) else ""
            item = QListWidgetItem(
                f"{entry.name} — {_short(address)} ({_source(entry)}){value}")
            item.setData(Qt.ItemDataRole.UserRole, address)
            item.setFont(QFont(Theme.MONO_FONT, 9))
            item.setToolTip(address)
            if selected and address.lower() == selected.lower():
                item.setSelected(True)
            self.list.addItem(item)

        many = len(entries) > self.FILTER_THRESHOLD
        self.filter_input.setVisible(many)
        self.filter_input.clear()

        # Wide enough for the chip it hangs from, and tall enough for the list
        # up to a ceiling - past that it scrolls rather than running off screen.
        width = max(chip.width(), 320)
        rows = min(len(entries), 8)
        height = rows * 26 + (34 if many else 0) + 16
        self.setFixedSize(width, max(height, 44))
        self.move(chip.mapToGlobal(chip.rect().bottomLeft()))
        self.show()
        (self.filter_input if many else self.list).setFocus()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            hidden = bool(needle) and needle not in item.text().lower() \
                and needle not in (item.data(Qt.ItemDataRole.UserRole) or "").lower()
            item.setHidden(hidden)

    def _on_item(self, item: QListWidgetItem) -> None:
        address = item.data(Qt.ItemDataRole.UserRole)
        self.hide()
        if address:
            self.chosen.emit(address)


class AddressSelector(QWidget):
    """Chip, dropdown, copy, and a menu of per-address actions.

    Emits `address_selected` when the choice changes - including the first
    population, so whatever renders the address does not need a separate nudge.
    """

    address_selected = pyqtSignal(str)
    copy_requested = pyqtSignal(str)
    details_requested = pyqtSignal(str)
    send_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list = []
        self._balances: dict = {}
        self._selected: Optional[str] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.chip = QToolButton()
        self.chip.setProperty("role", "address-chip")
        self.chip.setArrowType(Qt.ArrowType.NoArrow)
        self.chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chip.clicked.connect(self._open)
        layout.addWidget(self.chip)

        # One obvious place for everything that used to be a double-click or a
        # right-click on the table row. Copying is not among the buttons here -
        # it belongs beside the address it copies, which is shown above the
        # assets - but it stays in this menu so there is one place that has
        # every per-address action.
        self.more_btn = QToolButton()
        self.more_btn.setProperty("role", "chip-action")
        self.more_btn.setText("⋯")
        self.more_btn.setToolTip("Address actions")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._open_menu)
        layout.addWidget(self.more_btn)

        self._dropdown = AddressDropdown(self)
        self._dropdown.chosen.connect(self.select)

        self._render_chip()

    # ---- state -----------------------------------------------------------

    def set_addresses(self, entries, balances: Optional[dict] = None) -> None:
        """Replace the list, keeping the current selection if it survives."""
        self._entries = list(entries or [])
        self._balances = dict(balances or {})

        if not self._entries:
            self._selected = None
            self._render_chip()
            self.address_selected.emit("")
            return

        keep = None
        if self._selected:
            keep = next((e.address for e in self._entries
                         if e.address.lower() == self._selected.lower()), None)
        self.select(keep or self._entries[0].address)

    def set_balances(self, balances: dict) -> None:
        """Update the values shown in the dropdown. No effect on selection."""
        self._balances = dict(balances or {})

    def select(self, address: str) -> None:
        changed = (address or "").lower() != (self._selected or "").lower()
        self._selected = address or None
        self._render_chip()
        if changed:
            self.address_selected.emit(self._selected or "")

    def selected(self) -> Optional[str]:
        return self._selected

    def clear(self) -> None:
        self._entries = []
        self._balances = {}
        self._selected = None
        self._render_chip()

    # ---- rendering -------------------------------------------------------

    def _entry(self, address: str):
        return next((e for e in self._entries
                     if e.address.lower() == (address or "").lower()), None)

    def _render_chip(self) -> None:
        entry = self._entry(self._selected) if self._selected else None
        if entry is None:
            self.chip.setText("No address")
            self.chip.setEnabled(bool(self._entries))
            self.chip.setToolTip("")
            self.more_btn.setEnabled(False)
            return
        self.chip.setEnabled(True)
        self.chip.setText(
            f"{entry.name} — {_short(entry.address)} ({_source(entry)})  ▾")
        self.chip.setToolTip(entry.address)
        self.more_btn.setEnabled(True)

    # ---- interaction -----------------------------------------------------

    def _open(self) -> None:
        if not self._entries:
            return
        self._dropdown.show_for(self.chip, self._entries, self._balances,
                                self._selected)

    def _open_menu(self) -> None:
        if not self._selected:
            return
        from PyQt6.QtWidgets import QMenu
        entry = self._entry(self._selected)
        name = entry.name if entry else _short(self._selected)
        menu = QMenu(self)
        details = menu.addAction("Details…")
        send = menu.addAction(f"Send from {name}…")
        copy = menu.addAction("Copy address")
        chosen = menu.exec(
            self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))
        if chosen == details:
            self.details_requested.emit(self._selected)
        elif chosen == send:
            self.send_requested.emit(self._selected)
        elif chosen == copy:
            self.copy_requested.emit(self._selected)


class AddressBar(QWidget):
    """The selector plus whatever the tab wants beside it, on one row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.selector = AddressSelector()
        layout.addWidget(self.selector)
        layout.addStretch()
        self.trailing = QLabel("")
        layout.addWidget(self.trailing)
