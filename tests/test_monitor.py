"""实时监控触发/盈亏逻辑单测 (无网络, 直接调静态方法)。"""
import tempfile
import unittest
from pathlib import Path

from cryptoquant.config import MonitorConfig
from cryptoquant.live import monitor as monitor_mod
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


class TestRealizedValue(unittest.TestCase):
    def test_mix_closed_and_open(self):
        plan = _plan("long", [
            dict(_pos("AAA", 100, 2, 90, 150), closed=True, exit=150),  # 已平 +100
            _pos("BBB", 10, 3, 9, 15),                                  # 持仓: 现价 12 → +6
        ])
        v = Monitor.realized_value(plan, {"BBB": 12})
        self.assertAlmostEqual(v, 2 * (150 - 100) + 3 * (12 - 10))      # 106
        # 无行情的持仓被跳过, 不计入
        v2 = Monitor.realized_value(plan, {})
        self.assertAlmostEqual(v2, 2 * (150 - 100))


class FakeClient:
    """最小行情桩: 两个标的, 第二轮换一批以触发调仓选币。"""
    def __init__(self):
        self.round = 0
        self.tickers = [
            [{"symbol": "AAAUSDT", "lastPrice": "100", "priceChangePercent": "80",
              "quoteVolume": "9e9"},
             {"symbol": "BBBUSDT", "lastPrice": "10", "priceChangePercent": "70",
              "quoteVolume": "9e9"}],
            [{"symbol": "CCCUSDT", "lastPrice": "5", "priceChangePercent": "90",
              "quoteVolume": "9e9"}],
        ]
        self.marks = {"AAAUSDT": 110.0, "BBBUSDT": 10.0, "CCCUSDT": 5.0}

    def ticker_24hr(self):
        return self.tickers[min(self.round, len(self.tickers) - 1)]

    def premium_index(self):
        return [{"symbol": k, "markPrice": str(v)} for k, v in self.marks.items()]


class TestAccountRollingRebalance(unittest.TestCase):
    """开新快照时, 上一批到点平仓的盈亏并入累计已实现; 实时总额随之累计。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        monitor_mod.DATA_DIR = d
        monitor_mod.DB_PATH = d / "live.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self):
        cfg = MonitorConfig(side="long", capital=100.0, stop_loss=0.10,
                            take_profit=0.50, top=1, rebalance_sec=21600)
        prec = {s: {"qty_prec": 3, "price_prec": 4}
                for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}
        m = Monitor(FakeClient(), cfg, get_prec=lambda: prec)
        m.init_db()
        m._load_account()
        return m

    def test_rebalance_banks_pnl(self):
        m = self._make()
        m._open_snapshot()                       # 快照1: 选 AAAUSDT @100
        sym = m.plan["positions"][0]["symbol"]
        self.assertEqual(sym, "AAAUSDT")
        self.assertEqual(m.realized_cum, 0.0)
        sid1 = m.snapshot_id

        m.client.round = 1                       # 下一轮换 CCCUSDT
        m._open_snapshot()                        # 调仓: AAAUSDT @110 到点平仓并入累计
        self.assertGreater(m.realized_cum, 0.0)   # 110>100 → 正收益累计
        self.assertNotEqual(m.snapshot_id, sid1)
        self.assertEqual(m.plan["positions"][0]["symbol"], "CCCUSDT")

        snap = m.snapshot()                       # 实时总额 = 本金 + 累计 + 本轮浮盈
        s = snap["summary"]
        self.assertAlmostEqual(s["equity"], s["initial_capital"] + s["cum_pnl"], places=4)
        self.assertGreater(s["realized_cum"], 0.0)

    def test_account_persists_across_restart(self):
        m = self._make()
        m._open_snapshot()
        m.client.round = 1
        m._open_snapshot()
        banked = m.realized_cum
        self.assertGreater(banked, 0.0)
        # 新实例(模拟重启)应从 account 表读回累计
        m2 = Monitor(FakeClient(), m.cfg, get_prec=m.get_prec)
        m2._load_account()
        self.assertAlmostEqual(m2.realized_cum, banked, places=4)


if __name__ == "__main__":
    unittest.main()
