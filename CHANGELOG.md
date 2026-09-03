# Changelog

## 0.3.0

Vault is now two published editions over one shared engine, and adds Morpho
lending as a third policy lane alongside x402 payments and trading.

### Added
- **Morpho lending.** An agent can supply USDG to a Morpho vault and withdraw
  it again, gated by its own policy rules — a per-deposit cap, a total-exposure
  cap as a dollar figure and/or a percent of USDG held, a daily operation
  ceiling, and an auto-approve threshold — through the HTTP API
  (`POST /position`, `GET /position/status/{id}`, `POST /venues`), the CLI
  (`position pending|approve|reject`, `venues`), and a Morpho tab in the
  desktop policy dialog. Venues are resolved live from the chain: a trusted
  curator address resolves to the vaults it curates and the markets it has
  capped, re-checked on every read rather than cached, since a curator can be
  reassigned at any time. "Restrict to Steakhouse," on by default, is the
  venue control for this release. Exposure is read from chain on every check,
  never accumulated locally, so it always agrees with what Morpho's own
  interface shows for the same wallet. Every write is simulated on-chain
  before it is offered for approval, and the full round trip — supply and
  withdraw — is now live-tested on mainnet.
- **A skill file for Morpho**, `skills/vault-morpho/SKILL.txt`, alongside the
  existing agent docs — auth, `/venues`, `/position` supply and withdraw,
  denominations, error codes, polling.
- **Two editions.** **Vault Desktop** is the window, downloaded from Releases.
  **Vault Terminal** is `pip install primer-vault` and ships without Qt — the
  graphics library moved to a `desktop` extra, so it installs cleanly on a
  machine with no screen. Both run the same engine, the same policies and the
  same agent API, from the same source tree at the same version.
- **One command, no modes.** `primer-vault` opens a session; `primer-vault
  <command>` runs one and exits. Whether the process is the engine or attaches
  to one already running is decided automatically by the instance lock.
- **A live feed in the terminal.** Approval requests, signatures and trades
  print as they happen, above whatever you are typing.
- **Hardware wallet support in the terminal.** Ledger signing, and now
  enrolment too: `address ledger list` reads addresses off the device,
  `address ledger add <index>` enrols them, and `address ledger verify` checks
  a stored address against the connected device — so a screenless machine can
  set up a Ledger without a desktop session. Both editions derive through the
  same code, so the same index always means the same address.
- **A local control channel.** A second `primer-vault` against a running
  engine attaches to it instead of being refused, which is what makes a Vault
  started by the system at boot manageable at all.
- **`primer-vault install-service`.** Registers Vault with systemd or Windows
  Task Scheduler so it starts at boot.
- **`startup-wallet` and `start-agent-api` settings**, so a rebooted machine
  comes back serving. The password comes from `PRIMER_VAULT_PASSWORD`, never
  from `settings.json`.
- **`--json`.** Any command, or a piped batch, can print one JSON object per
  command — `success`, `output`, `error`, and the structured `data` behind it —
  instead of formatted text, for scripting against Vault without parsing
  prose. Exit codes are unchanged.
- **Proper line editing in the terminal** (history, Ctrl-R, tab completion),
  which also keeps the live feed from eating a half-typed command.
- **A CI job that installs the terminal edition with no Qt at all** and runs
  the full non-GUI test suite against it.
- **Wallet tab: an address chip replaces the address table.** A toolbar chip
  drops the address list down on click, returning the space the old table
  always reserved to what the selected address actually holds. Details and
  Send moved behind a `⋯` beside the chip, and the address grew an inline
  copy icon.

### Changed
- **Private key export requires the password every time**, even when the
  wallet is already open.
- **Approvals behave identically regardless of how Vault was started.** A
  request that needs a human always queues, always appears in the live feed,
  and expires on its timeout.
- **History records one row per on-chain transaction, not per operation.** A
  trade or a Morpho lend that needs a prior ERC-20 approval is two separate
  transactions on-chain; each now settles and appears independently — in
  History, the CLI, both CSV exports, and the agent-facing `/trade/status` and
  `/position/status` responses (`approval_tx_hash`) — rather than sharing one
  row.

### Removed
- **The Admin API and its client (~2,500 lines).** Replaced by the local
  control channel above, which is not reachable over the network, is scoped
  to the data directory, and needs no separate open/closed mode.
- `config set admin-api`, the `--admin-open` flag, port 4664, and the
  Settings → Security row that configured them.

### Fixed
- **A locked wallet is refused at intake**, rather than discovered only after
  a human has approved a request that was never going to work. A locked-wallet
  failure that slips through later is now reported accurately too, instead of
  as a generic error.
- **A node's outright rejection of a transaction is reported as exactly
  that.** A synchronous rejection before broadcast (insufficient gas, a stale
  nonce) is now distinguished from a timeout or dropped connection, where
  whether anything reached the network is genuinely unknown: "Rejected before
  broadcast: {reason}. Nothing was sent by this attempt."
- **A pending trade or Morpho position stays reachable through approval.**
  Approving a request could take real time — a re-quote, a policy re-check,
  and up to two on-chain confirmations — and a status check landing in that
  window used to report the request as not found. It now reports the request
  as executing until the real result is ready.
- The desktop build excludes the terminal stack explicitly, rather than
  relying on it never being imported.
- Qt no longer appears anywhere in the shared engine — the wallet dialogs
  that depended on it moved into the desktop UI layer, and the rule keeping
  Qt out of shared code now covers every shared package.

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
