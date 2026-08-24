"""The menu paths README.md tells a reader to click.

Whatever the README names after "Settings â†’" has to be something Vault's own
Settings menu actually offers, or the reader is hunting. Paths belonging to
other software â€” macOS System Settings, the Ledger Ethereum app â€” are not
Vault's to get right and are skipped.
"""

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
MAIN_WINDOW = (REPO / "src" / "primer_vault" / "ui" / "main_window.py").read_text(
    encoding="utf-8")

# Lines whose "Settings →" belongs to another program's UI.
NOT_OURS = ("System Settings", "Ethereum app")


def _settings_menu_items() -> list[str]:
    """The action labels _show_settings_menu() puts on the Settings menu."""
    body = re.search(
        r"def _show_settings_menu\(self\):(.*?)(?=\n    def )", MAIN_WINDOW, re.S
    ).group(1)
    return re.findall(r'menu\.addAction\("([^"]+)"', body)


def _readme_settings_paths() -> list[tuple[int, str]]:
    """Every 'Settings → X' in the README that refers to Vault's own window."""
    found = []
    for number, line in enumerate(README.splitlines(), 1):
        if any(other in line for other in NOT_OURS):
            continue
        # A menu entry is the leading run of capitalised words after the arrow;
        # "Settings → Security in the window" names the entry "Security".
        for item in re.findall(r"Settings\s*→\s*\*{0,2}([A-Z][a-z]*(?: [A-Z][a-z]*)*)",
                               line):
            found.append((number, item.strip()))
    return found


def test_the_settings_menu_offers_every_item_the_readme_says_to_click():
    """Each Vault 'Settings → X' in the README must match an entry on the
    Settings menu, allowing for a trailing '...' and for singular/plural.
    """
    items = _settings_menu_items()
    assert items, "could not read the Settings menu - test needs updating"

    def offered(name: str) -> bool:
        wanted = name.rstrip(". ").lower()
        for actual in items:
            actual = actual.rstrip(". ").lower()
            if wanted == actual or wanted.rstrip("s") == actual.rstrip("s"):
                return True
        return False

    paths = _readme_settings_paths()
    assert paths, "README no longer names a Settings path - test needs updating"

    missing = [f"README.md:{n} sends the reader to 'Settings -> {name}'"
               for n, name in paths if not offered(name)]

    assert not missing, (
        "the README sends the reader to a Settings entry the window does not "
        "have. The Settings menu offers " + ", ".join(repr(i) for i in items)
        + ". Not found: " + "; ".join(missing)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
