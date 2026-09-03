"""`python -m primer_vault` runs the terminal edition.

A module invocation comes from a terminal by definition, and the pip package is
the terminal edition. The desktop app is launched from its own binary.
"""

from .app_terminal import main

main()
