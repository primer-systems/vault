"""
Policy Store - JSON persistence for policies, agents, and transactions.

Provides a unified storage manager for all application data.
"""

import os
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .policy import SpendPolicy
from .agent import Agent
from .transaction import Transaction
from ..utils import fsync_dir, validate_name

# Secure file permissions (Unix only)
SECURE_FILE_MODE = 0o600  # Owner read/write only


def _set_secure_permissions(filepath: Path) -> None:
    """Set restrictive file permissions on Unix systems."""
    if os.name == 'posix':
        try:
            os.chmod(filepath, SECURE_FILE_MODE)
        except OSError:
            pass

def write_json_atomic(filepath: Path, data) -> None:
    """Write JSON to filepath so the file is never seen partially written.

    Public because settings.py saves through it too. One writer, one set
    of guarantees.

    Opening the target directly truncates it before the new contents are written,
    which leaves a window where the file is empty or half-finished. An interruption
    in that window - a crash, a power loss - destroys the data, because the old
    version was already gone. Two writers at once land in the same window and
    interleave.

    Instead the contents go to a temporary file in the same directory, are flushed
    all the way to disk, and are then moved onto the target. os.replace is atomic
    on POSIX and Windows alike, so a reader sees either the whole previous file or
    the whole new one. The temporary file is created alongside the target rather
    than in the system temp dir because a move is only atomic within one filesystem.

    Permissions are applied to the temporary file before the move, so the data is
    never briefly readable with default permissions.

    The directory is fsynced after the move, because the rename itself is a
    directory-entry change and stays in the journal until then - see fsync_dir
    for what a power loss in that window would cost.
    """
    filepath = Path(filepath)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(filepath.parent), prefix=f".{filepath.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # survive a power loss, not just a crash
        _set_secure_permissions(tmp_path)
        os.replace(tmp_path, filepath)
        fsync_dir(filepath.parent)  # make the rename durable, not just atomic
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


logger = logging.getLogger(__name__)


