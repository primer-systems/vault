"""
Signing helper tests.

`GET /sign/helper` serves a script agents run to sign x402 requests. It used to
exist twice - inline in server.py and as a file in the skill folder - and the two
drifted: the shipped file signed the field as "agent_code" while the server
verifies "agent_id", so every signature it produced was rejected with "Invalid
signature", which reads like a credentials problem rather than a bad helper.

There is now one copy. These tests run the served script the way an agent would
and check its output against the server's own verification, so the two cannot
drift apart again without a failure.
"""

import hashlib
import hmac
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services.server import get_signing_helper, _skill_file
from primer_vault.services.signing import SigningService

TOKEN = "AT_" + "ab" * 32
AGENT_ID = "ABC123"
PAYMENT_REQUIRED = "eyJhY2NlcHRzIjpbeyJuZXR3b3JrIjoiZWlwMTU1OjQ2NjMifV19"
REQUEST_URL = "https://api.example.com/resource"


@pytest.fixture(scope="module")
def helper():
    """The served script, executed as a module - exactly what an agent gets."""
    source = get_signing_helper()
    module = types.ModuleType("served_signing_helper")
    exec(compile(source, "served_signing_helper", "exec"), module.__dict__)
    return module


def server_signature(agent_id, token, **signed_fields):
    """Rebuild the message SigningService.verify_agent_signature checks against."""
    message = json.dumps({"agent_id": agent_id, **signed_fields},
                         separators=(',', ':'), sort_keys=True).encode("utf-8")
    return hmac.new(bytes.fromhex(token[3:]), message, hashlib.sha256).hexdigest()


class TestServedHelperIsTheShippedFile:

    def test_helper_is_served_not_reinvented(self):
        """The endpoint returns the skill folder's file, byte for byte."""
        path = _skill_file("vault-x402-payment/scripts/primer_sign.py")
        assert path is not None, "helper script missing from the skills folder"
        assert get_signing_helper() == path.read_text(encoding="utf-8")

    def test_served_helper_is_valid_python(self, helper):
        assert callable(helper.sign_request)
        assert callable(helper.send_to_primer_vault)

    def test_exposes_the_names_the_skill_tells_agents_to_import(self, helper):
        """vault-x402-payment/SKILL.txt documents these two by name."""
        skill = _skill_file("vault-x402-payment/SKILL.txt").read_text(encoding="utf-8")
        assert "sign_request" in skill
        assert "send_to_primer_vault" in skill
        assert hasattr(helper, "sign_request")
        assert hasattr(helper, "send_to_primer_vault")


class TestSignaturesVerifyServerSide:
    """The whole point: what the helper produces, the server must accept."""

    def test_signature_verifies_without_request_url(self, helper):
        signed = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED)
        _, timestamp, signature = signed["signature"].split(":", 2)
        expected = server_signature(AGENT_ID, TOKEN,
                                    timestamp=int(timestamp),
                                    payment_required=PAYMENT_REQUIRED)
        assert hmac.compare_digest(signature, expected)

    def test_signature_verifies_with_request_url(self, helper):
        signed = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED, REQUEST_URL)
        _, timestamp, signature = signed["signature"].split(":", 2)
        expected = server_signature(AGENT_ID, TOKEN,
                                    timestamp=int(timestamp),
                                    payment_required=PAYMENT_REQUIRED,
                                    request_url=REQUEST_URL)
        assert hmac.compare_digest(signature, expected)

    def test_accepted_by_the_real_verifier(self, helper):
        """End to end through SigningService, not a reimplementation of it."""
        signed = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED, REQUEST_URL)

        class FakeAgent:
            auth_mode = "hmac"

            def decrypt_auth_key(self, password):
                # verify_agent_hmac takes the secret as a hex string and decodes
                # it itself, so this must not be pre-decoded to bytes.
                return TOKEN[3:]

        error = SigningService().verify_agent_signature(
            FakeAgent(), AGENT_ID, signed["signature"], "irrelevant",
            {"payment_required": PAYMENT_REQUIRED, "request_url": REQUEST_URL},
        )
        assert error is None, error

    def test_field_name_is_agent_id(self, helper):
        """The regression that shipped: 'agent_code' fails every request."""
        source = get_signing_helper()
        assert '"agent_id"' in source
        assert "agent_code" not in source

        signed = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED)
        assert signed["agent_id"] == AGENT_ID

    def test_request_url_is_echoed_only_when_given(self, helper):
        assert "request_url" not in helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED)
        with_url = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED, REQUEST_URL)
        assert with_url["request_url"] == REQUEST_URL

    def test_each_call_signs_a_fresh_request(self, helper, monkeypatch):
        """Timestamps make signatures unique, which is what drives idempotency."""
        times = iter([1_000, 2_000])
        monkeypatch.setattr(helper.time, "time", lambda: next(times))
        first = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED)
        second = helper.sign_request(AGENT_ID, TOKEN, PAYMENT_REQUIRED)
        assert first["signature"] != second["signature"]


class TestAssetsHaveOneHome:
    """Assets exist in one place, so the logo cannot drift between the frozen
    build and the pip install."""

    def test_assets_resolve_inside_the_package(self):
        from primer_vault.utils import get_assets_dir
        assets = get_assets_dir()
        assert assets.exists()
        assert assets == Path(__file__).parent.parent / "src" / "primer_vault" / "assets"

    def test_no_second_assets_directory_at_the_repo_root(self):
        assert not (Path(__file__).parent.parent / "assets").exists()

    @pytest.mark.parametrize("name", [
        "icon256.ico", "icon256.icns", "wm_stacked.png", "wm_stacked_light.png",
    ])
    def test_expected_assets_are_present(self, name):
        from primer_vault.utils import get_assets_dir
        assert (get_assets_dir() / name).is_file()
