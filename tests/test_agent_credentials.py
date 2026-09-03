"""
Agent credential lifecycle: how an agent's secret is protected, and what has to
be true for one to exist at all.

An agent's shared secret is encrypted under the wallet's master key, not under
the wallet password. That single decision is what these tests pin down:

  - an agent cannot be created without a wallet, because there would be no key
    to encrypt its secret with, and nothing to commission it against;
  - rotating the password re-wraps the master key and leaves every agent secret
    exactly as it was;
  - unwrapping the master key is what proves a password correct, so a wallet
    holding no secrets of its own is still not openable with any password;
  - the KDF settings travel with the wallet, so they can be retuned later
    without stranding wallets written by an earlier build.

Each of these was a real defect. None of them had a test.
"""

import json
import secrets
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.models.agent import encrypt_agent_secret
from primer_vault.wallet.crypto import (
    NO_PASSWORD_SENTINEL,
    UnsupportedWalletVersion,
    VaultWallet,
    WeakPasswordError,
    unwrap_data_key,
    wrap_data_key,
)

PHRASE = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")


@pytest.fixture
def temp_data_dir():
    d = tempfile.mkdtemp()
    yield Path(d)


@pytest.fixture
def locked_core(temp_data_dir):
    """A Vault with no wallet open."""
    return Vault(data_dir=temp_data_dir)


def _wallet_file(data_dir, name="test.wallet"):
    return str(Path(data_dir) / "wallets" / name)


# ---------------------------------------------------------------------------
# A wallet is the precondition for an agent
# ---------------------------------------------------------------------------

class TestWalletIsRequired:

    def test_creating_an_agent_without_a_wallet_is_refused(self, locked_core):
        with pytest.raises(ValueError, match="wallet must be open"):
            locked_core.create_agent(name="NoWallet", auth_mode="hmac")

    def test_bearer_agents_are_refused_too(self, locked_core):
        """Bearer credentials need no key, but an agent still belongs to a
        wallet: it cannot be commissioned or sign without an address from one.
        A precondition that depended on the auth mode is the kind of asymmetry
        that hid the original defect."""
        with pytest.raises(ValueError, match="wallet must be open"):
            locked_core.create_agent(name="NoWallet", auth_mode="bearer")

    def test_refusal_leaves_no_agent_behind(self, locked_core):
        with pytest.raises(ValueError):
            locked_core.create_agent(name="NoWallet", auth_mode="hmac")
        assert locked_core.get_all_agents() == []

    def test_creating_an_agent_works_once_a_wallet_is_open(self, locked_core, temp_data_dir):
        path = _wallet_file(temp_data_dir)
        locked_core.create_wallet(path, "passphrase")
        locked_core.load_wallet(path, "passphrase")

        agent, token = locked_core.create_agent(name="HasWallet", auth_mode="hmac")
        assert token.startswith("AT_")
        assert locked_core.get_agent_by_id(agent.id) is not None

    def test_no_fallback_password_exists_in_the_source(self):
        """A constant compiled into the build is a published password, so the
        secret must never be encrypted under one. Assert the absence rather
        than trusting the refusal above.
        """
        src = Path(__file__).parent.parent / "src"
        for path in src.rglob("*.py"):
            assert "temporary_password" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------------------
# Rotating the password must not orphan anything
# ---------------------------------------------------------------------------

