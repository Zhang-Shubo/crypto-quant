"""实时监控触发/盈亏逻辑单测 (无网络, 直接调静态方法)。"""
import unittest

from cryptoquant.live.monitor import Monitor


def _plan(side, positions):
    return {"side": side, "positions": positions}


def _pos(symbol, entry, qty, sl, tp):
    return {"symbol": symbol, "entry": entry, "qty": qty, "sl": sl, "tp": tp,
            "closed": False, "exit": None, "reason": None, "exit_ts": None}


class TestTriggers(unittest.TestCase):
    def test_long_take_profit_latches(self):
        plan = _plan("long", [_pos("AAA", 100, 1, 90, 150)])
        Monitor.apply_triggers(plan, {"AAA": 160})        # >= tp 150 → 止盈平仓
        p = plan["positions"][0]
        self.assertTrue(p["closed"])
        self.assertEqual(p["reason"], "TP")
        self.assertEqual(p["exit"], 150)
        # 单调: 价格回落也不应翻回持仓
        Monitor.apply_triggers(plan, {"AAA": 100})
        self.assertTrue(plan["positions"][0]["closed"])
        self.assertEqual(plan["positions"][0]["exit"], 150)

    def test_short_stop_loss(self):
        plan = _plan("short", [_pos("BBB", 100, 1, 110, 50)])
        Monitor.apply_triggers(plan, {"BBB": 115})        # >= sl 110 → 止损
        p = plan["positions"][0]
        self.assertTrue(p["closed"])
        self.assertEqual(p["reason"], "SL")
        self.assertEqual(p["exit"], 110)

    def test_compute_pnl_frozen_at_exit(self):
        plan = _plan("long", [_pos("AAA", 100, 2, 90, 150)])
        Monitor.apply_triggers(plan, {"AAA": 150})        # 止盈 @150
        pnl, notional, hit = Monitor.compute_pnl(plan, {"AAA": 9999})  # 现价不影响已平仓
        self.assertAlmostEqual(pnl, 2 * (150 - 100))      # 定格在出场价
        self.assertAlmostEqual(notional, 2 * 100)
        self.assertEqual(hit, 1)


if __name__ == "__main__":
    unittest.main()
