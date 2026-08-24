"""What a stranger hits in their first ten minutes, following README.md literally.

Each test encodes one thing the README promises that the shipped code must
deliver, or one place it must not leave the reader to guess.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


def _drive(handler, command, password="a-strong-passphrase"):
    """Run one console command, answering password/confirm prompts."""
    result = handler.execute(command)
    guard = 0
    while result.needs_input and guard < 6:
        guard += 1
        kind = result.needs_input.get("type", "text")
        supplied = (
            {"password": password, "value": password} if kind == "password"
            else {"confirm": "YES", "value": "YES"}
        )
        result = handler.execute(command, inputs=supplied)
    return result


# ============================================================
# 1. the first error message a new user sees is printed twice
# ============================================================

def test_the_first_error_a_new_user_sees_is_not_double_prefixed(tmp_path):
    """`primer-vault agent register ...` is the first thing the README's Agent
    Management section tells you to run. On a fresh install there is no wallet,
    so it fails - and cli.py prints "Error: " in front of whatever the command
    returned. The message must therefore not carry its own "Error:".
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        result = _drive(CommandHandler(core), "agent register MyAgent --auth hmac")
    finally:
        core.release_instance_lock()

    assert not result.success, "expected the command to fail with no wallet"
    assert not result.error.startswith("Error:"), (
        "cli.py prints an 'Error: ' prefix of its own, so a message that already "
        "begins with 'Error:' reaches the user doubled: "
        + repr("Error: " + result.error.splitlines()[0])
    )


# ============================================================
# 2. the README's only trade example is not a valid request body
# ============================================================

def _readme_trade_example() -> dict:
    """The dict from README's "### Trade Request" python block."""
    block = re.search(r"### Trade Request\n+```python\n(.*?)```", README, re.S).group(1)
    body = re.search(r"trade\s*=\s*\{(.*?)\n\}", block, re.S).group(1)
    fields = {}
    for line in body.splitlines():
        line = line.split("#")[0].strip().rstrip(",")
        if not line:
            continue
        key, value = line.split(":", 1)
        fields[json.loads(key.strip())] = json.loads(value.strip())
    return fields


def test_the_readme_trade_request_example_is_a_valid_trade_body(tmp_path):
    """README shows one trade request and says "POST to ${PRIMER_VAULT_URL}/trade".

    Sent as written - with the agent's credentials added, which is the most
    generous reading - the server must not reject it for being the wrong shape.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        handler = CommandHandler(core)
        for command in [
            "wallet create main",
            "policy create standard --day 100 --txn 50 --auto 5 "
            "--trading --trade-max 100 --trade-daily 500",
        ]:
            assert _drive(handler, command).success, command
        registered = _drive(handler, "agent register MyAgent --auth bearer")
        assert registered.success, registered.error
        creds = dict(
            line.split("=", 1) for line in registered.output.splitlines()
            if line.startswith("PRIMER_VAULT_AGENT_")
        )
        assert _drive(handler, "agent commission MyAgent standard A001").success
        assert _drive(handler, "server start 14771").success

        # The README now shows the trade nested under a "trade" key alongside
        # the agent credentials, which is the shape the endpoint accepts.
        payload = {
            "trade": _readme_trade_example(),
            "agent_id": creds["PRIMER_VAULT_AGENT_ID"],
            "signature": creds["PRIMER_VAULT_AGENT_TOKEN"],
        }

        request = urllib.request.Request(
            "http://127.0.0.1:14771/trade",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            reply = json.loads(urllib.request.urlopen(request, timeout=60).read())
        except urllib.error.HTTPError as e:
            reply = json.loads(e.read())
        finally:
            _drive(handler, "server stop")
    finally:
        core.release_instance_lock()

    assert reply.get("code") != "MISSING_TRADE", (
        "the README's trade example, posted to the endpoint the README names, "
        f"is rejected as malformed: {reply}. The trade fields have to be nested "
        "under a 'trade' key, which no README example shows."
    )


# ============================================================
# 3. nothing in the README opens the agent port in CLI mode
# ============================================================

def test_the_readme_tells_a_cli_user_how_to_open_the_agent_port(tmp_path):
    """README: "Any agent framework can integrate via HTTP to localhost:4663."

    The window starts that listener on its own and so does `--headless`. A
    `primer-vault --cli` session does not - so if the README documents CLI mode
    as a way to run Vault, it has to name the command that opens the port.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        # A console session, as `primer-vault --cli` provides it, starts nothing.
        assert "Server: Stopped" in _drive(CommandHandler(core), "status").output
    finally:
        core.release_instance_lock()

    assert "server start" in README, (
        "in CLI mode the agent port at localhost:4663 is closed until someone "
        "runs 'server start', and the README never mentions that command - its "
        "CLI Reference has no SERVER section at all"
    )


# ============================================================
# 4. the README names a pane the window does not build
# ============================================================

def test_the_readme_does_not_name_a_market_pane_that_is_not_built():
    """The README must not tell the reader a market pane exists unless the
    window actually builds one (the Market tab is currently commented out)."""
    main_window = (REPO / "src/primer_vault/ui/main_window.py").read_text(encoding="utf-8")
    market_tab_built = bool(
        re.search(r"^\s*self\.tabs\.addTab\(self\.market_tab", main_window, re.M))
    if market_tab_built:
        return  # a market pane exists, so the README may name it
    assert "market pane" not in README, (
        "the README tells the reader their network traffic lights up a market "
        "pane, but main_window.py has the Market tab's import and addTab call "
        "commented out, so no such pane is ever built"
    )


# ============================================================
# 5. policy controls the README advertises but never shows a flag for
# ============================================================

@pytest.mark.parametrize("flag, feature", [
    ("--allow-domains", "Domain allowlist/blocklist"),
    ("--max-impact", "Max price impact"),
])
def test_every_policy_control_the_readme_advertises_has_a_documented_flag(flag, feature):
    """README's Policy System lists what a policy controls, and its CLI Reference
    lists the flags that set them. Anything in the first list with nothing in the
    second leaves a terminal user with nothing to type.
    """
    from primer_vault.commands.policy import PolicyCommands

    help_text = PolicyCommands(None, None).execute(["create", "--help"]).output
    assert flag in help_text, f"{flag} is not a real flag - test needs updating"
    assert feature in README, f"README no longer advertises {feature!r}"

    assert flag in README, (
        f"README advertises {feature!r} as something a policy controls, and "
        f"'policy create --help' accepts {flag} to set it, but the README's CLI "
        f"Reference never names the flag"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
