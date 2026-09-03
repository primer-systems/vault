"""The terminal interface: a prompt, a live feed, and one-shot commands.

There is one interface here, not several. The same commands are available
whether you type them at the prompt, pass them on the command line, or pipe
them in - the only difference is where the line comes from and where the answer
goes. Nothing here decides anything; it collects input, hands it to the engine,
and renders what comes back.

The engine may be this process or another one. `Backend` is the whole of that
difference: one method to run a command, one to subscribe to events. It is
deliberately not a mirror of `Vault` - a proxy with one method per engine
operation is what the old admin API client was, and it left every command with
two code paths.
"""

import getpass
import json
import os
import shlex
import subprocess
import sys
from typing import Callable, Optional, Protocol

from ..commands import CommandHandler, CommandResult
from ..core.events import Event
from ..version import __version__

BANNER = f"""
█ █ ▄▀█ █ █ █   ▀█▀
▀▄▀ █▀█ █▄█ █▄▄  █
v{__version__} - Type 'help' for commands, 'exit' to quit
"""


# ---------------------------------------------------------------- the backend

class Backend(Protocol):
    """Somewhere to send a command line. This process, or the running engine."""

    def execute(self, command: str, inputs: Optional[dict] = None) -> CommandResult:
        ...

    def stream_events(self, on_event: Callable[[str, dict], None]) -> None:
        ...


class LocalBackend:
    """The engine is in this process."""

    def __init__(self, core):
        self._core = core
        self._handler = CommandHandler(core)

    def execute(self, command: str, inputs: Optional[dict] = None) -> CommandResult:
        return self._handler.execute(command, inputs=inputs)

    def stream_events(self, on_event: Callable[[str, dict], None]) -> None:
        def relay(event: Event) -> None:
            on_event(event.type.value, event.data)
        self._core.event_bus.subscribe_all(relay)


# ------------------------------------------------------------------ the feed

def format_event(event_type: str, data: dict) -> Optional[str]:
    """One line for the feed, or None for events a person need not see.

    Most event types exist so the desktop can refresh a table. Printing those
    would bury the two that actually want an operator's attention.
    """
    if event_type == "approval_needed":
        agent = data.get("agent_name", "unknown")
        amount = (data.get("amount_micro") or 0) / 1_000_000
        request_id = (data.get("request_id") or "")[:8]
        recipient = data.get("recipient") or "?"
        return (f"[approval needed] {agent} wants ${amount:.6f} -> {recipient}\n"
                f"                  approve {request_id}   |   reject {request_id}")
    if event_type == "approval_resolved":
        request_id = (data.get("request_id") or "")[:8]
        verdict = "approved" if data.get("approved") else "rejected"
        return f"[{verdict}] request {request_id}"
    if event_type == "transaction_created":
        agent = data.get("agent_name", "unknown")
        amount = (data.get("amount_micro") or 0) / 1_000_000
        return f"[signed] {agent} ${amount:.6f}"
    if event_type == "trade_executed":
        return f"[trade] executed for {data.get('address', '?')}"
    if event_type == "activity":
        message = data.get("message", "")
        if not message:
            return None
        return f"[!] {message}" if data.get("is_error") else f"[.] {message}"
    if event_type == "server_started":
        return f"[agent api] listening on port {data.get('port', '?')}"
    if event_type == "server_stopped":
        return "[agent api] stopped"
    return None


# ------------------------------------------------------------------- prompting

class _Prompter:
    """Reads lines, and prints feed lines without eating a half-typed one.

    `prompt_toolkit` gives both: proper line editing (arrow keys, history,
    Ctrl-R) and `patch_stdout`, which lifts anything printed by another thread
    above the input line and redraws what was being typed underneath it.

    Falling back to bare `input()` when it is unavailable means no history and
    a feed line that lands in the middle of what you are typing. Usable, but
    the reason `prompt_toolkit` is a dependency rather than an extra.
    """

    def __init__(self):
        self._session = None
        self._patch = None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.patch_stdout import patch_stdout
            self._session = PromptSession(history=InMemoryHistory())
            self._patch = patch_stdout
        except ImportError:
            pass

    def __enter__(self):
        if self._patch is not None:
            self._ctx = self._patch(raw=True)
            self._ctx.__enter__()
        else:
            self._ctx = None
        return self

    def __exit__(self, *exc):
        if self._ctx is not None:
            self._ctx.__exit__(*exc)
        return False

    def ask(self, prompt: str = "> ") -> str:
        if self._session is not None:
            return self._session.prompt(prompt)
        return input(prompt)

    def forget(self) -> None:
        """Drop the command history.

        A typed line can carry a seed phrase, a private key or a password, so
        locking the wallet has to clear this too - the desktop console does the
        same thing for the same reason.
        """
        if self._session is not None:
            self._session.history = type(self._session.history)()


# ------------------------------------------------------- non-interactive input

class ScriptContext:
    """Pre-supplied answers, so a scripted command never blocks on a prompt."""

    def __init__(self, auto_confirm: bool = False, password: Optional[str] = None,
                 json_output: bool = False):
        self.auto_confirm = auto_confirm
        self.password = password
        self.json_output = json_output

    def get_input_for(self, input_type: str) -> Optional[dict]:
        if input_type == "confirm" and self.auto_confirm:
            return {"confirm": "YES"}
        if input_type == "password" and self.password is not None:
            return {"password": self.password, "value": self.password}
        return None


