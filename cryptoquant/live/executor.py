"""做空/做多涨幅榜 · 单次实盘调仓执行器 (Binance USDⓈ-M 合约)。

把回测策略的"一次调仓"落到真实下单: 选 24h 涨幅榜前 N → 按方向市价开仓
→ 挂 reduceOnly 止损 + 止盈 (整仓平)。配合 cron 每 6h 跑一次即成机器人。

安全分层 (默认最安全):
1. 不带 --execute        → dry-run, 只打印将要下的单, 不调用任何下单接口。
2. 不带 --live           → 走 testnet (testnet.binancefuture.com), 测试币, 零真金风险。
3. 真金实盘 = 同时加 --live --execute (会二次确认打印警告)。

⚠️ 策略未经样本外验证, 实盘风险自负; 非投资建议。
"""
from __future__ import annotations

import sys

from ..exchange import BinanceFutures
from ..strategy import pick_gainers, round_down, stop_take_levels


def run(side="short", top=5, sl=0.10, tp=0.50, leverage=1, capital=None,
        fraction=1.0, min_qvol=1_000_000, live=False, execute=False,
        signal="venue") -> int:
    """执行一次调仓 (或 dry-run)。参数语义见 CLI --help。返回退出码。"""
    testnet = not live
    client = BinanceFutures(testnet=testnet)
    # 排榜来源: live 用实盘行情(真实信号), 否则用执行所自身行情
    signal_client = BinanceFutures(testnet=False) if signal == "live" else client
    env = "TESTNET (测试币)" if testnet else "★ 实盘 LIVE (真金) ★"
    close_side = "BUY" if side == "short" else "SELL"   # 平仓方向
    open_side = "SELL" if side == "short" else "BUY"
    side_cn = "做空" if side == "short" else "做多"

    sig_src = "实盘行情(真实信号)" if signal == "live" else ("testnet模拟数据" if testnet else "实盘行情")
    print(f"环境: {env}   模式: {'下单' if execute else 'DRY-RUN(只打印)'}")
    print(f"策略: 每轮{side_cn} 24h 涨幅榜前 {top} · 止损{sl:.0%} 止盈{tp:.0%} · {leverage}x")
    print(f"排榜来源: {sig_src}")

    # 连通性
    try:
        client.ping()
    except Exception as e:
        print(f"[错误] 无法连接 Binance ({'testnet' if testnet else 'live'}): {e}", file=sys.stderr)
        return 1

    prec = client.precision_map()

    # 资金
    equity = capital
    if equity is None:
        if not client.key:
            print("[错误] 未设 --capital 且无 API Key 读不到余额; dry-run 请加 --capital 1000", file=sys.stderr)
            return 1
        equity = client.usdt_balance()
    deploy = equity * fraction
    notional_each = deploy / top
    print(f"资金: 总额 {equity:.2f} → 动用 {deploy:.2f} (×{fraction}) → 每仓 {notional_each:.2f} USDT\n")

    picks = pick_gainers(signal_client.ticker_24hr(), top, allowed=prec, min_quote_volume=min_qvol)
    if not picks:
        print("[提示] 无符合条件标的")
        return 0
    if signal == "live" and testnet:
        print("(已用实盘涨幅榜选标的, 自动跳过 testnet 不存在的合约)\n")

    if live and execute:
        print("⚠️⚠️  即将用【实盘真金】下单。Ctrl-C 可中止。\n")

    placed = 0
    for g in picks:
        sym, price = g["symbol"], g["price"]
        pp = prec[sym]
        qty = round_down(notional_each / price, pp["qty_prec"])
        notional = qty * price
        sl_raw, tp_raw = stop_take_levels(side, price, sl, tp)
        sl_price = round_down(sl_raw, pp["price_prec"])
        tp_price = round_down(tp_raw, pp["price_prec"])

        tag = (f"{sym:<13} 24h+{g['change_pct']:.1f}%  价{price:<12g} 量{qty:<10g} "
               f"≈{notional:.1f}U  止损@{sl_price:g} 止盈@{tp_price:g}")
        if qty <= 0 or notional < pp["min_notional"]:
            print(f"  跳过 {tag}  (名义 {notional:.1f} < 最小 {pp['min_notional']})")
            continue
        print(f"  {open_side:<4} {tag}")

        if execute:
            try:
                client.set_leverage(sym, leverage)
                o = client.market_entry(sym, open_side, qty)
                client.stop_market_close(sym, close_side, sl_price)
                client.take_profit_close(sym, close_side, tp_price)
                print(f"       ✅ 成交 orderId={o.get('orderId')} + 止损/止盈已挂")
                placed += 1
            except Exception as e:
                print(f"       ❌ 失败: {e}")

    print(f"\n{'已下单 ' + str(placed) + ' 笔' if execute else 'dry-run 结束 (未下单)'}。")
    if not execute:
        print("→ 确认无误后加 --execute 在 testnet 实测; 真金实盘再加 --live。")
    return 0
