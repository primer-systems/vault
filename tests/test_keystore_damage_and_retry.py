"""What the user is told when a keystore is damaged, and what survives a
restart in the x402 retry path.

Each test encodes the safe behaviour as its assertion.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS

PHRASE = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# A. a keystore damaged inside its encrypted payload
# ---------------------------------------------------------------------------
# load() classifies structural damage (bad JSON, no wrapped_key) as
# CorruptedWalletFile. Damage inside a seed's ciphertext passes those checks:
# the password unwraps the master key fine, and the failure surfaces from
# decrypt_with_key instead - as ValueError (bad hex) or InvalidTag (flipped
# bytes). Vault.load_wallet maps the first onto "Wrong password" and the
# second onto the catch-all.

def _wallet_with_a_seed(path: Path):
    from primer_vault.wallet import VaultWallet

    wallet = VaultWallet.create("password-123")
    seed_id = wallet.add_seed(PHRASE, "m/44'/60'/0'/0/{}")
    wallet.add_address_from_seed(seed_id, 0)
    wallet.save(path)
    return wallet


def test_damaged_seed_ciphertext_is_reported_as_damage_not_wrong_password(
        temp_data_dir):
    """Hex inside a seed entry mangled: the correct password still opens the
    master key, so "Wrong password" is the wrong answer."""
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        path = temp_data_dir / "wallets" / "w.wallet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _wallet_with_a_seed(path)

        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["seeds"][0]["encrypted_phrase"] = (
            "zz" + doc["seeds"][0]["encrypted_phrase"][2:])
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        result = core.load_wallet(str(path), "password-123")

        assert result["success"] is False
        assert result.get("code") != "WRONG_PASSWORD", (
            "a damaged keystore was reported as a wrong password: "
            f"{result.get('error')!r}")
    finally:
        core.release_instance_lock()


def test_flipped_seed_bytes_produce_a_message_the_user_can_act_on(temp_data_dir):
    """Ciphertext still valid hex but altered: AES-GCM rejects it with an
    exception whose text is empty, so the interface has nothing to show."""
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    try:
        path = temp_data_dir / "wallets" / "w2.wallet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _wallet_with_a_seed(path)

        doc = json.loads(path.read_text(encoding="utf-8"))
        blob = doc["seeds"][0]["encrypted_phrase"]
        flipped = ("00" if blob[:2] != "00" else "11") + blob[2:]
        doc["seeds"][0]["encrypted_phrase"] = flipped
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        result = core.load_wallet(str(path), "password-123")

        assert result["success"] is False
        assert result.get("error"), (
            "the wallet failed to open with an empty error message; the "
            "unlock screen shows the user 'Error: ' and nothing else")
    finally:
        core.release_instance_lock()


# ---------------------------------------------------------------------------
# B. the x402 retry guard does not survive a restart
# ---------------------------------------------------------------------------

def _x402(amount_micro, index=0):
    pay_to = "0x" + ("65" * 19) + f"{index:02x}"
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": str(amount_micro),
            "asset": TOKENS["USDG"].addresses[4663],
            "payTo": pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


def _commission(core, *, daily, per_req, auto, name="Payer"):
    agent, token = core.create_agent(name=name, auth_mode="bearer")
    policy = core.create_policy(
        name=f"P-{name}", networks=[4663],
        daily_limit_micro=daily, per_request_max_micro=per_req,
        auto_approve_below_micro=auto)
    address = core.get_wallet_addresses()[0]
    core.commission_agent(agent.code, policy.id, address["address"])
    return core._signing_service, agent, token, policy


def test_an_identical_retry_in_one_session_is_charged_once(core):
    """The guard that exists: same agent, same credential, same payload."""
    svc, agent, token, policy = _commission(
        core, daily=10_000_000, per_req=5_000_000, auto=10_000_000, name="Retry")

    first = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))
    second = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))

    assert first["status"] == "success", first
    assert second["status"] == "success", second
    assert second["transaction_id"] == first["transaction_id"]
    assert core.get_agent_by_code(agent.code).spent_today_micro == 2_000_000


def test_an_identical_retry_after_a_restart_is_still_charged_once(
        core, temp_data_dir):
    """A crash between the payment being handed out and the agent's retry must
    not turn one purchase into two independently redeemable authorizations."""
    from primer_vault.core import Vault

    svc, agent, token, policy = _commission(
        core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
        name="Crashed")

    first = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))
    assert first["status"] == "success", first
    core.release_instance_lock()

    restarted = Vault(data_dir=temp_data_dir)
    try:
        restarted.load_wallet(
            str(Path(temp_data_dir) / "wallets" / "test.wallet"), "testpass")
        again = restarted._signing_service.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))

        spent = restarted.get_agent_by_code(agent.code).spent_today_micro
        assert not (again.get("status") == "success"
                    and again.get("header_value") != first.get("header_value")), (
            "a second, independently redeemable payment authorization was "
            "issued for the same request after a restart")
        assert spent == 2_000_000, f"spend doubled to {spent} on a retry"
    finally:
        restarted.release_instance_lock()


def test_a_replay_of_an_already_settled_payment_is_refused_after_a_restart(
        core, temp_data_dir):
    """In-session, a replay whose transaction is settled is refused with
    PAYMENT_ALREADY_SETTLED. The evidence is on disk, so a restart should not
    change the answer."""
    from primer_vault.core import Vault

    svc, agent, token, policy = _commission(
        core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
        name="Settled")

    first = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(3_000_000))
    assert first["status"] == "success", first

    tx = core.get_transaction(first["transaction_id"])
    tx.mark_settled("0x" + "ab" * 32)
    core._policy_store.update_transaction(tx)

    in_session = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=_x402(3_000_000))
    assert in_session.get("code") == "PAYMENT_ALREADY_SETTLED", in_session

    core.release_instance_lock()
    restarted = Vault(data_dir=temp_data_dir)
    try:
        restarted.load_wallet(
            str(Path(temp_data_dir) / "wallets" / "test.wallet"), "testpass")
        after_restart = restarted._signing_service.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(3_000_000))

        assert after_restart.get("code") == "PAYMENT_ALREADY_SETTLED", (
            "after a restart, the same request that was refused as already "
            f"settled was signed again: {after_restart.get('status')}")
    finally:
        restarted.release_instance_lock()