def parse_global_flags(args: list[str]) -> tuple[list[str], ScriptContext]:
    """Split `--yes` / `--password` / `--json` off the front of a command line."""
    auto_confirm = False
    password = os.environ.get("PRIMER_VAULT_PASSWORD")
    json_output = False
    remaining = []

    i = 0
    while i < len(args):
        if args[i] in ("--yes", "-y"):
            auto_confirm = True
            i += 1
        elif args[i] == "--json":
            json_output = True
            i += 1
        elif args[i] == "--password":
            if i + 1 >= len(args):
                print("Error: --password requires a value", file=sys.stderr)
                sys.exit(1)
            password = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1

    return remaining, ScriptContext(auto_confirm=auto_confirm, password=password,
                                    json_output=json_output)


def collect_input(result: CommandResult, script_ctx: Optional[ScriptContext],
                   prompter: Optional[_Prompter] = None) -> dict:
    """Answer a command that asked for something."""
    needs = result.needs_input or {}
    input_type = needs.get("type", "text")
    prompt = needs.get("prompt", "> ")

    if script_ctx is not None:
        scripted = script_ctx.get_input_for(input_type)
        if scripted:
            return scripted

    if input_type == "password":
        try:
            value = getpass.getpass(prompt + " ")
        except EOFError:
            value = ""
        return {"password": value, "value": value}

    print(prompt)
    try:
        value = (prompter.ask("") if prompter is not None else input()).strip()
    except EOFError:
        value = ""
    if input_type == "confirm":
        return {"confirm": value}
    return {"value": value}


def _run_to_completion(backend: Backend, command: str,
                       script_ctx: Optional[ScriptContext] = None,
                       prompter: Optional[_Prompter] = None) -> CommandResult:
    """Run a command, answering any prompts it raises, until it is finished."""
    result = backend.execute(command)
    while result.needs_input:
        inputs = collect_input(result, script_ctx, prompter)
        result = backend.execute(command, inputs=inputs)
    return result


# ---------------------------------------------------------------------- modes

def run_interactive(backend: Backend) -> None:
    """The prompt, with the engine's events printing above it as they happen."""
    print(BANNER)

    with _Prompter() as prompter:
        def show(event_type: str, data: dict) -> None:
            if event_type == "wallet_locked":
                prompter.forget()
            line = format_event(event_type, data)
            if line:
                print(line)

        backend.stream_events(show)

        while True:
            try:
                command = prompter.ask("> ").strip()
                if not command:
                    continue

                result = _run_to_completion(backend, command, prompter=prompter)
                _print(result)

                if result.data and result.data.get("action") == "exit":
                    print("Goodbye!")
                    return
                if result.data and result.data.get("action") == "clear":
                    subprocess.run("cls" if os.name == "nt" else "clear",
                                   shell=True, check=False)
            except KeyboardInterrupt:
                print("\nUse 'exit' to quit.")
            except EOFError:
                print("\nGoodbye!")
                return


def run_piped(backend: Backend, script_ctx: Optional[ScriptContext] = None) -> None:
    """One command per line from stdin. No banner, no prompts."""
    for line in sys.stdin:
        command = line.strip()
        if not command:
            continue
        result = _run_to_completion(backend, command, script_ctx)
        _print(result, script_ctx)
        if result.data and result.data.get("action") == "exit":
            return


def run_one_shot(backend: Backend, args: list[str],
                 script_ctx: Optional[ScriptContext] = None) -> int:
    """Run a single command from the command line. Returns the exit code.

    `script_ctx` is optional so callers that already split the global flags off
    do not parse them twice; passing the args through unparsed still works.
    """
    if script_ctx is None:
        args, script_ctx = parse_global_flags(args)
    if not args:
        print("Error: No command provided", file=sys.stderr)
        return 1

    command = " ".join(shlex.quote(a) for a in args)
    result = _run_to_completion(backend, command, script_ctx)
    _print(result, script_ctx)
    return 0 if result.success else 1


def _print(result: CommandResult, script_ctx: Optional[ScriptContext] = None) -> None:
    if script_ctx is not None and script_ctx.json_output:
        _print_json(result)
        return
    if result.output:
        print(result.output)
    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)


def _print_json(result: CommandResult) -> None:
    """Emit one JSON object on stdout, for a caller that is a program.

    Everything goes to stdout, including the error - a caller parsing JSON
    wants the whole result in one place, and splitting half of it onto stderr
    is what makes the plain-text form awkward to consume.

    `data` is what this flag exists for. The human `output` is carried too,
    because several commands still say things in prose that no structured field
    captures, and dropping it would lose information rather than reformat it.

    default=str rather than letting a stray non-serialisable value raise: the
    control channel already JSON-encodes `data` on its way back, but a locally
    executed command has never been through an encoder, so this path is the
    first thing that would ever have seen such a value. Degrading one field to
    its repr beats failing a command that has already run.
    """
    print(json.dumps({
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "data": result.data,
    }, default=str))
