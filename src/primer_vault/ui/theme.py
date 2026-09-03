"""
Primer Design System - Centralized theming for PyQt6 applications.

Architecture:
    1. Token palettes (LIGHT/DARK) define all colors in one place
    2. build_qss() generates the complete stylesheet from a palette
    3. Widgets use semantic properties (role="muted", variant="primary")
    4. set_role() updates properties at runtime with automatic re-polish

Usage:
    # At app startup:
    app.setStyleSheet(build_qss(DARK))

    # On widgets:
    label.setProperty("role", "muted")
    button.setProperty("variant", "primary")

    # At runtime when state changes:
    set_role(status_dot, status="on")
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFrame,
    QPushButton, QLabel, QWidget
)
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QFont, QMouseEvent

# Asset paths for stylesheets
_ASSETS_DIR = Path(__file__).parent / "assets"
_DROPDOWN_ARROW = (_ASSETS_DIR / "dropdown-arrow.svg").as_posix()


# Pure color data + status/web helpers live in the Qt-free design_tokens
# module so the services layer can share them. Re-exported here so UI code
# keeps importing them from `.theme`.
from ..design_tokens import Theme, LIGHT, DARK, CONSOLE, active, set_active, status_token, status_color, colored_span  # noqa: F401 - re-exported for the ui package


# =============================================================================
# QSS Generation - organized by component category
# =============================================================================

def _base_styles(p: dict, font: str) -> str:
    """Core widget defaults and window backgrounds."""
    return f"""
    QWidget {{ font-family: "{font}"; color: {p['text']}; font-size: 12px; }}
    QMainWindow {{ background: {p['bg']}; }}
    QFrame#windowFrame {{ background: {p['bg']}; border: 1px solid {p['line2']}; }}
    QDialog {{ background: {p['bg']}; }}
    QStatusBar {{ background: {p['raised']}; color: {p['muted']}; border-top: 1px solid {p['line']}; }}
    QToolTip {{ background: {p['raised']}; color: {p['text']}; border: 1px solid {p['line2']}; padding: 4px 6px; }}
    """


def _menu_styles(p: dict) -> str:
    """Menu bar and dropdown menus."""
    return f"""
    QMenuBar {{ background: {p['raised']}; color: {p['muted']}; border-bottom: 1px solid {p['line']}; }}
    QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
    QMenuBar::item:selected {{ color: {p['text']}; background: {p['alt']}; }}
    QMenu {{ background: {p['bg']}; color: {p['text']}; border: 1px solid {p['line2']}; }}
    QMenu::item {{ padding: 6px 16px; }}
    QMenu::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
    """


def _tab_styles(p: dict) -> str:
    """Tab widget and tab bar."""
    return f"""
    QTabWidget::pane {{ border: none; background: {p['bg']}; }}
    QTabBar {{ background: {p['bg']}; }}
    QTabBar::tab {{
        background: transparent; color: {p['muted']}; padding: 8px 16px;
        border: none; border-bottom: 2px solid transparent; margin-right: 2px;
    }}
    QTabBar::tab:hover {{ color: {p['text']}; }}
    QTabBar::tab:selected {{ color: {p['text']}; border-bottom: 2px solid {p['accent']}; background: {p['raised']}; }}
    """


def _button_styles(p: dict) -> str:
    """Button variants: default (ghost), primary, danger, segment."""
    return f"""
    /* Default: ghost style */
    QPushButton {{
        background: transparent; color: {p['accent']}; border: 1px solid {p['line2']};
        border-radius: 4px; padding: 6px 12px;
    }}
    QPushButton:hover {{ border-color: {p['accent']}; background: {p['accent_soft']}; }}
    QPushButton:pressed {{ background: {p['alt']}; }}
    QPushButton:disabled {{ color: {p['dim']}; border-color: {p['line']}; }}

    /* Primary: solid accent */
    QPushButton[variant="primary"] {{
        background: {p['accent']}; color: {p['bg']}; border-color: {p['accent']}; font-weight: bold;
    }}
    QPushButton[variant="primary"]:hover {{ background: {p['accent_dim']}; border-color: {p['accent_dim']}; }}

    /* Danger: solid warn/error */
    QPushButton[variant="danger"] {{
        background: {p['warn']}; color: #ffffff; border-color: {p['warn']}; font-weight: bold;
    }}
    QPushButton[variant="danger"]:hover {{ background: {p['error']}; border-color: {p['error']}; }}

    /* Danger ghost: outline only */
    QPushButton[variant="danger-ghost"] {{ background: transparent; color: {p['error']}; border: 1px solid {p['error']}; }}
    QPushButton[variant="danger-ghost"]:hover {{ background: {p['error']}; color: {p['bg']}; }}

    /* Danger icon: for small icon-only buttons (no border) */
    QPushButton[variant="danger-icon"] {{ background: transparent; color: {p['muted']}; border: none; }}
    QPushButton[variant="danger-icon"]:hover {{ color: {p['error']}; }}

    /* Segment: toggle button group */
    QPushButton[segment="true"] {{ border-radius: 0; padding: 5px 18px; color: {p['muted']}; }}
    QPushButton[segment="true"]:hover {{ color: {p['text']}; background: {p['accent_soft']}; }}
    QPushButton[segment="true"]:checked {{ background: {p['accent']}; color: {p['bg']}; border-color: {p['accent']}; }}

    /* Compact: small buttons for table actions */
    QPushButton[compact="true"] {{ padding: 2px 4px; }}

    /* Sort toggle: small text buttons; active column is bold */
    QPushButton[sort="true"] {{ font-size: 10px; }}
    QPushButton[sort="true"][active-sort="true"] {{ font-weight: bold; }}
    """


def _input_styles(p: dict) -> str:
    """Text inputs, combo boxes, spin boxes."""
    return f"""
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
        background: {p['input_bg']}; color: {p['text']}; border: 1px solid {p['line2']};
        border-radius: 4px; padding: 5px 8px;
        selection-background-color: {p['accent_soft']}; selection-color: {p['text']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {{
        border-color: {p['accent']};
    }}
    /* Read-only fields: muted text on the alternate surface (theme-aware) */
    QLineEdit:read-only, QPlainTextEdit:read-only {{
        color: {p['muted']}; background: {p['alt']};
    }}
    QComboBox {{
        padding-right: 24px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 20px;
        border: none;
    }}
    QComboBox::down-arrow {{
        image: url("{_DROPDOWN_ARROW}");
        width: 10px;
        height: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {p['bg']}; color: {p['text']}; border: 1px solid {p['line2']};
        selection-background-color: {p['accent_soft']}; selection-color: {p['text']};
    }}
    """


def _container_styles(p: dict) -> str:
    """Group boxes, tables, lists."""
    return f"""
    /* Group boxes */
    QGroupBox {{
        border: 1px solid {p['line']}; border-radius: 4px; margin-top: 10px; background: {p['raised']};
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p['accent']}; }}

    /* Tables */
    QTableView, QTableWidget {{
        background: {p['bg']}; alternate-background-color: {p['alt']};
        color: {p['text']}; gridline-color: {p['line']}; border: 1px solid {p['line']};
    }}
    QTableView::item {{ padding: 4px 6px; }}
    QTableView::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
    QHeaderView {{ background: {p['raised']}; }}
    QHeaderView::section {{
        background: {p['raised']}; color: {p['muted']}; border: none;
        border-bottom: 1px solid {p['line']}; border-right: 1px solid {p['line']}; padding: 6px 8px;
    }}
    QTableCornerButton::section {{ background: {p['raised']}; border: none; border-bottom: 1px solid {p['line']}; }}

    /* Lists */
    QListWidget {{
        background: {p['bg']}; alternate-background-color: {p['alt']};
        color: {p['text']}; border: 1px solid {p['line']}; border-radius: 4px;
    }}
    QListWidget::item {{ padding: 4px 6px; }}
    QListWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
    QListWidget::item:hover {{ background: {p['alt']}; }}
    """


def _checkbox_styles(p: dict) -> str:
    """Checkboxes and radio buttons."""
    return f"""
    QCheckBox, QRadioButton {{ color: {p['text']}; spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px; height: 14px; border: 1px solid {p['line2']}; background: {p['input_bg']};
    }}
    QCheckBox::indicator {{ border-radius: 2px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {p['accent']}; border-color: {p['accent']};
    }}
    """


def _scrollbar_styles(p: dict) -> str:
    """Minimal scrollbar and splitter styling."""
    return f"""
    QScrollBar:vertical {{ background: {p['alt']}; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {p['line2']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
    QScrollBar:horizontal {{ background: {p['alt']}; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: {p['line2']}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Splitter handles - subtle divider line */
    QSplitter::handle {{ background: {p['line']}; }}
    QSplitter::handle:vertical {{ height: 1px; margin: 4px 0; }}
    QSplitter::handle:horizontal {{ width: 1px; margin: 0 4px; }}
    QSplitter::handle:hover {{ background: {p['line2']}; }}
    """


def _label_roles(p: dict, font: str) -> str:
    """Semantic label roles, plus the wallet address chip and its dropdown."""
    return f"""
    QLabel[role="muted"]   {{ color: {p['muted']}; }}
    QLabel[role="hint"]    {{ color: {p['muted']}; font-size: 11px; font-style: italic; }}
    QLabel[role="dim"]     {{ color: {p['dim']}; }}
    QLabel[role="error"]   {{ color: {p['error']}; }}
    QLabel[role="warn"]    {{ color: {p['warn']}; }}
    QLabel[role="success"] {{ color: {p['success']}; font-weight: bold; }}
    QLabel[role="accent"]  {{ color: {p['accent']}; }}
    QLabel[role="strong"]  {{ font-weight: bold; }}
    QLabel[role="mono"]    {{ color: {p['muted']}; font-size: 10px; }}
    QLabel[role="total"]   {{ color: {p['accent']}; font-weight: bold; font-size: 16px; }}
    QLabel[role="empty"]   {{ color: {p['dim']}; font-size: 14px; padding: 40px; }}
    QLabel[role="icon-lg"] {{ font-size: 48px; }}

    /* Message-box icon dots (color driven by dialog severity) */
    QLabel[icon="question"] {{ color: {p['accent']}; }}
    QLabel[icon="warning"]  {{ color: {p['warn']}; }}
    QLabel[icon="critical"] {{ color: {p['error']}; }}
    QLabel[icon="info"]     {{ color: {p['accent_dim']}; }}

    /* Runtime status (use set_role() to update). One rule per palette token
       that status_token() can return, so status labels color themselves. */
    QLabel[status="on"]      {{ color: {p['on']}; }}
    QLabel[status="off"]     {{ color: {p['off']}; }}
    QLabel[status="warn"]    {{ color: {p['warn_dot']}; }}
    QLabel[status="success"] {{ color: {p['success']}; }}
    QLabel[status="error"]   {{ color: {p['error']}; }}
    QLabel[status="muted"]   {{ color: {p['muted']}; }}
    QLabel[status="info"]    {{ color: {p['info']}; }}
    QLabel[status="pending"] {{ color: {p['pending']}; }}
    QLabel[status="accent"]  {{ color: {p['accent']}; }}

    /* Section toggle checkboxes (bold, used as lane headers in dialogs) */
    QCheckBox[role="section-toggle"] {{ font-weight: bold; }}

    /* The wallet's address chip and the list it drops down.
       Styled to match QComboBox exactly - same input background, border and
       arrow - so it reads as the same kind of control as every other dropdown
       in the app, not a bespoke one. */
    QToolButton[role="address-chip"] {{
        font-family: "{font}";
        font-size: 11px;
        color: {p['text']};
        background: {p['input_bg']};
        border: 1px solid {p['line2']};
        border-radius: 4px;
        padding: 5px 8px;
        text-align: left;
    }}
    QToolButton[role="address-chip"]:hover {{ border-color: {p['accent']}; }}
    QToolButton[role="address-chip"]:disabled {{ color: {p['dim']}; }}

    /* The menu button beside the chip: same height and border so the pair
       reads as one control rather than a control and a stray button. */
    QToolButton[role="chip-action"] {{
        color: {p['muted']};
        background: {p['raised']};
        border: 1px solid {p['line2']};
        border-radius: 4px;
        padding: 4px 8px;
        font-size: 12px;
    }}
    QToolButton[role="chip-action"]:hover {{
        color: {p['text']};
        border-color: {p['accent']};
    }}
    QToolButton[role="chip-action"]:disabled {{ color: {p['dim']}; }}

    /* A bare glyph that sits inline with text - no chrome until hovered. */
    QToolButton[role="icon-button"] {{
        color: {p['muted']};
        background: transparent;
        border: none;
        padding: 0px 4px;
        font-size: 13px;
    }}
    QToolButton[role="icon-button"]:hover {{ color: {p['accent']}; }}

    QFrame[role="dropdown"] {{
        background: {p['bg']};
        border: 1px solid {p['line2']};
    }}
    QListWidget[role="address-list"] {{
        background: {p['bg']};
        color: {p['text']};
        border: none;
    }}
    QListWidget[role="address-list"]::item {{
        padding: 4px 6px;
    }}
    QListWidget[role="address-list"]::item:selected {{
        background: {p['accent_soft']};
        color: {p['text']};
    }}
    QListWidget[role="address-list"]::item:hover {{ background: {p['alt']}; }}
    """


def _special_containers(p: dict, font: str) -> str:
    """Seed boxes, warning boxes, danger zones."""
    return f"""
    QLabel#seedBox, QTextEdit#seedBox {{
        background: {p['seed_bg']}; color: {p['text']};
        border: 1px solid {p['seed_border']}; border-radius: 4px;
        padding: 12px; font-family: "{font}";
    }}
    QFrame#warningBox, QLabel#warningBox, QGroupBox#warningBox {{
        background: {p['warnbox_bg']}; color: {p['warnbox_text']};
        border: 1px solid {p['warnbox_border']}; border-radius: 4px; padding: 10px;
    }}
    QGroupBox#warningBox QLabel {{ background: transparent; color: {p['warnbox_text']}; }}
    /* Collapsed keeps the box's reserved space but renders it invisible. */
    QLabel#warningBox[collapsed="true"] {{
        background: transparent; color: transparent; border-color: transparent;
    }}
    QGroupBox#dangerZone {{ border-color: {p['warn']}; }}
    QGroupBox#dangerZone::title {{ color: {p['warn']}; }}
    """


def _titlebar_styles(p: dict, is_dark: bool) -> str:
    """Custom frameless title bar with menu buttons and window controls."""
    # Use theme-appropriate hover colors
    hover_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "rgba(0, 0, 0, 0.06)"
    pressed_bg = "rgba(255, 255, 255, 0.12)" if is_dark else "rgba(0, 0, 0, 0.10)"
    ctrl_hover = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.08)"

    return f"""
    /* Title bar container */
    QFrame#titleBar {{
        background: {p['raised']};
        border-bottom: 1px solid {p['line']};
    }}

    /* Menu buttons (File, Agents, Settings, Help) */
    QPushButton[variant="menu"] {{
        background: transparent;
        color: {p['muted']};
        border: none;
        padding: 8px 12px;
        border-radius: 0;
    }}
    QPushButton[variant="menu"]:hover {{
        color: {p['text']};
        background: {hover_bg};
    }}
    QPushButton[variant="menu"]:pressed {{
        background: {pressed_bg};
    }}

    /* Tagline in the status bar */
    QStatusBar QLabel[role="tagline"] {{
        color: {p['muted']};
        font-style: italic;
        font-size: 11px;
        padding-right: 8px;
    }}

    /* Window control buttons base */
    #winControlMin, #winControlMax, #winControlClose {{
        background: transparent;
        color: {p['muted']};
        border: none;
        border-radius: 0;
    }}
    #winControlMin:hover, #winControlMax:hover {{
        background: {ctrl_hover};
        color: {p['text']};
    }}

    /* Close button - red on hover */
    #winControlClose:hover {{
        background: {p['close']};
        color: #ffffff;
    }}
    #winControlClose:pressed {{
        background: {p['close_pressed']};
    }}

    /* Dialog frame and title bar */
    QFrame#dialogFrame {{
        background: {p['bg']};
        border: 1px solid {p['line2']};
    }}
    QFrame#dialogTitleBar {{
        background: {p['raised']};
        border-bottom: 1px solid {p['line']};
    }}
    QFrame#dialogTitleBar QLabel {{
        color: {p['text']};
        background: transparent;
    }}
    #dialogClose {{
        background: transparent;
        color: {p['muted']};
        border: none;
        border-radius: 0;
    }}
    #dialogClose:hover {{
        background: {p['close']};
        color: #ffffff;
    }}
    #dialogClose:pressed {{
        background: {p['close_pressed']};
    }}
    """


def _dark_islands(p: dict, font: str, is_dark: bool) -> str:
    """Header and terminal styling - header follows theme, terminal stays dark."""
    # Header now follows theme colors
    header_bg = p['raised']
    header_text = p['text']
    activity_text = p['accent_dim'] if is_dark else p['accent']

    return f"""
    QWidget#header {{ background: {header_bg}; border-bottom: 1px solid {p['line']}; }}
    QWidget#header QLabel {{ color: {header_text}; background: transparent; }}
    QLabel#logo {{ color: {p['accent']}; font-weight: bold; font-size: 16px; }}
    QWidget#header QLabel[status="on"]  {{ color: {p['on']}; }}
    QWidget#header QLabel[status="off"] {{ color: {p['off']}; }}
    QWidget#header QTextEdit {{ background: transparent; border: none; color: {activity_text}; }}

    #terminal, QTextEdit#terminal, QPlainTextEdit#terminal {{
        background: {p['term_bg']}; color: {p['term_text']}; border: none;
        padding: 12px; font-family: "{font}"; selection-background-color: {p['accent_dim']};
    }}
    """


def _console_styles(c: dict, font: str) -> str:
    """Console dialog - a deliberate dark island, identical in every theme.

    Colors come from the CONSOLE constant set, not the active palette, so the
    terminal look is stable regardless of the app theme.
    """
    return f"""
    QTextEdit#console {{
        background-color: {c['bg']}; color: {c['text']};
        border: none; padding: 12px; font-family: "{font}";
        selection-background-color: {c['dim']};
    }}
    QWidget#consoleInputRow {{ background-color: {c['input_bg']}; }}
    QLabel#consolePrompt {{
        background-color: {c['input_bg']}; color: {c['text']};
        border: none; padding: 8px 0px 8px 12px; font-family: "{font}";
    }}
    QLabel#consolePrompt[mode="password"] {{ color: {c['error']}; }}
    QLineEdit#consoleInput {{
        background-color: {c['input_bg']}; color: {c['text']};
        border: none; border-top: 1px solid {c['input_bg']};
        padding: 8px 12px 8px 4px; font-family: "{font}";
        selection-background-color: {c['dim']};
    }}
    QLineEdit#consoleInput[mode="password"] {{
        color: {c['error']}; border-top: 1px solid {c['error']};
    }}
    """


def build_qss(palette: dict) -> str:
    """Generate the complete application stylesheet from a color palette.

    Args:
        palette: Either LIGHT or DARK palette dict.

    Returns:
        Complete QSS string to pass to QApplication.setStyleSheet().
    """
    font = Theme.MONO_FONT
    is_dark = palette is DARK

    return "".join([
        _base_styles(palette, font),
        _menu_styles(palette),
        _tab_styles(palette),
        _button_styles(palette),
        _input_styles(palette),
        _container_styles(palette),
        _checkbox_styles(palette),
        _scrollbar_styles(palette),
        _label_roles(palette, font),
        _special_containers(palette, font),
        _titlebar_styles(palette, is_dark),
        _dark_islands(palette, font, is_dark),
        _console_styles(CONSOLE, font),
    ])


def build_light_qss() -> str:
    """Convenience wrapper for the default (light) stylesheet."""
    return build_qss(LIGHT)


# =============================================================================
# Runtime Helpers
# =============================================================================

def set_role(widget, **props) -> None:
    """Set semantic properties on a widget and re-apply styles.

    Use this when a property changes at runtime (e.g., status indicator
    switching from "on" to "off"). At widget creation, just use
    widget.setProperty() directly.

    Example:
        set_role(status_label, status="on")
        set_role(error_label, role="error")
    """
    for key, value in props.items():
        widget.setProperty(key, value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


def refresh_theme_tree(root) -> None:
    """Call refresh_theme() on `root` and every descendant that implements it.

    QSS restyles automatically on setStyleSheet(), but QColor() item foregrounds
    and HTML span colors do not. Views that hold such content implement
    refresh_theme() to repaint from the now-active() palette; apply_theme() calls
    this after swapping the stylesheet.
    """
    from PyQt6.QtWidgets import QWidget

    # findChildren() already recurses the whole subtree, so root + its
    # descendants covers every widget once.
    for widget in [root, *root.findChildren(QWidget)]:
        refresh = getattr(widget, "refresh_theme", None)
        if callable(refresh):
            refresh()


# =============================================================================
# Frameless Dialog Base Class
# =============================================================================

class FramelessDialog(QDialog):
    """Base class for frameless dialogs with custom title bar.

    Provides:
    - Frameless window with DWM shadow (Windows)
    - Custom title bar with title and close button
    - Window dragging via title bar
    - Content area for subclass widgets

    Usage:
        class MyDialog(FramelessDialog):
            def __init__(self, parent=None):
                super().__init__("My Dialog Title", parent)
                # Add widgets to self.content_layout
                self.content_layout.addWidget(QLabel("Hello"))
    """

    def __init__(self, title: str, parent=None, width: int = 400):
        super().__init__(parent)
        self._drag_pos: QPoint | None = None

        # Frameless setup
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMinimumWidth(width)

        # Enable DWM shadow
        self._enable_dwm_shadow()

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Border frame
        self._frame = QFrame()
        self._frame.setObjectName("dialogFrame")
        self._frame.setFrameShape(QFrame.Shape.Box)
        main_layout.addWidget(self._frame)

        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        # Title bar
        title_bar = QFrame()
        title_bar.setObjectName("dialogTitleBar")
        title_bar.setFixedHeight(32)
        frame_layout.addWidget(title_bar)

        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(12, 0, 4, 0)
        title_layout.setSpacing(0)

        # Title text
        self._title_label = QLabel(title)
        self._title_label.setFont(QFont(Theme.MONO_FONT, 10))
        title_layout.addWidget(self._title_label)

        title_layout.addStretch()

        # Close button
        close_btn = QPushButton("✕")
        close_btn.setObjectName("dialogClose")
        close_btn.setFixedSize(32, 32)
        close_btn.setFont(QFont(Theme.MONO_FONT, 10))
        # Don't let the close button become the dialog's default button, or
        # pressing Enter anywhere (e.g. the console input) would trigger it
        # and reject() the dialog.
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.reject)
        title_layout.addWidget(close_btn)

        # Content area
        content_widget = QWidget()
        frame_layout.addWidget(content_widget)

        self.content_layout = QVBoxLayout(content_widget)
        self.content_layout.setContentsMargins(16, 12, 16, 16)
        self.content_layout.setSpacing(8)

    def setDialogTitle(self, title: str):
        """Update the dialog title."""
        self._title_label.setText(title)

    def _enable_dwm_shadow(self):
        """Enable Windows DWM drop shadow."""
        import sys
        if sys.platform != 'win32':
            return

        try:
            import ctypes

            hwnd = int(self.winId())

            DWMWA_NCRENDERING_POLICY = 2
            DWMNCRP_ENABLED = 2

            class MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]

            dwmapi = ctypes.windll.dwmapi

            policy = ctypes.c_int(DWMNCRP_ENABLED)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_NCRENDERING_POLICY,
                ctypes.byref(policy), ctypes.sizeof(policy)
            )

            margins = MARGINS(1, 1, 1, 1)
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))

        except Exception:
            pass

    def mousePressEvent(self, event: QMouseEvent):
        """Start dragging if clicking on title bar."""
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= 32:
                self._drag_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        """Handle window dragging."""
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            diff = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + diff)
            self._drag_pos = event.globalPosition().toPoint()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """End dragging."""
        self._drag_pos = None
        super().mouseReleaseEvent(event)


# =============================================================================
# Frameless Message Box
# =============================================================================

class FramelessMessageBox(FramelessDialog):
    """Styled message box that matches FramelessDialog appearance.

    Replaces QMessageBox with consistent frameless styling.

    Usage:
        # Question dialog
        if FramelessMessageBox.question(parent, "Confirm", "Delete this item?"):
            do_delete()

        # Warning dialog
        FramelessMessageBox.warning(parent, "Warning", "This cannot be undone.")

        # Info dialog
        FramelessMessageBox.information(parent, "Success", "Operation completed.")

        # Critical dialog
        FramelessMessageBox.critical(parent, "Error", "Something went wrong.")
    """

    # Result codes
    Yes = 1
    No = 0

    def __init__(self, title: str, message: str, buttons: list[str], parent=None,
                 default_button: int = 0, icon_type: str = "info"):
        super().__init__(title, parent, width=350)
        self.setModal(True)
        self._result = None

        layout = self.content_layout
        layout.setSpacing(12)

        # Message with optional icon indicator
        msg_layout = QHBoxLayout()
        msg_layout.setSpacing(12)

        # Icon indicator (colored circle)
        if icon_type in ("question", "warning", "critical", "info"):
            icon_label = QLabel("●")
            icon_label.setFont(QFont(Theme.MONO_FONT, 16))
            # Color comes from the QLabel[icon="..."] roles in _label_roles.
            icon_label.setProperty("icon", icon_type)
            msg_layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        # Message text
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setMinimumWidth(250)
        msg_layout.addWidget(msg_label, stretch=1)

        layout.addLayout(msg_layout)
        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._buttons = []
        for i, btn_text in enumerate(buttons):
            btn = QPushButton(btn_text)
            btn.setFixedWidth(80)
            if i == default_button:
                btn.setDefault(True)
                btn.setFocus()
            btn.clicked.connect(lambda checked, idx=i: self._on_button(idx))
            btn_layout.addWidget(btn)
            self._buttons.append(btn)

        layout.addLayout(btn_layout)

    def _on_button(self, index: int):
        self._result = index
        self.accept()

    def result_index(self) -> int:
        # Return last button index (No/Cancel) if dialog was closed without clicking a button
        return self._result if self._result is not None else len(self._buttons) - 1

    @classmethod
    def question(cls, parent, title: str, message: str, default_no: bool = True) -> bool:
        """Show a Yes/No question. Returns True if Yes clicked."""
        dlg = cls(title, message, ["Yes", "No"], parent,
                  default_button=1 if default_no else 0, icon_type="question")
        dlg.exec()
        return dlg.result_index() == 0  # Yes is index 0

    @classmethod
    def warning(cls, parent, title: str, message: str) -> None:
        """Show a warning dialog with OK button."""
        dlg = cls(title, message, ["OK"], parent, default_button=0, icon_type="warning")
        dlg.exec()

    @classmethod
    def information(cls, parent, title: str, message: str) -> None:
        """Show an info dialog with OK button."""
        dlg = cls(title, message, ["OK"], parent, default_button=0, icon_type="info")
        dlg.exec()

    @classmethod
    def critical(cls, parent, title: str, message: str) -> None:
        """Show a critical error dialog with OK button."""
        dlg = cls(title, message, ["OK"], parent, default_button=0, icon_type="critical")
        dlg.exec()


# =============================================================================
# Dialog Helpers (convenience functions)
# =============================================================================

def ask_question(parent, title: str, message: str, default_no: bool = True) -> bool:
    """Show a Yes/No question dialog. Returns True if Yes clicked."""
    return FramelessMessageBox.question(parent, title, message, default_no)


def show_warning(parent, title: str, message: str) -> None:
    """Show a warning dialog."""
    FramelessMessageBox.warning(parent, title, message)


def show_info(parent, title: str, message: str) -> None:
    """Show an info dialog."""
    FramelessMessageBox.information(parent, title, message)