class TestPasswordRotation:

    def test_agent_secret_survives_a_password_change(self, temp_data_dir):
        core = Vault(data_dir=temp_data_dir)
        path = _wallet_file(temp_data_dir)
        core.create_wallet(path, "first-password")
        core.load_wallet(path, "first-password")

        agent, token = core.create_agent(name="Rotator", auth_mode="hmac")
        secret_before = agent.decrypt_auth_key(core._unlocked_wallet.data_key)

        wallet = core._unlocked_wallet
        wallet.change_password("second-password")
        wallet.save(path)

        reopened = VaultWallet.load(path, "second-password")
        assert agent.decrypt_auth_key(reopened.data_key) == secret_before
        assert token == f"AT_{secret_before}"

    def test_seed_survives_a_password_change(self, temp_data_dir):
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("first-password")
        seed_id = wallet.add_seed(PHRASE)
        wallet.save(path)

        wallet.change_password("second-password")
        wallet.save(path)

        assert VaultWallet.load(path, "second-password").get_seed_phrase(seed_id) == PHRASE

    def test_old_password_stops_working(self, temp_data_dir):
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("first-password")
        wallet.add_seed(PHRASE)
        wallet.save(path)

        wallet.change_password("second-password")
        wallet.save(path)

        with pytest.raises(ValueError):
            VaultWallet.load(path, "first-password")

    def test_the_master_key_itself_is_unchanged(self, temp_data_dir):
        """This is the mechanism the two tests above depend on: rotating the
        password re-wraps the key rather than re-encrypting what it protects."""
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("first-password")
        before = wallet.data_key
        wallet.change_password("second-password")
        wallet.save(path)

        assert wallet.data_key == before
        assert VaultWallet.load(path, "second-password").data_key == before

    def test_the_password_cannot_be_removed(self, temp_data_dir):
        """The sentinel travels in the same channel as real passwords, so
        accepting it would let a caller strip encryption while believing it
        set a password. change_password refuses it."""
        wallet = VaultWallet.create("first-password")
        seed_id = wallet.add_seed(PHRASE)

        with pytest.raises(WeakPasswordError):
            wallet.change_password(NO_PASSWORD_SENTINEL)

        assert wallet.is_encrypted is True
        assert wallet.get_seed_phrase(seed_id) == PHRASE

    def test_a_legacy_unencrypted_wallet_is_refused_not_opened(
            self, temp_data_dir):
        """This build no longer opens an unencrypted wallet - the master key sits
        in the file in the clear. It is refused with a distinct type (not a
        wrong-password error), and the message points at the one-time conversion:
        open it in Vault 0.1, set a password, come back. The file is built the way
        the legacy code wrote it, since create() no longer can."""
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("temporary-pw")
        wallet.add_seed(PHRASE)
        wallet._encrypted = False
        wallet._wrapped_key = None
        wallet.save(path)

        with pytest.raises(UnsupportedWalletVersion) as excinfo:
            VaultWallet.load(path, NO_PASSWORD_SENTINEL)
        assert "unencrypted" in str(excinfo.value).lower()

    def test_verify_password_does_not_consult_a_stored_copy(self, temp_data_dir):
        wallet = VaultWallet.create("correct-horse")
        assert wallet.verify_password("correct-horse")
        assert not wallet.verify_password("Correct-Horse")
        assert not wallet.verify_password("")


# ---------------------------------------------------------------------------
# The password check does not depend on there being a secret to decrypt
# ---------------------------------------------------------------------------

class TestPasswordIsAlwaysChecked:

    def test_wallet_holding_only_a_hardware_address_rejects_a_wrong_password(self, temp_data_dir):
        """A Ledger-backed wallet stores no secrets by design. Inferring the
        password from whether something decrypted meant any password opened it.
        """
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("real-password")
        wallet.add_hardware_address("0x" + "ab" * 20, "m/44'/60'/0'/0/0", "bip44")
        wallet.save(path)

        with pytest.raises(ValueError):
            VaultWallet.load(path, "anything-at-all")
        assert len(VaultWallet.load(path, "real-password").addresses) == 1

    def test_empty_wallet_rejects_a_wrong_password(self, temp_data_dir):
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        VaultWallet.create("real-password").save(path)

        with pytest.raises(ValueError):
            VaultWallet.load(path, "anything-at-all")


# ---------------------------------------------------------------------------
# The stored KDF settings are the ones used
# ---------------------------------------------------------------------------

