"""涨幅榜多空策略回测引擎 (Gainers Momentum / Reversion) + 止损/止盈。

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

时点动态池 (默认)
-----------------
每个调仓点按"截至当时的滚动 24h 成交额"取前 N, 消除"用今天成交额选过去标的"
的前视偏差。--fixed-universe 可退回固定池 (当下成交额前 N, 含前视偏差)。

产出 (写入 frontend/runs/ + data/)
----------------------------------
    frontend/runs/<id>.json     单个 run 完整结果 (供 run.html?id=<id>)
    frontend/runs/manifest.json 所有 run 的汇总索引 (供 backtest.html)
    data/last_run.json          最近一个 run (调试/兼容)
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

from ..config import HOUR_MS, DAY_MS, RUNS_DIR, LAST_RUN_PATH
from ..exchange import (select_universe, onboard_dates, fetch_klines,
                        fetch_funding, funding_between)

# 预设矩阵: (side, stop_loss, take_profit)
PRESETS = [
    ("short", 0.10, 0.20),
    ("short", 0.10, 0.50),
    ("long", 0.20, 0.50),
    ("long", 0.10, 0.30),
]


# ----------------------------- 数据抓取 -----------------------------
def load_data(days, candidates, hours, warmup_days=0):
    """抓一次候选集数据 (成交额前 candidates), 供多个配置共用。

    warmup_days: 额外多抓的预热历史 (质量池的 7日中位数成交额需要预热);
    交易窗口(rebal_times)仍只覆盖最近 days 天, warmup_days=0 时与旧行为完全一致。
    """
    end_ms = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    fetch_start = end_ms - (days + warmup_days) * DAY_MS
    trade_start = end_ms - days * DAY_MS
    hold_ms = hours * HOUR_MS
    print(f"[数据] 候选集(成交额前 {candidates})...", file=sys.stderr)
    uni = select_universe(candidates)   # 按当前成交额降序
    print(f"[数据] 抓 {days + warmup_days}天 1h OHLCV + 资金费率 ({len(uni)} 标的)...", file=sys.stderr)
    prices, funding = {}, {}
    for i, sym in enumerate(uni, 1):
        try:
            kl = fetch_klines(sym, fetch_start, end_ms)
            if len(kl) < 30:
                continue
            prices[sym] = kl
            funding[sym] = fetch_funding(sym, fetch_start, end_ms)
        except Exception as e:
            print(f"      ! {sym} 跳过: {e}", file=sys.stderr)
        if i % 50 == 0:
            print(f"      {i}/{len(uni)}", file=sys.stderr)
        time.sleep(0.04)
    syms = [s for s in uni if s in prices]   # 候选(成交额降序)
    rebal_times = list(range(trade_start + DAY_MS, end_ms - hold_ms + 1, hold_ms))
    onboard = onboard_dates()
    print(f"[数据] 候选有效 {len(syms)}, 调仓周期 {len(rebal_times)}", file=sys.stderr)
    return {"syms": syms, "prices": prices, "funding": funding, "onboard": onboard,
            "rebal_times": rebal_times, "start_ms": fetch_start, "end_ms": end_ms,
            "days": days, "candidates_requested": candidates, "hours": hours}


def dynamic_universe(data, nsel):
    """时点动态池: 每个调仓点按"截至当时的滚动 24h 成交额"取前 nsel。
    返回 {t: [symbol,...]}。消除"用今天成交额选过去标的"的前视偏差。"""
    prices, rebal = data["prices"], data["rebal_times"]
    out = {}
    for t in rebal:
        vs = []
        for s, bars in prices.items():
            tv = 0.0
            for k in range(24):                      # 最近 24 根 1h K线的成交额
                x = bars.get(t - k * HOUR_MS)
                if x:
                    tv += x[3]
            if tv > 0 and bars.get(t):
                vs.append((s, tv))
        vs.sort(key=lambda r: r[1], reverse=True)
        out[t] = [s for s, _ in vs[:nsel]]
    return out


def quality_universe(data, vol_floor=1e7, age_days=180, med_days=7):
    """质量池: 每个调仓点选满足
        ① 上市≥age_days天 (onboardDate, 点时正确)
        ② 近7日「24h成交额」中位数 > vol_floor (抗单日拉盘)
      的全部标的, 每日刷新。返回 {t:[symbols]}。

    甜区参数(扫描所得): vol_floor=1000万, age_days=180(6月)。剔除新币 pump-and-dump,
    避免在抛物线顶部接盘; 但过滤过头(≥12月 或 >5000万)会把趋势源头也滤掉。
    需 load_data(..., warmup_days≥8) 提供 7 日中位数所需预热。
    """
    prices, rebal = data["prices"], data["rebal_times"]
    onboard = data.get("onboard", {})

    def daily_vol(bars, t):
        v = 0.0
        for k in range(24):
            x = bars.get(t - k * HOUR_MS)
            if x:
                v += x[3]
        return v

    day_cache, out = {}, {}
    for t in rebal:
        d = (t // DAY_MS) * DAY_MS
        if d not in day_cache:
            sel = []
            for s, bars in prices.items():
                ob = onboard.get(s, 0)
                if ob == 0 or (d - ob) < age_days * DAY_MS:        # 上市不足
                    continue
                dv = [daily_vol(bars, d - j * DAY_MS) for j in range(med_days)]
                dv = [x for x in dv if x > 0]
                if len(dv) >= med_days and statistics.median(dv) > vol_floor:
                    sel.append(s)
            day_cache[d] = sel
        out[t] = [s for s in day_cache[d] if prices[s].get(t)]
    return out


# ----------------------------- 离场 + 模拟 -----------------------------
def simulate_hold(bars, t, entry, hold_ms, sl_level, tp_level, side):
    h = t + HOUR_MS
    end = t + hold_ms
    while h <= end:
        bar = bars.get(h)
        if bar:
            _, hi, lo, _ = bar
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


def run_config(data, side, top, stop_loss, take_profit, fee, capital,
               nsel=120, dyn_univ=None, mode=None):
    """dyn_univ: 选池 {t:[symbols]}; None=固定池(候选前 nsel)。
    mode: 'dynamic'|'fixed'|'quality' (None 时从 dyn_univ 推断), 影响 id/标签/meta。"""
    syms, prices, funding = data["syms"], data["prices"], data["funding"]
    rebal_times, hours = data["rebal_times"], data["hours"]
    hold_ms = hours * HOUR_MS
    fixed_univ = syms[:nsel]            # 固定池 = 候选(当前成交额)前 nsel
    is_dynamic = dyn_univ is not None

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
        universe = dyn_univ[t] if is_dynamic else fixed_univ
        for sym in universe:
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
    if mode is None:
        mode = "dynamic" if is_dynamic else "fixed"
    umode = {"dynamic": "动态池", "fixed": "固定池", "quality": "质量池"}.get(mode, "动态池")
    suffix = "_q" if mode == "quality" else ""   # 质量池单独 id 共存对比; 动态/固定共用基础 id
    if is_dynamic:
        sizes = [len(v) for v in dyn_univ.values()]
        uni_eff = round(sum(sizes) / len(sizes)) if sizes else nsel
    else:
        uni_eff = nsel

    return {
        "meta": {
            "id": run_id(side, stop_loss, take_profit) + suffix,
            "label": f"{side_cn} · 止损{stop_loss:.0%}/止盈{take_profit:.0%} · {umode}",
            "strategy": f"每{hours}h{side_cn}24h涨幅榜前{top} · 止损{stop_loss:.0%}/止盈{take_profit:.0%} · {umode}",
            "side": side, "generated_at": int(time.time() * 1000),
            "days": data["days"], "rebalance_hours": hours, "top_n": top,
            "stop_loss": stop_loss, "take_profit": take_profit,
            "universe_mode": mode,
            "candidates_effective": len(syms),
            "universe_requested": nsel,
            "universe_effective": uni_eff, "fee_rate": fee, "start_equity": cap,
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
            "universe_mode": m.get("universe_mode", "fixed"),
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


def write_last_run(result):
    """末次 run 落 data/last_run.json (gitignored, 调试/兼容用)。"""
    os.makedirs(os.path.dirname(LAST_RUN_PATH), exist_ok=True)
    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
