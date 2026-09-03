# Architecture

Vault ships as two editions - a desktop window and a terminal - and they do the
same things. That only stays true if there is one implementation underneath and
the interfaces are thin. These are the rules that keep it that way.

Every rule here is enforced by `tests/test_architecture.py`. If you change a
rule, change the test — a rule that lives only in prose stops being true within a
release.

---

## The shape

```
┌─────────────────────────────────────────────────────────┐
│ SHARED — knows nothing about how it is being driven     │
│   Vault          the single entry point for operations  │
│   services/      signing, trading, the HTTP agent API   │
│   models/        agents, policies, transactions, store  │
│   wallet/        keys, encryption, hardware devices     │
│   commands/      every command, for both editions       │
└─────────────────────────────────────────────────────────┘
                          ▲
                          │  method calls, in process
              ┌───────────┴───────────┐
        ┌─────┴──────┐         ┌──────┴─────┐
        │ ui/        │         │ terminal/  │
        │ (PyQt6)    │         │ prompt     │
        └─────┬──────┘         └──────┬─────┘
              │                       │
      app_desktop.py           app_terminal.py
```

The shared tier owns all state and every decision. An interface collects input,
calls `Vault`, and renders what comes back. When an interface starts deciding
something, that decision exists in one place and is missing from the other — which
is exactly how a wallet ends up enforcing a limit in the window and not on the
server.

**`commands/` is shared.** It reads as terminal code and it is not: the desktop's
console panel (`ui/console.py`) imports the same `CommandHandler`. Forking it
would give the two editions two command languages and two sets of validation.
This is the single most likely thing in the tree for someone to get wrong.

**The two composition roots are the only places an edition is named.**
`app_desktop.py` and `app_terminal.py` build a `Vault` and register the handlers
that make it a product. Below them, code asks whether a capability is present -
"is a hardware-signing handler registered?" - never which product it is running
in.

---

## Rule 1 — No Qt in the shared tier

*Enforced by `test_no_qt_in_core` and
`test_shared_tier_imports_with_qt_uninstalled`.*

Nothing under `core/`, `services/`, `models/`, `wallet/` or `commands/`, and none
of `utils.py`, `networks.py`, `instance_lock.py`, `version.py` or
`design_tokens.py`, may import PyQt. Everything in that list ships in both
editions, and the Terminal edition is installed with no Qt present at all — so an
import that reaches into Qt is not a layering opinion, it is an ImportError on
every server.

The two tests do different jobs. The first reads the source, which is fast and
catches the common case. The second runs a subprocess with the Qt packages
poisoned in `sys.modules` and imports the whole shared tier, which is the only
thing that catches a Qt import buried inside a function body — the shape that hid
PyQt6 in `utils.py`, where a source scan sees nothing and the failure only
appears when the function is called.

`wallet` was missing from the first list until the 0.3 split, which is how
`wallet/dialogs.py` came to hold 2,058 lines of PyQt6 inside a package
`core/vault.py` depends on. Those dialogs now live in `ui/wallet_dialogs.py`, and
every file that draws a window is under `ui/`.

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
the terminal edition gets it too.

## Rule 3 — Operations go through the core, not around it

*Enforced by `test_no_forbidden_model_imports` and `test_no_direct_wallet_creation`.*

The UI must not import model-level functions that perform operations, and must
not construct wallets itself. Wallet creation in particular carries preconditions
— password validation, the master key, file permissions, the unlock that follows
— and an interface that builds its own wallet gets none of them.

## Rule 4 — State changes emit events

*Enforced by `test_core_emits_expected_events`.*

When the core changes something a user can see, it emits an event. Interfaces
render from those rather than polling or guessing. It is what puts an approval
request in front of a terminal operator the moment it arrives, instead of
leaving them to keep typing `pending` and hoping.

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

## Rule 6 — Shared code never asks which edition it is in

*Enforced by `test_no_edition_conditionals_in_shared_code`.*

No `if edition ==`, no `is_gui`, no `if headless`. Shared code asks whether a
capability is registered, and behaves accordingly:

```python
if not self._on_hardware_sign_needed:
    return {"status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE", ...}
```

That is a question about this deployment, and it is answered the same way in
both editions. `if edition == "desktop"` is a fork with extra steps: the two
branches drift, and the one nobody is running is the one that breaks.

When the desktop got hardware signing and the terminal did not, this rule is
what made adding it a matter of registering a handler rather than editing the
signing service.

---

## Common mistakes

**Adding a decision to the interface.** A check in the window is a check the
terminal does not have, and the terminal is the edition running unattended on a
server. Put it in the core and let the interface show a nicer message on top.

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
| `commands/` | Every command. **Shared** — the desktop console uses these too |
| `ui/` | PyQt6 interface — every file that draws a window, including the wallet dialogs |
| `terminal/` | The prompt, the local control channel, boot registration |
| `app_desktop.py`, `app_terminal.py` | The two composition roots |

## Running the rules

```bash
pytest tests/test_architecture.py -v
```

They run in the normal suite too. They are fast, they read the source rather than
executing it, and they are the only tests here that fail for reasons of design
rather than behaviour.
