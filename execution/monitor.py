#!/usr/bin/env python3
"""dry-run 实时监控服务 (不下任何单)。

启动时冻结一份"如果现在按策略开这 N 仓"的快照 (入场价/数量/止损/止盈),
之后持续用 Binance 实时标记价 (markPrice) 监控每仓距止损/止盈的远近与浮盈。
纯公共行情, 无需 API Key。

GET /            -> 监控页 (frontend/monitor.html)
GET /api/state   -> {plan, prices, now}

环境变量: MON_SIDE(long/short) MON_CAPITAL MON_SL MON_TP MON_TOP MON_PORT
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from binance_client import BinanceFutures          # noqa: E402
from live_trade import pick_gainers, round_down    # noqa: E402

SIDE = os.environ.get("MON_SIDE", "long")
CAPITAL = float(os.environ.get("MON_CAPITAL", "100"))
SL = float(os.environ.get("MON_SL", "0.10"))
TP = float(os.environ.get("MON_TP", "0.50"))
TOP = int(os.environ.get("MON_TOP", "5"))
MIN_QVOL = float(os.environ.get("MON_MIN_QVOL", "1000000"))
PORT = int(os.environ.get("MON_PORT", "8802"))
HTML = os.path.join(ROOT, "frontend", "monitor.html")

client = BinanceFutures(testnet=False)   # 实盘公共行情


def build_plan():
    prec = client.precision_map()
    picks = [g for g in pick_gainers(client, TOP, prec) if g["qvol"] >= MIN_QVOL]
    notional = CAPITAL / TOP if picks else 0.0
    pos = []
    for g in picks:
        sym, price, pp = g["symbol"], g["price"], prec[g["symbol"]]
        qty = round_down(notional / price, pp["qty_prec"])
        if SIDE == "long":
            sl = round_down(price * (1 - SL), pp["price_prec"])
            tp = round_down(price * (1 + TP), pp["price_prec"])
        else:
            sl = round_down(price * (1 + SL), pp["price_prec"])
            tp = round_down(price * (1 - TP), pp["price_prec"])
        pos.append({"symbol": sym, "entry": price, "qty": qty, "sl": sl, "tp": tp,
                    "chg24": g["chg24"] if "chg24" in g else g["chg"]})
    return {"side": SIDE, "capital": CAPITAL, "sl": SL, "tp": TP, "top": TOP,
            "notional_each": round(notional, 2),
            "frozen_at": int(time.time() * 1000), "positions": pos}


PLAN = build_plan()
_cache = {"t": 0.0, "px": {}}


def mark_prices(symbols):
    now = time.time()
    if now - _cache["t"] < 2 and _cache["px"]:
        return _cache["px"]
    data = client._request("GET", "/fapi/v1/premiumIndex")   # 公共, 全市场 markPrice
    px = {d["symbol"]: float(d["markPrice"]) for d in data if d["symbol"] in symbols}
    _cache.update(t=now, px=px)
    return px


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            syms = {p["symbol"] for p in PLAN["positions"]}
            try:
                px = mark_prices(syms)
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}), "application/json")
                return
            self._send(200, json.dumps({"plan": PLAN, "prices": px,
                                        "now": int(time.time() * 1000)}), "application/json")
        elif self.path in ("/", "/monitor.html") or self.path.startswith("/?"):
            try:
                with open(HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "monitor.html not found", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[monitor] 127.0.0.1:{PORT} side={SIDE} capital={CAPITAL} sl={SL:.0%} tp={TP:.0%} "
          f"冻结 {len(PLAN['positions'])} 仓", file=sys.stderr)
    for p in PLAN["positions"]:
        print(f"   {p['symbol']:<13} entry={p['entry']:g} SL={p['sl']:g} TP={p['tp']:g} qty={p['qty']:g}",
              file=sys.stderr)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
