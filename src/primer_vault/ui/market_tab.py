"""
Market Tab - Browse x402 services from Agentic.Market.

Fetches the public service catalog from api.agentic.market and
displays it in a searchable, filterable table. GUI-only feature
for human operators to discover available x402 services and copy
endpoint details for agent configuration.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QLineEdit, QApplication, QTextEdit, QSplitter, QAbstractItemView
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor

from .theme import Theme, active, colored_span
from ..version import USER_AGENT

logger = logging.getLogger(__name__)

# Agentic.Market API
MARKET_API = "https://api.agentic.market/v1/services"
MARKET_URL = "https://agentic.market"
FETCH_LIMIT = 500  # Get all services in one call


class MarketFetchThread(QThread):
    """Background thread for fetching market data."""

    finished = pyqtSignal(list, str)  # services, error_message

    def run(self):
        try:
            all_services = []
            offset = 0
            while True:
                url = f"{MARKET_API}?limit=200&offset={offset}"
                req = Request(url, headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                })
                with urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                batch = data.get("services", [])
                all_services.extend(batch)
                total = data.get("total", 0)
                if not batch or len(all_services) >= total:
                    break
                offset += len(batch)
            self.finished.emit(all_services, "")
        except URLError as e:
            self.finished.emit([], f"Network error: {e.reason}")
        except Exception as e:
            self.finished.emit([], str(e))


class MarketTab(QWidget):
    """Tab for browsing Agentic.Market x402 services."""

    activity = pyqtSignal(str, bool)  # message, is_error

    def __init__(self):
        super().__init__()
        self._services: list[dict] = []
        self._categories: list[str] = []
        self._last_fetch: Optional[datetime] = None
        self._fetch_thread: Optional[MarketFetchThread] = None
        self._selected_service: Optional[dict] = None

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search services...")
        self.search_input.setFont(QFont(Theme.MONO_FONT, 10))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filters)
        toolbar.addWidget(self.search_input, stretch=3)

        self.category_combo = QComboBox()
        self.category_combo.setFont(QFont(Theme.MONO_FONT, 10))
        self.category_combo.addItem("All Categories")
        self.category_combo.currentTextChanged.connect(self._apply_filters)
        toolbar.addWidget(self.category_combo, stretch=1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFont(QFont(Theme.MONO_FONT, 10))
        self.refresh_btn.clicked.connect(self.fetch_services)
        toolbar.addWidget(self.refresh_btn)

        layout.addLayout(toolbar)

        # --- Splitter: table on top, detail panel on bottom ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Service table ---
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Service", "Category", "Provider", "Endpoints", "Price Range"
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 130)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.currentItemChanged.connect(self._on_service_selected)

        table_layout.addWidget(self.table)
        splitter.addWidget(table_container)

        # --- Detail panel ---
        detail_container = QWidget()
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(0, 4, 0, 0)

        # Header row with service name and copy button
        detail_header = QHBoxLayout()

        self.detail_title = QLabel("Select a service to view details")
        self.detail_title.setFont(QFont(Theme.MONO_FONT, 11, QFont.Weight.Bold))
        detail_header.addWidget(self.detail_title)

        detail_header.addStretch()

        self.copy_snippet_btn = QPushButton("Copy Agent Snippet")
        self.copy_snippet_btn.setFont(QFont(Theme.MONO_FONT, 9))
        self.copy_snippet_btn.setProperty("variant", "primary")
        self.copy_snippet_btn.clicked.connect(self._copy_agent_snippet)
        self.copy_snippet_btn.setEnabled(False)
        detail_header.addWidget(self.copy_snippet_btn)

        self.copy_url_btn = QPushButton("Copy URL")
        self.copy_url_btn.setFont(QFont(Theme.MONO_FONT, 9))
        self.copy_url_btn.clicked.connect(self._copy_selected_url)
        self.copy_url_btn.setEnabled(False)
        detail_header.addWidget(self.copy_url_btn)

        detail_layout.addLayout(detail_header)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont(Theme.MONO_FONT, 9))
        self.detail_text.setMinimumHeight(120)
        detail_layout.addWidget(self.detail_text)

        splitter.addWidget(detail_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

        # --- Status bar ---
        status_row = QHBoxLayout()

        self.status_label = QLabel("No data loaded")
        self.status_label.setFont(QFont(Theme.MONO_FONT, 8))
        self.status_label.setProperty("role", "muted")
        status_row.addWidget(self.status_label)

        status_row.addStretch()

        market_link = QLabel(
            f'<a href="{MARKET_URL}">agentic.market</a>'
        )
        market_link.setFont(QFont(Theme.MONO_FONT, 8))
        market_link.setOpenExternalLinks(True)
        status_row.addWidget(market_link)

        layout.addLayout(status_row)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def fetch_services(self):
        """Fetch services from Agentic.Market API in background thread."""
        if self._fetch_thread and self._fetch_thread.isRunning():
            return

        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Loading...")
        self.status_label.setText("Fetching services from agentic.market...")

        self._fetch_thread = MarketFetchThread()
        self._fetch_thread.finished.connect(self._on_fetch_complete)
        self._fetch_thread.start()

    def _on_fetch_complete(self, services: list, error: str):
        """Handle fetch completion."""
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Refresh")

        if error:
            self.status_label.setText(f"Error: {error}")
            self.status_label.setProperty("role", "error")
            self.activity.emit(f"Market fetch failed: {error}", True)
            return

        self._services = services
        self._last_fetch = datetime.now(timezone.utc)

        # Extract categories
        cats = sorted(set(s.get("category", "Other") for s in services))
        current_cat = self.category_combo.currentText()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All Categories")
        for cat in cats:
            self.category_combo.addItem(cat)
        # Restore previous selection if still valid
        idx = self.category_combo.findText(current_cat)
        if idx >= 0:
            self.category_combo.setCurrentIndex(idx)
        self.category_combo.blockSignals(False)

        self._apply_filters()

        self.status_label.setProperty("role", "muted")
        self.status_label.setText(
            f"{len(services)} services · Last updated: {self._last_fetch.strftime('%H:%M:%S')}"
        )
        self.activity.emit(f"Market: loaded {len(services)} services from agentic.market", False)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _apply_filters(self):
        """Filter and display services based on search text and category."""
        query = self.search_input.text().strip().lower()
        category = self.category_combo.currentText()

        filtered = []
        for svc in self._services:
            # Category filter
            if category != "All Categories" and svc.get("category", "") != category:
                continue

            # Text search — match against name, description, provider, domain
            if query:
                searchable = " ".join([
                    svc.get("name", ""),
                    svc.get("description", ""),
                    svc.get("provider", ""),
                    svc.get("domain", ""),
                ]).lower()
                if query not in searchable:
                    continue

            filtered.append(svc)

        self._populate_table(filtered)

    def _populate_table(self, services: list):
        """Fill the table with filtered services."""
        self.table.setRowCount(len(services))

        for row, svc in enumerate(services):
            # Store the full service dict on the first column item
            name_item = QTableWidgetItem(svc.get("name", "Unknown"))
            name_item.setData(Qt.ItemDataRole.UserRole, svc)
            name_item.setForeground(QColor(active()["text"]))
            name_item.setFont(QFont(Theme.MONO_FONT, 10, QFont.Weight.Bold))
            self.table.setItem(row, 0, name_item)

            cat_item = QTableWidgetItem(svc.get("category", "—"))
            cat_item.setForeground(QColor(active()["muted"]))
            self.table.setItem(row, 1, cat_item)

            provider = svc.get("provider", svc.get("domain", "—"))
            self.table.setItem(row, 2, QTableWidgetItem(provider))

            endpoints = svc.get("endpoints", [])
            self.table.setItem(row, 3, QTableWidgetItem(str(len(endpoints))))

            # Price range
            prices = []
            for ep in endpoints:
                amt = ep.get("pricing", {}).get("amount", "")
                if amt:
                    try:
                        prices.append(float(amt))
                    except ValueError:
                        pass

            if prices:
                lo, hi = min(prices), max(prices)
                if lo == hi:
                    price_str = f"${lo:.4f}"
                else:
                    price_str = f"${lo:.4f} – ${hi:.4f}"
            else:
                price_str = "—"
            self.table.setItem(row, 4, QTableWidgetItem(price_str))

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _on_service_selected(self, current, previous):
        """Handle service row selection — show detail panel."""
        if current is None:
            self._selected_service = None
            self.detail_title.setText("Select a service to view details")
            self.detail_text.clear()
            self.copy_snippet_btn.setEnabled(False)
            self.copy_url_btn.setEnabled(False)
            return

        row = current.row()
        item = self.table.item(row, 0)
        if item is None:
            return

        svc = item.data(Qt.ItemDataRole.UserRole)
        if svc is None:
            return

        self._selected_service = svc
        self.copy_snippet_btn.setEnabled(True)
        self.copy_url_btn.setEnabled(True)

        self.detail_title.setText(svc.get("name", "Unknown"))
        self._render_detail(svc)

    # Endpoint HTTP method -> palette token for its badge color.
    _METHOD_TOKEN = {"GET": "success", "POST": "warn"}

    def _render_detail(self, svc: dict):
        """Render the selected service's detail HTML from the active palette."""
        desc = svc.get("description", "")
        domain = svc.get("domain", "")
        provider = svc.get("provider", "")
        networks = ", ".join(svc.get("networks", []))

        lines = [colored_span(desc, "muted")]

        if provider and provider != domain:
            meta = f"Provider: {provider} · Domain: {domain} · Networks: {networks}"
        else:
            meta = f"Domain: {domain} · Networks: {networks}"
        lines.append(colored_span(meta, "muted"))
        lines.append("")

        p = active()
        for ep in svc.get("endpoints", []):
            method = ep.get("method", "GET")
            url = ep.get("url", "")
            ep_desc = ep.get("description", "")
            amount = ep.get("pricing", {}).get("amount", "")
            price_str = f"${float(amount):.4f} USDC" if amount else "free / unlisted"

            method_hex = p[self._METHOD_TOKEN.get(method, "error")]
            lines.append(
                f'<span style="color: {method_hex}; font-weight: bold;">{method}</span> '
                + colored_span(url, "text")
            )
            lines.append("    " + colored_span(f"{ep_desc} · {price_str}", "dim"))

        self.detail_text.setHtml("<br>".join(lines))

    def refresh_theme(self):
        """Repaint palette-driven content (table foregrounds, detail HTML) after
        a theme switch. QSS handles the rest automatically."""
        p = active()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            if name_item is not None:
                name_item.setForeground(QColor(p["text"]))
            cat_item = self.table.item(row, 1)
            if cat_item is not None:
                cat_item.setForeground(QColor(p["muted"]))
        if self._selected_service:
            self._render_detail(self._selected_service)

    # ------------------------------------------------------------------
    # Copy functions
    # ------------------------------------------------------------------

    def _copy_agent_snippet(self):
        """Copy a ready-to-paste agent instruction snippet for the selected service."""
        svc = self._selected_service
        if not svc:
            return

        name = svc.get("name", "Unknown")
        desc = svc.get("description", "")
        endpoints = svc.get("endpoints", [])

        lines = [
            f"## {name}",
            f"{desc}",
            "",
            "Available x402 endpoints (pay per request in USDC on Base):",
            "",
        ]

        for ep in endpoints:
            method = ep.get("method", "GET")
            url = ep.get("url", "")
            ep_desc = ep.get("description", "")
            amount = ep.get("pricing", {}).get("amount", "")
            price = f"${float(amount):.4f}" if amount else "unlisted"

            lines.append(f"- {method} {url}")
            lines.append(f"  {ep_desc} ({price} USDC)")

        lines.append("")
        lines.append("Use the x402 payment protocol. When you receive HTTP 402, "
                      "sign the payment via Vault at localhost:4663/sign and retry with the PAYMENT-SIGNATURE header.")

        snippet = "\n".join(lines)
        QApplication.clipboard().setText(snippet)
        self.activity.emit(f"Copied agent snippet for {name}", False)

    def _copy_selected_url(self):
        """Copy just the first endpoint URL."""
        svc = self._selected_service
        if not svc:
            return

        endpoints = svc.get("endpoints", [])
        if endpoints:
            url = endpoints[0].get("url", "")
            QApplication.clipboard().setText(url)
            self.activity.emit(f"Copied URL: {url}", False)

    # ------------------------------------------------------------------
    # Tab activation
    # ------------------------------------------------------------------

    def showEvent(self, event):
        """Auto-fetch when tab is shown for the first time."""
        super().showEvent(event)
        if not self._services and not (self._fetch_thread and self._fetch_thread.isRunning()):
            QTimer.singleShot(100, self.fetch_services)
