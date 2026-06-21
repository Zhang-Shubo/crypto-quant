"""市场看板数据源: 缓存合约 24h / 1h 涨幅榜, 供 /api/gainers。

后台 market_loop 周期刷新; 1h 榜需逐标的取 2 根 K 线, 较慢, 故降频。
"""
from __future__ import annotations

import sys
import threading
import time

from ..config import LEVERAGED_SUFFIXES


class Market:
    def __init__(self, client, universe: int):
        self.client = client
        self.universe = universe
        self.lock = threading.Lock()
        self.perp: set[str] = set()
        self.prec: dict = {}
        self.cache = {"24h": {"t": 0, "rows": []}, "1h": {"t": 0, "rows": []}}

    def load_meta(self):
        """加载永续符号集 + 精度表 (一次), 供榜单过滤与监控下单精度共用。"""
        info = self.client.exchange_info()
        self.perp = {s["symbol"] for s in info["symbols"]
                     if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
                     and s.get("quoteAsset") == "USDT"}
        self.prec = self.client.precision_map()

    def refresh_24h(self) -> list[str]:
        rows = []
        for t in self.client.ticker_24hr():
            sym = t["symbol"]
            if sym not in self.perp or sym.endswith(LEVERAGED_SUFFIXES):
                continue
            try:
                rows.append({"symbol": sym, "chg": float(t["priceChangePercent"]),
                             "price": float(t["lastPrice"]), "qvol": float(t.get("quoteVolume", 0))})
            except (KeyError, ValueError):
                continue
        rows.sort(key=lambda r: r["chg"], reverse=True)
        with self.lock:
            self.cache["24h"] = {"t": int(time.time() * 1000), "rows": rows}
        # 返回成交额前 N 作为 1h 榜的标的池
        return [r["symbol"] for r in sorted(rows, key=lambda r: r["qvol"], reverse=True)[:self.universe]]

    def refresh_1h(self, universe):
        rows = []
        for sym in universe:
            try:
                kl = self.client.klines(sym, "1h", 2)
                if len(kl) >= 2 and float(kl[-2][4]) > 0:
                    last, prev = float(kl[-1][4]), float(kl[-2][4])
                    rows.append({"symbol": sym, "chg": (last / prev - 1) * 100,
                                 "price": last, "qvol": float(kl[-1][7])})
            except Exception:
                continue
            time.sleep(0.03)
        rows.sort(key=lambda r: r["chg"], reverse=True)
        with self.lock:
            self.cache["1h"] = {"t": int(time.time() * 1000), "rows": rows}

    def snapshot(self, window: str) -> dict:
        with self.lock:
            return dict(self.cache[window])

    def run_forever(self):
        last_1h = 0
        while True:
            try:
                uni = self.refresh_24h()
                if time.time() - last_1h > 110:
                    self.refresh_1h(uni)
                    last_1h = time.time()
            except Exception as e:
                print(f"[market] {e}", file=sys.stderr)
            time.sleep(30)
