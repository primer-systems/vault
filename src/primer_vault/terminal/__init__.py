"""Terminal edition - the prompt, the control channel, and boot registration.

Ships without Qt. Nothing in the shared tier and nothing in `ui/` may import
from here; this package and `ui/` are the two leaves of the tree, and each is
absent from the other edition's build.
"""
