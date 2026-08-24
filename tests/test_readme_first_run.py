"""What a stranger gets when they follow README.md literally.

Every test here encodes one promise the README makes to someone who has never
seen the project: a claim the code must keep, or a shipped file the
repository must actually contain.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


# ============================================================
# the shipped tree is missing a module it imports
# ============================================================

def test_every_module_the_package_imports_is_in_the_repository():
    """A release is built from the tagged commit, not from a working copy.

    Anything the package imports has to be committed, or the binary CI builds
    and the wheel pip installs are missing a file the first import needs.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    tracked = {Path(p).as_posix() for p in tracked}

    on_disk = {
        p.relative_to(REPO).as_posix()
        for p in (REPO / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    }

    missing = sorted(on_disk - tracked)
    assert not missing, (
        "these modules exist on disk and are imported by the package, but are "
        f"not committed, so a clone of the tagged commit will not have them: {missing}"
    )


# ============================================================
# the documented wallet setup sequence
# ============================================================

def _readme_bash_blocks_after(heading: str) -> list[list[str]]:
    """The non-comment lines of each fenced bash block after `heading`."""
    start = README.index(heading)
    blocks = re.findall(r"```bash\n(.*?)```", README[start:], re.S)
    return [
        [ln.strip() for ln in block.splitlines()
         if ln.strip() and not ln.strip().startswith("#")]
        for block in blocks
    ]


def _drive(handler, command: str):
    """Run one command, answering password/confirm prompts the way the tests do."""
    result = handler.execute(command)
    guard = 0
    while result.needs_input and guard < 4:
        guard += 1
        kind = result.needs_input.get("type", "text")
        supplied = (
            {"password": "a-long-enough-password", "value": "a-long-enough-password"}
            if kind == "password" else {"confirm": "YES", "value": "YES"}
        )
        result = handler.execute(command, inputs=supplied)
    return result


def _console_lines(block: list[str]) -> list[str]:
    """The `> `-prefixed commands of a README console-session block."""
    return [ln[2:].strip() for ln in block if ln.startswith("> ")]


def test_the_readme_wallet_setup_works_as_written(tmp_path):
    """README "Wallet Security" gives one shell command, then a console session.

    The shell command must work as its own process; the console lines must work
    in order within one session, which is what `primer-vault --cli` provides.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler
    import primer_vault.utils as utils

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    utils.get_app_dir = lambda: data_dir  # every process resolves the same folder

    shell_block, console_block = _readme_bash_blocks_after("### Wallet Security")[:2]
    assert shell_block[0].startswith("primer-vault wallet create"), shell_block

    failures = []
    for line in shell_block:
        command = line.replace("primer-vault ", "", 1)
        core = Vault(data_dir=data_dir)  # one fresh process per shell line
        try:
            result = _drive(CommandHandler(core), command)
            if not result.success:
                failures.append(f"{line!r} -> {result.error}")
        finally:
            core.release_instance_lock()

    console = _console_lines(console_block)
    assert console, f"no console commands found in the block: {console_block}"
    core = Vault(data_dir=data_dir)  # the console: one process, many commands
    try:
        handler = CommandHandler(core)
        for command in console:
            result = _drive(handler, command)
            if not result.success:
                failures.append(f"console {command!r} -> {result.error}")
    finally:
        core.release_instance_lock()

    assert not failures, (
        "README commands that fail when run the way the README writes them:\n  "
        + "\n  ".join(failures)
    )


def test_the_readme_agent_setup_works_in_the_console(tmp_path):
    """README "Agent Management" is a console session: register, commission.

    Driven in one handler session, as the console runs it, after creating the
    wallet and the `standard` policy the way the README's other sections do.
    The mandate line is skipped because `--upload` performs network egress.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler
    import primer_vault.utils as utils

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    utils.get_app_dir = lambda: data_dir

    blocks = _readme_bash_blocks_after("### Agent Management")
    # The wallet line is provided by setup below (as the README's other sections
    # do); the mandate line performs network egress. Both are excluded here so
    # this test drives the agent-management commands themselves.
    console = [c for c in _console_lines(blocks[0])
               if "mandate" not in c and not c.startswith("wallet ")]
    assert any(c.startswith("agent register") for c in console), console
    assert any(c.startswith("agent commission") for c in console), console

    core = Vault(data_dir=data_dir)
    try:
        handler = CommandHandler(core)
        failures = []
        # The setup the README's other sections provide: a wallet and the
        # `standard` policy its Policy Management example creates.
        for command in ["wallet create main",
                        "policy create standard --day 100 --txn 50 --auto 5"]:
            result = _drive(handler, command)
            assert result.success, f"setup {command!r} -> {result.error}"
        for command in console:
            result = _drive(handler, command)
            if not result.success:
                failures.append(f"console {command!r} -> {result.error}")
    finally:
        core.release_instance_lock()

    assert not failures, (
        "README agent-setup commands that fail in the console session they "
        "document:\n  " + "\n  ".join(failures)
    )


def test_the_single_instance_section_states_the_admin_api_condition():
    """Driving a running instance from the terminal requires opening the Admin
    API; the "Single Instance" section must say so rather than promising it
    unconditionally."""
    start = README.index("### Single Instance")
    section = README[start:README.index("---", start)]
    assert "Admin API" in section and "--admin-open" in section, (
        "the Single Instance section promises terminal control of a running "
        "instance without naming how to open the Admin API (the window's "
        "Security settings, or --admin-open for a headless server)"
    )


# ============================================================
# the README's own example password is rejected
# ============================================================

def test_every_password_the_readme_puts_in_an_example_is_accepted():
    from primer_vault.wallet.crypto import MIN_PASSWORD_LENGTH

    examples = re.findall(r'--password\s+"([^"]*)"', README)
    examples += re.findall(r'PRIMER_VAULT_PASSWORD="([^"]*)"', README)
    assert examples, "no password examples found in README - test needs updating"

    too_short = sorted({p for p in examples if len(p) < MIN_PASSWORD_LENGTH})
    assert not too_short, (
        f"README shows these passwords in copy-paste examples, but Vault requires "
        f"at least {MIN_PASSWORD_LENGTH} characters and refuses them: {too_short}"
    )


# ============================================================
# the README commissions against a policy it never creates
# ============================================================

def test_policies_the_readme_commissions_against_are_ones_it_creates():
    # `<policy>` and friends are placeholders in the CLI reference, not examples.
    def real(name):
        return not name.startswith("<")

    created = {n for n in re.findall(r"policy create (\S+)", README) if real(n)}
    commissioned = [
        n for n in re.findall(r"agent commission \S+ (\S+) \S+", README) if real(n)
    ]
    assert commissioned, "no commission examples found in README - test needs updating"

    unknown = sorted({p for p in commissioned if p not in created})
    assert not unknown, (
        "README tells the reader to commission an agent against these policies, "
        f"but no README example ever creates them: {unknown}. Policies the README "
        f"does create: {sorted(created)}"
    )


# ============================================================
# a network call documented against a tab that is not built
# ============================================================

def test_hosts_the_readme_lists_under_network_calls_are_actually_contacted():
    """The Network Calls table is the privacy promise: every host Vault talks to,
    and nothing else. A host listed there that no shipped code ever contacts
    tells the reader to expect traffic that cannot happen.
    """
    table = README[README.index("### Network Calls"):README.index("## Technical Details")]
    hosts = set(re.findall(r"^\| `([a-z0-9.\-]+)`", table, re.M))
    assert hosts, "no hosts found in the Network Calls table - test needs updating"

    live_source = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (REPO / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    # A host only counts if the line reaching it is live code, not commented out.
    reachable = set()
    for host in hosts:
        for line in live_source.splitlines():
            if host in line and not line.lstrip().startswith("#"):
                reachable.add(host)
                break

    # market_tab.py is never imported: main_window.py comments out both the
    # import and the addTab call, so nothing can open the Market tab.
    main_window = (REPO / "src/primer_vault/ui/main_window.py").read_text(encoding="utf-8")
    market_tab_is_built = bool(
        re.search(r"^\s*self\.tabs\.addTab\(self\.market_tab", main_window, re.M)
    )

    unreachable = sorted(h for h in hosts if h not in reachable)
    if "api.agentic.market" in hosts and not market_tab_is_built:
        unreachable.append("api.agentic.market (Market tab is not added to the window)")

    assert not unreachable, (
        "README's Network Calls table lists hosts that no reachable code contacts: "
        f"{sorted(set(unreachable))}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
