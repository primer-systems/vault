"""
Primer Design Tokens - the single, framework-free source of truth for color.

This module holds ONLY pure data and pure-string helpers: the brand constants,
the LIGHT/DARK palettes, the console island palette, the status->token mapping,
and the served-dashboard CSS variables. It imports nothing from Qt, so it can be
used by both the UI layer (ui/theme.py) and the services layer (services/server.py)
without violating the "no Qt outside the UI" architecture rule.

ui/theme.py re-exports everything here, so UI code continues to import color
tokens from `.theme` as before.
"""


# =============================================================================
# Brand Constants (theme-independent)
# =============================================================================


class Theme:
    """Static brand constants - fonts, sizes, and raw hex values.

    For theme-aware colors, use the LIGHT/DARK palettes below.
    These constants are kept for: (1) special cases like syntax highlighting,
    (2) console windows that intentionally ignore theming.
    """

    MONO_FONT = "JetBrains Mono"

    # Raw brand colors (use sparingly - prefer palette tokens)
    LIME = "#baea2a"
    LIME_DIM = "#7a9a1a"
    BLACK = "#09090b"
    BLACK_LIGHT = "#121214"
    CHARCOAL = "#4A4543"
    RUST = "#B7410E"
    WHITE = "#fafafa"

    # Semantic status colors
    SUCCESS = "#22c55e"
    ERROR = "#ef4444"
    WARNING = "#f59e0b"

    # Syntax highlighting (console/help text)
    PLACEHOLDER = "#22d3ee"  # Cyan - <user_input> placeholders
    OPTIONAL = "#a78bfa"     # Purple - [optional] parameters

    # Standard dialog dimensions
    MIN_DIALOG_WIDTH = 350
    MIN_POPUP_WIDTH = 300


# =============================================================================
# Color Palettes
# =============================================================================

LIGHT = {
    # Backgrounds (layered hierarchy)
    "bg": "#faf8f5",
    "raised": "#f6f4ee",
    "alt": "#f4f1ec",
    "input_bg": "#ffffff",

    # Borders
    "line": "#ded9d2",
    "line2": "#cec8c0",

    # Text hierarchy
    "text": "#1c1917",
    "muted": "#78716c",
    "dim": "#a8a29e",

    # Accent
    "accent": "#6f8f16",
    "accent_dim": "#5c7414",
    "accent_soft": "#eef2df",
    "link": "#5c7414",

    # Semantic
    "error": "#c0392b",
    "warn": "#b23410",
    "success": "#3d8b40",
    "info": "#2f6db0",
    "pending": "#b8860b",

    # Window/dialog close-button hover
    "close": "#c42b1c",
    "close_pressed": "#b22a1c",

    # Status indicators
    "on": "#6f8f16",
    "off": "#b23410",
    "warn_dot": "#c98a00",

    # Dark islands (header/terminal stay dark in both themes)
    "header_bg": "#09090b",
    "header_text": "#fafafa",
    "header_dim": "#8f8b88",
    "term_bg": "#000000",
    "term_text": "#7a9a1a",
    "term_dim": "#4A4543",

    # Special containers
    "seed_bg": "#fbf7df",
    "seed_border": "#e3d98a",
    "warnbox_bg": "#fdf1e7",
    "warnbox_border": "#e8c3a4",
    "warnbox_text": "#8a4b1a",
}

DARK = {
    # Backgrounds
    "bg": "#09090b",
    "raised": "#121214",
    "alt": "#17171c",
    "input_bg": "#1a1a1e",

    # Borders
    "line": "#4A4543",
    "line2": "#5a5553",

    # Text hierarchy
    "text": "#fafafa",
    "muted": "#8f8b88",
    "dim": "#5a5553",

    # Accent
    "accent": "#baea2a",
    "accent_dim": "#7a9a1a",
    "accent_soft": "#17200a",
    "link": "#7a9a1a",

    # Semantic
    "error": "#ef4444",
    "warn": "#cf4a1c",
    "success": "#22c55e",
    "info": "#4a90d9",
    "pending": "#d9a74a",

    # Window/dialog close-button hover
    "close": "#c42b1c",
    "close_pressed": "#b22a1c",

    # Status indicators
    "on": "#baea2a",
    "off": "#cf4a1c",
    "warn_dot": "#f59e0b",

    # Dark islands
    "header_bg": "#09090b",
    "header_text": "#fafafa",
    "header_dim": "#8f8b88",
    "term_bg": "#000000",
    "term_text": "#7a9a1a",
    "term_dim": "#4A4543",

    # Special containers
    "seed_bg": "#1a1a10",
    "seed_border": "#4a4520",
    "warnbox_bg": "#1e120a",
    "warnbox_border": "#5a3a1a",
    "warnbox_text": "#e08a4a",
}


