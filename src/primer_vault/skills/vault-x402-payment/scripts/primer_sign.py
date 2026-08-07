#!/usr/bin/env python3
"""
Primer Signing Helper - Sign x402 payment requests for Primer.

Uses HMAC-SHA256 for signing (stdlib only, no extra dependencies).

Usage:
    python primer_sign.py <agent_code> <agent_token> <payment_required_header> [request_url]

Or import and use directly:
    from primer_sign import sign_request, send_to_primer

Idempotency:
    Primer uses signature-based idempotency. The signature includes a timestamp,
    so calling sign_request() twice generates different signatures (= new requests).
    To retry the same request (get cached result), reuse the same signed payload.
"""

import hmac
import hashlib
import json
import sys
import time
import urllib.request


def sign_request(
    agent_code: str,
    agent_token: str,
    payment_required: str,
    request_url: str = None
) -> dict:
    """
    Sign a request for Primer using HMAC-SHA256.

    Args:
        agent_code: Your agent code (e.g., "ABC123")
        agent_token: Your agent token (e.g., "AT_abc123...")
        payment_required: The Payment-Required header value from the 402 response
        request_url: The URL you fetched (for domain verification/audit trail)

    Returns:
        The signed request ready to POST to Primer's /sign endpoint
    """
    # Extract shared secret from token (strip "AT_" prefix)
    shared_secret = bytes.fromhex(agent_token[3:])

    # Create message to sign
    timestamp = int(time.time())
    message_data = {
        "agent_code": agent_code,
        "timestamp": timestamp,
        "payment_required": payment_required
    }
    if request_url:
        message_data["request_url"] = request_url
    message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode()

    # Sign with HMAC-SHA256
    sig = hmac.new(shared_secret, message, hashlib.sha256).hexdigest()

    result = {
        "agent_code": agent_code,
        "signature": f"SIG:{timestamp}:{sig}",
        "payment_required": payment_required
    }
    if request_url:
        result["request_url"] = request_url
    return result


def send_to_primer(signed_request: dict, primer_url: str = "http://localhost:4663") -> dict:
    """
    Send a signed request to Primer and get the payment header.

    Returns the Primer response with payment header on success.
    """
    url = f"{primer_url}/sign"
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
        print("Usage: python primer_sign.py <agent_code> <agent_token> <payment_required_header> [request_url]")
        sys.exit(1)

    agent_code = sys.argv[1]
    agent_token = sys.argv[2]
    payment_required = sys.argv[3]
    request_url = sys.argv[4] if len(sys.argv) > 4 else None

    signed = sign_request(agent_code, agent_token, payment_required, request_url)
    result = send_to_primer(signed)
    print(json.dumps(result, indent=2))
