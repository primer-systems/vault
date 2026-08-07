"""
x402 v2 support tests.

Covers the version-neutral intake parser (eip3009.parse_x402) and, critically,
that the amount actually SIGNED on the wire equals the amount the merchant
quoted - for both dialects. A v2 402 that only asserted "no error" would have
passed even when the old code silently signed value 0, so these tests assert
the concrete signed value.

Dialect reference (official coinbase/x402):
- v1: amount field is `maxAmountRequired`; `resource` is a per-accept string.
- v2: amount field is `amount`; `resource` is a top-level object {url,...}.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services.eip3009 import parse_x402, create_payment
from primer_vault.services.signing import (
    validate_x402_request,
    decode_payment_required_header,
)

# A throwaway test key (Hardhat account #1) - never used for real funds.
TEST_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

# A synthetic v2 payment_required (base64) on Robinhood Chain / USDG, adapted from a
# real Megalith payload (api.megalithlabs.ai) for dialect-parsing tests.
MEGALITH_V2_B64 = (
    "eyJ4NDAyVmVyc2lvbiI6IDIsICJlcnJvciI6ICJQYXltZW50IHJlcXVpcmVkIiwgInJlc291cmNlIjogeyJ1cmwiOiAiaHR0cDovL2FwaS5tZWdhbGl0aGxhYnMuYWkvYXNzZXRzL29ydGhvL29ydGhvLXIzLWM0LnRpZiIsICJkZXNjcmlwdGlvbiI6ICIiLCAibWltZVR5cGUiOiAiIn0sICJhY2NlcHRzIjogW3sic2NoZW1lIjogImV4YWN0IiwgIm5ldHdvcmsiOiAiZWlwMTU1OjQ2NjMiLCAiYW1vdW50IjogIjEwMDAiLCAiYXNzZXQiOiAiMHg1ZmM1MzYwRDA0MDBhMEZkNGYyYWY1NTJBREQwNDJENzE2RjFkMTY4IiwgInBheVRvIjogIjB4NjVCQzU1NTVhMjVlMjQ1NjlFNjlCQjBBZjNmNTFjNEVCZEE0MmU2ZCIsICJtYXhUaW1lb3V0U2Vjb25kcyI6IDYwLCAiZXh0cmEiOiB7Im5hbWUiOiAiR2xvYmFsIERvbGxhciIsICJ2ZXJzaW9uIjogIjEifX1dfQ=="
)

# A minimal v1 payload equivalent (same amount/recipient/asset, v1 field names).
V1_PAYLOAD = {
    "x402Version": 1,
    "accepts": [{
        "scheme": "exact",
        "network": "robinhood",
        "maxAmountRequired": "1000",
        "asset": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "payTo": "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d",
        "resource": "http://api.megalithlabs.ai/assets/ortho/ortho-r3-c4.tif",
        "maxTimeoutSeconds": 60,
        "extra": {"name": "Global Dollar", "version": "1"},
    }],
}


def _megalith_v2_dict():
    decoded, version, err = decode_payment_required_header(MEGALITH_V2_B64)
    assert err == "", err
    assert version == 2
    return decoded


# ---------------------------------------------------------------------------
# Parsing - both dialects into the neutral representation
# ---------------------------------------------------------------------------

class TestParseDialects:

    def test_v2_payment_required_parses(self):
        req = parse_x402(_megalith_v2_dict())
        assert req.x402_version == 2
        assert req.max_amount_required == "1000"
        assert req.network == "eip155:4663"
        assert req.pay_to == "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"
        assert req.asset == "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
        # v2 resource is a top-level object; we lift the url out
        assert req.resource == "http://api.megalithlabs.ai/assets/ortho/ortho-r3-c4.tif"

    def test_v2_inline_x402_data_parses(self):
        # Same content handed in directly as JSON (AP2/A2A path), not base64
        data = _megalith_v2_dict()
        req = parse_x402(data)
        assert req.x402_version == 2
        assert req.max_amount_required == "1000"
        assert req.resource.endswith("ortho-r3-c4.tif")

    def test_v1_payload_parses(self):
        req = parse_x402(V1_PAYLOAD)
        assert req.x402_version == 1
        assert req.max_amount_required == "1000"
        assert req.network == "eip155:4663"  # normalized from "robinhood"
        assert req.resource == "http://api.megalithlabs.ai/assets/ortho/ortho-r3-c4.tif"

    def test_v1_without_version_tag_inferred(self):
        # Legacy v1 emitters omit x402Version - inferred from the amount field name
        data = {"accepts": [dict(V1_PAYLOAD["accepts"][0])]}
        req = parse_x402(data)
        assert req.x402_version == 1
        assert req.max_amount_required == "1000"

    def test_v2_without_version_tag_inferred(self):
        data = _megalith_v2_dict()
        data.pop("x402Version")
        req = parse_x402(data)
        assert req.x402_version == 2
        assert req.max_amount_required == "1000"


# ---------------------------------------------------------------------------
# Rejections - never guess a payment past a bad/ambiguous payload
# ---------------------------------------------------------------------------

class TestParseRejections:

    def test_neither_amount_field_rejected(self):
        data = {"x402Version": 2, "accepts": [{
            "network": "eip155:4663", "payTo": "0x" + "ab" * 20, "asset": "0x" + "cd" * 20,
        }]}
        with pytest.raises(ValueError):
            parse_x402(data)

    def test_ambiguous_both_amounts_no_version_rejected(self):
        data = {"accepts": [{
            "network": "eip155:4663", "maxAmountRequired": "1000", "amount": "1000",
            "payTo": "0x" + "ab" * 20, "asset": "0x" + "cd" * 20,
        }]}
        with pytest.raises(ValueError, match="[Aa]mbiguous"):
            parse_x402(data)

    def test_declared_v1_but_v2_field_rejected(self):
        # Claims v1 but sends only the v2 'amount' - refuse rather than reinterpret
        data = {"x402Version": 1, "accepts": [{
            "network": "robinhood", "amount": "1000",
            "payTo": "0x" + "ab" * 20, "asset": "0x" + "cd" * 20,
        }]}
        with pytest.raises(ValueError, match="refusing to guess"):
            parse_x402(data)

    def test_declared_v2_but_v1_field_rejected(self):
        data = {"x402Version": 2, "accepts": [{
            "network": "eip155:4663", "maxAmountRequired": "1000",
            "payTo": "0x" + "ab" * 20, "asset": "0x" + "cd" * 20,
        }]}
        with pytest.raises(ValueError, match="refusing to guess"):
            parse_x402(data)

    def test_unsupported_version_rejected(self):
        data = {"x402Version": 3, "accepts": [{
            "network": "eip155:4663", "amount": "1000",
            "payTo": "0x" + "ab" * 20, "asset": "0x" + "cd" * 20,
        }]}
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            parse_x402(data)


# ---------------------------------------------------------------------------
# The signed value on the wire equals the quoted amount (the point of all this)
# ---------------------------------------------------------------------------

class TestSignedValueMatchesQuote:

    def test_v2_signed_value_equals_quoted_amount(self):
        req = parse_x402(_megalith_v2_dict())
        payment = create_payment(TEST_PRIVATE_KEY, req)
        # This is what actually goes into the PAYMENT-SIGNATURE header.
        assert payment["payload"]["authorization"]["value"] == "1000"
        assert payment["payload"]["authorization"]["to"] == \
            "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"
        # A real signature was produced (not an empty/zero placeholder)
        assert payment["payload"]["signature"].startswith("0x")
        assert len(payment["payload"]["signature"]) > 2

    def test_v1_signed_value_equals_quoted_amount(self):
        req = parse_x402(V1_PAYLOAD)
        payment = create_payment(TEST_PRIVATE_KEY, req)
        assert payment["payload"]["authorization"]["value"] == "1000"


# ---------------------------------------------------------------------------
# Outbound PAYMENT-SIGNATURE payload is v2-shaped (accepted + resource)
# ---------------------------------------------------------------------------

class TestOutboundPayloadShape:

    def _v2_payment(self):
        data = _megalith_v2_dict()
        req = parse_x402(data)
        return create_payment(
            TEST_PRIVATE_KEY, req,
            accepted=data["accepts"][0],
            resource=data.get("resource"),
        )

    def test_v2_payload_has_accepted_block(self):
        p = self._v2_payment()
        assert p["x402Version"] == 2
        acc = p["accepted"]
        # scheme/network live inside accepted, not at the top level
        assert "scheme" not in p and "network" not in p
        assert acc["scheme"] == "exact"
        assert acc["network"] == "eip155:4663"
        assert acc["amount"] == "1000"
        assert acc["payTo"] == "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"

    def test_v2_accepted_carries_extra(self):
        # The server reads accepted.extra to rebuild the EIP-712 domain.
        acc = self._v2_payment()["accepted"]
        assert acc["extra"] == {"name": "Global Dollar", "version": "1"}

    def test_v2_payload_has_top_level_resource(self):
        p = self._v2_payment()
        assert p["resource"]["url"] == "http://api.megalithlabs.ai/assets/ortho/ortho-r3-c4.tif"

    def test_v1_conversion_is_flat(self):
        from primer_vault.services.signing import SigningService
        p = self._v2_payment()
        v1 = SigningService._convert_payment_to_v1(SigningService.__new__(SigningService), p)
        assert v1["x402Version"] == 1
        assert v1["scheme"] == "exact"
        assert v1["network"] == "robinhood"        # v1 network name
        assert "accepted" not in v1 and "resource" not in v1
        assert v1["payload"] == p["payload"]  # signature untouched


# ---------------------------------------------------------------------------
# Cross-product guard: validate wrapper agrees with the parser on version
# ---------------------------------------------------------------------------

class TestValidateWrapper:

    def test_validate_reports_v2_for_megalith(self):
        is_valid, version, error = validate_x402_request(_megalith_v2_dict())
        assert is_valid is True
        assert version == 2
        assert error == ""

    def test_validate_reports_v1(self):
        is_valid, version, error = validate_x402_request(V1_PAYLOAD)
        assert is_valid is True
        assert version == 1
