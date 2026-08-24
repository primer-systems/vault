# Architecture

Vault runs as a desktop app, a terminal, and a headless daemon, and all three do
the same things. That only stays true if there is one implementation underneath
and the interfaces are thin. These are the rules that keep it that way.

Every rule here is enforced by `tests/test_architecture.py`. If you change a
rule, change the test — a rule that lives only in prose stops being true within a
release.

---

## The shape

```
┌─────────────────────────────────────────────────────────┐
│ CORE — knows nothing about how it is being driven       │
│   Vault          the single entry point for operations  │
│   services/      signing, trading, the HTTP agent API   │
│   models/        agents, policies, transactions, store  │
│   wallet/        keys, encryption, hardware devices     │
└─────────────────────────────────────────────────────────┘
                          ▲
                          │  method calls, or HTTP to the admin API
          ┌───────────────┼───────────────┐
    ┌─────┴─────┐   ┌─────┴─────┐   ┌─────┴──────┐
    │ GUI       │   │ CLI       │   │ Headless   │
    │ (PyQt6)   │   │ terminal  │   │ daemon     │
    └───────────┘   └───────────┘   └────────────┘
```

The core owns all state and every decision. An interface collects input, calls
`Vault`, and renders what comes back. When an interface starts deciding
something, that decision exists in one place and is missing from the other two —
which is exactly how a wallet ends up enforcing a limit in the GUI and not in the
daemon.

---

## Rule 1 — No Qt in the core

*Enforced by `test_no_qt_in_core`.*

Nothing under `core/`, `services/`, `models/` or `wallet/` may import PyQt. The
daemon runs on machines with no display and no Qt installed; an import that
reaches into Qt turns a headless deployment into an import error.

Core code reports events through the `EventBus` and through callbacks. The GUI
subscribes and bridges those to Qt signals at the boundary.

## Rule 2 — Interfaces use the public core API

*Enforced by `test_no_private_core_access` and `test_ui_calls_existing_methods`.*

The UI may not reach for `core._signing_service`, `core._policy_store`,
`core._wallet` or any other underscore attribute, and every `core.<method>()` the
UI calls must actually exist on `Vault`. The second half catches the failure the
first half invites: an interface calling a method that was renamed or never
existed, which no test would otherwise notice until a user clicked the button.

If the UI needs something the core does not expose, add it to `Vault`. That way
the CLI and the daemon get it too.

## Rule 3 — Operations go through the core, not around it

*Enforced by `test_no_forbidden_model_imports` and `test_no_direct_wallet_creation`.*

The UI must not import model-level functions that perform operations, and must
not construct wallets itself. Wallet creation in particular carries preconditions
— password validation, the master key, file permissions, the unlock that follows
— and an interface that builds its own wallet gets none of them.

## Rule 4 — State changes emit events

*Enforced by `test_core_emits_expected_events`.*

When the core changes something a user can see, it emits an event. Interfaces
render from those rather than polling or guessing. This is what lets a CLI
command update a running GUI: both are watching the same core.

## Rule 5 — Styling is semantic, never inline

*Enforced by `test_no_widget_level_setstylesheet`, `test_no_raw_theme_color_constants`,
`test_no_literal_hex_outside_color_source`.*

Three rules with one purpose — the app has a light and a dark theme, and anything
that hardcodes a colour is correct in one of them and wrong in the other.

- No `setStyleSheet()` on a widget. Inline styling does not restyle when the
  theme switches, so the widget keeps yesterday's colours.
- No raw `Theme.<COLOR>` constants. Use a palette token through `active()` or a
  helper like `status_color()`, which resolve per theme.
- No literal hex colours outside the files that define the palette.

Set a semantic property on the widget and write a QSS rule for it. The colour
then comes from whichever palette is active.

---

## Common mistakes

**Adding a decision to the interface.** A check in the GUI is a check the CLI and
the daemon do not have. A rule implemented in two of three interfaces is a rule
the third does not enforce. Put it in the core and let the interface show a
nicer message on top.

**Trusting a field the caller supplied.** If the core already knows something
from authenticating the caller, it must not read that same thing out of the
request body. An agent could name a different wallet address and a different
agent id in a trade request, and both were believed.

**Two copies of a rule.** Wherever a value is computed in two places, the two
will answer differently the moment either changes — and the one nobody notices is
the one that is published. Fee-tier conversion had two divisors that differed by
a hundredfold.

**A validator that is only in the UI.** Interfaces may validate early for a good
error message, but the core must enforce it as well. The interface check is
courtesy; the core check is the guarantee.

**Catching an exception the caller does not.** A service returning a dict of
results should return one for every failure it can produce. An error type that
escapes the handler becomes a dropped connection rather than a refusal.

---

## Where things live

| Path | Holds |
|---|---|
| `core/vault.py` | `Vault` — the public API every interface calls |
| `core/events.py` | The event bus and event types |
| `core/settings.py` | Settings, with file watching |
| `services/signing.py` | x402 payment authorisation and policy checks |
| `services/trading.py` | Trade quoting, policy, execution |
| `services/server.py` | The HTTP API agents talk to |
| `services/dex*.py` | Uniswap v3 / v4 adapters |
| `models/` | Agent, policy, transaction, and their JSON store |
| `wallet/crypto.py` | Master key, encryption, seeds, addresses |
| `wallet/ledger.py` | Hardware wallet support |
| `daemon/` | Headless mode and the admin API |
| `ui/`, `wallet/dialogs.py` | PyQt6 interface |
| `commands/` | CLI command handlers |

## Running the rules

```bash
pytest tests/test_architecture.py -v
```

They run in the normal suite too. They are fast, they read the source rather than
executing it, and they are the only tests here that fail for reasons of design
rather than behaviour.
