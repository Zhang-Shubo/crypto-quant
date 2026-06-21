"""集中式配置: 路径常量、交易所 endpoint、运行期可调参数 (环境变量)。

所有模块从这里取路径与默认值, 避免各文件各自 os.path.dirname(__file__) 拼接、
以及环境变量散落。修改默认行为优先改这里。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ----------------------------- 路径 -----------------------------
ROOT = Path(__file__).resolve().parent.parent          # 仓库根目录
FRONTEND_DIR = ROOT / "frontend"                       # 静态前端
RUNS_DIR = FRONTEND_DIR / "runs"                       # 回测产物 (<id>.json + manifest.json)
DATA_DIR = ROOT / "data"                               # 运行期数据 (live.db, 末次回测)
DB_PATH = DATA_DIR / "live.db"                          # 实时监控采样库
LAST_RUN_PATH = DATA_DIR / "last_run.json"              # 末次回测结果 (调试/兼容)

# ----------------------------- Endpoint -----------------------------
SPOT_BASE = "https://api.binance.com"                  # 现货公共行情
FAPI_BASE = "https://fapi.binance.com"                 # USDⓈ-M 永续公共/实盘
TESTNET_BASE = "https://testnet.binancefuture.com"     # 合约 testnet

USER_AGENT = "crypto-quant/0.2"

# 杠杆代币后缀: 排除以免污染涨幅榜 (单点定义, 各处引用)
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# 时间常量 (毫秒)
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class MonitorConfig:
    """实时监控/Web 后端配置, 从环境变量读取 (见 docs/web-api.md)。"""
    port: int = 8800
    side: str = "long"            # long / short
    capital: float = 100.0        # 监控用名义本金 (USDT)
    stop_loss: float = 0.10
    take_profit: float = 0.50
    top: int = 5
    min_quote_volume: float = 1_000_000.0
    universe: int = 150           # 1h 榜刷新的标的池大小
    sample_sec: int = 60          # 浮盈采样周期
    quality_pool: bool = False    # 质量池筛选: 上市≥N月 + 成交额>X
    quality_age_months: int = 6   # 质量池: 上市时长门槛(月)
    quality_vol: float = 10_000_000.0   # 质量池: 24h成交额门槛

    @classmethod
    def from_env(cls, env=os.environ) -> "MonitorConfig":
        g = env.get
        return cls(
            port=int(g("APP_PORT", "8800")),
            side=g("MON_SIDE", "long"),
            capital=float(g("MON_CAPITAL", "100")),
            stop_loss=float(g("MON_SL", "0.10")),
            take_profit=float(g("MON_TP", "0.50")),
            top=int(g("MON_TOP", "5")),
            min_quote_volume=float(g("MON_MIN_QVOL", "1000000")),
            universe=int(g("MON_UNIVERSE", "150")),
            sample_sec=int(g("SAMPLE_SEC", "60")),
            quality_pool=g("MON_QUALITY", "0") in ("1", "true", "True", "yes"),
            quality_age_months=int(g("MON_QUALITY_AGE_MONTHS", "6")),
            quality_vol=float(g("MON_QUALITY_VOL", "10000000")),
        )
