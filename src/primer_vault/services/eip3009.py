# EIP-3009 Payment Signing for x402
# Self-contained implementation - no external SDK dependencies
# https://eips.ethereum.org/EIPS/eip-3009

import secrets
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from ..networks import NAME_TO_CAIP


# x402 protocol version
X402_VERSION = 2


@dataclass
class PaymentRequirements:
    """Parsed payment requirements from a 402 response.

    This is Vault's single, version-neutral representation. Whichever x402
    dialect came in on the wire (v1 or v2), the amount lives in
    `max_amount_required` and the resource in `resource` - downstream code
    (policy checks, signing, receipts) never touches the raw dialect fields.
    """
    scheme: str
    network: str  # CAIP-2 format (e.g., 'eip155:4663')
    chain_id: int
    max_amount_required: str  # atomic units, as a string
    asset: str
    pay_to: str
    resource: Optional[str] = None
    token_name: Optional[str] = None
    token_version: Optional[str] = None
    x402_version: int = 2  # the wire version this was parsed from (1 or 2)


def parse_caip_network(network: str) -> int:
    """
    Extract chain ID from CAIP-2 network identifier.

    Args:
        network: CAIP-2 format like 'eip155:4663' or just chain ID like '4663'

    Returns:
        Chain ID as integer
    """
    if network.startswith("eip155:"):
        return int(network.split(":")[1])
    # Assume it's just a chain ID
    return int(network)


def to_caip_network(network: str) -> str:
    """
    Ensure network is in CAIP-2 format.

    Args:
        network: Network identifier (may or may not have eip155: prefix)

    Returns:
        CAIP-2 format string
    """
    if network.startswith("eip155:"):
        return network
    # Legacy name mapping is derived from the central registry (networks.py)
    if network.lower() in NAME_TO_CAIP:
        return NAME_TO_CAIP[network.lower()]
    # Assume it's a chain ID
    try:
        chain_id = int(network)
        return f"eip155:{chain_id}"
    except ValueError:
        # Return as-is, let it fail later with a clear error
        return network


def _resolve_x402_version(x402_data: Dict[str, Any], req: Dict[str, Any]) -> int:
    """
    Decide which x402 dialect a payment is, decisively.

    Rule (agreed):
    - If `x402Version` is present, it is authoritative. Reject if the body's
      amount field contradicts it - we do not guess a payment past a
      mismatched version tag.
    - If `x402Version` is absent (legacy v1 emitters often omit it), infer from
      the one field that differs between dialects: the amount field name.
      `maxAmountRequired` -> v1, `amount` -> v2. Both or neither -> reject.

    Returns the resolved version (1 or 2); raises ValueError otherwise.
    """
    has_v1_amount = req.get("maxAmountRequired") is not None
    has_v2_amount = req.get("amount") is not None

    declared = x402_data.get("x402Version")
    if declared is not None:
        if declared not in (1, 2):
            raise ValueError(f"Unsupported x402Version: {declared!r} (expected 1 or 2)")
        if declared == 1 and not has_v1_amount and has_v2_amount:
            raise ValueError(
                "x402Version is 1 but the amount is given as the v2 field 'amount' "
                "(expected 'maxAmountRequired'); refusing to guess"
            )
        if declared == 2 and not has_v2_amount and has_v1_amount:
            raise ValueError(
                "x402Version is 2 but the amount is given as the v1 field 'maxAmountRequired' "
                "(expected 'amount'); refusing to guess"
            )
        return declared

    # No declared version - infer from the amount field name.
    if has_v1_amount and has_v2_amount:
        raise ValueError(
            "Ambiguous payment: both 'maxAmountRequired' (v1) and 'amount' (v2) present "
            "and no x402Version to disambiguate"
        )
    if has_v1_amount:
        return 1
    if has_v2_amount:
        return 2
    raise ValueError(
        "Missing amount: expected 'maxAmountRequired' (v1) or 'amount' (v2) in accepts"
    )


