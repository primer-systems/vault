"""
Models package - Data models for Vault.

Contains:
- SpendPolicy: Reusable spending rules
- Agent: Registered AI agents with authentication tokens
- Transaction: Payment request history
- PolicyStore: JSON persistence
"""

from .policy import SpendPolicy, TradingRules
from .agent import (
    Agent,
    generate_agent_token,
    generate_agent_id,
    verify_agent_hmac,
    verify_bearer_token,
    hash_bearer_token,
    generate_intent_mandate,
    encrypt_agent_secret,
    decrypt_agent_secret,
)
from .transaction import (
    Transaction,
    STATUS_RECEIVED,
    STATUS_SIGNED,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_SETTLED,
    STATUS_FAILED,
    TYPE_X402,
    TYPE_TRADE,
    TYPE_TRANSFER,
)
from .store import PolicyStore

__all__ = [
    "SpendPolicy",
    "TradingRules",
    "Agent",
    "generate_agent_token",
    "generate_agent_id",
    "verify_agent_hmac",
    "verify_bearer_token",
    "hash_bearer_token",
    "generate_intent_mandate",
    "encrypt_agent_secret",
    "decrypt_agent_secret",
    "Transaction",
    "STATUS_RECEIVED",
    "STATUS_SIGNED",
    "STATUS_REJECTED",
    "STATUS_SUBMITTED",
    "STATUS_SETTLED",
    "STATUS_FAILED",
    "TYPE_X402",
    "TYPE_TRADE",
    "TYPE_TRANSFER",
    "PolicyStore",
]