class TestStoredKdfParametersAreHonoured:

    def test_wallet_written_with_other_settings_still_opens(self, temp_data_dir):
        """The file records how its key was wrapped. If the loader ignores that
        and uses the constants compiled into the build, retuning them later
        makes every existing wallet unopenable.
        """
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("passphrase")
        seed_id = wallet.add_seed(PHRASE)
        wallet.save(path)

        # Re-wrap with deliberately different (weaker) settings, as an older or
        # a future build might have written.
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        wrapped = wrap_data_key(wallet.data_key, "passphrase")
        wrapped["time_cost"] = 1
        wrapped["memory_cost"] = 8192
        wrapped["parallelism"] = 1
        from primer_vault.wallet.crypto import encrypt_with_key
        from argon2.low_level import hash_secret_raw, Type
        kek = hash_secret_raw(
            secret=b"passphrase", salt=bytes.fromhex(wrapped["salt"]),
            time_cost=1, memory_cost=8192, parallelism=1, hash_len=32, type=Type.ID)
        ct, iv, tag = encrypt_with_key(kek, wallet.data_key.hex())
        wrapped.update(ciphertext=ct, iv=iv, tag=tag)
        data["wrapped_key"] = wrapped
        Path(path).write_text(json.dumps(data), encoding="utf-8")

        reopened = VaultWallet.load(path, "passphrase")
        assert reopened.get_seed_phrase(seed_id) == PHRASE

    def test_wrapping_round_trips(self):
        key = secrets.token_bytes(32)
        assert unwrap_data_key(wrap_data_key(key, "passphrase"), "passphrase") == key

    def test_wrong_password_does_not_unwrap(self):
        with pytest.raises(ValueError):
            unwrap_data_key(wrap_data_key(secrets.token_bytes(32), "passphrase"), "nope")

    def test_malformed_wrapping_is_reported_as_such(self):
        with pytest.raises(ValueError, match="malformed"):
            unwrap_data_key({"salt": "zz"}, "passphrase")


# ---------------------------------------------------------------------------
# The secret is still bound to the agent it belongs to
# ---------------------------------------------------------------------------

class TestSecretBinding:

    def test_ciphertext_does_not_transfer_between_agents(self):
        key = secrets.token_bytes(32)
        secret = secrets.token_hex(32)
        encrypted, iv, tag = encrypt_agent_secret(secret, key, "AAA111")

        from primer_vault.models.agent import decrypt_agent_secret
        assert decrypt_agent_secret(encrypted, iv, tag, key, "AAA111") == secret
        with pytest.raises(InvalidTag):
            decrypt_agent_secret(encrypted, iv, tag, key, "BBB222")

    def test_locked_wallet_yields_no_key(self, temp_data_dir):
        wallet = VaultWallet.create("passphrase")
        wallet.lock()
        with pytest.raises(ValueError, match="locked"):
            _ = wallet.data_key

    def test_credentials_are_unavailable_while_locked(self, temp_data_dir):
        core = Vault(data_dir=temp_data_dir)
        path = _wallet_file(temp_data_dir)
        core.create_wallet(path, "passphrase")
        core.load_wallet(path, "passphrase")
        agent, _ = core.create_agent(name="Locked", auth_mode="hmac")

        core.lock_wallet()
        _, token, mode = core.get_agent_credentials(agent.code)
        assert token is None and mode == "hmac"


def test_argon2_parameters_are_strong_in_production():
    """Pin the shipped key-stretching cost.

    The suite runs with these lowered for speed (see conftest), so without this
    check a weakened default could ship unnoticed. 256MB is the point of the
    setting: memory is what an attacker with graphics cards finds expensive,
    much more so than passes.
    """
    import importlib
    from primer_vault.wallet import crypto

    shipped = importlib.import_module("primer_vault.wallet.crypto")
    source = Path(shipped.__file__).read_text(encoding="utf-8")
    assert "ARGON2_MEMORY_COST = 262144" in source, "shipped memory cost changed"
    assert "ARGON2_TIME_COST = 3" in source, "shipped time cost changed"
    assert crypto.ARGON2_PARALLELISM == 4
    assert crypto.ARGON2_HASH_LEN == 32


# ---------------------------------------------------------------------------
# Password floor
# ---------------------------------------------------------------------------

