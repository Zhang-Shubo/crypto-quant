"""回测模块: 涨幅榜多空 + 止损/止盈引擎。

库用法:
    from cryptoquant.backtest import load_data, run_config
CLI:
    python3 -m cryptoquant.backtest --batch
"""
from .engine import (
    load_data,
    dynamic_universe,
    run_config,
    run_id,
    write_run,
    update_manifest,
    PRESETS,
)

__all__ = [
    "load_data", "dynamic_universe", "run_config", "run_id",
    "write_run", "update_manifest", "PRESETS",
]
