"""
Pytest configuration and shared fixtures.
"""

import sys
from pathlib import Path

import pytest

# Add src directory to Python path
SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True, scope="session")
def cheap_key_derivation():
    """Run the suite with weak key-stretching parameters.

    Wrapping a wallet key deliberately costs ~280ms and 256MB in production. The
    fixtures here create wallets in almost every test, so paying that would add
    minutes to a run and tempt someone to lower the real setting instead.

    Only the cost is changed, not the scheme: wallets record the parameters they
    were written with and are opened using those, which is the same mechanism a
    production wallet relies on. test_argon2_parameters_are_strong_in_production
    pins the shipped values so this cannot hide a weak default.
    """
    from primer_vault.wallet import crypto

    original = (crypto.ARGON2_MEMORY_COST, crypto.ARGON2_TIME_COST)
    crypto.ARGON2_MEMORY_COST, crypto.ARGON2_TIME_COST = 8192, 1
    yield
    crypto.ARGON2_MEMORY_COST, crypto.ARGON2_TIME_COST = original


@pytest.fixture(autouse=True)
def fixed_eth_price(monkeypatch):
    """Hold the ETH price still for every test.

    Trade valuation calls a live price feed. Three test files were reaching it,
    so the suite's result moved with the market: a 0.5 ETH trade sat under a
    $1000 per-trade cap until ETH passed $2000, at which point a test that had
    nothing to do with pricing started failing.

    A test that depends on a third party is a test that fails for reasons no one
    can act on, and on a machine with no network it fails always. Tests that care
    about a particular price override this with their own value.
    """
    from primer_vault.services import pricing

    monkeypatch.setattr(pricing, "get_eth_usd", lambda *a, **k: 2000.0)
    pricing._cache.clear()


@pytest.fixture
def core(temp_data_dir):
    """A Vault with an open wallet, which is the only state agents exist in.

    Agent credentials are encrypted under the wallet's master key, and an agent
    cannot be commissioned without an address from it, so a core without a
    wallet cannot register one. Tests that want the locked case unlock nothing
    and assert the refusal.
    """
    from primer_vault.core import Vault

    core = Vault(data_dir=temp_data_dir)
    wallet_path = str(Path(temp_data_dir) / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass")
    core.load_wallet(wallet_path, "testpass")
    return core


def pytest_configure(config):
    """Configure pytest."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
