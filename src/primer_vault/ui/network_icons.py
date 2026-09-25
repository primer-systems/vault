"""Network chain marks for the History tab.

Every wallet identifies a chain by its mark rather than its name, because a
mark is recognised at a glance where a name has to be read. History rows carry
one next to the address, which is what let the Network *column* go away without
losing the chain information the multi-network work wanted kept visible.

The marks are the real brand SVGs, in `assets/`, rendered at paint time. A
chain with no artwork falls back to a neutral grey ring, so an unknown or
older-than-the-registry row still reads as "some network" rather than looking
networkless - and, importantly, the column stays aligned.

Artwork contract: each SVG is expected to supply its own background (the RHC
mark is a lime disc with the feather knocked out of it, and Base's is a blue
disc). Nothing here draws a disc behind them - two files with different padding
would otherwise stack badly against a disc sized for one of them. A file that
arrives as a bare glyph on transparent will therefore look unbounded next to
one that brings its own; the fix is to fix the file, not to special-case it
here.
"""

from typing import Optional
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPixmap, QIcon, QColor, QPen
from PyQt6.QtSvg import QSvgRenderer

from ..design_tokens import NETWORK_BRAND_UNKNOWN

_ASSETS_DIR = Path(__file__).parent / "assets"

#: Brand artwork per chain id. A chain absent from this map (or whose file is
#: missing/unparseable) gets the neutral mark instead.
_ART = {
    8453: "Base_chain_icon.svg",
    4663: "RHC_chain_icon.svg",
}

#: Marks are drawn on this nominal grid then scaled to the requested size.
_GRID = 100.0

# Cache keyed by (chain_id, size) - a table repaints constantly while scrolling
# and re-rasterising an identical mark per row per repaint is pure waste.
# Bounded in practice by (networks x call sizes).
_cache: dict[tuple[object, int], QIcon] = {}

# QSvgRenderer parses the file on construction, so these are built once and
# reused rather than per icon size. Keyed by filename; a file that fails to
# parse is remembered as None so a broken asset is not re-read every repaint.
_renderers: dict[str, Optional[QSvgRenderer]] = {}


def _renderer(filename: str) -> Optional[QSvgRenderer]:
    """The parsed renderer for an asset, or None if it is missing or invalid."""
    if filename in _renderers:
        return _renderers[filename]

    path = _ASSETS_DIR / filename
    renderer = None
    if path.exists():
        candidate = QSvgRenderer(str(path))
        # isValid() covers a malformed file; a valid-but-empty viewBox would
        # divide by zero downstream in the aspect-fit maths below.
        if candidate.isValid() and not candidate.viewBoxF().isEmpty():
            renderer = candidate

    _renderers[filename] = renderer
    return renderer


def _neutral_mark(p: QPainter):
    """Unknown chain: a hollow grey ring, deliberately not letter-shaped."""
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(NETWORK_BRAND_UNKNOWN), 9))
    p.drawEllipse(QRectF(30, 30, 40, 40))


def _fit(view: QRectF) -> QRectF:
    """Aspect-fit the artwork's viewBox into the square grid, centred.

    Square artwork maps 1:1. A non-square file is centred rather than stretched
    - a squashed brand mark is worse than a slightly smaller one.
    """
    if view.width() == view.height():
        return QRectF(0, 0, _GRID, _GRID)
    scale = _GRID / max(view.width(), view.height())
    w, h = view.width() * scale, view.height() * scale
    return QRectF((_GRID - w) / 2, (_GRID - h) / 2, w, h)


def network_icon(chain_id: Optional[int], size: int = 16) -> QIcon:
    """The brand mark for `chain_id`, cached per (chain, size).

    `chain_id` of None - an absent or unresolvable network on a transaction -
    gets the neutral mark rather than an empty icon.
    """
    key = (chain_id, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    # Painted at 4x and handed back with a devicePixelRatio, so the mark stays
    # sharp on a HiDPI screen without the caller knowing the ratio.
    scale = 4
    px = QPixmap(size * scale, size * scale)
    px.fill(QColor(0, 0, 0, 0))

    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(size * scale / _GRID, size * scale / _GRID)

    filename = _ART.get(chain_id)
    renderer = _renderer(filename) if filename else None
    if renderer is not None:
        renderer.render(p, _fit(renderer.viewBoxF()))
    else:
        _neutral_mark(p)
    p.end()

    px.setDevicePixelRatio(float(scale))
    icon = QIcon(px)
    _cache[key] = icon
    return icon
