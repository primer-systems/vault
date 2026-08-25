# Changelog

## 0.2.1

### Added
- **An agent can read its own address and balances.** `/mandate` now returns the
  agent's `wallet_address` (and `wallet_id`) in its live response, so an agent no
  longer has to infer the address it signs from. A new authenticated
  `POST /balances` returns that address plus its on-chain holdings (native +
  tokens), behind the same credential as `/mandate`, and degrades gracefully if
  the block explorer is unavailable. Read-only; no keys involved.

## 0.2.0

### Added
- **Ledger hardware wallet support.** Derive agent addresses from a connected
  Ledger (Ledger Live, BIP44, Legacy MEW and custom paths), sign x402
  authorizations and DEX trades on-device (including the ERC-20 approval step),
  and verify a stored address on the device. Hardware addresses are badged and
  refuse private-key export; keys never touch the computer
- **Price-impact check on every trade.** Vault probes the agent's chosen pool
  for its true rate and escalates any trade above `max_price_impact_percent`
  (default 5%) — catching a pool too thin to fill, which a slippage check alone
  would pass. The ceiling is user-set; an agent cannot raise it
- **Trading limits published on `/mandate`** — per-trade max, daily volume and
  what is left today, the auto-approve threshold, the slippage and impact
  ceilings, and the ETH floor, alongside the payment limits
- **Stale price feeds escalate.** An ETH/USD reference older than 15 minutes
  makes a trade unvaluable and routes it to manual approval rather than guessing
- **Server hardening.** Both HTTP servers serve connections concurrently, cap
  request bodies (Admin API, 1 MB), and drop a connection idle for 30 seconds;
  finished trade and payment results are capped and aged out
- **Damaged-data resilience.** A corrupt record in `agents.json`,
  `policies.json` or `transactions.json` is skipped (not discarded) and Vault
  starts without it; Vault explains, naming the path, when it cannot write its
  data folder
- Trade execution runs behind a non-cancellable progress dialog; the approval
  dialog states when a trade could not be priced and its limits were not applied

### Fixed
- **Trade history recorded the quote, not the fill.** The received amount is now
  read from the receipt and kept beside the quote; an unreadable fill is shown as
  unread, never replaced by the prediction
- **Hardware-wallet trades could not execute** — calldata handed to the Ledger as
  a hex string is now bytes, so approvals, swaps and unwraps sign
- Agent-API status codes corrected: execution failures are 500 (not 400),
  per-request-max is 403 (not 429), and an unknown id answers `REQUEST_NOT_FOUND`
  on both the sign and trade registries
- A mandate uploaded from the CLI now records its registry id, so
  `agent mandate/commission --upload` produce verifiable mandates
- A dozen documentation corrections across the README, agent skills and endpoint
  reference — data locations, Argon2id memory cost, daily-reset semantics, the
  cost of open Admin-API mode, the `--admin-open`/`--allow-lan`/`admin-api`
  settings, mandate fields and the agent-id format
- Assorted build and asset fixes — the light-theme wordmark and dropdown arrow
  ship in downloaded builds, `requests` is declared for the release build, and
  the CLI banner renders correctly

### Security
Hardening across key-material handling, API authorisation,
displayed-versus-signed integrity, spending-limit enforcement, money arithmetic
and crash/corruption resilience — spanning the wallet at rest, the agent and
admin APIs, the approval dialogs, and the trading and payment paths. Specific
details are withheld while 0.1 remains published; **upgrading is strongly
recommended.**
- Tightened authorisation and information-disclosure handling across the agent
  and admin HTTP APIs, and throttled repeated unlock attempts
- Strengthened enforcement of per-trade, daily and reserve limits across the
  trading and payment paths, including under concurrency
- Hardened the wallet and its data files against interrupted saves, unreadable
  or unwritable files, and secrets left on screen
- Ensured approval prompts show only the values that are actually signed

### Removed
- The unused v1 wallet implementation — 1,420 lines (`Wallet`,
  `PrivateKeyWallet`, `WalletManager`, `WalletIndex`, first-run dialogs) serving
  a format no release ever wrote. `VaultWallet` is the wallet
- Dead code, unused imports and unreachable helpers
- The served pages no longer request a Google webfont their own CSP blocked

### Changed
- Dependencies pinned to tested ranges and capped below the next breaking
  release (`web3` `>=7,<8`); release binaries build from `requirements.lock`
- Tests run on Windows, macOS and Linux (Windows is the primary target), with
  expanded coverage of wallet save/permissions, DEX transaction building, API
  concurrency, and status/route parity
- README and docs list every host Vault contacts and confirm there is no
  telemetry; both data locations are documented
- Release binaries are no longer UPX-compressed — the size saving is not worth
  the antivirus heuristics on an unsigned, key-holding binary

### Notes
- Ledger signing requires GUI mode; headless and CLI return
  `LEDGER_SIGN_NOT_AVAILABLE` for hardware-backed addresses
- Blind signing must be enabled in the Ledger Ethereum app
- Auto-approve is a policy decision only — the device always requires a physical
  confirmation

## 0.1.0 — Initial Release

### Added
- Self-custodial wallet management with BIP-39/BIP-44 HD wallets
- AES-256-GCM encryption with Argon2id key derivation
- Agent registration with HMAC-SHA256 or Bearer token authentication
- Spend policies with daily limits, per-request caps, and domain restrictions
- x402 payment signing (v1 and v2 protocol support)
- DeFi trading via Uniswap v3/v4 on Robinhood Chain
- GUI, CLI, and headless daemon modes
- AP2-compatible transaction receipts and Intent Mandates
