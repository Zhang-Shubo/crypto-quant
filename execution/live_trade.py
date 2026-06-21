#!/usr/bin/env python3
"""做空/做多涨幅榜 · 单次实盘调仓执行器 (Binance USDⓈ-M 合约)。

把回测策略的"一次调仓"落到真实下单: 选 24h 涨幅榜前 N → 按方向市价开仓
→ 挂 reduceOnly 止损 + 止盈 (整仓平)。配合 cron 每 6h 跑一次即成机器人。

安全分层 (默认最安全):
1. 不带 --execute        → dry-run, 只打印将要下的单, 不调用任何下单接口。
2. 不带 --live           → 走 testnet (testnet.binancefuture.com), 测试币, 零真金风险。
3. 真金实盘 = 同时加 --live --execute (会二次确认打印警告)。

用法:
    # 服务器先 source 环境变量 (见 execution/.env.example)
    python3 execution/live_trade.py                          # testnet + dry-run
    python3 execution/live_trade.py --execute                # testnet 真下单(测试币)
    python3 execution/live_trade.py --side long --sl 0.2 --tp 0.5 --execute
    python3 execution/live_trade.py --live --execute         # ⚠️ 实盘真金

⚠️ 策略未经样本外验证, 实盘风险自负; 非投资建议。
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from binance_client import BinanceFutures

LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def round_down(x: float, prec: int) -> float:
    f = 10 ** prec
    return math.floor(x * f) / f


def pick_gainers(ticker_client: BinanceFutures, top: int, prec: dict) -> list[dict]:
    """从 ticker_client 的 24h 行情选 USDT 永续涨幅榜前 N。
    prec 来自【执行所】交易规则: 排榜来源可与执行所不同(如实盘排榜/testnet执行),
    sym not in prec 会自动跳过执行所不存在的合约。"""
    rows = []
    for t in ticker_client.ticker_24hr():
        sym = t["symbol"]
        if not sym.endswith("USDT") or sym.endswith(LEVERAGED) or sym not in prec:
            continue
        try:
            rows.append({"symbol": sym,
                         "chg": float(t["priceChangePercent"]),
                         "price": float(t["lastPrice"]),
                         "qvol": float(t.get("quoteVolume", 0))})
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda r: r["chg"], reverse=True)
    return rows[:top]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="涨幅榜实盘调仓执行器")
    p.add_argument("--side", choices=["short", "long"], default="short")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--sl", type=float, default=0.10, help="止损 (仓位亏损比例)")
    p.add_argument("--tp", type=float, default=0.50, help="止盈 (仓位盈利比例)")
    p.add_argument("--leverage", type=int, default=1)
    p.add_argument("--capital", type=float, default=None,
                   help="部署总额 USDT; 不填则读账户 availableBalance")
    p.add_argument("--fraction", type=float, default=1.0, help="动用资金比例 (默认1.0=满仓)")
    p.add_argument("--min-qvol", type=float, default=1_000_000, help="标的最小24h成交额过滤")
    p.add_argument("--live", action="store_true", help="⚠️ 用实盘 (默认 testnet)")
    p.add_argument("--execute", action="store_true", help="真正下单 (默认 dry-run 只打印)")
    p.add_argument("--signal", choices=["venue", "live"], default="venue",
                   help="涨幅榜数据来源: venue=执行所自身(testnet数据是模拟的) / live=实盘数据 (默认venue)")
    args = p.parse_args(argv)

    testnet = not args.live
    client = BinanceFutures(testnet=testnet)
    # 排榜来源: live 用实盘行情、testnet 执行(真实信号); 否则用执行所自身行情
    signal_client = BinanceFutures(testnet=False) if args.signal == "live" else client
    env = "TESTNET (测试币)" if testnet else "★ 实盘 LIVE (真金) ★"
    close_side = "BUY" if args.side == "short" else "SELL"   # 平仓方向
    open_side = "SELL" if args.side == "short" else "BUY"
    side_cn = "做空" if args.side == "short" else "做多"

    sig_src = "实盘行情(真实信号)" if args.signal == "live" else ("testnet模拟数据" if testnet else "实盘行情")
    print(f"环境: {env}   模式: {'下单' if args.execute else 'DRY-RUN(只打印)'}")
    print(f"策略: 每轮{side_cn} 24h 涨幅榜前 {args.top} · 止损{args.sl:.0%} 止盈{args.tp:.0%} · {args.leverage}x")
    print(f"排榜来源: {sig_src}")

    # 连通性
    try:
        client.ping()
    except Exception as e:
        print(f"[错误] 无法连接 Binance ({'testnet' if testnet else 'live'}): {e}", file=sys.stderr)
        return 1

    prec = client.precision_map()

    # 资金
    equity = args.capital
    if equity is None:
        if not client.key:
            print("[错误] 未设 --capital 且无 API Key 读不到余额; dry-run 请加 --capital 1000", file=sys.stderr)
            return 1
        equity = client.usdt_balance()
    deploy = equity * args.fraction
    notional_each = deploy / args.top
    print(f"资金: 总额 {equity:.2f} → 动用 {deploy:.2f} (×{args.fraction}) → 每仓 {notional_each:.2f} USDT\n")

    picks = [g for g in pick_gainers(signal_client, args.top, prec) if g["qvol"] >= args.min_qvol]
    if not picks:
        print("[提示] 无符合条件标的"); return 0
    if args.signal == "live" and testnet:
        print("(已用实盘涨幅榜选标的, 自动跳过 testnet 不存在的合约)\n")

    if args.live and args.execute:
        print("⚠️⚠️  即将用【实盘真金】下单。Ctrl-C 可中止。\n")

    placed = 0
    for g in picks:
        sym, price = g["symbol"], g["price"]
        pp = prec[sym]
        qty = round_down(notional_each / price, pp["qty_prec"])
        notional = qty * price
        if args.side == "short":
            sl_price = round_down(price * (1 + args.sl), pp["price_prec"])
            tp_price = round_down(price * (1 - args.tp), pp["price_prec"])
        else:
            sl_price = round_down(price * (1 - args.sl), pp["price_prec"])
            tp_price = round_down(price * (1 + args.tp), pp["price_prec"])

        tag = f"{sym:<13} 24h+{g['chg']:.1f}%  价{price:<12g} 量{qty:<10g} ≈{notional:.1f}U  止损@{sl_price:g} 止盈@{tp_price:g}"
        if qty <= 0 or notional < pp["min_notional"]:
            print(f"  跳过 {tag}  (名义 {notional:.1f} < 最小 {pp['min_notional']})")
            continue
        print(f"  {open_side:<4} {tag}")

        if args.execute:
            try:
                client.set_leverage(sym, args.leverage)
                o = client.market_entry(sym, open_side, qty)
                client.stop_market_close(sym, close_side, sl_price)
                client.take_profit_close(sym, close_side, tp_price)
                print(f"       ✅ 成交 orderId={o.get('orderId')} + 止损/止盈已挂")
                placed += 1
            except Exception as e:
                print(f"       ❌ 失败: {e}")

    print(f"\n{'已下单 ' + str(placed) + ' 笔' if args.execute else 'dry-run 结束 (未下单)'}。")
    if not args.execute:
        print("→ 确认无误后加 --execute 在 testnet 实测; 真金实盘再加 --live。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
