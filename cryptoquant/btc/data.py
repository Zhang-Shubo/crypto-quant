"""BTC 日线行情抓取 + 本地缓存。

主仓库的 exchange/public.py 走 fapi.binance.com(USDⓈ-M 永续),在部分地区返回
HTTP 451(地域封锁)。这里改用 **data-api.binance.vision**(Binance 公共数据域,
同样的 /api/v3/klines 接口,无地域封锁),抓 **现货 BTCUSDT** 日线,够做长周期回测。

返回的 bar schema: {"t": openTime_ms, "o","h","l","c","v": float}。
缓存到 data/btc_<interval>.json,二次运行直接读盘(除非 --refresh)。
"""
from __future__ import annotations

import json
import time
import urllib.request

from ..config import DATA_DIR

VISION_BASE = "https://data-api.binance.vision"
DAY_MS = 86_400_000


def _get(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-quant-btc/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1d",
                 start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
    """分页抓 [start, end) 现货 K 线 → 升序 bar 列表。

    binance.vision 单次最多 1000 根; 日线 1000 根 ≈ 2.7 年, 需翻页。
    """
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if start_ms is None:
        start_ms = 1_502_928_000_000          # 2017-08-17, BTCUSDT 现货最早
    out: list[dict] = []
    cur = start_ms
    step = DAY_MS if interval == "1d" else 3_600_000
    while cur < end_ms:
        url = (f"{VISION_BASE}/api/v3/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        data = _get(url)
        if not data:
            break
        for k in data:
            out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
        nxt = int(data[-1][0]) + step
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1000:
            break
        time.sleep(0.15)
    # 去重(分页边界可能重叠)并按时间升序
    dedup = {b["t"]: b for b in out}
    return [dedup[t] for t in sorted(dedup)]


def load(symbol: str = "BTCUSDT", interval: str = "1d",
         refresh: bool = False) -> list[dict]:
    """带缓存的加载: 优先读 data/btc_<interval>.json, 否则抓网络并写盘。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{symbol.lower()}_{interval}.json"
    if path.exists() and not refresh:
        bars = json.loads(path.read_text())
        if bars:
            return bars
    bars = fetch_klines(symbol, interval)
    path.write_text(json.dumps(bars, separators=(",", ":")))
    return bars