class PolicyStore:
    """Manages storage of policies, agents, and transaction history."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.policies_file = data_dir / "policies.json"
        self.agents_file = data_dir / "agents.json"
        self.transactions_file = data_dir / "transactions.json"

        # The agent API serves requests on multiple threads, so two of them can
        # save at the same moment. The atomic write above stops that corrupting
        # the file; this stops the later writer silently discarding the earlier
        # one's changes by serialising the read-modify-write.
        self._save_lock = threading.RLock()

        # Ensure data directory exists
        data_dir.mkdir(parents=True, exist_ok=True)

        # Load data
        self._policies: dict[str, SpendPolicy] = {}
        self._agents: dict[str, Agent] = {}
        self._transactions: list[Transaction] = []
        # Raw entries _load_records could not read, keyed by file name. Every
        # save writes the whole file from memory, so anything not carried in
        # this dict would be erased by the first save after startup — the
        # opposite of the "left as it is, still repairable by hand" promise
        # made at the skip site.
        self._unreadable: dict[str, list] = {}
        # File names that could not be READ at startup and could not be copied
        # aside either — so the file on disk is the only copy of that data.
        # A save writes the whole file from memory, which holds none of it, so
        # overwriting would destroy it. Saves to these files are refused until a
        # restart reads them cleanly. See _load_records and the _save_* guards.
        self._protected: set[str] = set()
        self._load()

    #: Everything a malformed record can raise on the way in. The from_dict
    #: validators raise ValueError for a bad value and TypeError for an
    #: unexpected field; json.JSONDecodeError is a ValueError, so it is covered.
    _RECORD_ERRORS = (ValueError, TypeError, KeyError, AttributeError)

    def _load_records(self, path: Path, build, kind: str) -> list:
        """Read one JSON file and build its records, skipping any that are bad.

        This runs inside PolicyStore's constructor, which runs inside Vault's,
        which runs during startup - so anything raised here is the difference
        between an application that starts and one that cannot. A hand-edited
        file or a half-written save must therefore cost at most the records it
        damaged.

        A record Vault cannot read is a record it cannot honour, so skipping is
        the safe reading: an agent that fails to load cannot spend, and a policy
        that fails to load leaves its agents uncommissioned. Both are reported
        loudly enough to act on, and the file is left as it is rather than
        rewritten, so a recoverable mistake stays recoverable.

        "Left as it is" has to survive the next save too: saves write the whole
        file from memory, so each skipped entry is kept, raw and untouched, in
        self._unreadable and appended back by every _save_* below. A record
        this build cannot read may still be repairable by hand, or readable by
        the newer build that wrote it.
        """
        self._unreadable[path.name] = []
        if not path.exists():
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            aside = self._set_aside(path)
            if aside is None:
                # Could not read the file AND could not copy it aside (typically
                # a file that cannot be opened at all - the copy fails at the
                # same door the read did). The original is now the only copy, so
                # the next save must not overwrite it with an empty file.
                self._protected.add(path.name)
                logger.error(
                    "Could not read %s (%s) and could not set a copy aside. "
                    "Starting with none; the file will be left untouched, not "
                    "overwritten, so it stays recoverable until a restart can "
                    "read it.", path.name, e)
            else:
                logger.error(
                    "Could not read %s (%s). Starting with none; a copy has "
                    "been kept as %s.", path.name, e, aside)
            return []

        if not isinstance(data, list):
            aside = self._set_aside(path)
            if aside is None:
                self._protected.add(path.name)
            logger.error("%s does not contain a list of %s; ignoring it. A "
                         "copy has been kept as %s.", path.name, kind,
                         aside or "(copy failed; the original is untouched)")
            return []

        records = []
        for index, item in enumerate(data):
            try:
                records.append(build(item))
            except self._RECORD_ERRORS as e:
                self._unreadable[path.name].append(item)
                logger.error("Skipping unreadable %s at position %d in %s: %s",
                             kind, index, path.name, e)

        if self._unreadable[path.name]:
            logger.error(
                "%d of %d %s in %s could not be read and were skipped. Vault "
                "has started without them; they stay in the file, untouched, "
                "until they are repaired or removed.",
                len(self._unreadable[path.name]), len(data), kind, path.name)

        return records

    def _set_aside(self, path: Path) -> Optional[str]:
        """Keep a copy of a file Vault could not read at all before the next
        save replaces it. Best effort — the copy failing must not stop startup,
        for the same reason the unreadable file itself must not.

        Returns the copy's name on success, or None if the copy could not be
        made (the caller then protects the original from being overwritten).
        """
        aside = path.with_name(path.name + ".unreadable")
        try:
            import shutil
            shutil.copy2(path, aside)
            _set_secure_permissions(aside)
        except OSError:
            return None
        return aside.name

    def _load(self) -> None:
        """Load policies, agents and transactions from disk."""
        for policy in self._load_records(
                self.policies_file, SpendPolicy.from_dict, "policies"):
            self._policies[policy.id] = policy

        # Agents are indexed by code, which is the internal UUID.
        for agent in self._load_records(
                self.agents_file, Agent.from_dict, "agents"):
            self._agents[agent.code] = agent

        self._transactions.extend(self._load_records(
            self.transactions_file, Transaction.from_dict, "transactions"))

    def is_transactions_protected(self) -> bool:
        """True if the transactions ledger could not be read at startup and is
        now protected from writes — so a new transaction cannot be recorded and
        the on-disk idempotency guard is blind. A caller that must record before
        it acts (payment signing) checks this and refuses, rather than acting on
        something it cannot log."""
        return self.transactions_file.name in self._protected

    def _refuse_if_protected(self, path: Path) -> bool:
        """True if `path` must not be overwritten — it could not be read at
        startup and no copy was kept, so the file on disk is the only copy of
        its data. Writing memory (which never loaded it) over it would erase it.
        """
        if path.name in self._protected:
            logger.error(
                "Not saving %s: it could not be read at startup and no copy "
                "was kept, so it is left untouched to stay recoverable. Restart "
                "Vault while the file is readable to resume saving to it.",
                path.name)
            return True
        return False

    def _save_policies(self) -> None:
        """Save policies to disk."""
        with self._save_lock:
            if self._refuse_if_protected(self.policies_file):
                return
            data = [p.to_dict() for p in self._policies.values()]
            data += self._unreadable.get(self.policies_file.name, [])
            write_json_atomic(self.policies_file, data)

    def _save_agents(self) -> None:
        """Save agents to disk."""
        with self._save_lock:
            if self._refuse_if_protected(self.agents_file):
                return
            data = [a.to_dict() for a in self._agents.values()]
            data += self._unreadable.get(self.agents_file.name, [])
            write_json_atomic(self.agents_file, data)

    def _save_transactions(self) -> None:
        """Save transactions to disk."""
        with self._save_lock:
            if self._refuse_if_protected(self.transactions_file):
                return
            data = [t.to_dict() for t in self._transactions]
            data += self._unreadable.get(self.transactions_file.name, [])
            write_json_atomic(self.transactions_file, data)

    # Policy methods

    def add_policy(self, policy: SpendPolicy) -> None:
        """Add a new policy. Raises ValueError if name is invalid or already exists."""
        with self._save_lock:
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
        with self._save_lock:
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
        with self._save_lock:
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

    # ---- Agents ----

    def add_agent(self, agent: Agent) -> None:
        """Add a new agent. Raises ValueError if name is invalid or already exists."""
        with self._save_lock:
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
        with self._save_lock:
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
        with self._save_lock:
            if agent_code in self._agents:
                del self._agents[agent_code]
                self._save_agents()

    # ---- Transactions ----

    def add_transaction(self, tx: Transaction) -> None:
        """Add a new transaction."""
        with self._save_lock:
            self._transactions.insert(0, tx)  # Most recent first
            self._save_transactions()

    def get_transaction(self, tx_id: str) -> Optional[Transaction]:
        """Get a transaction by ID."""
        for tx in self._transactions:
            if tx.id == tx_id:
                return tx
        return None

    def get_transaction_by_cache_key(self, cache_key: str) -> Optional[Transaction]:
        """The transaction recorded for a request's idempotency key, if any.

        Used to recognise a duplicate payment request after a restart has
        emptied the in-memory signature cache.
        """
        if not cache_key:
            return None
        for tx in self._transactions:
            if tx.cache_key == cache_key:
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
        with self._save_lock:
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
        with self._save_lock:
            count = len(self._transactions)
            self._transactions.clear()
            # An explicit "clear everything" covers the entries that could not
            # be read, too — this is the one save allowed to drop them.
            self._unreadable[self.transactions_file.name] = []
            self._save_transactions()
            return count
