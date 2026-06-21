"""实时模块: 实盘单次调仓执行器 + dry-run 监控采样。

CLI (实盘执行器):
    python3 -m cryptoquant.live --side long --execute
监控 (冻结快照 + 周期采样浮盈) 由 cryptoquant.web 后端驱动, 见 monitor.py。
"""
from .monitor import Monitor

__all__ = ["Monitor"]
