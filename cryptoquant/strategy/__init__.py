"""策略层: 选标的 + 止损/止盈价位等纯逻辑 (无 I/O, 易测试)。"""
from .gainers import (
    round_down,
    pick_gainers,
    stop_take_levels,
    is_leveraged,
)

__all__ = ["round_down", "pick_gainers", "stop_take_levels", "is_leveraged"]
