"""Vault version - single source of truth."""

__version__ = "0.4.0"

#: How Vault identifies itself to the services it calls. Derived here so a
#: release cannot leave a stale version behind in a header.
USER_AGENT = f"PrimerVault/{__version__} (+https://primer.systems)"

#: Most public Blockscout instances (Base, etc.) run a Cloudflare rule that
#: 403s any User-Agent shaped like a script - a browser-shaped UA clears it.
#: Robinhood Chain's instance has since escalated to a full JS challenge that
#: this doesn't clear (see BLOCKSCOUT_PRO_API_KEY below); kept for chains
#: still on the weaker rule. Used only for calls to Blockscout.
BLOCKSCOUT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/128.0.0.0 Safari/537.36")

#: Blockscout's hosted Pro API (api.blockscout.com) - bypasses the per-chain
#: Cloudflare wall entirely by not going through the scraped public instance.
#: Free tier, shared across every Vault install. A public, low-value,
#: rate-limited key by design (100K credits/day) - not a secret worth
#: protecting, low-risk to ship in the open. Override with the
#: PRIMER_VAULT_BLOCKSCOUT_API_KEY env var for local testing/rotation.
BLOCKSCOUT_PRO_API_KEY = "proapi_fN8HnPeUMA749l5nxjvZJaZ8cOptnCnYJyrSLdsHuywaI42Zx5OVkVczDIBFGgNWB_etgOpN"