def _extract_resource(x402_data: Dict[str, Any], req: Dict[str, Any], version: int) -> Optional[str]:
    """
    Pull the resource URL out, tolerant of both dialects.

    - v2: resource is a top-level object {url, description, mimeType}.
    - v1: resource is a per-accept string.
    We prefer the location the resolved version specifies but fall back to the
    other so a slightly-off emitter still yields a usable URL.
    """
    top = x402_data.get("resource")
    top_url = top.get("url") if isinstance(top, dict) else top
    per_accept = req.get("resource")
    if version == 2:
        return top_url or per_accept
    return per_accept or top_url


def _select_offer(accepts: list) -> Any:
    """Pick the offer Vault can settle, from a list of alternatives.

    Supported means: a chain Vault knows, and USDG on that chain. The choice is
    a convenience - every gate that matters runs afterwards on whatever is
    returned here.
    """
    from ..networks import NETWORKS, TOKENS

    usdg = TOKENS["USDG"].addresses
    for entry in accepts:
        if not isinstance(entry, dict):
            continue
        asset = entry.get("asset")
        network = entry.get("network")
        if not asset or not network:
            continue
        try:
            chain_id = parse_caip_network(to_caip_network(str(network)))
        except (ValueError, IndexError):
            continue
        expected = usdg.get(chain_id)
        if chain_id in NETWORKS and expected and str(asset).lower() == expected.lower():
            return entry

    # Nothing supported. Return the first so the error names its asset rather
    # than saying only that none of them matched.
    return accepts[0]


def parse_x402(x402_data: Dict[str, Any]) -> PaymentRequirements:
    """
    Parse an x402 payment requirement (v1 or v2) into the neutral
    PaymentRequirements. This is the single intake parser - the only place that
    knows the difference between the dialects. Everything downstream reads the
    result, never the raw wire fields.

    Raises:
        ValueError: on any malformed or contradictory payload (never guesses).
    """
    if not isinstance(x402_data, dict):
        raise ValueError("x402 data must be an object")

    accepts = x402_data.get("accepts")
    if accepts is None:
        raise ValueError("Missing 'accepts' array")
    if not isinstance(accepts, list):
        raise ValueError("'accepts' must be an array")
    if len(accepts) == 0:
        raise ValueError("Empty 'accepts' array")

    # `accepts` is a list of alternatives by design - a merchant that serves
    # several chains offers each of them. Reading only the first meant a service
    # offering [USDC on Base, USDG on RHC] was refused for an unsupported asset
    # while offering exactly what Vault pays in.
    #
    # This picks the first entry Vault could actually settle, and falls back to
    # the first entry so an unsupported payload still produces the same specific
    # error it did before rather than a vaguer one. Choosing here does not widen
    # what is allowed: the asset and chain are still checked against policy
    # afterwards, and that check is what decides.
    req = _select_offer(accepts)
    if not isinstance(req, dict):
        raise ValueError("Invalid accepts[0] format")

    version = _resolve_x402_version(x402_data, req)

    # Amount - read the field the resolved version dictates.
    if version == 1:
        amount = req.get("maxAmountRequired")
        if amount is None:
            raise ValueError("Missing 'maxAmountRequired' in accepts")
    else:
        amount = req.get("amount")
        if amount is None:
            raise ValueError("Missing 'amount' in accepts")

    network = req.get("network")
    if not network:
        raise ValueError("Missing 'network' in accepts")

    pay_to = req.get("payTo")
    if not pay_to:
        raise ValueError("Missing 'payTo' in accepts")

    asset = req.get("asset")
    if not asset:
        raise ValueError("Missing 'asset' in accepts")

    # Normalize to CAIP-2. Rebuild the string from the parsed chain id rather
    # than keeping the payee's raw text - a merchant controls this field, and
    # anything after the chain id (newlines, a fabricated recipient block) would
    # otherwise be carried onto the approval dialog verbatim.
    chain_id = parse_caip_network(to_caip_network(network))
    caip_network = f"eip155:{chain_id}"

    # Extract token metadata from 'extra' field if available (same in both dialects)
    extra = req.get("extra") or {}

    return PaymentRequirements(
        scheme=req.get("scheme", "exact"),
        network=caip_network,
        chain_id=chain_id,
        max_amount_required=str(amount),
        asset=asset,
        pay_to=pay_to,
        resource=_extract_resource(x402_data, req, version),
        token_name=extra.get("name"),
        token_version=extra.get("version"),
        x402_version=version,
    )


