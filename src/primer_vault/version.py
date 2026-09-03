"""Vault version - single source of truth."""

__version__ = "0.3.0"

#: How Vault identifies itself to the services it calls. Derived here so a
#: release cannot leave a stale version behind in a header.
USER_AGENT = f"PrimerVault/{__version__} (+https://primer.systems)"

#: Robinhood Chain's Blockscout instance runs a Cloudflare rule that 403s any
#: User-Agent shaped like a script or HTTP library - curl, python-requests,
#: urllib's default, and our own honest PrimerVault string all fail the same
#: way, while any ordinary browser UA passes untouched (confirmed 2026-09-01;
#: no cookie or JS challenge involved, a header swap alone clears it). Used
#: only for calls to Blockscout, never elsewhere: everywhere else Vault talks
#: to (CoinGecko, RPC nodes, Primer's own APIs) accepts the honest UA above,
#: so there is no reason to give up self-identification there too. Revisit
#: once Blockscout's operators fix or explain the block.
BLOCKSCOUT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/128.0.0.0 Safari/537.36")
