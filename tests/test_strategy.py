"""策略纯逻辑单测 (无网络)。 运行: python3 -m pytest tests/  或  python3 -m unittest"""
import unittest

from cryptoquant.strategy import round_down, pick_gainers, stop_take_levels, is_leveraged
from cryptoquant.exchange.public import rank_tickers, funding_between


class TestHelpers(unittest.TestCase):
    def test_round_down(self):
        self.assertEqual(round_down(1.23456, 2), 1.23)
        self.assertEqual(round_down(1.239, 2), 1.23)      # 向下, 不进位
        self.assertEqual(round_down(100.0, 0), 100.0)

    def test_is_leveraged(self):
        self.assertTrue(is_leveraged("BTCUPUSDT"))
        self.assertTrue(is_leveraged("ETHDOWNUSDT"))
        self.assertFalse(is_leveraged("BTCUSDT"))

    def test_stop_take_levels(self):
        # short: 止损在上方 (+10%), 止盈在下方 (-50%)
        sl, tp = stop_take_levels("short", 100.0, 0.10, 0.50)
        self.assertAlmostEqual(sl, 110.0)
        self.assertAlmostEqual(tp, 50.0)
        # long: 反向
        sl, tp = stop_take_levels("long", 100.0, 0.10, 0.50)
        self.assertAlmostEqual(sl, 90.0)
        self.assertAlmostEqual(tp, 150.0)


class TestPickGainers(unittest.TestCase):
    def _tk(self, sym, chg, price=10.0, qv=2_000_000):
        return {"symbol": sym, "priceChangePercent": chg, "lastPrice": price, "quoteVolume": qv}

    def test_ranks_and_filters(self):
        tickers = [
            self._tk("AAAUSDT", 30), self._tk("BBBUSDT", 10),
            self._tk("CCCUSDT", 50), self._tk("BTCUPUSDT", 99),   # 杠杆代币 → 排除
            self._tk("DDDUSD", 80),                                # 非 USDT → 排除
            self._tk("EEEUSDT", 5, qv=10),                         # 成交额不足 → 过滤
        ]
        picks = pick_gainers(tickers, top=2, min_quote_volume=1_000_000)
        self.assertEqual([p["symbol"] for p in picks], ["CCCUSDT", "AAAUSDT"])
        self.assertEqual(picks[0]["change_pct"], 50)

    def test_allowed_filter(self):
        tickers = [self._tk("AAAUSDT", 30), self._tk("ZZZUSDT", 40)]
        picks = pick_gainers(tickers, top=5, allowed={"AAAUSDT"})
        self.assertEqual([p["symbol"] for p in picks], ["AAAUSDT"])


class TestRankTickers(unittest.TestCase):
    def test_losers_and_schema(self):
        tickers = [
            {"symbol": "AUSDT", "priceChangePercent": "5", "lastPrice": "1", "quoteVolume": "2e6"},
            {"symbol": "BUSDT", "priceChangePercent": "-8", "lastPrice": "2", "quoteVolume": "2e6"},
        ]
        losers = rank_tickers(tickers, top=1, losers=True, min_quote_volume=1e6)
        self.assertEqual(losers[0]["symbol"], "BUSDT")
        self.assertIn("change_pct", losers[0])
        self.assertIn("quote_volume", losers[0])


class TestFunding(unittest.TestCase):
    def test_between_inclusive_hi_exclusive_lo(self):
        funding = [(100, 0.01), (200, -0.02), (300, 0.03)]
        # (100, 300]: 含 200 与 300, 不含 100
        self.assertAlmostEqual(funding_between(funding, 100, 300), 0.01)


if __name__ == "__main__":
    unittest.main()
