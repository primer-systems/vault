"""`--json`: the same commands, addressed to a program rather than a person.

Every command already assembles a structured `CommandResult.data` - address
lists, trade quotes, agent records - and the printer threw all of it away,
leaving a caller to scrape formatted prose. That is brittle everywhere and
worse here than most: a column width changes and a balance is misread.

The contract these tests pin down is small and worth keeping stable, because
anything consuming it is a program that cannot notice a change:

  - one JSON object per command, on one line
  - always the four keys, even when a value is null
  - everything on stdout, including the error
  - the exit code is unchanged, so a caller can branch before parsing
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.terminal.session import (  # noqa: E402
    LocalBackend,
    ScriptContext,
    parse_global_flags,
    run_one_shot,
    run_piped,
)


@pytest.fixture
def temp_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def backend(core):
    return LocalBackend(core)


def _run(backend, args, capsys) -> tuple[int, str, str]:
    code = run_one_shot(backend, args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------

class TestFlagParsing:

    def test_json_is_stripped_from_the_command(self):
        """It is a global flag, not an argument - the command must not see it."""
        remaining, ctx = parse_global_flags(["--json", "address", "list"])
        assert remaining == ["address", "list"]
        assert ctx.json_output is True

    def test_absent_by_default(self):
        _, ctx = parse_global_flags(["status"])
        assert ctx.json_output is False

    def test_it_composes_with_the_other_global_flags(self):
        """A scripted caller wants all three at once, and order should not matter."""
        remaining, ctx = parse_global_flags(
            ["--json", "--yes", "--password", "pw", "wallet", "delete", "old"])
        assert remaining == ["wallet", "delete", "old"]
        assert ctx.json_output is True
        assert ctx.auto_confirm is True
        assert ctx.password == "pw"

    def test_it_can_appear_after_the_command(self):
        remaining, ctx = parse_global_flags(["address", "list", "--json"])
        assert remaining == ["address", "list"]
        assert ctx.json_output is True


# ---------------------------------------------------------------------------
# The shape of what comes out
# ---------------------------------------------------------------------------

class TestOutputShape:

    def test_one_object_on_one_line(self, backend, capsys):
        """One line per command is what makes piped mode parseable."""
        code, out, err = _run(backend, ["--json", "address", "list"], capsys)
        assert code == 0
        assert len(out.strip().splitlines()) == 1
        json.loads(out)

    def test_every_key_is_always_present(self, backend, capsys):
        """A caller should never have to test for a missing key, only a null one."""
        _, out, _ = _run(backend, ["--json", "status"], capsys)
        payload = json.loads(out)
        assert set(payload) == {"success", "output", "error", "data"}

    def test_structured_data_is_what_the_command_produced(self, backend, capsys):
        _, out, _ = _run(backend, ["--json", "address", "list"], capsys)
        addresses = json.loads(out)["data"]["addresses"]
        assert addresses
        assert addresses[0]["address"].startswith("0x")

    def test_the_human_text_is_carried_too(self, backend, capsys):
        """Several commands say things in prose that no structured field holds.
        Dropping it would lose information rather than reformat it."""
        _, out, _ = _run(backend, ["--json", "address", "list"], capsys)
        assert "Addresses:" in json.loads(out)["output"]

    def test_data_is_null_when_a_command_has_none(self, backend, capsys):
        _, out, _ = _run(backend, ["--json", "help"], capsys)
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["data"] is None


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

class TestFailures:

    def test_a_failure_is_still_one_parseable_object(self, backend, capsys):
        code, out, _ = _run(backend, ["--json", "address", "delete", "A999", "--yes"],
                            capsys)
        assert code == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["error"]

    def test_the_error_goes_to_stdout_not_stderr(self, backend, capsys):
        """The plain form splits a result across two streams, which is exactly
        what makes it awkward to consume. A caller parsing JSON reads one."""
        _, out, err = _run(backend, ["--json", "address", "delete", "A999", "--yes"],
                           capsys)
        assert json.loads(out)["error"]
        assert err == ""

    def test_stdout_stays_clean_enough_to_parse(self, backend, capsys):
        """Nothing may be printed alongside the object - one stray line and every
        caller's parse fails."""
        _, out, _ = _run(backend, ["--json", "address", "delete", "A999", "--yes"],
                         capsys)
        json.loads(out)  # would raise if anything else had been printed

    def test_the_exit_code_is_unchanged(self, backend, capsys):
        """So a caller can branch on success without parsing anything."""
        ok, _, _ = _run(backend, ["--json", "status"], capsys)
        bad, _, _ = _run(backend, ["--json", "address", "delete", "A999", "--yes"],
                         capsys)
        assert (ok, bad) == (0, 1)

    def test_a_value_that_will_not_serialise_does_not_kill_the_command(
            self, backend, capsys, monkeypatch):
        """The control channel already JSON-encodes `data`, but a locally executed
        command has never met an encoder - so this is the first thing that would
        ever see such a value. The command has already run and its effects have
        already happened; degrading one field to its repr beats reporting a
        failure that did not occur.
        """
        from primer_vault.commands import CommandResult
        from primer_vault.terminal import session

        monkeypatch.setattr(
            session, "_run_to_completion",
            lambda *a, **k: CommandResult.ok("done", data={"when": object()}))

        code, out, _ = _run(backend, ["--json", "status"], capsys)
        assert code == 0
        assert json.loads(out)["success"] is True


# ---------------------------------------------------------------------------
# Without the flag
# ---------------------------------------------------------------------------

class TestPlainOutputUnchanged:
    """The default is a person reading a terminal, and that must not move."""

    def test_no_json_by_default(self, backend, capsys):
        _, out, _ = _run(backend, ["address", "list"], capsys)
        assert out.startswith("Addresses:")
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_errors_still_go_to_stderr_by_default(self, backend, capsys):
        _, out, err = _run(backend, ["address", "delete", "A999", "--yes"], capsys)
        assert "Error:" in err


# ---------------------------------------------------------------------------
# Piped mode
# ---------------------------------------------------------------------------

class TestPipedMode:
    """A batch of commands is the other shape a program drives Vault in."""

    def test_each_command_yields_its_own_object(self, backend, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO("address list\nstatus\naddress list\n"))
        run_piped(backend, ScriptContext(json_output=True))

        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        for line in lines:
            assert set(json.loads(line)) == {"success", "output", "error", "data"}

    def test_piped_mode_is_plain_text_without_the_flag(self, backend, capsys,
                                                      monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("address list\n"))
        run_piped(backend)
        assert "Addresses:" in capsys.readouterr().out
