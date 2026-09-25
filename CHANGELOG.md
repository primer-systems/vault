# Changelog

## 0.4.1

Lint-only fix; no functional or behavioral changes from 0.4.0.

### Fixed
- Two unused imports (`DEFAULT_NETWORK` in `ui/dialogs.py`,
  `timedelta` in `tests/test_history_time_column.py`) that failed CI's
  `ruff check`.

## 0.4.0

Base is now a second supported network alongside Robinhood Chain, and Vault
gained a secrets/signing partner in 1Claw. x402 no longer requires a single
allowlisted settlement asset, and the History view is rebuilt around having
more than one chain to show. Several bugs were fixed along the way, including
one security-relevant one in the signing path and every GUI timestamp having
been shown in UTC rather than local time.

### Added
- **Base network support** (chain 8453): Uniswap v3/v4 deployments and
  Morpho (Steakhouse as curator, three seed vaults), alongside the existing
  Robinhood Chain entries.
- **USDC and USDT added as trusted stablecoins on Base**, alongside RHC's
  USDG, for trading and Morpho valuation.
- **x402 no longer requires one allowlisted asset.** A payment request can
  name any asset; Vault signs for it if the chain is supported. The chain's
  reference stablecoin prices 1:1, anything else gets a bounded on-chain
  quote, and an asset that can't be priced always goes to manual approval
  rather than being treated as free.
- **Wallet tab: master network toggle** next to the address selector -
  switches which chain's balances display and which chain Send targets.
- **History tab shows which chain each row was on**, via a brand mark on the
  Address cell, and gained a network filter (defaults to "All Networks").
- **Terminal `history list` now shows when each transaction happened**
  (previously only `history show <id>` did), and its JSON output gained a
  `timestamp` field.
- **Settings > Network rebuilt around more than one chain** - a General tab
  for app-wide settings (agent link, rate limit, settlement verification),
  plus one tab per registered network with that chain's live status, RPC
  override and contract addresses.
- **Policy editor: General tab**, carrying network scope (which chains a
  policy authorizes) as a checkbox list shared across every lane. No
  default - Save is blocked until at least one network is checked.
- **Terminal: `policy create` requires `--networks`** (chain IDs, or `all`)
  - previously defaulted silently to unrestricted when omitted.
- **Balance fetching now goes through Blockscout's hosted Pro API for RHC.**
  Base keeps using its own public instance.
- **Default agent-API port changed from 4663 to 9402**, decoupling it from
  Robinhood Chain's chain ID now that a second chain exists.
- **`vault-1claw` skill**, covering 1Claw's HSM-backed secrets vault,
  multi-chain signing and human approvals - a separate wallet from Vault's
  own, brought in through our partnership. Fetched on demand at
  `GET /agent/1claw` rather than folded into `/agent`'s combined
  instructions, which carries a one-line pointer to it instead.

### Fixed
- **Policy network-restriction check treated `[]` and `None` as the same
  thing**, so an empty network selection silently meant "every network
  allowed" rather than "none." `[]` is now a real, enforced restriction.
- **A payment-amount anti-tamper check assumed every x402 payment's raw wire
  amount equaled the checked amount** - true only for the (previously sole)
  reference asset. Would have rejected every legitimately quoted,
  non-reference-asset payment; replaced with a re-price-and-compare check.
- **Unpriced x402 payments displayed as a fabricated "0.000000 USDG"**
  across the approval dialog, tray notification, activity log and
  Terminal's `pending` output. Now shows the real requested asset/amount
  everywhere, including settled transaction history.
- **Every stored timestamp in the GUI was rendered as UTC, not local time.**
  Now local everywhere a human reads a time; receipts still print UTC and
  CSV export keeps the raw ISO string with its offset.
- **The receipt dialog labelled timestamps "UTC" without converting them**,
  printing a false time on the one document meant to be shared and trusted.
- **A missing timestamp crashed the History table**, rather than rendering
  an empty cell.
