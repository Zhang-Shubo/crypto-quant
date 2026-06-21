"""Binance 24h 涨幅榜 (现货 USDT 交易对) —— CLI。

调用公共行情接口 /api/v3/ticker/24hr, 过滤现货 USDT 交易对, 按 24h 涨跌幅排序。
仅依赖标准库。

用法:
    python3 -m cryptoquant.gainers                # 涨幅榜 Top 20
    python3 -m cryptoquant.gainers --top 10       # Top 10
    python3 -m cryptoquant.gainers --losers       # 跌幅榜
    python3 -m cryptoquant.gainers --quote BTC    # 以 BTC 计价
    python3 -m cryptoquant.gainers --json         # JSON 输出
"""
from __future__ import annotations

import argparse
import json
import sys

from .exchange.public import spot_tickers, rank_tickers


def fmt_volume(v: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cryptoquant.gainers", description="Binance 24h 涨幅榜")
    p.add_argument("--top", type=int, default=20, help="榜单条数 (默认 20)")
    p.add_argument("--quote", default="USDT", help="计价货币 (默认 USDT)")
    p.add_argument("--losers", action="store_true", help="显示跌幅榜")
    p.add_argument("--min-volume", type=float, default=1_000_000,
                   help="最小 24h 计价成交额过滤 (默认 1,000,000)")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = p.parse_args(argv)

    quote = args.quote.upper()
    try:
        tickers = spot_tickers(quote)
    except Exception as e:
        print(f"[错误] 请求 Binance 失败: {e}", file=sys.stderr)
        return 1

    ranked = rank_tickers(tickers, quote=quote, top=args.top,
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
        print(f"{i:>2}  {r['symbol']:<14}{r['price']:>16.8g}"
              f"{sign}{r['change_pct']:>8.2f}%{fmt_volume(r['quote_volume']):>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