def sign_transfer_authorization(
    private_key: str,
    chain_id: int,
    token_address: str,
    token_name: str,
    token_version: str,
    from_address: str,
    to_address: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes
) -> str:
    """
    Sign an EIP-3009 TransferWithAuthorization message.

    This creates a signature that authorizes a transfer of tokens
    from one address to another, without requiring gas from the sender.

    Args:
        private_key: Hex-encoded private key (with or without 0x prefix)
        chain_id: EVM chain ID
        token_address: ERC-20 token contract address
        token_name: Token name (for EIP-712 domain)
        token_version: Token version (for EIP-712 domain)
        from_address: Address tokens are transferred from
        to_address: Address tokens are transferred to
        value: Amount in token's smallest unit
        valid_after: Unix timestamp after which the authorization is valid
        valid_before: Unix timestamp before which the authorization is valid
        nonce: 32-byte random nonce

    Returns:
        Hex-encoded signature with 0x prefix
    """
    # Ensure private key has 0x prefix
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)

    # EIP-712 typed data structure for TransferWithAuthorization
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"}
            ]
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": token_name,
            "version": token_version,
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(token_address)
        },
        "message": {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce
        }
    }

    # Encode and sign
    encoded = encode_typed_data(full_message=typed_data)
    signed = account.sign_message(encoded)

    # Return signature with 0x prefix
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex
    return sig_hex


def build_transfer_authorization_typed_data(
    chain_id: int,
    token_address: str,
    token_name: str,
    token_version: str,
    from_address: str,
    to_address: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes
) -> Dict[str, Any]:
    """
    Build EIP-712 typed data for TransferWithAuthorization without signing.

    Used for hardware wallet signing where the signing happens on the device.

    Args:
        chain_id: EVM chain ID
        token_address: ERC-20 token contract address
        token_name: Token name (for EIP-712 domain)
        token_version: Token version (for EIP-712 domain)
        from_address: Address tokens are transferred from
        to_address: Address tokens are transferred to
        value: Amount in token's smallest unit
        valid_after: Unix timestamp after which the authorization is valid
        valid_before: Unix timestamp before which the authorization is valid
        nonce: 32-byte random nonce

    Returns:
        EIP-712 typed data dictionary
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"}
            ]
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": token_name,
            "version": token_version,
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(token_address)
        },
        "message": {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce
        }
    }


def create_payment_from_signature(
    from_address: str,
    requirements: PaymentRequirements,
    signature: str,
    valid_after: int,
    valid_before: int,
    nonce_hex: str,
    accepted: Optional[Dict[str, Any]] = None,
    resource: Any = None,
) -> Dict[str, Any]:
    """
    Create an x402 v2 PaymentPayload from a pre-existing signature.

    Used for hardware wallet signing where the signature comes from the device.

    Args:
        from_address: The signer's address
        requirements: Parsed payment requirements
        signature: Hex-encoded signature from the hardware wallet
        valid_after: Unix timestamp used in the signature
        valid_before: Unix timestamp used in the signature
        nonce_hex: Hex-encoded nonce used in the signature (with 0x prefix)
        accepted: The merchant's selected `accepts[]` entry
        resource: The request's top-level resource

    Returns:
        x402 v2 PaymentPayload
    """
    # Ensure signature has 0x prefix
    if not signature.startswith("0x"):
        signature = "0x" + signature

    payment = {
        "x402Version": X402_VERSION,
        "accepted": _build_accepted(requirements, accepted),
        "payload": {
            "signature": signature,
            "authorization": {
                "from": from_address,
                "to": requirements.pay_to,
                "value": requirements.max_amount_required,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_hex
            }
        }
    }
    resource_obj = _normalize_resource(resource if resource is not None else requirements.resource)
    if resource_obj is not None:
        payment["resource"] = resource_obj
    return payment


def _build_accepted(requirements: PaymentRequirements, accepted_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build the `accepted` block for a v2 PaymentPayload: the chosen payment
    requirement echoed back to the server. Uses the normalized values from
    `requirements` (correct v2 field names, CAIP-2 network) and carries through
    `maxTimeoutSeconds` / `extra` from the merchant's original offer. `extra`
    (the token's EIP-712 domain name/version) matters — the server reads it to
    reconstruct the verification domain.
    """
    raw = accepted_raw if isinstance(accepted_raw, dict) else {}
    accepted = {
        "scheme": requirements.scheme,
        "network": requirements.network,  # CAIP-2
        "amount": requirements.max_amount_required,
        "asset": requirements.asset,
        "payTo": requirements.pay_to,
    }
    if "maxTimeoutSeconds" in raw:
        accepted["maxTimeoutSeconds"] = raw["maxTimeoutSeconds"]
    extra = raw.get("extra")
    if extra is not None:
        accepted["extra"] = extra
    elif requirements.token_name is not None or requirements.token_version is not None:
        accepted["extra"] = {"name": requirements.token_name, "version": requirements.token_version}
    return accepted


