#!/usr/bin/env python3
"""crypto-quant 统一后端 (单进程, 127.0.0.1:8800)。

整合三块:
  市场: GET /api/gainers?window=1h|24h   GET /api/klines?symbol=&interval=&limit=
  回测: 静态文件 frontend/runs/*.json (由 backtest/short_gainers.py 生成)
  实时: GET /api/monitor   GET /api/monitor/history   (dry-run, 不下单)
        实时策略每 SAMPLE_SEC 采样 PnL 并写 SQLite (data/live.db), 重启后历史保留。
静态前端: 直接服务 frontend/ 目录。

后台线程:
  market_loop  —— 刷新 1h/24h 涨幅榜 (缓存)
  sample_loop  —— 冻结一组实时策略持仓, 周期采样浮盈写库

环境变量: APP_PORT MON_SIDE MON_CAPITAL MON_SL MON_TP MON_TOP MON_UNIVERSE SAMPLE_SEC
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FRONTEND = os.path.join(ROOT, "frontend")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "live.db")
sys.path.insert(0, os.path.join(ROOT, "execution"))
from binance_client import BinanceFutures          # noqa: E402
from live_trade import pick_gainers, round_down     # noqa: E402

# ---------- 配置 ----------
PORT = int(os.environ.get("APP_PORT", "8800"))
SIDE = os.environ.get("MON_SIDE", "long")
CAPITAL = float(os.environ.get("MON_CAPITAL", "100"))
SL = float(os.environ.get("MON_SL", "0.10"))
TP = float(os.environ.get("MON_TP", "0.50"))
TOP = int(os.environ.get("MON_TOP", "5"))
MIN_QVOL = float(os.environ.get("MON_MIN_QVOL", "1000000"))
UNIVERSE = int(os.environ.get("MON_UNIVERSE", "150"))
SAMPLE_SEC = int(os.environ.get("SAMPLE_SEC", "60"))

LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
client = BinanceFutures(testnet=False)

# ---------- 共享状态 ----------
_perp: set[str] = set()
_prec: dict = {}
_market = {"24h": {"t": 0, "rows": []}, "1h": {"t": 0, "rows": []}}
_lock = threading.Lock()
PLAN = {"side": SIDE, "capital": CAPITAL, "sl": SL, "tp": TP, "top": TOP,
        "notional_each": 0.0, "frozen_at": 0, "positions": []}
SNAPSHOT_ID = None


# ---------- SQLite ----------
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at INTEGER, side TEXT, capital REAL, sl REAL, tp REAL, top INTEGER,
      plan_json TEXT);
    CREATE TABLE IF NOT EXISTS samples(
      snapshot_id INTEGER, ts INTEGER,
      total_pnl REAL, total_notional REAL, hit INTEGER, prices_json TEXT);
    CREATE INDEX IF NOT EXISTS idx_samples ON samples(snapshot_id, ts);
    """)
    con.commit()
    con.close()


# ---------- 行情榜单 ----------
def load_meta():
    global _perp, _prec
    info = client.exchange_info()
    _perp = {s["symbol"] for s in info["symbols"]
             if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
             and s.get("quoteAsset") == "USDT"}
    _prec = client.precision_map()


def refresh_24h():
    rows = []
    for t in client.ticker_24hr():
        sym = t["symbol"]
        if sym not in _perp or sym.endswith(LEVERAGED):
            continue
        try:
            rows.append({"symbol": sym, "chg": float(t["priceChangePercent"]),
                         "price": float(t["lastPrice"]), "qvol": float(t.get("quoteVolume", 0))})
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda r: r["chg"], reverse=True)
    with _lock:
        _market["24h"] = {"t": int(time.time() * 1000), "rows": rows}
    return [r["symbol"] for r in sorted(rows, key=lambda r: r["qvol"], reverse=True)[:UNIVERSE]]


def refresh_1h(universe):
    rows = []
    for sym in universe:
        try:
            kl = client.klines(sym, "1h", 2)
            if len(kl) >= 2 and float(kl[-2][4]) > 0:
                last, prev = float(kl[-1][4]), float(kl[-2][4])
                rows.append({"symbol": sym, "chg": (last / prev - 1) * 100,
                             "price": last, "qvol": float(kl[-1][7])})
        except Exception:
            continue
        time.sleep(0.03)
    rows.sort(key=lambda r: r["chg"], reverse=True)
    with _lock:
        _market["1h"] = {"t": int(time.time() * 1000), "rows": rows}


def market_loop():
    last_1h = 0
    while True:
        try:
            uni = refresh_24h()
            if time.time() - last_1h > 110:
                refresh_1h(uni)
                last_1h = time.time()
        except Exception as e:
            print(f"[market] {e}", file=sys.stderr)
        time.sleep(30)


# ---------- 实时策略 (冻结 + 采样) ----------
def build_plan():
    picks = [g for g in pick_gainers(client, TOP, _prec) if g["qvol"] >= MIN_QVOL]
    notional = CAPITAL / TOP if picks else 0.0
    pos = []
    for g in picks:
        sym, price, pp = g["symbol"], g["price"], _prec[g["symbol"]]
        qty = round_down(notional / price, pp["qty_prec"])
        if SIDE == "long":
            sl, tp = round_down(price * (1 - SL), pp["price_prec"]), round_down(price * (1 + TP), pp["price_prec"])
        else:
            sl, tp = round_down(price * (1 + SL), pp["price_prec"]), round_down(price * (1 - TP), pp["price_prec"])
        pos.append({"symbol": sym, "entry": price, "qty": qty, "sl": sl, "tp": tp, "chg24": g["chg"]})
    return {"side": SIDE, "capital": CAPITAL, "sl": SL, "tp": TP, "top": TOP,
            "notional_each": round(notional, 2), "frozen_at": int(time.time() * 1000), "positions": pos}


