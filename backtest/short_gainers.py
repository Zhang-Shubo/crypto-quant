#!/usr/bin/env python3
"""涨幅榜多空策略回测 (Gainers Momentum / Reversion) + 止损/止盈。

策略
----
- 标的池: Binance USDT 永续合约中 24h 成交额最高的 N 个。
- 每 6 小时调仓: 按"过去 24h 涨幅"排序, 取涨幅榜前 5, 等权 (各 20% 净值名义)。
- 方向 (side): short=做空涨幅榜(逆动量) / long=做多涨幅榜(顺动量)。
- 每仓独立离场:
    short: 价格涨 stop_loss 止损 / 跌 take_profit 止盈 / 否则 6h 到点平仓。
    long : 价格跌 stop_loss 止损 / 涨 take_profit 止盈 / 否则 6h 到点平仓。
  触发用逐根 1h K线最高/最低价判定; 同根都触发保守按止损先成交。
- 成本: taker 手续费逐笔开平; 资金费每 8h 结算, 仅持仓期间累计
  (short 费率>0 收取; long 费率>0 支付)。离场后资金闲置到下次调仓。

多 run 产出
-----------
每个 (side, 止损, 止盈) 组合是一个 run, 写入:
    frontend/runs/<id>.json     单个 run 完整结果 (供 run.html?id=<id>)
    frontend/runs/manifest.json 所有 run 的汇总索引 (供主页 index.html)
    backtest/results.json       最近一个 run (程序消费/兼容)

用法:
    python3 backtest/short_gainers.py --side long --stop-loss 0.2 --take-profit 0.5
    python3 backtest/short_gainers.py --batch          # 一次抓数, 跑预设矩阵
仅依赖标准库 (需直连 fapi.binance.com)。
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
RUNS_DIR = os.path.join(ROOT, "frontend", "runs")

# 预设矩阵: (side, stop_loss, take_profit)
PRESETS = [
    ("short", 0.10, 0.20),
    ("short", 0.10, 0.50),
    ("long", 0.20, 0.50),
    ("long", 0.10, 0.30),
]


# ----------------------------- HTTP -----------------------------
def get_json(path, params=None, timeout=20, retries=4):
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
                time.sleep(2 * (attempt + 1)); continue
            if e.code in (418, 451):
                raise
            time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {path}: {last}")


# ----------------------------- 数据抓取 -----------------------------
def select_universe(n):
    info = get_json("/fapi/v1/exchangeInfo")
    perp = {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"}
    tickers = get_json("/fapi/v1/ticker/24hr")
    rows = [t for t in tickers if t["symbol"] in perp]
    rows.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def fetch_klines(symbol, start_ms, end_ms):
    out = {}
    cur = start_ms
    while cur < end_ms:
        data = get_json("/fapi/v1/klines",
                        {"symbol": symbol, "interval": "1h", "startTime": cur,
                         "endTime": end_ms, "limit": 1500})
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


def fetch_funding(symbol, start_ms, end_ms):
    out = []
    cur = start_ms
    while cur < end_ms:
        data = get_json("/fapi/v1/fundingRate",
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


def funding_between(funding, lo, hi):
    return sum(r for ts, r in funding if lo < ts <= hi)


def load_data(days, universe, hours):
    """抓一次数据, 供多个配置共用。"""
    end_ms = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start_ms = end_ms - days * DAY_MS
    hold_ms = hours * HOUR_MS
    print(f"[数据] 标的池(成交额前 {universe})...", file=sys.stderr)
    uni = select_universe(universe)
    print(f"[数据] 抓 {days}天 1h OHLC + 资金费率 ({len(uni)} 标的)...", file=sys.stderr)
    prices, funding = {}, {}
    for i, sym in enumerate(uni, 1):
        try:
            kl = fetch_klines(sym, start_ms, end_ms)
            if len(kl) < 30:
                continue
            prices[sym] = kl
            funding[sym] = fetch_funding(sym, start_ms, end_ms)
        except Exception as e:
            print(f"      ! {sym} 跳过: {e}", file=sys.stderr)
        if i % 30 == 0:
            print(f"      {i}/{len(uni)}", file=sys.stderr)
        time.sleep(0.05)
    syms = [s for s in uni if s in prices]
    rebal_times = list(range(start_ms + DAY_MS, end_ms - hold_ms + 1, hold_ms))
    print(f"[数据] 有效标的 {len(syms)}, 调仓周期 {len(rebal_times)}", file=sys.stderr)
    return {"syms": syms, "prices": prices, "funding": funding,
            "rebal_times": rebal_times, "start_ms": start_ms, "end_ms": end_ms,
            "days": days, "universe_requested": universe, "hours": hours}


# ----------------------------- 离场 + 模拟 -----------------------------
def simulate_hold(bars, t, entry, hold_ms, sl_level, tp_level, side):
    h = t + HOUR_MS
    end = t + hold_ms
    while h <= end:
        bar = bars.get(h)
        if bar:
            _, hi, lo = bar
            if side == "short":
                if hi >= sl_level:
                    return sl_level, h, "SL"
                if lo <= tp_level:
                    return tp_level, h, "TP"
            else:
                if lo <= sl_level:
                    return sl_level, h, "SL"
                if hi >= tp_level:
                    return tp_level, h, "TP"
        h += HOUR_MS
    end_bar = bars.get(end)
    if end_bar:
        return end_bar[0], end, "TIME"
    return None, None, None


def run_config(data, side, top, stop_loss, take_profit, fee, capital):
    syms, prices, funding = data["syms"], data["prices"], data["funding"]
    rebal_times, hours = data["rebal_times"], data["hours"]
    hold_ms = hours * HOUR_MS

    def close_at(sym, t):
        b = prices[sym].get(t)
        return b[0] if b else None

    equity = float(capital)
    curve, cycles = [], []
    tot_fee = tot_fund = tot_price = 0.0
    cycle_rets = []
    reason_count = {"SL": 0, "TP": 0, "TIME": 0, "NA": 0}

    for t in rebal_times:
        ranked = []
        t24 = t - DAY_MS
        for sym in syms:
            p_now = close_at(sym, t)
            p_old = close_at(sym, t24)
            if p_now and p_old and p_old > 0:
                ranked.append((sym, p_now / p_old - 1.0, p_now))
        ranked.sort(key=lambda r: r[1], reverse=True)
        picks = ranked[:top]

        notional = equity / top if picks else 0.0
        cyc_price = cyc_fund = cyc_fee = 0.0
        legs = []
        for sym, ret24, entry in picks:
            if not entry or entry <= 0:
                continue
            if side == "short":
                sl_level = entry * (1 + stop_loss)
                tp_level = entry * (1 - take_profit)
            else:
                sl_level = entry * (1 - stop_loss)
                tp_level = entry * (1 + take_profit)
            exit_p, exit_t, reason = simulate_hold(prices[sym], t, entry, hold_ms,
                                                   sl_level, tp_level, side)
            if exit_p is None:
                reason_count["NA"] += 1
                continue
            reason_count[reason] += 1
            fsum = funding_between(funding[sym], t, exit_t)
            if side == "short":
                price_pnl = notional * (entry - exit_p) / entry
                fund = notional * fsum
            else:
                price_pnl = notional * (exit_p - entry) / entry
                fund = -notional * fsum
            f_open = fee * notional
            f_close = fee * notional * (exit_p / entry)
            leg_fee = f_open + f_close
            cyc_price += price_pnl
            cyc_fund += fund
            cyc_fee += leg_fee
            legs.append({"symbol": sym, "ret24_pct": round(ret24 * 100, 2),
                         "entry": entry, "exit": round(exit_p, 8), "reason": reason,
                         "hold_h": round((exit_t - t) / HOUR_MS, 1),
                         "pnl": round(price_pnl + fund - leg_fee, 2)})

        net = cyc_price + cyc_fund - cyc_fee
        eq_before = equity
        equity += net
        tot_price += cyc_price; tot_fund += cyc_fund; tot_fee += cyc_fee
        if eq_before:
            cycle_rets.append(net / eq_before)
        cycles.append({"time": t, "equity": round(equity, 2),
                       "pnl_price": round(cyc_price, 2), "pnl_funding": round(cyc_fund, 2),
                       "fee": round(cyc_fee, 2), "net": round(net, 2), "shorts": legs})
        curve.append({"time": t, "equity": round(equity, 2)})

    # 指标
    cap = float(capital)
    final_eq = equity
    total_ret = final_eq / cap - 1.0
    years = max(data["days"] / 365.0, 1e-9)
    cagr = (final_eq / cap) ** (1 / years) - 1.0 if final_eq > 0 else -1.0
    peak, max_dd = -1e18, 0.0
    for c in curve:
        peak = max(peak, c["equity"])
        if peak > 0:
            max_dd = min(max_dd, c["equity"] / peak - 1.0)
    import statistics
    if len(cycle_rets) > 1 and statistics.pstdev(cycle_rets) > 0:
        cpy = 365 * 24 / hours
        sharpe = (statistics.fmean(cycle_rets) / statistics.pstdev(cycle_rets)) * (cpy ** 0.5)
    else:
        sharpe = 0.0
    wins = sum(1 for c in cycles if c["net"] > 0)
    n_eff = sum(1 for c in cycles if c["net"] != 0)
    win_rate = wins / n_eff if n_eff else 0.0
    n_legs = sum(reason_count.values())
    side_cn = "做多" if side == "long" else "做空"

    return {
        "meta": {
            "id": run_id(side, stop_loss, take_profit),
            "label": f"{side_cn} · 止损{stop_loss:.0%}/止盈{take_profit:.0%}",
            "strategy": f"每{hours}h{side_cn}24h涨幅榜前{top} · 止损{stop_loss:.0%}/止盈{take_profit:.0%}",
            "side": side, "generated_at": int(time.time() * 1000),
            "days": data["days"], "rebalance_hours": hours, "top_n": top,
            "stop_loss": stop_loss, "take_profit": take_profit,
            "universe_requested": data["universe_requested"],
            "universe_effective": len(syms), "fee_rate": fee, "start_equity": cap,
            "period_start": rebal_times[0] if rebal_times else data["start_ms"],
            "period_end": rebal_times[-1] if rebal_times else data["end_ms"],
        },
        "summary": {
            "final_equity": round(final_eq, 2), "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2), "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2), "win_rate_pct": round(win_rate * 100, 1),
            "num_cycles": len(cycles), "total_fees": round(tot_fee, 2),
            "total_funding": round(tot_fund, 2), "total_price_pnl": round(tot_price, 2),
            "exits": reason_count,
            "exit_sl_pct": round(100 * reason_count["SL"] / n_legs, 1) if n_legs else 0,
            "exit_tp_pct": round(100 * reason_count["TP"] / n_legs, 1) if n_legs else 0,
            "exit_time_pct": round(100 * reason_count["TIME"] / n_legs, 1) if n_legs else 0,
        },
        "curve": curve, "cycles": cycles,
    }


def run_id(side, sl, tp):
    return f"{side}_sl{int(round(sl*100))}_tp{int(round(tp*100))}"


# ----------------------------- 写出 + manifest -----------------------------
def write_run(result):
    os.makedirs(RUNS_DIR, exist_ok=True)
    rid = result["meta"]["id"]
    with open(os.path.join(RUNS_DIR, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))


def update_manifest(results):
    os.makedirs(RUNS_DIR, exist_ok=True)
    path = os.path.join(RUNS_DIR, "manifest.json")
    by_id = {}
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
            for r in old.get("runs", []):
                by_id[r["id"]] = r
        except Exception:
            pass
    for res in results:
        m, s = res["meta"], res["summary"]
        by_id[m["id"]] = {
            "id": m["id"], "label": m["label"], "side": m["side"],
            "strategy": m["strategy"], "stop_loss": m["stop_loss"],
            "take_profit": m["take_profit"], "rebalance_hours": m["rebalance_hours"],
            "top_n": m["top_n"], "days": m["days"],
            "universe_effective": m["universe_effective"],
            "generated_at": m["generated_at"], "start_equity": m["start_equity"],
            "period_start": m["period_start"], "period_end": m["period_end"],
            "final_equity": s["final_equity"], "total_return_pct": s["total_return_pct"],
            "cagr_pct": s["cagr_pct"], "max_drawdown_pct": s["max_drawdown_pct"],
            "sharpe": s["sharpe"], "win_rate_pct": s["win_rate_pct"],
            "num_cycles": s["num_cycles"], "total_fees": s["total_fees"],
            "total_funding": s["total_funding"],
            "exit_sl_pct": s["exit_sl_pct"], "exit_tp_pct": s["exit_tp_pct"],
            "exit_time_pct": s["exit_time_pct"],
        }
    runs = sorted(by_id.values(), key=lambda r: r["total_return_pct"], reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": int(time.time() * 1000), "runs": runs},
                  f, ensure_ascii=False, separators=(",", ":"))
    return runs


def main(argv=None):
    p = argparse.ArgumentParser(description="涨幅榜多空回测 (止损/止盈, 多 run)")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--universe", type=int, default=120)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--side", choices=["short", "long"], default="short")
    p.add_argument("--hours", type=int, default=6)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--take-profit", type=float, default=0.50)
    p.add_argument("--fee", type=float, default=0.0005)
    p.add_argument("--capital", type=float, default=10000)
    p.add_argument("--batch", action="store_true", help="跑预设矩阵 (一次抓数多配置)")
    args = p.parse_args(argv)

    data = load_data(args.days, args.universe, args.hours)
    configs = PRESETS if args.batch else [(args.side, args.stop_loss, args.take_profit)]

    results = []
    for side, sl, tp in configs:
        print(f"[模拟] {side} 止损{sl:.0%}/止盈{tp:.0%}...", file=sys.stderr)
        res = run_config(data, side, args.top, sl, tp, args.fee, args.capital)
        write_run(res)
        results.append(res)
        s = res["summary"]
        print(f"        → 净值 {s['final_equity']} ({s['total_return_pct']:+}%) "
              f"DD {s['max_drawdown_pct']}% 夏普 {s['sharpe']} 胜率 {s['win_rate_pct']}% "
              f"[SL{s['exit_sl_pct']}/TP{s['exit_tp_pct']}/TIME{s['exit_time_pct']}]", file=sys.stderr)

    runs = update_manifest(results)
    with open(os.path.join(HERE, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results[-1], f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n[完成] {len(results)} 个 run 已写出, manifest 共 {len(runs)} 个 run", file=sys.stderr)
    print(f"        {RUNS_DIR}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
