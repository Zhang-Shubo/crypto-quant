#!/usr/bin/env python3
"""做空涨幅榜策略回测 (Short-the-Gainers)。

策略逻辑
--------
- 标的池: Binance USDT 永续合约中 24h 成交额最高的 N 个。
- 每 6 小时调仓一次: 按"过去 24h 涨幅"排序, 做空涨幅榜前 5, 等权 (各 20% 净值名义)。
- 持有 6h 到下次调仓, 平掉/换成新的前 5。
- 计入成本:
    * 手续费: 按 taker 费率, 对调仓换手的名义金额收取 (开/平/调整都算)。
    * 资金费: 永续每 8h 结算一次; 资金费率为正时空头收取, 为负时空头支付。
- 这是一个均值回归式的押注: 短期暴涨的币随后回落。

仅依赖标准库。数据取自 Binance 合约公共接口 (需能直连 fapi.binance.com)。

用法:
    python3 backtest/short_gainers.py                       # 默认参数
    python3 backtest/short_gainers.py --days 45 --universe 120 --top 5
    python3 backtest/short_gainers.py --fee 0.0005 --capital 10000
输出:
    backtest/results.json   完整回测结果 (供程序消费)
    frontend/data.js        window.RESULTS = {...} (供前端 file:// 直接打开)
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
            if e.code == 429:  # 限频, 退避重试
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
    """选成交额最高的 N 个 USDT 永续。"""
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


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """1h 收盘价, 返回 {openTime_ms: close}。"""
    out: dict[int, float] = {}
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
            out[int(k[0])] = float(k[4])
        nxt = int(data[-1][0]) + HOUR_MS
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1500:
            break
    return out


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """资金费率历史, 返回按时间排序的 [(fundingTime_ms, rate)]。"""
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


# ----------------------------- 回测 -----------------------------
def funding_between(funding: list[tuple[int, float]], lo: int, hi: int) -> float:
    """累计 (lo, hi] 内的资金费率之和。"""
    return sum(r for ts, r in funding if lo < ts <= hi)


def run_backtest(args) -> dict:
    end_ms = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start_ms = end_ms - args.days * DAY_MS

    print(f"[1/3] 选取标的池 (成交额前 {args.universe})...", file=sys.stderr)
    universe = select_universe(args.universe)
    print(f"      标的池 {len(universe)} 个", file=sys.stderr)

    print(f"[2/3] 抓取 {args.days} 天 1h K线 + 资金费率...", file=sys.stderr)
    prices: dict[str, dict[int, float]] = {}
    funding: dict[str, list[tuple[int, float]]] = {}
    for i, sym in enumerate(universe, 1):
        try:
            kl = fetch_klines(sym, start_ms, end_ms)
            if len(kl) < 30:
                continue
            prices[sym] = kl
            funding[sym] = fetch_funding(sym, start_ms, end_ms)
        except Exception as e:  # 个别标的失败跳过, 不中断
            print(f"      ! {sym} 跳过: {e}", file=sys.stderr)
        if i % 20 == 0:
            print(f"      {i}/{len(universe)}", file=sys.stderr)
        time.sleep(0.05)
    syms = [s for s in universe if s in prices]
    print(f"      有效标的 {len(syms)} 个", file=sys.stderr)

    # 调仓时点: start+24h 起, 每 6h 一次
    rebal_times = list(range(start_ms + DAY_MS, end_ms + 1, args.hours * HOUR_MS))

    print(f"[3/3] 模拟 {len(rebal_times)} 个调仓周期...", file=sys.stderr)
    equity = float(args.capital)
    fee = args.fee
    held: dict[str, dict] = {}  # symbol -> {notional, entry, open_idx, ret24}
    curve: list[dict] = []
    cycles: list[dict] = []
    tot_fee = tot_fund = tot_price = 0.0
    cycle_rets: list[float] = []

    def price_at(sym: str, t: int):
        return prices[sym].get(t)

    prev_t = None
    for k, t in enumerate(rebal_times):
        # 1) 结算上一周期持仓盈亏 (价格 + 资金费)
        cyc_price = cyc_fund = 0.0
        if held and prev_t is not None:
            for sym, pos in held.items():
                p_now = price_at(sym, t)
                if p_now is None:
                    continue
                pp = pos["notional"] * (pos["entry"] - p_now) / pos["entry"]  # 空头
                fr = funding_between(funding[sym], prev_t, t)
                pf = pos["notional"] * fr  # 资金费率>0 空头收取
                cyc_price += pp
                cyc_fund += pf
                # 把已实现盈亏回填到开仓那一周期
                oc = cycles[pos["open_idx"]]
                for leg in oc["shorts"]:
                    if leg["symbol"] == sym:
                        leg["exit"] = p_now
                        leg["pnl"] = round(pp + pf, 2)
                        leg["funding"] = round(pf, 2)
                        break
        equity += cyc_price + cyc_fund
        tot_price += cyc_price
        tot_fund += cyc_fund

        # 2) 计算新的涨幅榜前 N
        ranked = []
        t24 = t - DAY_MS
        for sym in syms:
            p_now = price_at(sym, t)
            p_old = price_at(sym, t24)
            if p_now and p_old and p_old > 0:
                ranked.append((sym, p_now / p_old - 1.0, p_now))
        ranked.sort(key=lambda r: r[1], reverse=True)
        picks = ranked[: args.top]

        # 3) 调仓换手手续费
        target_notional = (equity / args.top) if picks else 0.0
        target = {sym: target_notional for sym, _, _ in picks}
        turnover = 0.0
        for sym in set(target) | set(held):
            turnover += abs(target.get(sym, 0.0) - (held.get(sym, {}).get("notional", 0.0)))
        cyc_fee = turnover * fee
        equity -= cyc_fee
        tot_fee += cyc_fee

        net = cyc_price + cyc_fund - cyc_fee
        if prev_t is not None and held:
            cycle_rets.append(net / (equity - net) if (equity - net) else 0.0)

        # 4) 记录本周期
        cyc_rec = {
            "time": t,
            "equity": round(equity, 2),
            "pnl_price": round(cyc_price, 2),
            "pnl_funding": round(cyc_fund, 2),
            "fee": round(cyc_fee, 2),
            "net": round(net, 2),
            "shorts": [
                {"symbol": sym, "ret24_pct": round(r * 100, 2), "entry": p,
                 "exit": None, "pnl": None, "funding": None}
                for sym, r, p in picks
            ],
        }
        cycles.append(cyc_rec)
        curve.append({"time": t, "equity": round(equity, 2)})

        # 5) 建立新持仓
        held = {sym: {"notional": target_notional, "entry": p, "open_idx": k, "ret24": r}
                for sym, r, p in picks}
        prev_t = t

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
        cycles_per_year = 365 * 24 / args.hours
        sharpe = (statistics.fmean(cycle_rets) / statistics.pstdev(cycle_rets)) * (cycles_per_year ** 0.5)
    else:
        sharpe = 0.0
    wins = sum(1 for c in cycles if c["net"] > 0)
    n_eff = sum(1 for c in cycles if c["net"] != 0)
    win_rate = wins / n_eff if n_eff else 0.0

    return {
        "meta": {
            "generated_at": int(time.time() * 1000),
            "strategy": "每6h做空24h涨幅榜前5 (USDT永续, 等权)",
            "days": args.days,
            "rebalance_hours": args.hours,
            "top_n": args.top,
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
        },
        "curve": curve,
        "cycles": cycles,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="做空涨幅榜策略回测")
    p.add_argument("--days", type=int, default=45, help="回测天数 (默认 45)")
    p.add_argument("--universe", type=int, default=120, help="标的池大小 (成交额前 N, 默认 120)")
    p.add_argument("--top", type=int, default=5, help="做空涨幅榜前几 (默认 5)")
    p.add_argument("--hours", type=int, default=6, help="调仓周期/小时 (默认 6)")
    p.add_argument("--fee", type=float, default=0.0005, help="taker 手续费率 (默认 0.0005=0.05%%)")
    p.add_argument("--capital", type=float, default=10000, help="初始资金 USDT (默认 10000)")
    args = p.parse_args(argv)

    result = run_backtest(args)

    os.makedirs(os.path.join(ROOT, "frontend"), exist_ok=True)
    json_path = os.path.join(HERE, "results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    data_js = os.path.join(ROOT, "frontend", "data.js")
    with open(data_js, "w", encoding="utf-8") as f:
        f.write("window.RESULTS = ")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    s = result["summary"]
    print("\n===== 回测结果 =====", file=sys.stderr)
    print(f"周期数        : {s['num_cycles']}", file=sys.stderr)
    print(f"期末净值      : {s['final_equity']}  (初始 {result['meta']['start_equity']})", file=sys.stderr)
    print(f"总收益        : {s['total_return_pct']}%   年化 {s['cagr_pct']}%", file=sys.stderr)
    print(f"最大回撤      : {s['max_drawdown_pct']}%", file=sys.stderr)
    print(f"夏普          : {s['sharpe']}", file=sys.stderr)
    print(f"胜率          : {s['win_rate_pct']}%", file=sys.stderr)
    print(f"价格盈亏/资金费/手续费: {s['total_price_pnl']} / {s['total_funding']} / -{s['total_fees']}", file=sys.stderr)
    print(f"\n已写出: {json_path}\n        {data_js}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