def mark_prices(symbols):
    return {d["symbol"]: float(d["markPrice"]) for d in client.premium_index() if d["symbol"] in symbols}


def compute_pnl(plan, px):
    dr = 1 if plan["side"] == "long" else -1
    tot_pnl = tot_notional = 0.0
    hit = 0
    for p in plan["positions"]:
        cur = px.get(p["symbol"])
        if cur is None:
            continue
        tot_pnl += p["qty"] * (cur - p["entry"]) * dr
        tot_notional += p["qty"] * p["entry"]
        if plan["side"] == "long":
            if cur <= p["sl"] or cur >= p["tp"]:
                hit += 1
        else:
            if cur >= p["sl"] or cur <= p["tp"]:
                hit += 1
    return tot_pnl, tot_notional, hit


def sample_loop():
    global PLAN, SNAPSHOT_ID
    # 等行情元数据就绪
    while not _prec:
        time.sleep(1)
    PLAN = build_plan()
    con = db()
    cur = con.execute("INSERT INTO snapshots(created_at,side,capital,sl,tp,top,plan_json) VALUES(?,?,?,?,?,?,?)",
                      (PLAN["frozen_at"], SIDE, CAPITAL, SL, TP, TOP, json.dumps(PLAN, ensure_ascii=False)))
    SNAPSHOT_ID = cur.lastrowid
    con.commit()
    con.close()
    print(f"[live] 冻结快照 #{SNAPSHOT_ID}: {len(PLAN['positions'])} 仓", file=sys.stderr)
    while True:
        try:
            syms = {p["symbol"] for p in PLAN["positions"]}
            px = mark_prices(syms) if syms else {}
            pnl, notional, hit = compute_pnl(PLAN, px)
            con = db()
            with _lock:
                con.execute("INSERT INTO samples(snapshot_id,ts,total_pnl,total_notional,hit,prices_json) VALUES(?,?,?,?,?,?)",
                            (SNAPSHOT_ID, int(time.time() * 1000), round(pnl, 4),
                             round(notional, 2), hit, json.dumps(px)))
                con.commit()
            con.close()
        except Exception as e:
            print(f"[sample] {e}", file=sys.stderr)
        time.sleep(SAMPLE_SEC)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/gainers":
                win = q.get("window", ["24h"])[0]
                win = "1h" if win == "1h" else "24h"
                desc = q.get("order", ["desc"])[0] != "asc"
                with _lock:
                    m = dict(_market[win])
                rows = m["rows"] if desc else list(reversed(m["rows"]))
                topn = int(q.get("top", ["30"])[0])
                return self._json({"window": win, "updated": m["t"], "rows": rows[:topn]})
            if path == "/api/klines":
                sym = q.get("symbol", [""])[0].upper()
                interval = q.get("interval", ["1h"])[0]
                limit = min(int(q.get("limit", ["200"])[0]), 1000)
                if not sym:
                    return self._json({"error": "symbol required"}, 400)
                kl = client.klines(sym, interval, limit)
                candles = [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]),
                            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in kl]
                return self._json({"symbol": sym, "interval": interval, "candles": candles})
            if path == "/api/monitor":
                with _lock:
                    plan = dict(PLAN)
                syms = {p["symbol"] for p in plan["positions"]}
                px = mark_prices(syms) if syms else {}
                pnl, notional, hit = compute_pnl(plan, px)
                return self._json({"plan": plan, "prices": px, "now": int(time.time() * 1000),
                                   "summary": {"total_pnl": round(pnl, 4), "total_notional": round(notional, 2), "hit": hit},
                                   "snapshot_id": SNAPSHOT_ID, "sample_sec": SAMPLE_SEC})
            if path == "/api/monitor/history":
                con = db()
                rows = con.execute("SELECT ts,total_pnl,total_notional,hit FROM samples WHERE snapshot_id=? ORDER BY ts",
                                   (SNAPSHOT_ID,)).fetchall()
                con.close()
                return self._json({"snapshot_id": SNAPSHOT_ID, "frozen_at": PLAN["frozen_at"],
                                   "samples": [dict(r) for r in rows]})
            # 静态文件
            return self._static(path)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(FRONTEND, rel))
        if not full.startswith(FRONTEND) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        ctype = ("text/html; charset=utf-8" if full.endswith(".html")
                 else "application/javascript" if full.endswith(".js")
                 else "application/json" if full.endswith(".json")
                 else "text/css" if full.endswith(".css") else "text/plain")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def log_message(self, *a):
        pass


def main():
    init_db()
    load_meta()
    threading.Thread(target=market_loop, daemon=True).start()
    threading.Thread(target=sample_loop, daemon=True).start()
    print(f"[app] 127.0.0.1:{PORT}  side={SIDE} cap={CAPITAL} sl={SL:.0%} tp={TP:.0%} "
          f"universe={UNIVERSE} sample={SAMPLE_SEC}s", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
