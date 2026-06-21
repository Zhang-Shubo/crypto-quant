"""crypto-quant —— 加密货币量化研究工具集 (纯标准库)。

三大模块, 共享同一套交易所 I/O 与策略逻辑:
  · 市场看板  cryptoquant.web        实时涨幅榜 + K 线
  · 策略回测  cryptoquant.backtest   涨幅榜多空 + 止损/止盈, 含资金费/手续费
  · 实时运行  cryptoquant.live       单次实盘调仓执行器 + dry-run 监控采样

数据来源: Binance 公共行情 / USDⓈ-M 永续合约。
"""

__version__ = "0.2.0"
