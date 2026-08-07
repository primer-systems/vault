"""
Policy Store - JSON persistence for policies, agents, and transactions.

Provides a unified storage manager for all application data.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional

from .policy import SpendPolicy
from .agent import Agent
from .transaction import Transaction
from ..utils import validate_name

# Secure file permissions (Unix only)
SECURE_FILE_MODE = 0o600  # Owner read/write only


def _set_secure_permissions(filepath: Path) -> None:
    """Set restrictive file permissions on Unix systems."""
    if os.name == 'posix':
        try:
            os.chmod(filepath, SECURE_FILE_MODE)
        except OSError:
            pass

logger = logging.getLogger(__name__)


class PolicyStore:
    """Manages storage of policies, agents, and transaction history."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.policies_file = data_dir / "policies.json"
        self.agents_file = data_dir / "agents.json"
        self.transactions_file = data_dir / "transactions.json"

        # Ensure data directory exists
        data_dir.mkdir(parents=True, exist_ok=True)

        # Load data
        self._policies: dict[str, SpendPolicy] = {}
        self._agents: dict[str, Agent] = {}
        self._transactions: list[Transaction] = []
        self._load()

    def _load(self) -> None:
        """Load policies and agents from disk."""
        # Load policies
        if self.policies_file.exists():
            try:
                with open(self.policies_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        policy = SpendPolicy.from_dict(item)
                        self._policies[policy.id] = policy
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load policies: {e}")

        # Load agents (indexed by code which is the internal UUID)
        if self.agents_file.exists():
            try:
                with open(self.agents_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        agent = Agent.from_dict(item)
                        self._agents[agent.code] = agent
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load agents: {e}")

        # Load transactions
        if self.transactions_file.exists():
            try:
                with open(self.transactions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        tx = Transaction.from_dict(item)
                        self._transactions.append(tx)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load transactions: {e}")

    def _save_policies(self) -> None:
        """Save policies to disk."""
        data = [p.to_dict() for p in self._policies.values()]
        with open(self.policies_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _set_secure_permissions(self.policies_file)

    def _save_agents(self) -> None:
        """Save agents to disk."""
        data = [a.to_dict() for a in self._agents.values()]
        with open(self.agents_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _set_secure_permissions(self.agents_file)

    def _save_transactions(self) -> None:
        """Save transactions to disk."""
        data = [t.to_dict() for t in self._transactions]
        with open(self.transactions_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _set_secure_permissions(self.transactions_file)

    # Policy methods

    def add_policy(self, policy: SpendPolicy) -> None:
        """Add a new policy. Raises ValueError if name is invalid or already exists."""
        # Validate name format
        validate_name(policy.name, "Policy name")
        # Check for duplicate name
        for existing in self._policies.values():
            if existing.name == policy.name:
                raise ValueError(f"Policy name '{policy.name}' already exists")
        self._policies[policy.id] = policy
        self._save_policies()

    def get_policy(self, policy_id: str) -> Optional[SpendPolicy]:
        """Get a policy by ID."""
        return self._policies.get(policy_id)

    def get_policy_by_name(self, name: str) -> Optional[SpendPolicy]:
        """Get a policy by its name."""
        for policy in self._policies.values():
            if policy.name == name:
                return policy
        return None

    def get_all_policies(self) -> list[SpendPolicy]:
        """Get all policies."""
        return list(self._policies.values())

    def update_policy(self, policy: SpendPolicy) -> None:
        """Update an existing policy. Raises ValueError if name is invalid or conflicts."""
        if policy.id in self._policies:
            # Validate name format
            validate_name(policy.name, "Policy name")
            # Check for duplicate name (excluding self)
            for existing in self._policies.values():
                if existing.id != policy.id and existing.name == policy.name:
                    raise ValueError(f"Policy name '{policy.name}' already exists")
            self._policies[policy.id] = policy
            self._save_policies()

    def delete_policy(self, policy_id: str) -> list[str]:
        """Delete a policy and decommission any agents using it.

        Returns list of decommissioned agent names (empty if policy not found).
        """
        if policy_id not in self._policies:
            return []

        # Find and decommission agents using this policy
        decommissioned = []
        for agent in self._agents.values():
            if agent.policy_id == policy_id:
                agent.policy_id = None
                agent.status = "uncommissioned"
                decommissioned.append(agent.name)

        if decommissioned:
            self._save_agents()

        del self._policies[policy_id]
        self._save_policies()
        return decommissioned

    # Agent methods

    def add_agent(self, agent: Agent) -> None:
        """Add a new agent. Raises ValueError if name is invalid or already exists."""
        # Validate name format
        validate_name(agent.name, "Agent name")
        # Check for duplicate name
        for existing in self._agents.values():
            if existing.name == agent.name:
                raise ValueError(f"Agent name '{agent.name}' already exists")
        self._agents[agent.code] = agent  # Index by code (UUID)
        self._save_agents()

    def get_agent_by_code(self, code: str) -> Optional[Agent]:
        """Get an agent by internal code (UUID)."""
        return self._agents.get(code)

    def get_agent_by_auth_key(self, auth_key: str) -> Optional[Agent]:
        """Get an agent by auth key."""
        for agent in self._agents.values():
            if agent.auth_key == auth_key:
                return agent
        return None

    def get_agent_by_id(self, agent_id: str) -> Optional[Agent]:
        """Get an agent by its short ID (user-facing identifier)."""
        for agent in self._agents.values():
            if agent.id == agent_id:
                return agent
        return None

    def get_agent_by_name(self, name: str) -> Optional[Agent]:
        """Get an agent by its name."""
        for agent in self._agents.values():
            if agent.name == name:
                return agent
        return None

    def get_all_agents(self) -> list[Agent]:
        """Get all agents."""
        return list(self._agents.values())

    def update_agent(self, agent: Agent) -> None:
        """Update an existing agent. Raises ValueError if name is invalid or conflicts."""
        if agent.code in self._agents:
            # Validate name format
            validate_name(agent.name, "Agent name")
            # Check for duplicate name (excluding self)
            for existing in self._agents.values():
                if existing.code != agent.code and existing.name == agent.name:
                    raise ValueError(f"Agent name '{agent.name}' already exists")
            self._agents[agent.code] = agent
            self._save_agents()

    def delete_agent(self, agent_code: str) -> None:
        """Delete an agent by its internal code (UUID)."""
        if agent_code in self._agents:
            del self._agents[agent_code]
            self._save_agents()

    # Transaction methods

    def add_transaction(self, tx: Transaction) -> None:
        """Add a new transaction."""
        self._transactions.insert(0, tx)  # Most recent first
        self._save_transactions()

    def get_transaction(self, tx_id: str) -> Optional[Transaction]:
        """Get a transaction by ID."""
        for tx in self._transactions:
            if tx.id == tx_id:
                return tx
        return None

    def get_transaction_by_hash(self, tx_hash: str) -> Optional[Transaction]:
        """Get a transaction by on-chain tx_hash (for settled transactions)."""
        for tx in self._transactions:
            if tx.tx_hash == tx_hash:
                return tx
        return None

    def update_transaction(self, tx: Transaction) -> None:
        """Update an existing transaction."""
        for i, existing in enumerate(self._transactions):
            if existing.id == tx.id:
                self._transactions[i] = tx
                self._save_transactions()
                return

    def get_all_transactions(self) -> list[Transaction]:
        """Get all transactions (most recent first)."""
        return self._transactions.copy()

    def get_transactions_by_agent(self, agent_code: str) -> list[Transaction]:
        """Get all transactions for a specific agent (by internal code/UUID)."""
        return [tx for tx in self._transactions if tx.agent_code == agent_code]

    def get_transactions_by_status(self, status: str) -> list[Transaction]:
        """Get all transactions with a specific status."""
        return [tx for tx in self._transactions if tx.status == status]

    def get_recent_transactions(self, limit: int = 100) -> list[Transaction]:
        """Get the most recent transactions."""
        return self._transactions[:limit]

    def clear_transactions(self) -> int:
        """Clear all transactions. Returns count of cleared transactions."""
        count = len(self._transactions)
        self._transactions.clear()
        self._save_transactions()
        return count
