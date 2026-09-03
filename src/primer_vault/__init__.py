"""Vault - self-custodial execution wallet for AI agents.

Delegate spending authority to agents without sharing private keys.

Deliberately exports nothing but the version. The two editions have separate
composition roots - `app_desktop` and `app_terminal` - and importing either one
from here would put terminal code in the desktop build and Qt-adjacent code in
the terminal build. Whichever edition is installed points its own entry point at
its own root.
"""

from .version import __version__

__all__ = ["__version__"]
