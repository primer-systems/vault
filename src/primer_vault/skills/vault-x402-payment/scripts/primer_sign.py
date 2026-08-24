#!/usr/bin/env python3
"""
Vault Signing Helper - Sign x402 payment requests for Vault.

Uses HMAC-SHA256 for signing (stdlib only, no extra dependencies).

This file is the single copy: it ships inside the skill folder for agents that
run it directly, and Vault serves its contents at GET /sign/helper.

Usage:
    python primer_sign.py <agent_id> <agent_token> <payment_required_header> [request_url]

Or import and use directly:
    from vault_sign import sign_request, send_to_primer_vault

Idempotency:
    Vault uses signature-based idempotency. The signature includes a timestamp,
    so calling sign_request() twice generates different signatures (= new
    requests). To retry the same request and get the cached result, resend the
    same signed payload rather than signing again.
"""

import hmac
import hashlib
import json
import sys
import time
import urllib.request


def sign_request(agent_id: str, agent_token: str, payment_required: str, request_url: str = None) -> dict:
    """
    Sign a request for Vault using HMAC-SHA256.

    Args:
        agent_id: Your agent ID (e.g., "ABC123")
        agent_token: Your agent token (e.g., "AT_abc123...")
        payment_required: The Payment-Required header value from the 402 response
        request_url: Optional URL you fetched (for domain verification)

    Returns:
        The signed request ready to POST to Vault's /sign endpoint
    """
    # Extract shared secret from token (strip "AT_" prefix)
    shared_secret = bytes.fromhex(agent_token[3:])

    # Create message to sign. The field name must be "agent_id": the server
    # rebuilds this exact dict to verify, so any other key fails every request.
    timestamp = int(time.time())
    message_data = {
        "agent_id": agent_id,
        "timestamp": timestamp,
        "payment_required": payment_required
    }
    if request_url:
        message_data["request_url"] = request_url
    message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode()

    # Sign with HMAC-SHA256
    sig = hmac.new(shared_secret, message, hashlib.sha256).hexdigest()

    result = {
        "agent_id": agent_id,
        "signature": f"SIG:{timestamp}:{sig}",
        "payment_required": payment_required
    }
    if request_url:
        result["request_url"] = request_url
    return result


def sign_callback(agent_id: str, agent_token: str, transaction_id: str,
                  event: str, tx_hash: str = None) -> dict:
    """
    Sign a settlement callback for Vault's /callback endpoint.

    A callback writes the payment record, so it is authenticated the same way a
    signing request is. Build it with this rather than by hand: the server
    rebuilds this exact dict to verify, so the field names and nesting matter.

    Args:
        agent_id: Your agent ID (e.g., "ABC123")
        agent_token: Your agent token (e.g., "AT_abc123...")
        transaction_id: The transaction_id Vault returned from /sign
        event: "submitted", "settled", or "failed"
        tx_hash: On-chain hash; required when event is "settled"

    Returns:
        The signed body ready to POST to Vault's /callback endpoint
    """
    shared_secret = bytes.fromhex(agent_token[3:])

    timestamp = int(time.time())
    message_data = {
        "agent_id": agent_id,
        "timestamp": timestamp,
        "callback": {
            "transaction_id": transaction_id,
            "event": event,
            "tx_hash": tx_hash,
        },
    }
    message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode()
    sig = hmac.new(shared_secret, message, hashlib.sha256).hexdigest()

    result = {
        "agent_id": agent_id,
        "signature": f"SIG:{timestamp}:{sig}",
        "transaction_id": transaction_id,
        "event": event,
    }
    if tx_hash is not None:
        result["tx_hash"] = tx_hash
    return result


def send_to_primer_vault(signed_request: dict, primer_vault_url: str = "http://localhost:4663") -> dict:
    """
    Send a signed request to Vault and get the payment header.

    Returns the Vault response with payment header on success.
    """
    url = f"{primer_vault_url}/sign"
    data = json.dumps(signed_request).encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python primer_sign.py <agent_id> <agent_token> <payment_required_header> [request_url]")
        sys.exit(1)

    agent_id = sys.argv[1]
    agent_token = sys.argv[2]
    payment_required = sys.argv[3]
    request_url = sys.argv[4] if len(sys.argv) > 4 else None

    signed = sign_request(agent_id, agent_token, payment_required, request_url)
    result = send_to_primer_vault(signed)
    print(json.dumps(result, indent=2))
