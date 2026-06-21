"""BTC 单标的经典量化策略库 —— 10 个策略 + 单资产回测引擎。

与主仓库的「涨幅榜多空」策略族独立: 这里聚焦 **单一 BTC** 上的经典信号
(趋势 / 均值回归 / 突破 / 波动率), 用日线做长周期回测, 多空仅做多(long/flat),
基准 = 买入持有。数据走 data-api.binance.vision (现货, 免地域封锁), 仅依赖标准库。

入口: python3 -m cryptoquant.btc  (抓数→跑 10 策略→写 Obsidian 文档 + JSON)
"""
