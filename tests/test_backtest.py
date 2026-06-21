"""回测引擎单测: simulate_hold 离场判定 + run_config 在合成数据上的端到端形状。"""
import unittest

from cryptoquant.config import HOUR_MS, DAY_MS
from cryptoquant.backtest.engine import simulate_hold, run_config, run_id


class TestSimulateHold(unittest.TestCase):
    def _bars(self, seq, t0):
        """seq: [(close,high,low)] 逐根 1h, 返回 {ts:(close,high,low,qvol)}。"""
        return {t0 + (i + 1) * HOUR_MS: (c, h, l, 1.0) for i, (c, h, l) in enumerate(seq)}

    def test_short_stop_loss(self):
        t0 = 0
        bars = self._bars([(100, 120, 99)], t0)       # high 120 >= sl 110 → 止损
        px, ts, reason = simulate_hold(bars, t0, 100.0, 6 * HOUR_MS, 110.0, 50.0, "short")
        self.assertEqual(reason, "SL")
        self.assertEqual(px, 110.0)

    def test_short_take_profit(self):
        t0 = 0
        bars = self._bars([(60, 61, 49)], t0)          # low 49 <= tp 50 → 止盈
        px, ts, reason = simulate_hold(bars, t0, 100.0, 6 * HOUR_MS, 110.0, 50.0, "short")
        self.assertEqual(reason, "TP")
        self.assertEqual(px, 50.0)

    def test_time_exit(self):
        t0 = 0
        bars = self._bars([(100, 101, 99)] * 6, t0)    # 一直不触发 → 到时间
        px, ts, reason = simulate_hold(bars, t0, 100.0, 6 * HOUR_MS, 110.0, 50.0, "short")
        self.assertEqual(reason, "TIME")
        self.assertEqual(px, 100.0)


class TestRunConfig(unittest.TestCase):
    def _synthetic(self, days=2, hours=6):
        """两个标的、规则上涨的合成行情, 校验引擎跑通并产出完整结构。"""
        end_ms = 1_000_000 * HOUR_MS
        start_ms = end_ms - days * DAY_MS
        rebal_times = list(range(start_ms + DAY_MS, end_ms - hours * HOUR_MS + 1, hours * HOUR_MS))
        prices, funding = {}, {}
        for s, drift in (("AAAUSDT", 1.001), ("BBBUSDT", 1.002)):
            bars = {}
            p = 100.0
            t = start_ms
            while t <= end_ms:
                p *= drift
                bars[t] = (p, p * 1.005, p * 0.995, 5_000_000.0)
                t += HOUR_MS
            prices[s] = bars
            funding[s] = [(t, 0.0001) for t in range(start_ms, end_ms, 8 * HOUR_MS)]
        return {"syms": ["AAAUSDT", "BBBUSDT"], "prices": prices, "funding": funding,
                "rebal_times": rebal_times, "start_ms": start_ms, "end_ms": end_ms,
                "days": days, "candidates_requested": 2, "hours": hours}

    def test_run_config_structure(self):
        data = self._synthetic()
        res = run_config(data, side="long", top=2, stop_loss=0.1, take_profit=0.5,
                         fee=0.0005, capital=10000, nsel=2, dyn_univ=None)
        self.assertEqual(res["meta"]["id"], run_id("long", 0.1, 0.5))
        for key in ("final_equity", "total_return_pct", "sharpe", "max_drawdown_pct"):
            self.assertIn(key, res["summary"])
        self.assertEqual(len(res["curve"]), len(data["rebal_times"]))
        # 持续上涨 + 做多 → 期末净值应 > 初始
        self.assertGreater(res["summary"]["final_equity"], 10000)


if __name__ == "__main__":
    unittest.main()