# =============================================================================
# Console Island (theme-independent)
# =============================================================================
#
# The console dialog and the activity-log terminal are deliberately dark in both
# light and dark themes - they emulate a terminal. Their colors therefore do NOT
# come from LIGHT/DARK, but they are still named tokens (never scattered hex) so
# there is a single source of truth for the island.

CONSOLE = {
    "bg":       "#000000",   # output background
    "panel_bg": "#09090b",   # nested panel / detail box background
    "input_bg": "#4a4543",   # input row background
    "text":     "#baea2a",   # default output / lime
    "bright":   "#fafafa",   # echoed user input (near-white)
    "dim":      "#7a9a1a",   # secondary lime
    "muted":    "#888888",   # muted grey text
    "border":   "#4a4543",   # dividers
    "error":    "#e94435",   # errors
    "warn":     "#f59e0b",   # warnings / denials (amber)
    "incoming": "#5eead4",   # incoming activity (teal)
    "approval": "#ffa500",   # approval prompts (amber)
    "prompt":   "#7a9a1a",   # prompt label
}


# =============================================================================
# Runtime Palette State
# =============================================================================
#
# QSS reaches most widgets, but QColor() foregrounds (table/list items) and
# HTML `<span style="color:...">` cannot be styled by a stylesheet. Those read
# the *currently active* palette through active() and are regenerated when the
# theme changes (see MainWindow.apply_theme / refresh_theme_tree).

_ACTIVE: dict = DARK


def active() -> dict:
    """Return the palette currently applied to the app.

    Use for QColor() item foregrounds and HTML span colors - anywhere QSS can't
    reach. apply_theme() keeps this in sync via set_active().
    """
    return _ACTIVE


def set_active(palette: dict) -> None:
    """Record the palette now applied to the app. Called by apply_theme()."""
    global _ACTIVE
    _ACTIVE = palette


# Single source of truth for status -> semantic token. Used by both the Qt UI
# and the served HTML dashboard, so the two surfaces never drift.
_STATUS_TOKEN = {
    "received":  "muted",
    "signed":    "info",
    "submitted": "pending",
    "pending":   "pending",
    "rejected":  "error",
    "settled":   "success",
    "approved":  "success",
    "failed":    "error",
    # An on-chain check Vault could not perform. Not an error: it says nothing
    # about the payment, only that the network could not be reached.
    "unavailable": "pending",
    "not_found":   "error",
    # Payment lifecycle (served dashboard + payment dialogs)
    "payment-completed": "accent",
    "payment-verified":  "success",
    "payment-submitted": "info",
    "payment-required":  "muted",
    "payment-rejected":  "error",
    "payment-failed":    "error",
}


def status_token(status: str) -> str:
    """Resolve a transaction/payment status to its palette token name.

    Use as a QLabel `status` property so QSS colors it (auto theme-adaptive);
    every token this returns has a matching QLabel[status="..."] rule.
    """
    return _STATUS_TOKEN.get(status, "muted")


def status_color(status: str, palette: dict | None = None) -> str:
    """Resolve a transaction/payment status to its themed hex color."""
    p = palette or active()
    return p[status_token(status)]


def colored_span(text: str, token: str, palette: dict | None = None) -> str:
    """Build an HTML span colored from a palette token.

    Theme-aware when the holding widget regenerates it on theme change.
    """
    p = palette or active()
    return f'<span style="color: {p[token]};">{text}</span>'


def web_css_vars(palette: dict | None = None) -> str:
    """CSS custom properties for the served dashboard (services/server.py).

    Sources the web surface's colors from the same palette as the Qt app so the
    two never drift. Defaults to DARK (the served page is always dark).
    """
    p = palette or DARK
    return (
        ":root{"
        f"--bg:{p['bg']};"
        f"--fg:{p['text']};"
        f"--accent:{p['accent']};"
        f"--dim:{p['accent_dim']};"
        f"--line:{p['line']};"
        f"--warn:{p['warn']};"
        f"--rust:{Theme.RUST};"
        "--muted:rgba(250,250,250,0.5);"
        "--accent-tint:rgba(186,234,42,0.03);"
        "--accent-tint-strong:rgba(186,234,42,0.05);"
        "}"
    )
