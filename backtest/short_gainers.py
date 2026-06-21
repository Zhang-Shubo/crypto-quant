#!/usr/bin/env python3
"""做空涨幅榜策略回测 (Short-the-Gainers) + 止损/止盈。

策略逻辑
--------
- 标的池: Binance USDT 永续合约中 24h 成交额最高的 N 个。
- 每 6 小时调仓: 按"过去 24h 涨幅"排序, 做空涨幅榜前 5, 等权 (各 20% 净值名义)。
- 每个仓位独立的离场规则 (做空方向):
    * 止损: 仓位亏损达 stop_loss (默认 10%) → 标的价格涨 10%, 在止损位成交平仓。
    * 止盈: 仓位盈利达 take_profit (默认 50%) → 标的价格跌 50%, 在止盈位成交平仓。
    * 到时间: 6h 内都没触发, 到下次调仓时点按收盘价平仓。
  触发判定细化到逐根 1h K线, 用最高/最低价判断; 同一根内止损/止盈都触发时, 保守按止损先成交。
- 计入成本:
    * 手续费: taker 费率, 逐笔开仓 + 平仓各收一次 (按各自成交名义)。
    * 资金费: 永续每 8h 结算; 仅在持仓期间累计; 费率为正时空头收取, 为负时支付。
- 离场后该笔资金闲置到下次调仓 (不中途再入场)。

仅依赖标准库。数据取自 Binance 合约公共接口 (需能直连 fapi.binance.com)。

用法:
    python3 backtest/short_gainers.py
    python3 backtest/short_gainers.py --days 45 --universe 120 --top 5 \
            --stop-loss 0.10 --take-profit 0.50 --fee 0.0005 --capital 10000
输出:
    backtest/results.json   完整回测结果
    frontend/data.js        window.RESULTS = {...} (供前端打开)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ----------------------------- HTTP -----------------------------
def get_json(path: str, params: dict | None = None, timeout: int = 20, retries: int = 4):
    url = f"{FAPI}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-quant/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if e.code in (418, 451):
                raise
            time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {path}: {last}")


# ----------------------------- 数据抓取 -----------------------------
def select_universe(n: int) -> list[str]:
    info = get_json("/fapi/v1/exchangeInfo")
    perp = {
        s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    tickers = get_json("/fapi/v1/ticker/24hr")
    rows = [t for t in tickers if t["symbol"] in perp]
    rows.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> dict[int, tuple]:
    """1h OHLC, 返回 {openTime_ms: (close, high, low)}。"""
    out: dict[int, tuple] = {}
    cur = start_ms
    while cur < end_ms:
        data = get_json(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1h", "startTime": cur,
             "endTime": end_ms, "limit": 1500},
        )
        if not data:
            break
        for k in data:
            out[int(k[0])] = (float(k[4]), float(k[2]), float(k[3]))  # close, high, low
        nxt = int(data[-1][0]) + HOUR_MS
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1500:
            break
    return out


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    cur = start_ms
    while cur < end_ms:
        data = get_json(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000},
        )
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


def funding_between(funding: list[tuple[int, float]], lo: int, hi: int) -> float:
    return sum(r for ts, r in funding if lo < ts <= hi)


# ----------------------------- 离场模拟 -----------------------------
def simulate_hold(bars: dict[int, tuple], t: int, entry: float,
                  hold_ms: int, sl_level: float, tp_level: float, side: str):
    """逐根 1h K线判定仓位离场。返回 (exit_price, exit_time, reason)。
    short: sl_level=entry*(1+sl) 价格涨到此止损; tp_level=entry*(1-tp) 价格跌到此止盈。
    long : sl_level=entry*(1-sl) 价格跌到此止损; tp_level=entry*(1+tp) 价格涨到此止盈。
    同根都触发时保守按止损先成交。无数据返回 (None,None,None)。
    """
    h = t + HOUR_MS
    end = t + hold_ms
    while h <= end:
        bar = bars.get(h)
        if bar:
            _, hi, lo = bar
            if side == "short":
                if hi >= sl_level:           # 价格上涨 → 止损 (保守优先)
                    return sl_level, h, "SL"
                if lo <= tp_level:           # 价格下跌 → 止盈
                    return tp_level, h, "TP"
            else:                            # long
                if lo <= sl_level:           # 价格下跌 → 止损 (保守优先)
                    return sl_level, h, "SL"
                if hi >= tp_level:           # 价格上涨 → 止盈
                    return tp_level, h, "TP"
        h += HOUR_MS
    end_bar = bars.get(end)
    if end_bar:
        return end_bar[0], end, "TIME"       # 到时间按收盘平仓
    return None, None, None


def run_backtest(args) -> dict:
    end_ms = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start_ms = end_ms - args.days * DAY_MS
    hold_ms = args.hours * HOUR_MS

    print(f"[1/3] 选取标的池 (成交额前 {args.universe})...", file=sys.stderr)
    universe = select_universe(args.universe)
    print(f"      标的池 {len(universe)} 个", file=sys.stderr)

    print(f"[2/3] 抓取 {args.days} 天 1h OHLC + 资金费率...", file=sys.stderr)
    prices: dict[str, dict[int, tuple]] = {}
    funding: dict[str, list[tuple[int, float]]] = {}
    for i, sym in enumerate(universe, 1):
        try:
            kl = fetch_klines(sym, start_ms, end_ms)
            if len(kl) < 30:
                continue
            prices[sym] = kl
            funding[sym] = fetch_funding(sym, start_ms, end_ms)
        except Exception as e:
            print(f"      ! {sym} 跳过: {e}", file=sys.stderr)
        if i % 20 == 0:
            print(f"      {i}/{len(universe)}", file=sys.stderr)
        time.sleep(0.05)
    syms = [s for s in universe if s in prices]
    print(f"      有效标的 {len(syms)} 个", file=sys.stderr)

    rebal_times = list(range(start_ms + DAY_MS, end_ms - hold_ms + 1, hold_ms))

    print(f"[3/3] 模拟 {len(rebal_times)} 个调仓周期 (止损 {args.stop_loss:.0%} / 止盈 {args.take_profit:.0%})...",
          file=sys.stderr)
    equity = float(args.capital)
    fee = args.fee
    curve: list[dict] = []
    cycles: list[dict] = []
    tot_fee = tot_fund = tot_price = 0.0
    cycle_rets: list[float] = []
    reason_count = {"SL": 0, "TP": 0, "TIME": 0, "NA": 0}

    def close_at(sym, t):
        b = prices[sym].get(t)
        return b[0] if b else None

    for t in rebal_times:
        # 选涨幅榜前 N
        ranked = []
        t24 = t - DAY_MS
        for sym in syms:
            p_now = close_at(sym, t)
            p_old = close_at(sym, t24)
            if p_now and p_old and p_old > 0:
                ranked.append((sym, p_now / p_old - 1.0, p_now))
        ranked.sort(key=lambda r: r[1], reverse=True)
        picks = ranked[: args.top]

        notional = equity / args.top if picks else 0.0
        cyc_price = cyc_fund = cyc_fee = 0.0
        legs = []
        for sym, ret24, entry in picks:
            if not entry or entry <= 0:
                continue
            if args.side == "short":
                sl_level = entry * (1 + args.stop_loss)
                tp_level = entry * (1 - args.take_profit)
            else:  # long
                sl_level = entry * (1 - args.stop_loss)
                tp_level = entry * (1 + args.take_profit)
            exit_p, exit_t, reason = simulate_hold(
                prices[sym], t, entry, hold_ms, sl_level, tp_level, args.side)
            if exit_p is None:
                reason_count["NA"] += 1
                continue
            reason_count[reason] += 1
            fsum = funding_between(funding[sym], t, exit_t)
            if args.side == "short":
                price_pnl = notional * (entry - exit_p) / entry    # 空头: 价格跌则赚
                fund = notional * fsum                              # 费率>0 空头收取
            else:
                price_pnl = notional * (exit_p - entry) / entry    # 多头: 价格涨则赚
                fund = -notional * fsum                             # 费率>0 多头支付
            f_open = fee * notional
            f_close = fee * notional * (exit_p / entry)
            leg_fee = f_open + f_close
            cyc_price += price_pnl
            cyc_fund += fund
            cyc_fee += leg_fee
            legs.append({
                "symbol": sym,
                "ret24_pct": round(ret24 * 100, 2),
                "entry": entry,
                "exit": round(exit_p, 8),
                "reason": reason,
                "hold_h": round((exit_t - t) / HOUR_MS, 1),
                "pnl": round(price_pnl + fund - leg_fee, 2),
            })

        net = cyc_price + cyc_fund - cyc_fee
        eq_before = equity
        equity += net
        tot_price += cyc_price
        tot_fund += cyc_fund
        tot_fee += cyc_fee
        if eq_before:
            cycle_rets.append(net / eq_before)

        cycles.append({
            "time": t,
            "equity": round(equity, 2),
            "pnl_price": round(cyc_price, 2),
            "pnl_funding": round(cyc_fund, 2),
            "fee": round(cyc_fee, 2),
            "net": round(net, 2),
            "shorts": legs,
        })
        curve.append({"time": t, "equity": round(equity, 2)})

    # ----------------------------- 指标 -----------------------------
    cap = float(args.capital)
    final_eq = equity
    total_ret = final_eq / cap - 1.0
    years = max(args.days / 365.0, 1e-9)
    cagr = (final_eq / cap) ** (1 / years) - 1.0 if final_eq > 0 else -1.0

    peak = -1e18
    max_dd = 0.0
    for c in curve:
        peak = max(peak, c["equity"])
        if peak > 0:
            max_dd = min(max_dd, c["equity"] / peak - 1.0)

    import statistics
    if len(cycle_rets) > 1 and statistics.pstdev(cycle_rets) > 0:
        cpy = 365 * 24 / args.hours
        sharpe = (statistics.fmean(cycle_rets) / statistics.pstdev(cycle_rets)) * (cpy ** 0.5)
    else:
        sharpe = 0.0
    wins = sum(1 for c in cycles if c["net"] > 0)
    n_eff = sum(1 for c in cycles if c["net"] != 0)
    win_rate = wins / n_eff if n_eff else 0.0
    n_legs = sum(reason_count.values())

    return {
        "meta": {
            "generated_at": int(time.time() * 1000),
            "strategy": f"每{args.hours}h{'做多' if args.side=='long' else '做空'}24h涨幅榜前{args.top} · 止损{args.stop_loss:.0%}/止盈{args.take_profit:.0%}",
            "side": args.side,
            "days": args.days,
            "rebalance_hours": args.hours,
            "top_n": args.top,
            "stop_loss": args.stop_loss,
            "take_profit": args.take_profit,
            "universe_requested": args.universe,
            "universe_effective": len(syms),
            "fee_rate": fee,
            "start_equity": cap,
            "period_start": rebal_times[0] if rebal_times else start_ms,
            "period_end": rebal_times[-1] if rebal_times else end_ms,
        },
        "summary": {
            "final_equity": round(final_eq, 2),
            "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2),
            "win_rate_pct": round(win_rate * 100, 1),
            "num_cycles": len(cycles),
            "total_fees": round(tot_fee, 2),
            "total_funding": round(tot_fund, 2),
            "total_price_pnl": round(tot_price, 2),
            "exits": reason_count,
            "exit_sl_pct": round(100 * reason_count["SL"] / n_legs, 1) if n_legs else 0,
            "exit_tp_pct": round(100 * reason_count["TP"] / n_legs, 1) if n_legs else 0,
            "exit_time_pct": round(100 * reason_count["TIME"] / n_legs, 1) if n_legs else 0,
        },
        "curve": curve,
        "cycles": cycles,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="做空涨幅榜回测 (止损/止盈)")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--universe", type=int, default=120)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--side", choices=["short", "long"], default="short",
                   help="做空(short)还是做多(long)涨幅榜, 默认 short")
    p.add_argument("--hours", type=int, default=6, help="调仓/最大持仓周期(小时)")
    p.add_argument("--stop-loss", type=float, default=0.10, help="止损 (仓位亏损比例, 默认0.10)")
    p.add_argument("--take-profit", type=float, default=0.50, help="止盈 (仓位盈利比例, 默认0.50)")
    p.add_argument("--fee", type=float, default=0.0005)
    p.add_argument("--capital", type=float, default=10000)
    args = p.parse_args(argv)

    result = run_backtest(args)

    os.makedirs(os.path.join(ROOT, "frontend"), exist_ok=True)
    json_path = os.path.join(HERE, "results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(ROOT, "frontend", "data.js"), "w", encoding="utf-8") as f:
        f.write("window.RESULTS = ")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    s, m = result["summary"], result["meta"]
    print("\n===== 回测结果 =====", file=sys.stderr)
    print(f"策略          : {m['strategy']}", file=sys.stderr)
    print(f"周期数        : {s['num_cycles']}", file=sys.stderr)
    print(f"期末净值      : {s['final_equity']}  (初始 {m['start_equity']})", file=sys.stderr)
    print(f"总收益        : {s['total_return_pct']}%   年化 {s['cagr_pct']}%", file=sys.stderr)
    print(f"最大回撤      : {s['max_drawdown_pct']}%   夏普 {s['sharpe']}   胜率 {s['win_rate_pct']}%", file=sys.stderr)
    print(f"离场构成      : 止损 {s['exit_sl_pct']}% / 止盈 {s['exit_tp_pct']}% / 到时间 {s['exit_time_pct']}%  ({s['exits']})", file=sys.stderr)
    print(f"价格/资金费/手续费: {s['total_price_pnl']} / {s['total_funding']} / -{s['total_fees']}", file=sys.stderr)
    print(f"\n已写出: {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
