"""The README's own agent, sending the README's own trade request.

README.md registers a bearer agent and documents one trade request that puts the
raw token in `signature`. Sent as written, the request must reach the policy
checks rather than be turned away at authentication.
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


def test_the_readme_register_command_and_the_readme_trade_body_fit_together(tmp_path):
    """Register the agent the way the README's console session does, then send
    the trade request the README documents, wrapped the way the README says.

    The reader has been given nothing else to go on, so this exact pair has to
    reach the policy checks. Being turned away at authentication means one of
    the two is wrong on the page people read first.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    # The README's Agent Management section registers with --auth bearer, whose
    # request format matches the trade body the README documents.
    assert "agent register MyAgent --auth bearer" in README, (
        "README no longer registers its example agent with --auth bearer; "
        "this test needs updating"
    )

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
        assert _drive(handler, "server start 14783").success

        # Exactly the wrapper the README prints, with the credentials the
        # register command just handed the reader.
        payload = {
            "agent_id": creds["PRIMER_VAULT_AGENT_ID"],
            "signature": creds["PRIMER_VAULT_AGENT_TOKEN"],
            "trade": _readme_trade_example(),
        }

        request = urllib.request.Request(
            "http://127.0.0.1:14783/trade",
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

    assert reply.get("code") != "AUTH_FAILED", (
        "the README's registered agent and its documented trade body do not fit "
        f"together - sent as written the request is rejected at auth: {reply}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
