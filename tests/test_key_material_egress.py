"""Key material does not leave the process by any channel.

The real create / load / import / sign / lock paths are exercised with known
secrets while every outbound channel is captured:

  - every logging record emitted anywhere, at any level
  - stdout and stderr
  - every byte of every file written under the data directory
  - the dicts returned to callers (GUI, CLI and admin API all read these)

None of the secrets may appear in any of them. The needle set includes the
private keys derived from the test seed, not only the ones typed in, so the
seed-derived signing path is covered too.
"""

import io
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS

RHC = 4663
USDG = TOKENS["USDG"].addresses[RHC]
PAY_TO = "0x00000000000000000000000000000000000c0De0"

# Known secrets. Distinctive enough that a substring match is meaningful.
MNEMONIC = ("legal winner thank year wave sausage worth useful legal winner "
            "thank yellow")
SECOND_MNEMONIC = "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong"
PKEY = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "another-passphrase-entirely"


def derived_keys() -> list[str]:
    """The private keys the test seed actually produces at indices 0 and 1.

    These - not PKEY - are what the signing path handles, so they are what a
    leak would consist of.
    """
    from primer_vault.wallet.crypto import _derive_private_key, ETH_DERIVATION_PATH
    return [_derive_private_key(MNEMONIC, ETH_DERIVATION_PATH.format(i)).hex()
            for i in (0, 1)]


def secret_needles() -> list[str]:
    """Every form a secret could plausibly be written in."""
    needles = [MNEMONIC, SECOND_MNEMONIC, PASSWORD, NEW_PASSWORD]
    for k in [PKEY] + derived_keys():
        needles += [k, k.upper(), "0x" + k]
    return needles