class TestPasswordFloor:
    """An 8-character minimum, matching Rabby's floor, and nothing else.

    Deliberately a length rule only. Composition rules ("one capital, one
    symbol") are deprecated practice: they push people towards predictable
    shapes like "Password1!", which lowers real entropy. Whitespace counts and
    nothing is stripped, so a pasted passphrase arrives as the manager made it.
    """

    def test_the_floor_is_eight(self):
        from primer_vault.wallet.crypto import MIN_PASSWORD_LENGTH
        assert MIN_PASSWORD_LENGTH == 8

    @pytest.mark.parametrize("password", ["", "a", "short", "sevench"])
    def test_a_short_password_cannot_create_a_wallet(self, password):
        from primer_vault.wallet.crypto import WeakPasswordError
        with pytest.raises(WeakPasswordError, match="at least 8"):
            VaultWallet.create(password)

    @pytest.mark.parametrize("password", ["exactly8", "abcd1980", "        ",
                                          "a much longer passphrase", "🔐🔐🔐🔐🔐🔐🔐🔐"])
    def test_eight_or_more_is_accepted_whatever_it_contains(self, password):
        """No content rules: eight spaces and eight emoji both pass, as they do
        in Rabby. The floor removes the indefensible, not the unwise."""
        assert VaultWallet.create(password).is_encrypted

    def test_a_short_password_cannot_be_set_as_a_replacement(self):
        from primer_vault.wallet.crypto import WeakPasswordError
        wallet = VaultWallet.create("goodpassword")
        with pytest.raises(WeakPasswordError):
            wallet.change_password("no")

    def test_a_refused_reset_leaves_the_old_password_working(self, temp_data_dir):
        from primer_vault.wallet.crypto import WeakPasswordError
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wallet = VaultWallet.create("goodpassword")
        seed_id = wallet.add_seed(PHRASE)
        wallet.save(path)

        with pytest.raises(WeakPasswordError):
            wallet.change_password("bad")
        wallet.save(path)

        assert VaultWallet.load(path, "goodpassword").get_seed_phrase(seed_id) == PHRASE

    def test_the_floor_is_not_applied_when_opening_a_wallet(self, temp_data_dir):
        """Validate on set, never on verify.

        The file is already encrypted with whatever password was set. Checking
        the policy at unlock would lock people out of their own wallets the
        moment the floor moved, so a wallet whose password predates the rule
        must still open.
        """
        from primer_vault.wallet.crypto import wrap_data_key
        path = _wallet_file(temp_data_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        wallet = VaultWallet.create("goodpassword")
        seed_id = wallet.add_seed(PHRASE)
        # Re-wrap under a password the floor would now refuse, as an older build
        # could have written.
        wallet._wrapped_key = wrap_data_key(wallet.data_key, "old")
        wallet.save(path)

        reopened = VaultWallet.load(path, "old")
        assert reopened.get_seed_phrase(seed_id) == PHRASE

    def test_the_core_refuses_rather_than_raising_at_the_caller(self, temp_data_dir):
        """Every interface goes through create_wallet, so the message has to come
        back as a result rather than an exception the CLI would not catch."""
        core = Vault(data_dir=temp_data_dir)
        result = core.create_wallet(_wallet_file(temp_data_dir), "short", unlock=False)
        assert result["success"] is False
        assert "at least 8" in result["error"]

    def test_an_unencrypted_wallet_cannot_be_created(self, temp_data_dir):
        """The sentinel is an in-band magic value in the password channel, and
        the 8-character floor makes it redundant, so it is not offered as a
        CLI choice. Legacy files still open (above)."""
        with pytest.raises(WeakPasswordError):
            VaultWallet.create(NO_PASSWORD_SENTINEL)


class TestNoPasswordIsNotOfferedInTheApp:

    def test_no_gui_dialog_offers_an_unencrypted_wallet(self):
        """Creating a wallet properly and then stripping the password off it a
        minute later is the same hole with an extra step, so no dialog offers
        it."""
        dialogs = (Path(__file__).parent.parent / "src" / "primer_vault"
                   / "ui" / "wallet_dialogs.py").read_text(encoding="utf-8")
        assert "no_password_check" not in dialogs
        assert "No password" not in dialogs

    def test_the_gui_never_sets_the_sentinel_when_creating_or_changing(self):
        """tabs.py may still pass the sentinel to load_wallet, to open a wallet
        made via the CLI. It must not be reachable as a creation choice."""
        dialogs = (Path(__file__).parent.parent / "src" / "primer_vault"
                   / "ui" / "wallet_dialogs.py").read_text(encoding="utf-8")
        assert "NO_PASSWORD_SENTINEL" not in dialogs


class TestTradingLimitsAreReadable:
    """An agent can read the trading limits it is held to.

    The payment side has always published its limits through /mandate, and the
    trading skill tells agents to check before spending a request. Without the
    trading half the only way to find a ceiling was to hit it - so an agent
    could not pace itself, and every discovery cost a wasted request.
    """

    def _summary(self, rules, volume=0.0, reset_date=None):
        import datetime
        from types import SimpleNamespace
        from primer_vault.services.signing import SigningService
        from primer_vault.models.policy import SpendPolicy

        policy = SpendPolicy.create(name="p", networks=[4663],
                                    daily_limit_micro=1_000_000, trading_rules=rules)
        agent = SimpleNamespace(
            trading_volume_today_usd=volume,
            last_trading_reset_date=reset_date or datetime.date.today().isoformat(),
            last_trading_reset_at="")
        return SigningService()._trading_summary(agent, policy)

    def test_every_limit_is_published(self):
        from primer_vault.models.policy import TradingRules

        summary = self._summary(TradingRules(
            enabled=True, per_trade_max_usd=1.0, daily_volume_limit_usd=5.0,
            auto_approve_below_usd=0.10, min_reserve_eth=0.0001,
            max_slippage_percent=3.0, max_price_impact_percent=5.0))

        assert summary == {
            "enabled": True,
            "per_trade_max_usd": 1.0,
            "daily_volume_limit_usd": 5.0,
            "auto_approve_below_usd": 0.10,
            "max_slippage_percent": 3.0,
            "max_price_impact_percent": 5.0,
            "min_reserve_eth": 0.0001,
            "volume_today_usd": 0.0,
            "committed_today_usd": 0.0,
            "remaining_today_usd": 5.0,
        }

    def test_remaining_excludes_volume_promised_to_queued_trades(self):
        """A trade awaiting approval has already claimed its volume. Reporting a
        balance that ignored it would be a balance the next request refuses."""
        from primer_vault.models.policy import TradingRules
        from primer_vault.services.signing import SigningService
        from primer_vault.models.policy import SpendPolicy
        from types import SimpleNamespace
        import datetime

        policy = SpendPolicy.create(
            name="p", networks=[4663], daily_limit_micro=1,
            trading_rules=TradingRules(enabled=True, daily_volume_limit_usd=100.0))
        agent = SimpleNamespace(
            id="A1", trading_volume_today_usd=25.0,
            last_trading_reset_date=datetime.date.today().isoformat(),
            last_trading_reset_at="")

        service = SigningService()
        service.set_reserved_volume_provider(lambda agent_id: 30.0)
        summary = service._trading_summary(agent, policy)

        assert summary["volume_today_usd"] == 25.0
        assert summary["committed_today_usd"] == 55.0
        assert summary["remaining_today_usd"] == 45.0

    def test_remaining_accounts_for_volume_already_used(self):
        from primer_vault.models.policy import TradingRules
        summary = self._summary(
            TradingRules(enabled=True, daily_volume_limit_usd=5.0), volume=1.75)
        assert summary["remaining_today_usd"] == 3.25

    def test_a_stale_daily_total_is_reported_as_reset(self):
        """Volume resets when a trade is evaluated, so a read after midnight
        would otherwise report yesterday's total. A read must not write, so the
        effective figure is computed rather than stored."""
        from primer_vault.models.policy import TradingRules
        summary = self._summary(
            TradingRules(enabled=True, daily_volume_limit_usd=5.0),
            volume=4.9, reset_date="2020-01-01")

        assert summary["volume_today_usd"] == 0.0
        assert summary["remaining_today_usd"] == 5.0

    def test_trading_disabled_says_so_plainly(self):
        from primer_vault.models.policy import TradingRules
        assert self._summary(TradingRules(enabled=False)) == {"enabled": False}

    def test_no_trading_rules_says_so_plainly(self):
        assert self._summary(None) == {"enabled": False}
