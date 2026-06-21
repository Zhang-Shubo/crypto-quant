#!/usr/bin/env python3
"""Binance 24h 涨幅榜 (现货 USDT 交易对)。

调用 Binance 公共行情接口 /api/v3/ticker/24hr，过滤现货 USDT 交易对，
按 24 小时涨跌幅排序，输出涨幅榜 / 跌幅榜。仅依赖标准库。

用法:
    python3 data/binance_gainers.py                # 涨幅榜 Top 20
    python3 data/binance_gainers.py --top 10       # Top 10
    python3 data/binance_gainers.py --losers       # 跌幅榜
    python3 data/binance_gainers.py --quote BTC    # 以 BTC 计价的交易对
    python3 data/binance_gainers.py --json         # 输出 JSON
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error

API_URL = "https://api.binance.com/api/v3/ticker/24hr"

# 杠杆代币 / 非普通现货后缀，默认排除，避免污染涨幅榜
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def fetch_tickers(timeout: int = 15) -> list[dict]:
    req = urllib.request.Request(API_URL, headers={"User-Agent": "crypto-quant/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rank(
    tickers: list[dict],
    quote: str = "USDT",
    top: int = 20,
    losers: bool = False,
    min_quote_volume: float = 0.0,
) -> list[dict]:
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith(quote):
            continue
        if quote == "USDT" and sym.endswith(LEVERAGED_SUFFIXES):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            if qv < min_quote_volume:
                continue
            rows.append(
                {
                    "symbol": sym,
                    "last": float(t.get("lastPrice", 0)),
                    "change_pct": float(t.get("priceChangePercent", 0)),
                    "quote_volume": qv,
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["change_pct"], reverse=not losers)
    return rows[:top]


def fmt_volume(v: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Binance 24h 涨幅榜")
    p.add_argument("--top", type=int, default=20, help="榜单条数 (默认 20)")
    p.add_argument("--quote", default="USDT", help="计价货币 (默认 USDT)")
    p.add_argument("--losers", action="store_true", help="显示跌幅榜")
    p.add_argument("--min-volume", type=float, default=1_000_000,
                   help="最小 24h 计价成交额过滤 (默认 1,000,000)")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = p.parse_args(argv)

    try:
        tickers = fetch_tickers()
    except urllib.error.URLError as e:
        print(f"[错误] 请求 Binance 失败: {e}", file=sys.stderr)
        return 1

    quote = args.quote.upper()
    ranked = rank(tickers, quote=quote, top=args.top,
                  losers=args.losers, min_quote_volume=args.min_volume)

    if args.json:
        print(json.dumps(ranked, ensure_ascii=False, indent=2))
        return 0

    title = "跌幅榜" if args.losers else "涨幅榜"
    print(f"Binance 24h {title} (计价 {quote}, 成交额>{fmt_volume(args.min_volume)}, "
          f"共 {len(ranked)} 条)")
    print(f"{'#':>2}  {'交易对':<14}{'最新价':>16}{'涨跌幅':>10}{'成交额':>12}")
    print("-" * 56)
    for i, r in enumerate(ranked, 1):
        sign = "+" if r["change_pct"] >= 0 else ""
        print(f"{i:>2}  {r['symbol']:<14}{r['last']:>16.8g}"
              f"{sign}{r['change_pct']:>8.2f}%{fmt_volume(r['quote_volume']):>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