- **History table's Time column was too narrow for its own contents**,
  silently eliding the clock time on every row. Now sized to fit a relative
  format (today's clock time, `19 Aug` for older rows); the full timestamp
  remains on hover, in the details dialog, and in CSV export.
- **History table's Amount column was redundant with Activity** for every
  transaction type except x402. Folded x402's amount into its Activity
  sentence, then removed the Amount and row-number columns entirely.
- **The network settings dialog had its own RHC-only shadow copy of Uniswap
  addresses**, built at import time instead of read from the active
  network - removed in favor of registry lookups.


## 0.3.1

### Changed
- **Mandate issuer is `Vault`, not `VaultDesktop`.** Every mandate the
  product uploads to the registry carried `"issuer": {"type":
  "VaultDesktop"}`, which stopped being accurate once there was more than
  one edition. Mandates already in the registry still say `VaultDesktop`;
  new ones say `Vault`. Anything downstream matching on that string should
  accept both.

### Fixed
- **The agent status page no longer sends terminal users to an app they do
  not have.** It said above-threshold requests are "held for you to approve
  in the Vault desktop app," which is wrong on a screenless machine. It now
  describes what the agent actually experiences: the request is held and
  the agent gets a `pending` reply to poll on.

## 0.3.0

Vault is now two published editions over one shared engine, and adds Morpho
lending as a third policy lane alongside x402 payments and trading.

### Added
- **Morpho lending.** An agent can supply USDG to a Morpho vault and
  withdraw it again, gated by policy rules (per-deposit cap, total-exposure
  cap, daily operation ceiling, auto-approve threshold), through the HTTP
  API (`POST /position`, `GET /position/status/{id}`, `POST /venues`), the
  CLI (`position pending|approve|reject`, `venues`), and a Morpho tab in the
  policy dialog. Venues resolve live from the chain against a trusted
  curator address; "Restrict to Steakhouse" is on by default.
- **A skill file for Morpho**, `skills/vault-morpho/SKILL.txt` - auth,
  `/venues`, `/position` supply and withdraw, denominations, error codes,
  polling.
- **Two editions.** **Vault Desktop** is the window, downloaded from
  Releases. **Vault Terminal** is `pip install primer-vault` and ships
  without Qt, so it installs cleanly on a machine with no screen. Both run
  the same engine, policies and agent API from the same source tree.
- **One command, no modes.** `primer-vault` opens a session; `primer-vault
  <command>` runs one and exits.
- **A live feed in the terminal.** Approval requests, signatures and trades
  print as they happen, above whatever you are typing.
- **Hardware wallet support in the terminal.** Ledger signing and
  enrolment: `address ledger list` reads addresses off the device,
  `address ledger add <index>` enrols them, `address ledger verify` checks
  a stored address against the connected device.
- **A local control channel.** A second `primer-vault` against a running
  engine attaches to it instead of being refused, enabling a Vault started
  by the system at boot to be managed from the terminal.
- **`primer-vault install-service`.** Registers Vault with systemd or
  Windows Task Scheduler so it starts at boot.
- **`startup-wallet` and `start-agent-api` settings**, so a rebooted
  machine comes back serving. The password comes from
  `PRIMER_VAULT_PASSWORD`, never from `settings.json`.
- **`--json`.** Any command, or a piped batch, can print one JSON object
  per command (`success`, `output`, `error`, `data`) instead of formatted
  text. Exit codes are unchanged.
- **Proper line editing in the terminal** (history, Ctrl-R, tab
  completion).
- **A CI job that installs the terminal edition with no Qt at all** and
  runs the full non-GUI test suite against it.
- **Wallet tab: an address chip replaces the address table.** Details and
  Send moved behind a `⋯` beside the chip, and the address grew an inline
  copy icon.

### Changed
- **Private key export requires the password every time**, even when the
  wallet is already open.
- **Approvals behave identically regardless of how Vault was started.** A
  request that needs a human always queues, always appears in the live
  feed, and expires on its timeout.
- **History records one row per on-chain transaction, not per operation.**
  A trade or Morpho lend needing a prior ERC-20 approval is two separate
  transactions on-chain; each now settles and appears independently.

### Removed
- **The Admin API and its client (~2,500 lines).** Replaced by the local
  control channel above, which is not reachable over the network and needs
  no separate open/closed mode.
- `config set admin-api`, the `--admin-open` flag, port 4664, and the
  Settings → Security row that configured them.

### Fixed
- **A locked wallet is refused at intake**, rather than discovered only
  after a human has approved a request that was never going to work.
- **A node's outright rejection of a transaction is reported as exactly
  that**, distinguished from a timeout or dropped connection where whether
  anything reached the network is genuinely unknown.
- **A pending trade or Morpho position stays reachable through approval.**
  A status check landing mid-approval used to report the request as not
  found; it now reports the request as executing until the real result is
  ready.
- The desktop build excludes the terminal stack explicitly, rather than
  relying on it never being imported.
- Qt no longer appears anywhere in the shared engine.

## 0.2.1

### Added
- **An agent can read its own address and balances.** `/mandate` now
  returns the agent's `wallet_address` (and `wallet_id`). A new
  authenticated `POST /balances` returns that address plus its on-chain
  holdings (native + tokens). Read-only; no keys involved.

## 0.2.0

### Added
- **Ledger hardware wallet support.** Derive agent addresses from a
  connected Ledger (Ledger Live, BIP44, Legacy MEW and custom paths), sign
  x402 authorizations and DEX trades on-device, and verify a stored address
  on the device. Hardware addresses are badged and refuse private-key
  export.
- **Price-impact check on every trade.** Vault probes the agent's chosen
  pool for its true rate and escalates any trade above
  `max_price_impact_percent` (default 5%) - catching a pool too thin to
  fill, which a slippage check alone would pass. User-set; an agent cannot
  raise it.
- **Trading limits published on `/mandate`** - per-trade max, daily volume
  and what is left today, the auto-approve threshold, the slippage and
  impact ceilings, and the ETH floor, alongside the payment limits.
- **Stale price feeds escalate.** An ETH/USD reference older than 15
  minutes makes a trade unvaluable and routes it to manual approval.
- **Server hardening.** Both HTTP servers serve connections concurrently,
  cap request bodies (Admin API, 1 MB), and drop a connection idle for 30
  seconds; finished trade and payment results are capped and aged out.
- **Damaged-data resilience.** A corrupt record in `agents.json`,
  `policies.json` or `transactions.json` is skipped (not discarded) and
  Vault starts without it.
- Trade execution runs behind a non-cancellable progress dialog; the
  approval dialog states when a trade could not be priced.

### Fixed
- **Trade history recorded the quote, not the fill.** The received amount
  is now read from the receipt and kept beside the quote.
- **Hardware-wallet trades could not execute** - calldata handed to the
  Ledger as a hex string is now bytes.
- Agent-API status codes corrected: execution failures are 500 (not 400),
  per-request-max is 403 (not 429), and an unknown id answers
  `REQUEST_NOT_FOUND` on both the sign and trade registries.
- A mandate uploaded from the CLI now records its registry id.
- A dozen documentation corrections across the README, agent skills and
  endpoint reference.
- Assorted build and asset fixes - the light-theme wordmark and dropdown
  arrow ship in downloaded builds, `requests` is declared for the release
  build, and the CLI banner renders correctly.

### Security
Hardening across key-material handling, API authorisation,
displayed-versus-signed integrity, spending-limit enforcement, money
arithmetic and crash/corruption resilience. Specific details are withheld
while 0.1 remains published; **upgrading is strongly recommended.**
- Tightened authorisation and information-disclosure handling across the
  agent and admin HTTP APIs, and throttled repeated unlock attempts.
- Strengthened enforcement of per-trade, daily and reserve limits,
  including under concurrency.
- Hardened the wallet and its data files against interrupted saves,
  unreadable or unwritable files, and secrets left on screen.
- Ensured approval prompts show only the values that are actually signed.

### Removed
- The unused v1 wallet implementation (1,420 lines) serving a format no
  release ever wrote. `VaultWallet` is the wallet.
- Dead code, unused imports and unreachable helpers.
- The served pages no longer request a Google webfont their own CSP
  blocked.

### Changed
- Dependencies pinned to tested ranges and capped below the next breaking
  release; release binaries build from `requirements.lock`.
- Tests run on Windows, macOS and Linux (Windows is the primary target).
- README and docs list every host Vault contacts and confirm there is no
  telemetry.
- Release binaries are no longer UPX-compressed - the size saving is not
  worth the antivirus heuristics on an unsigned, key-holding binary.

### Notes
- Ledger signing requires GUI mode; headless and CLI return
  `LEDGER_SIGN_NOT_AVAILABLE` for hardware-backed addresses.
- Blind signing must be enabled in the Ledger Ethereum app.
- Auto-approve is a policy decision only - the device always requires a
  physical confirmation.

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
