"""涨幅榜策略 —— 共享的纯逻辑 (无网络/无状态), 供实盘执行与监控复用。

策略思想: 按「过去窗口涨幅」排序取前 N, 等权建仓; 方向 short=逆动量 / long=顺动量;
每仓挂止损/止盈。这里只放与方向无关、可单测的计算: 选标的、止损止盈价位、下取整。
"""
from __future__ import annotations

import math

from ..config import LEVERAGED_SUFFIXES


def is_leveraged(symbol: str) -> bool:
    """是否杠杆代币 (UP/DOWN/BULL/BEAR), 选标的时排除。"""
    return symbol.endswith(LEVERAGED_SUFFIXES)


def round_down(x: float, prec: int) -> float:
    """按精度向下取整 (下单数量/价格必须 <= 交易所精度, 不能四舍五入超)。"""
    f = 10 ** prec
    return math.floor(x * f) / f


def pick_gainers(tickers: list[dict], top: int, allowed: dict | set | None = None,
                 min_quote_volume: float = 0.0) -> list[dict]:
    """从合约 24h ticker 选 USDT 永续涨幅榜前 N。

    tickers : BinanceFutures.ticker_24hr() 原始返回。
    allowed : 可交易符号集 (如执行所 precision_map 的 key); 不在其中的跳过。
              支持排榜来源与执行所不同 (实盘排榜 / testnet 执行)。
    返回统一 schema: {symbol, price, change_pct, quote_volume}。
    """
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or is_leveraged(sym):
            continue
        if allowed is not None and sym not in allowed:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            if qv < min_quote_volume:
                continue
            rows.append({
                "symbol": sym,
                "price": float(t["lastPrice"]),
                "change_pct": float(t["priceChangePercent"]),
                "quote_volume": qv,
            })
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows[:top]


def stop_take_levels(side: str, entry: float, stop_loss: float, take_profit: float
                     ) -> tuple[float, float]:
    """给定方向与入场价, 返回 (止损价, 止盈价)。

    short: 价格上涨触止损、下跌触止盈; long 反之。stop_loss/take_profit 为比例 (0.1=10%)。
    """
    if side == "short":
        return entry * (1 + stop_loss), entry * (1 - take_profit)
    return entry * (1 - stop_loss), entry * (1 + take_profit)
