"""技术指标 (纯标准库, 输入/输出都是等长 list, 不足窗口处为 None)。

所有指标在 t 处只用 [0..t] 的数据(无前视); 回测引擎再把信号整体滞后 1 根执行,
所以「收盘算信号、次根成交」的口径下不存在前视偏差。
"""
from __future__ import annotations

import math


def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    k = 2 / (n + 1)
    e = None
    for i, x in enumerate(xs):
        if i == n - 1:                      # 用前 n 根 SMA 作种子
            e = sum(xs[:n]) / n
            out[i] = e
        elif i >= n:
            e = x * k + e * (1 - k)
            out[i] = e
    return out


def rolling_std(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1: i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def bollinger(xs: list[float], n: int = 20, k: float = 2.0):
    mid = sma(xs, n)
    sd = rolling_std(xs, n)
    up = [m + k * s if m is not None else None for m, s in zip(mid, sd)]
    lo = [m - k * s if m is not None else None for m, s in zip(mid, sd)]
    return mid, up, lo


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder RSI。"""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (macd_line, signal_line, hist)。"""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(ef, es)]
    vals = [x for x in line if x is not None]
    sig_compact = ema(vals, signal)
    sig: list[float | None] = [None] * len(closes)
    j = 0
    for i, x in enumerate(line):
        if x is not None:
            sig[i] = sig_compact[j]
            j += 1
    hist = [(l - s) if (l is not None and s is not None) else None
            for l, s in zip(line, sig)]
    return line, sig, hist


def donchian(highs: list[float], lows: list[float], n: int):
    """返回 (prior_high, prior_low): 截至 t-1 的近 n 根最高/最低
    (排除当根, 供「突破前 n 日高点」判定, 无前视)。"""
    ph: list[float | None] = [None] * len(highs)
    pl: list[float | None] = [None] * len(lows)
    for i in range(n, len(highs)):
        ph[i] = max(highs[i - n: i])
        pl[i] = min(lows[i - n: i])
    return ph, pl


def atr(highs, lows, closes, n: int = 14) -> list[float | None]:
    tr = [None] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    a = sum(tr[1: n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(closes)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out
