"""crypto-quant 统一后端 (单进程, 127.0.0.1:8800)。

整合三块:
  市场: GET /api/gainers?window=1h|24h   GET /api/klines?symbol=&interval=&limit=
  回测: 静态文件 frontend/runs/*.json (由 cryptoquant.backtest 生成)
  实时: GET /api/monitor   GET /api/monitor/history   (dry-run, 不下单)
静态前端: 直接服务 frontend/ 目录。

后台线程:
  Market.run_forever   —— 刷新 1h/24h 涨幅榜 (缓存)
  Monitor.run_forever  —— 冻结实时策略持仓, 周期采样浮盈写 SQLite (data/live.db)

配置经环境变量 (见 cryptoquant/config.py: MonitorConfig 与 docs/web-api.md)。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import FRONTEND_DIR, MonitorConfig
from ..exchange import BinanceFutures
from ..live.monitor import Monitor
from .market import Market

FRONTEND = str(FRONTEND_DIR)


def make_handler(market: Market, monitor: Monitor):
    """绑定 market/monitor 状态, 返回 HTTP Handler 类。"""

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
                    m = market.snapshot(win)
                    rows = m["rows"] if desc else list(reversed(m["rows"]))
                    topn = int(q.get("top", ["30"])[0])
                    return self._json({"window": win, "updated": m["t"], "rows": rows[:topn]})
                if path == "/api/klines":
                    sym = q.get("symbol", [""])[0].upper()
                    interval = q.get("interval", ["1h"])[0]
                    limit = min(int(q.get("limit", ["200"])[0]), 1000)
                    if not sym:
                        return self._json({"error": "symbol required"}, 400)
                    kl = market.client.klines(sym, interval, limit)
                    candles = [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]),
                                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in kl]
                    pp = market.prec.get(sym, {})
                    return self._json({"symbol": sym, "interval": interval, "candles": candles,
                                       "price_precision": pp.get("price_prec", 2),
                                       "tick_size": pp.get("tick_size")})
                if path == "/api/monitor":
                    return self._json(monitor.snapshot())
                if path == "/api/monitor/history":
                    return self._json(monitor.history())
                return self._static(path)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        def _static(self, path):
            if path in ("/", ""):
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

    return Handler


def main():
    cfg = MonitorConfig.from_env()
    client = BinanceFutures(testnet=False)
    market = Market(client, cfg.universe)
    market.load_meta()
    monitor = Monitor(client, cfg, get_prec=lambda: market.prec)
    monitor.init_db()

    threading.Thread(target=market.run_forever, daemon=True).start()
    threading.Thread(target=monitor.run_forever, daemon=True).start()

    print(f"[app] 127.0.0.1:{cfg.port}  side={cfg.side} cap={cfg.capital} "
          f"sl={cfg.stop_loss:.0%} tp={cfg.take_profit:.0%} universe={cfg.universe} "
          f"sample={cfg.sample_sec}s", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", cfg.port), make_handler(market, monitor)).serve_forever()


if __name__ == "__main__":
    main()
