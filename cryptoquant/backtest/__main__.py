"""回测 CLI: python3 -m cryptoquant.backtest [args]

    python3 -m cryptoquant.backtest --side long --stop-loss 0.2 --take-profit 0.5
    python3 -m cryptoquant.backtest --batch           # 一次抓数, 跑预设矩阵
    python3 -m cryptoquant.backtest --fixed-universe   # 退回固定池(含前视偏差)
    python3 -m cryptoquant.backtest --quality-pool     # 质量池(上市≥6月 + 中位成交额>1000万)

选池模式(三选一): 默认时点动态池 / --fixed-universe 固定池 / --quality-pool 质量池。
仅依赖标准库 (需直连 fapi.binance.com)。
"""
from __future__ import annotations

import argparse
import sys

from .engine import (
    PRESETS, load_data, dynamic_universe, quality_universe, run_config,
    write_run, update_manifest, write_last_run,
)
from ..config import RUNS_DIR


def main(argv=None):
    p = argparse.ArgumentParser(prog="cryptoquant.backtest",
                                description="涨幅榜多空回测 (止损/止盈, 多 run)")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--universe", type=int, default=120, help="实际交易的池子大小 (选前N)")
    p.add_argument("--candidates", type=int, default=400, help="抓取的候选集 (动态池从中按时点成交额选)")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--side", choices=["short", "long"], default="short")
    p.add_argument("--hours", type=int, default=6)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--take-profit", type=float, default=0.50)
    p.add_argument("--fee", type=float, default=0.0005)
    p.add_argument("--capital", type=float, default=10000)
    p.add_argument("--batch", action="store_true", help="跑预设矩阵 (一次抓数多配置)")
    p.add_argument("--fixed-universe", action="store_true",
                   help="用固定池(当下成交额前N, 含前视偏差); 默认时点动态池")
    p.add_argument("--quality-pool", action="store_true",
                   help="质量池: 上市≥N月 + 7日中位数24h成交额>X, 每日刷新 (甜区参数)")
    p.add_argument("--quality-vol", type=float, default=1e7,
                   help="质量池成交额门槛 (默认 1e7=1000万)")
    p.add_argument("--quality-age-months", type=int, default=6,
                   help="质量池上市时长门槛/月 (默认 6)")
    args = p.parse_args(argv)

    # 选池模式: quality > fixed > dynamic(默认)
    if args.quality_pool:
        cand = max(args.candidates, args.universe)
        data = load_data(args.days, cand, args.hours, warmup_days=8)   # 7日中位数预热
        dyn = quality_universe(data, args.quality_vol, args.quality_age_months * 30)
        mode = "quality"
        avg = round(sum(len(v) for v in dyn.values()) / max(len(dyn), 1))
        print(f"[池] 质量池: 上市≥{args.quality_age_months}月 + 7日中位成交额>{args.quality_vol/1e4:.0f}万 "
              f"→ 平均池 {avg} (候选{len(data['syms'])})", file=sys.stderr)
    elif args.fixed_universe:
        data = load_data(args.days, args.universe, args.hours)
        dyn, mode = None, "fixed"
        print(f"[池] 固定池(含前视偏差): 当下成交额前{args.universe}", file=sys.stderr)
    else:
        cand = max(args.candidates, args.universe)
        data = load_data(args.days, cand, args.hours)
        dyn, mode = dynamic_universe(data, args.universe), "dynamic"
        print(f"[池] 时点动态池: 候选{len(data['syms'])} → 每期按滚动24h成交额选前{args.universe}", file=sys.stderr)
    configs = PRESETS if args.batch else [(args.side, args.stop_loss, args.take_profit)]

    results = []
    for side, sl, tp in configs:
        print(f"[模拟] {side} 止损{sl:.0%}/止盈{tp:.0%}...", file=sys.stderr)
        res = run_config(data, side, args.top, sl, tp, args.fee, args.capital,
                         nsel=args.universe, dyn_univ=dyn, mode=mode)
        write_run(res)
        results.append(res)
        s = res["summary"]
        print(f"        → 净值 {s['final_equity']} ({s['total_return_pct']:+}%) "
              f"DD {s['max_drawdown_pct']}% 夏普 {s['sharpe']} 胜率 {s['win_rate_pct']}% "
              f"[SL{s['exit_sl_pct']}/TP{s['exit_tp_pct']}/TIME{s['exit_time_pct']}]", file=sys.stderr)

    runs = update_manifest(results)
    write_last_run(results[-1])

    print(f"\n[完成] {len(results)} 个 run 已写出, manifest 共 {len(runs)} 个 run", file=sys.stderr)
    print(f"        {RUNS_DIR}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
