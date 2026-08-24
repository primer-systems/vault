"""The status page has to describe the server it is served by.

`http://localhost:4663/` is the first thing anyone curious about Vault opens, so
it has to stay in step with the server behind it: every endpoint listed, trading
described alongside payment, and counters that move for trades as well as
payments.

None of that breaks anything, which is exactly why it lasted. A page nothing
tests is a page that documents whatever was true when it was written.

These tests read the routes out of the request handler and hold the page to
them, so the next endpoint added has to appear there or the suite says so.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services import server as server_module
from primer_vault.services.server import get_branded_html, server_stats
from primer_vault.services.trading import TradingService

SOURCE = Path(server_module.__file__).read_text(encoding="utf-8")

#: `self.path == "/x"` and `base_path.startswith("/x/")` in do_GET / do_POST.
ROUTE = re.compile(r'(?:self\.path|base_path)\s*(?:==|\.startswith\()\s*"(/[^"]*)"')


def served_routes():
    """Every path the handler answers, read from its own dispatch."""
    return {m.group(1) for m in ROUTE.finditer(SOURCE)}


def listed_routes(html):
    """Every path in the page's endpoint table, with {id} placeholders stripped
    back to the prefix the handler actually matches on."""
    out = set()
    for path in re.findall(r"<td><code>(/[^<]*)</code></td>", html):
        out.add(re.sub(r"\{id\}$", "", path))
    return out


@pytest.fixture
def page():
    return get_branded_html(4663)


class TestEveryEndpointIsListed:

    def test_no_route_is_missing_from_the_table(self, page):
        missing = sorted(served_routes() - listed_routes(page))
        assert not missing, (
            f"the server answers these and the status page does not mention "
            f"them: {missing}")

    def test_nothing_is_listed_that_the_server_does_not_serve(self, page):
        """The other direction. A documented endpoint that 404s is worse than an
        undocumented one that works."""
        phantom = sorted(listed_routes(page) - served_routes())
        assert not phantom, (
            f"the status page advertises endpoints the server does not answer: "
            f"{phantom}")

    @pytest.mark.parametrize("path", [
        "/trade", "/mandate", "/ping", "/callback", "/receipt/", "/sign/helper",
    ])
    def test_the_ones_it_used_to_omit(self, page, path):
        """Named individually so a regression says which one came back."""
        assert path.rstrip("/") in page


class TestItDescribesTheWholeProduct:

    def test_trading_is_mentioned(self, page):
        """It described Vault as a payment oracle only, though the agent skill
        calls trading its primary purpose."""
        assert "trade" in page.lower()

    def test_payments_are_still_mentioned(self, page):
        assert "x402" in page.lower()

    def test_the_keys_never_leave_claim_is_present(self, page):
        """The one line on this page that is a security claim. If it is ever
        removed that should be deliberate."""
        assert "never leave" in page.lower()


class TestTheCountersCoverBothProducts:

    @pytest.fixture(autouse=True)
    def clean_counters(self):
        server_stats.reset()
        yield
        server_stats.reset()

    def test_an_executed_trade_is_counted(self):
        TradingService()._remember_result("t1", {"status": "executed"})
        assert server_stats.traded == 1
        assert server_stats.signed == 0, "a trade is not a signature"

    @pytest.mark.parametrize("status", ["rejected", "failed"])
    def test_a_refused_or_failed_trade_is_counted(self, status):
        TradingService()._remember_result("t1", {"status": status})
        assert server_stats.trade_rejected == 1

    def test_a_pending_trade_is_not_counted(self):
        """It is waiting on the user. Counting it would report work the server
        has not done, and counting it again on approval would double it."""
        TradingService()._remember_result("t1", {"status": "pending"})
        assert (server_stats.traded, server_stats.trade_rejected) == (0, 0)

    def test_a_trade_refused_at_intake_is_counted(self):
        """Those never become pending, so they do not pass through the resolver
        that counts the rest."""
        TradingService()._count_refusal({"status": "rejected"})
        assert server_stats.trade_rejected == 1

    def test_the_counters_appear_in_the_status_json(self):
        """/status is what a monitoring script reads, and it reported signing
        alone."""
        for field in ("traded", "trade_rejected", "signed", "rejected"):
            assert f'"{field}": server_stats.{field}' in SOURCE

    def test_the_counters_appear_on_the_page(self):
        server_stats.traded = 7
        server_stats.trade_rejected = 2
        page = get_branded_html(4663)
        assert ">7<" in page and ">2<" in page
