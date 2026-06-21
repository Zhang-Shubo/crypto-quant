"""Binance 公共行情 (免签): 现货/合约 ticker、K 线、资金费率、标的池。

供「现货涨幅榜 CLI」与「回测引擎」共用。实盘签名接口在 futures.py。
"""
from __future__ import annotations

from ..config import FAPI_BASE, SPOT_BASE, HOUR_MS, LEVERAGED_SUFFIXES
from .http import get_json


# ----------------------------- 现货 (spot) -----------------------------
def spot_tickers(quote: str = "USDT") -> list[dict]:
    """现货 24h ticker 全表。"""
    return get_json(SPOT_BASE, "/api/v3/ticker/24hr")


def rank_tickers(tickers: list[dict], quote: str = "USDT", top: int = 20,
                 losers: bool = False, min_quote_volume: float = 0.0,
                 exclude_leveraged: bool = True) -> list[dict]:
    """按 24h 涨跌幅排序, 返回统一 schema:
        {symbol, price, change_pct, quote_volume}

    quote: 计价货币后缀过滤; losers: 取跌幅榜; exclude_leveraged: 排除杠杆代币。
    """
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith(quote):
            continue
        if exclude_leveraged and quote == "USDT" and sym.endswith(LEVERAGED_SUFFIXES):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            if qv < min_quote_volume:
                continue
            rows.append({
                "symbol": sym,
                "price": float(t.get("lastPrice", 0)),
                "change_pct": float(t.get("priceChangePercent", 0)),
                "quote_volume": qv,
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["change_pct"], reverse=not losers)
    return rows[:top]


# ----------------------------- 合约 (USDⓈ-M 永续) -----------------------------
def perpetual_symbols() -> set[str]:
    """TRADING 状态的 USDT 本位永续合约符号集。"""
    info = get_json(FAPI_BASE, "/fapi/v1/exchangeInfo")
    return {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"}


def select_universe(n: int) -> list[str]:
    """按当前 24h 成交额降序取前 N 个永续合约 (回测候选集)。"""
    perp = perpetual_symbols()
    tickers = get_json(FAPI_BASE, "/fapi/v1/ticker/24hr")
    rows = [t for t in tickers if t["symbol"] in perp]
    rows.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "1h") -> dict:
    """分页抓 [start, end) 区间 K 线, 返回 {openTime: (close, high, low, quoteVol)}。"""
    out: dict[int, tuple] = {}
    cur = start_ms
    while cur < end_ms:
        data = get_json(FAPI_BASE, "/fapi/v1/klines",
                        {"symbol": symbol, "interval": interval, "startTime": cur,
                         "endTime": end_ms, "limit": 1500})
        if not data:
            break
        for k in data:
            out[int(k[0])] = (float(k[4]), float(k[2]), float(k[3]), float(k[7]))
        nxt = int(data[-1][0]) + HOUR_MS
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1500:
            break
    return out


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """分页抓资金费率历史, 返回升序 [(fundingTime, rate), ...]。"""
    out = []
    cur = start_ms
    while cur < end_ms:
        data = get_json(FAPI_BASE, "/fapi/v1/fundingRate",
                        {"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000})
        if not data:
            break
        for d in data:
            out.append((int(d["fundingTime"]), float(d["fundingRate"])))
        nxt = int(data[-1]["fundingTime"]) + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1000:
            break
    out.sort()
    return out


def funding_between(funding: list[tuple], lo: int, hi: int) -> float:
    """累加 (lo, hi] 区间内的资金费率。"""
    return sum(r for ts, r in funding if lo < ts <= hi)