def _normalize_resource(resource: Any) -> Optional[Dict[str, Any]]:
    """Return the top-level v2 resource object {url, description, mimeType}."""
    if isinstance(resource, dict):
        return resource
    if isinstance(resource, str):
        return {"url": resource, "description": "", "mimeType": ""}
    return None


def create_payment(
    private_key: str,
    requirements: PaymentRequirements,
    token_name: Optional[str] = None,
    token_version: Optional[str] = None,
    accepted: Optional[Dict[str, Any]] = None,
    resource: Any = None,
) -> Dict[str, Any]:
    """
    Create a signed x402 v2 PaymentPayload.

    Args:
        private_key: Hex-encoded private key
        requirements: Parsed payment requirements
        token_name: Token name (defaults to requirements or 'Global Dollar')
        token_version: Token version (defaults to requirements or '1')
        accepted: The merchant's selected `accepts[]` entry (echoed back in the
            v2 `accepted` block). Falls back to `requirements` if not given.
        resource: The request's top-level resource (object or url string),
            emitted as the v2 top-level `resource`.

    Returns:
        x402 v2 PaymentPayload ready for the PAYMENT-SIGNATURE header, shaped as
        {x402Version, accepted, resource?, payload{signature, authorization}}.
    """
    # Ensure private key format
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)
    from_address = account.address

    # Use provided token metadata, fall back to requirements, then defaults.
    # The domain normally comes from the payment request's `extra`; this literal
    # is only the last-resort fallback. "Global Dollar"/"1" is USDG's EIP-712
    # domain, verified on RHC (chain 4663) against the on-chain DOMAIN_SEPARATOR.
    name = token_name or requirements.token_name or "Global Dollar"
    version = token_version or requirements.token_version or "1"

    # Generate random nonce (32 bytes)
    nonce_bytes = secrets.token_bytes(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    # Time bounds
    now = int(time.time())
    valid_after = now - 60  # Valid 1 minute ago (clock skew tolerance)
    valid_before = now + 3600  # Valid for 1 hour

    # Sign the authorization
    signature = sign_transfer_authorization(
        private_key=private_key,
        chain_id=requirements.chain_id,
        token_address=requirements.asset,
        token_name=name,
        token_version=version,
        from_address=from_address,
        to_address=requirements.pay_to,
        value=int(requirements.max_amount_required),
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=nonce_bytes
    )

    # Build x402 v2 PaymentPayload: the chosen requirement is echoed back in
    # `accepted` (with `extra`), the resource sits at the top level, and only
    # `authorization` is signed. scheme/network live inside `accepted`, not at
    # the top level (that was the v1 shape).
    payment = {
        "x402Version": X402_VERSION,
        "accepted": _build_accepted(requirements, accepted),
        "payload": {
            "signature": signature,
            "authorization": {
                "from": from_address,
                "to": requirements.pay_to,
                "value": requirements.max_amount_required,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_hex
            }
        }
    }
    resource_obj = _normalize_resource(resource if resource is not None else requirements.resource)
    if resource_obj is not None:
        payment["resource"] = resource_obj
    return payment
