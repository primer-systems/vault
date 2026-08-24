"""
Agent command implementations.
"""

from typing import TYPE_CHECKING

import requests as _requests

from .result import CommandResult
from ..utils import agent_config_snippet

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


class AgentCommands:
    """Agent-related commands."""

    _MANDATE_REGISTRY_URL = "https://ap2.primer.systems/api/mandates"

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def _upload_mandate(self, mandate: dict, agent_code: str) -> dict:
        """Upload a mandate to the AP2 registry and record where it landed.

        Takes the agent code because publishing is not finished when the request
        returns: the registry hands back the id it filed the mandate under, and
        that id has to go onto the stored mandate. Without it the agent's
        `mandate_registry_id` is null forever, so an agent commissioned here
        cannot tell a merchant where to verify it - and nothing reports an error,
        because the upload itself succeeded. The window path stamps the mandate
        the same way; doing it here rather than at each call site is what stops
        the two drifting apart again.

        Uploads directly rather than through the CoreClient proxy.
        """
        try:
            response = _requests.post(
                self._MANDATE_REGISTRY_URL,
                json=mandate,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if response.status_code in (200, 201):
                data = response.json()
                mandate_id = data.get("id", mandate.get("id"))
                viewer_url = f"https://ap2.primer.systems/mandate.html?id={mandate_id}"
                mandate["registryId"] = mandate_id
                mandate["registryUrl"] = viewer_url
                self.core.set_agent_mandate(agent_code, mandate)
                return {
                    "success": True,
                    "mandate_id": mandate_id,
                    "viewer_url": viewer_url,
                }
            return {"success": False, "error": f"Registry returned status {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute(self, args: list[str]) -> CommandResult:
        """Route agent subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "list":
            return self._list()
        elif subcmd == "show":
            if "--help" in args or "-h" in args:
                return self._show_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: agent show <name|ID>")
            return self._show(args[1])
        elif subcmd == "register":
            if "--help" in args or "-h" in args:
                return self._register_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: agent register <name> [--auth hmac|bearer]")
            return self._register(args[1:])
        elif subcmd == "commission":
            if "--help" in args or "-h" in args:
                return self._commission_help()
            if len(args) < 4:
                return CommandResult.fail("Usage: agent commission <agent> <policy> <address> [--mandate] [--upload]")
            # Parse optional flags
            mandate_flag = "--mandate" in args
            upload_flag = "--upload" in args
            return self._commission(args[1], args[2], args[3], mandate=mandate_flag, upload=upload_flag)
        elif subcmd == "mandate":
            if "--help" in args or "-h" in args:
                return self._mandate_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: agent mandate <agent> [--upload]")
            upload = "--upload" in args
            return self._mandate(args[1], upload=upload)
        elif subcmd == "suspend":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("agent suspend - Temporarily disable an agent\n\nUsage: agent suspend <name|ID>")
            if len(args) < 2:
                return CommandResult.fail("Usage: agent suspend <name|ID>")
            return self._suspend(args[1])
        elif subcmd == "activate":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("agent activate - Re-enable a suspended agent\n\nUsage: agent activate <name|ID>")
            if len(args) < 2:
                return CommandResult.fail("Usage: agent activate <name|ID>")
            return self._activate(args[1])
        elif subcmd == "edit":
            if "--help" in args or "-h" in args:
                return self._edit_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: agent edit <agent> [--policy P] [--address A]")
            return self._edit(args[1:])
        elif subcmd == "delete":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("agent delete - Permanently remove an agent\n\nUsage: agent delete <name|ID>\n\nYou will be asked to confirm.")
            if len(args) < 2:
                return CommandResult.fail("Usage: agent delete <name|id>")
            return self._delete(args[1])
        elif subcmd == "instructions":
            if "--help" in args or "-h" in args:
                return self._instructions_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: agent instructions <name|ID> [--regenerate]")
            regenerate = "--regenerate" in args
            return self._instructions(args[1], regenerate=regenerate)
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show agent command help."""
        help_text = """agent - Manage agents

Subcommands:
  list                              - List all agents
  show <agent>                      - Show agent details
  register <name> [--auth hmac|bearer] - Register new agent
  commission <agent> <policy> <address> - Commission agent
  edit <agent> [--policy P] [--address A] - Edit agent
  instructions <agent> [--regenerate] - Show agent credentials
  mandate <agent> [--upload]        - Generate intent mandate
  suspend <agent>                   - Suspend agent
  activate <agent>                  - Activate agent
  delete <agent>                    - Delete agent

Use 'agent <subcommand> --help' for subcommand options."""
        return CommandResult.ok(help_text)

    def _find_agent(self, identifier: str):
        """Find agent by name or ID."""
        agent = self.core.get_agent_by_id(identifier.upper())
        if agent:
            return agent
        for a in self.core.get_all_agents():
            if a.name.lower() == identifier.lower():
                return a
        return None

    def _find_policy(self, identifier: str):
        """Find policy by name."""
        for p in self.core.get_all_policies():
            if p.name.lower() == identifier.lower():
                return p
        return None

    def _find_policy_by_id(self, policy_id: str):
        """Find policy by ID."""
        return self.core.get_policy(policy_id)

    def _list(self) -> CommandResult:
        """List all agents."""
        agents = self.core.get_all_agents()
        if not agents:
            return CommandResult.ok("No agents registered.")

        lines = ["Agents:"]
        for agent in agents:
            policy = self.core.get_policy(agent.policy_id) if agent.policy_id else None
            policy_name = policy.name if policy else "-"
            lines.append(f"  {agent.id}  {agent.name}  [{agent.status}]  policy: {policy_name}")

        return CommandResult.ok("\n".join(lines), data={"agents": [
            {"id": a.id, "name": a.name, "status": a.status, "policy_id": a.policy_id}
            for a in agents
        ]})

    def _show_help(self) -> CommandResult:
        """Help for agent show."""
        return CommandResult.ok("""agent show - Display agent details

Usage: agent show <name|ID>

Shows agent ID, status, auth mode, policy, and address.""")

    def _show(self, identifier: str) -> CommandResult:
        """Show agent details."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        policy = self.core.get_policy(agent.policy_id) if agent.policy_id else None
        lines = [
            f"Agent: {agent.name}",
            f"  ID:         {agent.id}",
            f"  Status:     {agent.status}",
            f"  Auth Mode:  {agent.auth_mode}",
            f"  Policy:     {policy.name if policy else 'None'}",
            f"  Address:    {agent.wallet_address or 'None'}",
            f"  Created:    {agent.created_at[:19] if agent.created_at else 'Unknown'}",
        ]
        return CommandResult.ok("\n".join(lines), data={
            "agent": {
                "id": agent.id,
                "name": agent.name,
                "status": agent.status,
                "auth_mode": agent.auth_mode,
                "policy_id": agent.policy_id,
                "wallet_address": agent.wallet_address,
            }
        })

    def _register_help(self) -> CommandResult:
        """Help for agent register."""
        return CommandResult.ok("""agent register - Register a new agent

Usage: agent register <name> [--auth hmac|bearer]

Arguments:
  <name>          Name for the new agent (required)

Options:
  [--auth <mode>] Authentication mode: hmac (default) or bearer

Example:
  agent register myagent --auth bearer""")

    def _register(self, args: list[str]) -> CommandResult:
        """Register a new agent."""
        # Require unlocked wallet: agent credentials are encrypted under the
        # wallet's master key, which only exists while the wallet is open.
        if not self.core.is_wallet_unlocked():
            return CommandResult.fail(
                "Please unlock a wallet first.\n"
                "Agent credentials are encrypted with your wallet's key."
            )

        # Parse arguments
        auth_mode = "hmac"
        name = None
        i = 0
        while i < len(args):
            if args[i] == "--auth":
                if i + 1 < len(args):
                    auth_mode = args[i + 1]
                    i += 2
                else:
                    return CommandResult.fail("--auth requires a value (hmac or bearer)")
            elif args[i].startswith("-"):
                return CommandResult.fail(f"Unknown option: {args[i]}")
            else:
                if name is None:
                    name = args[i]
                i += 1

        if not name:
            return CommandResult.fail("Missing agent name")

        try:
            agent, secret = self.core.create_agent(name, auth_mode)
            port = self.core.server_port

            lines = [
                "Agent registered!",
                "",
                f"  Name:       {agent.name}",
                f"  ID:         {agent.id}",
                f"  Auth Mode:  {auth_mode}",
                "",
                "Paste this into your agent's system prompt:",
                "",
                agent_config_snippet(agent.id, secret, auth_mode, url=f"http://localhost:{port}"),
                "",
                "Save the token now - it cannot be retrieved later!",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "agent_id": agent.id,
                "agent_name": agent.name,
                "token": secret,
                "auth_mode": auth_mode,
            })
        except ValueError as e:
            return CommandResult.fail(str(e))

    def _commission_help(self) -> CommandResult:
        """Help for agent commission."""
        return CommandResult.ok("""agent commission - Commission an agent with a policy and address

Usage: agent commission <agent> <policy> <address> [--mandate] [--upload]

Arguments:
  <agent>     Agent name or code
  <policy>    Policy name
  <address>   Address ID (e.g., A001) or full address

Options:
  --mandate   Also generate intent mandate after commissioning
  --upload    Upload mandate to AP2 registry (requires --mandate)

Examples:
  agent commission myagent standard A001
  agent commission myagent premium A001 --mandate
  agent commission myagent premium A001 --mandate --upload""")

    def _commission(self, agent_name: str, policy_name: str, address_id: str,
                     mandate: bool = False, upload: bool = False) -> CommandResult:
        """Commission an agent with a policy and wallet address."""
        agent = self._find_agent(agent_name)
        if not agent:
            return CommandResult.fail(f"Agent not found: {agent_name}")

        policy = self._find_policy(policy_name)
        if not policy:
            return CommandResult.fail(f"Policy not found: {policy_name}")

        if not self.core.is_wallet_unlocked():
            return CommandResult.fail("Wallet must be unlocked first.")

        addresses = self.core.get_wallet_addresses()
        address = None
        for addr in addresses:
            if addr["id"].upper() == address_id.upper() or addr["address"].lower() == address_id.lower():
                address = addr
                break

        if not address:
            return CommandResult.fail(f"Address not found: {address_id}")

        try:
            self.core.commission_agent(agent.code, policy.id, address["address"])
            lines = [f"Agent '{agent.name}' commissioned with policy '{policy.name}'."]

            # Handle mandate generation if requested
            if mandate:
                # Re-fetch agent to get updated state
                agent = self._find_agent(agent_name)

                mandate_data = self.core.generate_intent_mandate(
                    agent_code=agent.code,
                    policy_id=agent.policy_id,
                    wallet_address=agent.wallet_address,
                    sign=True
                )
                self.core.set_agent_mandate(agent.code, mandate_data)

                lines.append("")
                lines.append("Intent mandate generated:")
                lines.append(f"  ID: {mandate_data.get('id', 'N/A')}")

                if upload:
                    result = self._upload_mandate(mandate_data, agent.code)
                    if result.get("success"):
                        lines.append("")
                        lines.append("Mandate uploaded to registry.")
                        lines.append(f"  View at: {result['viewer_url']}")
                    else:
                        lines.append("")
                        lines.append(f"Registry upload failed: {result.get('error')}")

            return CommandResult.ok("\n".join(lines))
        except Exception as e:
            return CommandResult.fail(str(e))

    def _mandate_help(self) -> CommandResult:
        """Help for agent mandate."""
        return CommandResult.ok("""agent mandate - Generate an intent mandate for a commissioned agent

Usage: agent mandate <agent> [--upload]

Arguments:
  <agent>     Agent name or ID (must be commissioned)

Options:
  --upload    Upload mandate to AP2 registry

Example:
  agent mandate AG001 --upload""")

    def _mandate(self, identifier: str, upload: bool = False) -> CommandResult:
        """Generate an intent mandate for a commissioned agent."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        if not agent.policy_id or not agent.wallet_address:
            return CommandResult.fail(
                f"Agent '{agent.name}' is not commissioned.\n"
                "Commission the agent first with: agent commission <agent> <policy> <address>"
            )

        if not self.core.is_wallet_unlocked():
            return CommandResult.fail(
                "Wallet must be unlocked to generate signed mandate.\n"
                "Use: wallet open"
            )

        try:
            mandate = self.core.generate_intent_mandate(
                agent_code=agent.code,
                policy_id=agent.policy_id,
                wallet_address=agent.wallet_address,
                sign=True
            )

            self.core.set_agent_mandate(agent.code, mandate)

            # Get policy name for display (mandate stores policyId for privacy)
            policy = self._find_policy_by_id(agent.policy_id) if agent.policy_id else None
            policy_name = policy.name if policy else 'N/A'

            lines = [
                f"Intent mandate generated for agent '{agent.name}'",
                f"  ID: {mandate.get('id', 'N/A')}",
                f"  Policy: {policy_name}",
                f"  Address: {mandate.get('wallet', {}).get('address', 'N/A')}",
            ]

            if upload:
                result = self._upload_mandate(mandate, agent.code)
                if result.get("success"):
                    lines.append("")
                    lines.append("Mandate uploaded to registry.")
                    lines.append(f"  View at: {result['viewer_url']}")
                else:
                    lines.append("")
                    lines.append(f"Registry upload failed: {result.get('error')}")

            return CommandResult.ok("\n".join(lines), data={"mandate": mandate})
        except Exception as e:
            return CommandResult.fail(f"Error generating mandate: {e}")

    def _suspend(self, identifier: str) -> CommandResult:
        """Suspend an agent."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        if not agent.policy_id:
            return CommandResult.fail(
                f"Agent '{agent.name}' is not commissioned and cannot be suspended."
            )

        try:
            self.core.suspend_agent(agent.code)
            return CommandResult.ok(f"Agent '{agent.name}' suspended.")
        except Exception as e:
            return CommandResult.fail(str(e))

    def _activate(self, identifier: str) -> CommandResult:
        """Activate a suspended agent."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        if not agent.policy_id:
            return CommandResult.fail(
                f"Agent '{agent.name}' is not commissioned and cannot be activated.\n"
                "Commission it first: agent commission <agent> <policy> <address>"
            )

        try:
            self.core.activate_agent(agent.code)
            return CommandResult.ok(f"Agent '{agent.name}' activated.")
        except Exception as e:
            return CommandResult.fail(str(e))

    def _edit_help(self) -> CommandResult:
        """Help for agent edit."""
        return CommandResult.ok("""agent edit - Modify an existing agent

Usage: agent edit <agent> [--policy P] [--address A]

Arguments:
  <agent>       Agent name or ID

Options:
  --policy P    Change policy to P (by name)
  --address A   Change wallet address to A (ID like A001 or full address)

Example:
  agent edit myagent --policy strict
  agent edit AG001 --address A002""")

    def _edit(self, args: list[str]) -> CommandResult:
        """Edit an existing agent's policy or address."""
        # Parse arguments
        identifier = None
        policy_name = None
        address_id = None
        i = 0
        while i < len(args):
            if args[i] == "--policy":
                if i + 1 < len(args):
                    policy_name = args[i + 1]
                    i += 2
                else:
                    return CommandResult.fail("--policy requires a value")
            elif args[i] == "--address":
                if i + 1 < len(args):
                    address_id = args[i + 1]
                    i += 2
                else:
                    return CommandResult.fail("--address requires a value")
            elif args[i].startswith("-"):
                return CommandResult.fail(f"Unknown option: {args[i]}")
            else:
                if identifier is None:
                    identifier = args[i]
                i += 1

        if not identifier:
            return CommandResult.fail("Missing agent name or ID")

        if not policy_name and not address_id:
            return CommandResult.fail("Specify --policy and/or --address to change")

        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        changes = []

        # Handle policy change
        if policy_name:
            policy = self._find_policy(policy_name)
            if not policy:
                return CommandResult.fail(f"Policy not found: {policy_name}")
            agent.policy_id = policy.id
            changes.append(f"policy -> {policy.name}")

        # Handle address change
        if address_id:
            if not self.core.is_wallet_unlocked():
                return CommandResult.fail("Wallet must be unlocked to change address.")

            addresses = self.core.get_wallet_addresses()
            address = None
            for addr in addresses:
                if addr["id"].upper() == address_id.upper() or addr["address"].lower() == address_id.lower():
                    address = addr
                    break

            if not address:
                return CommandResult.fail(f"Address not found: {address_id}")

            agent.wallet_address = address["address"]
            changes.append(f"address -> {address['id']}")

        try:
            self.core.update_agent(agent)
            return CommandResult.ok(f"Agent '{agent.name}' updated: {', '.join(changes)}")
        except Exception as e:
            return CommandResult.fail(str(e))

    def _delete(self, identifier: str, inputs: dict = None) -> CommandResult:
        """Delete an agent with confirmation."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        # Check if we have confirmation
        if inputs and inputs.get("confirm") == "YES":
            try:
                self.core.delete_agent(agent.code)
                return CommandResult.ok(f"Agent '{agent.name}' deleted.")
            except Exception as e:
                return CommandResult.fail(str(e))

        # Need confirmation
        self.handler._set_pending(
            lambda inp, **ctx: self._delete(ctx["identifier"], inp),
            identifier=identifier
        )
        return CommandResult.need_input(
            "confirm",
            f"Delete agent '{agent.name}' ({agent.id})? This cannot be undone.\nType YES to confirm:",
        )

    def _instructions_help(self) -> CommandResult:
        """Help for agent instructions."""
        return CommandResult.ok("""agent instructions - Show agent credentials and setup instructions

Usage: agent instructions <name|ID> [--regenerate]

Arguments:
  <name|ID>     Agent name or ID

Options:
  --regenerate  For Bearer mode only: generate a new token (invalidates old one)

Shows the configuration snippet to paste into your agent's system prompt,
including the agent ID, token, auth mode, and server URL.

For HMAC agents, the original signing secret is retrieved.
For Bearer agents, the token cannot be retrieved after creation.
Use --regenerate to create a new Bearer token if needed.

Examples:
  agent instructions myagent
  agent instructions AG001 --regenerate""")

    def _instructions(self, identifier: str, regenerate: bool = False) -> CommandResult:
        """Show agent credentials and setup instructions."""
        agent = self._find_agent(identifier)
        if not agent:
            return CommandResult.fail(f"Agent not found: {identifier}")

        port = self.core.server_port

        # Handle Bearer regeneration
        if regenerate:
            if agent.auth_mode != "bearer":
                return CommandResult.fail(
                    f"--regenerate only applies to Bearer mode agents.\n"
                    f"Agent '{agent.name}' uses {agent.auth_mode.upper()} authentication."
                )
            try:
                token = self.core.regenerate_agent_token(agent.code)
                lines = [
                    f"New Bearer token generated for agent '{agent.name}'.",
                    "The old token is now invalid.",
                    "",
                    "Paste this into your agent's system prompt:",
                    "",
                    agent_config_snippet(agent.id, token, agent.auth_mode, url=f"http://localhost:{port}"),
                    "",
                    "Save the token now - it cannot be retrieved later!",
                ]
                return CommandResult.ok("\n".join(lines), data={
                    "agent_id": agent.id,
                    "token": token,
                    "auth_mode": agent.auth_mode,
                    "regenerated": True,
                })
            except Exception as e:
                return CommandResult.fail(f"Error regenerating token: {e}")

        # Get existing credentials
        try:
            agent_id, token, auth_mode = self.core.get_agent_credentials(agent.code)
        except Exception as e:
            return CommandResult.fail(f"Error retrieving credentials: {e}")

        if auth_mode == "bearer" and token is None:
            # Bearer token cannot be retrieved
            lines = [
                f"Agent: {agent.name} ({agent.id})",
                f"Auth Mode: {auth_mode}",
                "",
                "Bearer tokens cannot be retrieved after creation.",
                "Use --regenerate to create a new token:",
                "",
                f"  agent instructions {identifier} --regenerate",
                "",
                "Note: This will invalidate the current token.",
            ]
            return CommandResult.ok("\n".join(lines), data={
                "agent_id": agent.id,
                "auth_mode": auth_mode,
                "token_available": False,
            })

        if token is None:
            # HMAC, but the secret would not decrypt. The password is not what
            # encrypts it, so a wrong password is not the cause - the open wallet
            # holds a different key from the one this agent was created under.
            return CommandResult.fail(
                f"Could not retrieve credentials for agent '{agent.name}'.\n"
                "This agent belongs to a different wallet. Open that wallet to "
                "read its credentials."
            )

        # Success - show full instructions
        lines = [
            f"Agent: {agent.name}",
            "",
            "Paste this into your agent's system prompt:",
            "",
            agent_config_snippet(agent_id, token, auth_mode, url=f"http://localhost:{port}"),
        ]
        return CommandResult.ok("\n".join(lines), data={
            "agent_id": agent_id,
            "token": token,
            "auth_mode": auth_mode,
        })