class Capture:
    """Captures logging, stdout and stderr for the duration of a with-block."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._handler = None
        self._old = None

    class _Handler(logging.Handler):
        def __init__(self, sink):
            super().__init__(level=0)
            self._sink = sink

        def emit(self, record):
            self._sink.append(record)

    def __enter__(self):
        root = logging.getLogger()
        self._handler = self._Handler(self.records)
        root.addHandler(self._handler)
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        self._old = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self.stdout, self.stderr
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self._old
        root = logging.getLogger()
        root.removeHandler(self._handler)
        root.setLevel(self._old_level)
        return False

    def text(self) -> str:
        """Everything captured, flattened: message, args, formatted output and
        any attached exception text."""
        parts = [self.stdout.getvalue(), self.stderr.getvalue()]
        formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
        for r in self.records:
            parts.append(str(r.msg))
            parts.append(repr(r.args))
            try:
                parts.append(formatter.format(r))
            except Exception as e:  # a formatting failure is itself worth seeing
                parts.append(f"<unformattable: {e}>")
            if r.exc_info:
                import traceback
                parts.append("".join(traceback.format_exception(*r.exc_info)))
        return "\n".join(parts)


def read_all_files(root: Path) -> str:
    """Every file under `root`, decoded loosely so binary still gets scanned."""
    chunks = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                chunks.append(f"### {p}\n" + p.read_bytes().decode("utf-8", "replace"))
            except OSError:
                pass
    return "\n".join(chunks)


def assert_clean(haystack: str, where: str, allow: tuple = ()):
    found = [n for n in secret_needles() if n and n not in allow and n in haystack]
    assert not found, (
        f"secret material found in {where}: "
        + ", ".join(f"{n[:14]}..." for n in found)
    )


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "wallets").mkdir(parents=True)
    return d


def x402_offer(amount="1000000"):
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": f"eip155:{RHC}",
            "amount": amount,
            "asset": USDG,
            "payTo": PAY_TO,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


def build_core(data_dir):
    """Exercise the real wallet lifecycle with the known secrets."""
    from primer_vault.core import Vault

    core = Vault(data_dir=data_dir)
    wallet_path = str(data_dir / "wallets" / "sweep.wallet")
    results = {}
    results["create"] = core.create_wallet(
        wallet_path=wallet_path,
        password=PASSWORD,
        seed_phrase=MNEMONIC,
        address_indices=[0, 1],
        unlock=True,
    )
    results["load"] = core.load_wallet(wallet_path, PASSWORD)
    results["wrong_password"] = core.load_wallet(wallet_path, "not-the-password")
    results["reload"] = core.load_wallet(wallet_path, PASSWORD)
    results["add_seed_bad"] = core.add_seed(SECOND_MNEMONIC)
    results["import"] = core.add_imported_key(PKEY, name="Imported")
    return core, wallet_path, results


class TestWalletLifecycleEgress:

    def test_no_secret_in_logs_stdout_or_files_during_lifecycle(self, data_dir):
        with Capture() as cap:
            core, wallet_path, results = build_core(data_dir)
            core.save_wallet()
            core.lock_wallet()

        assert results["create"].get("success"), results["create"]
        assert results["import"].get("success"), results["import"]

        assert_clean(cap.text(), "logging/stdout/stderr during wallet lifecycle")
        assert_clean(json.dumps(results, default=str),
                     "the dicts create_wallet/load_wallet/add_* return to callers",
                     # create_wallet deliberately returns the seed so the GUI
                     # can show it for backup; that is the feature, not a leak.
                     allow=(MNEMONIC,))
        assert_clean(read_all_files(data_dir), "files written under the data dir")

    def test_wallet_file_holds_no_plaintext_secret(self, data_dir):
        core, wallet_path, _ = build_core(data_dir)
        core.save_wallet()
        assert_clean(Path(wallet_path).read_text(encoding="utf-8"),
                     "the wallet file on disk")
        prev = Path(wallet_path + ".previous")
        if prev.exists():
            assert_clean(prev.read_text(encoding="utf-8"),
                         "the .previous recovery copy")

    def test_password_change_leaves_nothing_readable(self, data_dir):
        core, wallet_path, _ = build_core(data_dir)
        with Capture() as cap:
            core.get_wallet().change_password(NEW_PASSWORD)
            core.save_wallet()
        assert_clean(cap.text(), "logging during change_password")
        assert_clean(read_all_files(data_dir), "files after change_password")


class TestSigningEgress:

    def _commissioned(self, data_dir):
        core, wallet_path, _ = build_core(data_dir)
        agent, token = core.create_agent(name="Payer", auth_mode="bearer")
        policy = core.create_policy(
            name="P", networks=[RHC], daily_limit_micro=100_000_000,
            per_request_max_micro=50_000_000, auto_approve_below_micro=10_000_000)
        address = core.get_wallet_addresses()[0]
        core.commission_agent(agent.code, policy.id, address["address"])
        return core, agent, token

    def test_successful_signing_emits_no_key_material(self, data_dir):
        core, agent, token = self._commissioned(data_dir)
        svc = core._signing_service
        activity = []
        svc._on_activity = lambda msg, is_error=False: activity.append(msg)
        with Capture() as cap:
            result = svc.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=x402_offer())
        assert result["status"] == "success", result
        assert_clean(cap.text(), "logging/stdout during a successful signing")
        assert_clean("\n".join(activity), "the activity feed during signing")
        assert_clean(json.dumps(result, default=str),
                     "the signing result returned to the agent")
        assert_clean(read_all_files(data_dir),
                     "files written during a successful signing")

    def test_approval_queue_and_rejection_emit_no_key_material(self, data_dir):
        from primer_vault.core.interfaces import HeadlessApprovalHandler

        core, agent, token = self._commissioned(data_dir)
        # The default headless handler auto-rejects, which would skip the
        # approve path entirely. Let requests sit in the queue instead.
        core.set_approval_handler(HeadlessApprovalHandler(core, auto_reject=False))
        svc = core._signing_service
        activity = []
        svc._on_activity = lambda msg, is_error=False: activity.append(msg)
        with Capture() as cap:
            queued = svc.handle_sign_request(
                agent_id=agent.id, signature=token,
                x402_data=x402_offer(amount="20000000"))
            assert queued["status"] == "pending", queued
            rejected = svc.reject_request(queued["request_id"], "no thanks")
            second = svc.handle_sign_request(
                agent_id=agent.id, signature=token, idempotency_key="k2",
                x402_data=x402_offer(amount="20000000"))
            assert second["status"] == "pending", second
            approved = svc.approve_request(second["request_id"])
        assert rejected["status"] == "rejected", rejected
        assert approved["status"] == "success", approved
        assert_clean(cap.text(), "logging across queue/reject/approve")
        assert_clean("\n".join(activity), "the activity feed across queue/reject/approve")
        assert_clean(json.dumps([queued, rejected, second, approved], default=str),
                     "the dicts returned across queue/reject/approve")
        assert_clean(read_all_files(data_dir),
                     "files written across queue/reject/approve")


class TestNoReachableExceptionCarriesTheKey:
    """services/signing.py passes str(e) straight into logger.error
    and the activity feed while the raw private key is live in that frame.

    Nothing between the key and those channels scrubs anything, so the handler
    is safe only for as long as no exception raised beneath it quotes its input.
    These pin that premise against the inputs a merchant controls.
    """

    ASSET = TOKENS["USDG"].addresses[RHC]

    def _requirements(self, **overrides):
        from primer_vault.services.eip3009 import PaymentRequirements
        fields = dict(scheme="exact", network=f"eip155:{RHC}", chain_id=RHC,
                      max_amount_required="1000000", asset=self.ASSET,
                      pay_to=PAY_TO)
        fields.update(overrides)
        return PaymentRequirements(**fields)

    @pytest.mark.parametrize("label,overrides", [
        ("asset is not an address", {"asset": "not-an-address"}),
        ("payTo is malformed hex", {"pay_to": "0xzz"}),
        ("amount is not a number", {"max_amount_required": "abc"}),
        ("amount overflows uint256", {"max_amount_required": str(2 ** 300)}),
        ("token name is not text", {"token_name": {"a": 1}}),
        ("token version is not text", {"token_version": [1, 2]}),
        ("chain id is not a number", {"chain_id": "not-a-chain"}),
        ("chain id is negative", {"chain_id": -1}),
    ])
    def test_create_payment_failure_never_quotes_the_key(self, label, overrides):
        from primer_vault.services.eip3009 import create_payment
        key = derived_keys()[0]
        try:
            create_payment(key, self._requirements(**overrides))
        except BaseException as e:
            assert key not in str(e) and key not in repr(e), (
                f"{label}: the private key reaches the exception message, which "
                f"signing.py writes to the log and the activity feed"
            )

    def test_eth_account_rejects_a_bad_key_without_quoting_it(self):
        from eth_account import Account
        for bad in (bytes.fromhex("ff" * 32),
                    bytes.fromhex("fffffffffffffffffffffffffffffffe"
                                  "baaedce6af48a03bbfd25e8cd0364141")):
            with pytest.raises(Exception) as exc:
                Account.from_key(bad)
            assert bad.hex() not in str(exc.value)
            assert bad.hex() not in repr(exc.value)

    def test_local_account_repr_does_not_reveal_the_key(self):
        from eth_account import Account
        acct = Account.from_key(bytes.fromhex(PKEY))
        assert PKEY not in repr(acct)
        assert PKEY not in str(acct)
