"""实盘执行器 CLI: python3 -m cryptoquant.live [args]

    # 服务器先 source 环境变量 (见 .env.example)
    python3 -m cryptoquant.live                          # testnet + dry-run
    python3 -m cryptoquant.live --execute                # testnet 真下单(测试币)
    python3 -m cryptoquant.live --side long --sl 0.2 --tp 0.5 --execute
    python3 -m cryptoquant.live --live --execute         # ⚠️ 实盘真金
"""
from __future__ import annotations

import argparse

from .executor import run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cryptoquant.live", description="涨幅榜实盘调仓执行器")
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

    return run(side=args.side, top=args.top, sl=args.sl, tp=args.tp,
               leverage=args.leverage, capital=args.capital, fraction=args.fraction,
               min_qvol=args.min_qvol, live=args.live, execute=args.execute,
               signal=args.signal)


if __name__ == "__main__":
    raise SystemExit(main())
