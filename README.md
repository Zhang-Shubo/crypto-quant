# crypto-quant

加密货币量化研究项目。数据取自 Binance 公共接口（行情 / 永续合约 / 资金费率），抓取在能直连 Binance 的首尔服务器上跑。

## 模块

```
crypto-quant/
├── data/
│   └── binance_gainers.py     # Binance 24h 涨幅/跌幅榜 (现货, 标准库)
├── backtest/
│   ├── short_gainers.py       # 「做空涨幅榜」策略回测引擎 (含资金费+手续费)
│   └── results.json           # 最近一次回测结果
├── frontend/
│   ├── index.html             # 回测可视化看板 (净值/盈亏拆解/交易回放)
│   └── data.js                # 回测结果 (window.RESULTS, 供 file:// 直接打开)
├── strategies/ execution/ utils/ tests/
└── requirements.txt
```

## 1. 涨幅榜接口 `data/binance_gainers.py`

调 `/api/v3/ticker/24hr`，过滤 USDT 现货（排除杠杆代币），按 24h 涨跌幅排序。

```bash
python3 data/binance_gainers.py            # 涨幅榜 Top 20
python3 data/binance_gainers.py --top 5    # Top 5
python3 data/binance_gainers.py --losers   # 跌幅榜
python3 data/binance_gainers.py --json     # JSON 输出
```

## 2. 做空涨幅榜回测 `backtest/short_gainers.py`

**策略**：每 6 小时，按「过去 24h 涨幅」排序，等权做空涨幅榜前 5（USDT 永续），持有 6h 后换仓。
**成本建模**：taker 手续费按换手名义收取；资金费每 8h 结算，费率为正时空头收取、为负时空头支付。

```bash
python3 backtest/short_gainers.py                          # 默认: 45天, 标的池120, top5, fee 0.05%
python3 backtest/short_gainers.py --days 45 --universe 120 --top 5 --hours 6 --fee 0.0005
```

输出 `backtest/results.json` 与 `frontend/data.js`（含净值曲线、逐周期持仓与盈亏归因、汇总指标）。

> ⚠️ 仅供研究演示，非投资建议。未计滑点 / 借币费 / 爆仓；等权 1x 无杠杆。
> 实测结论：naive「追空涨幅榜」在加密市场是**负 edge**——做空暴涨币等于逆动量，强趋势下被持续碾压。

## 3. 可视化 `frontend/index.html`

直接用浏览器打开（`data.js` 经 `<script>` 内联，无需起服务器）：

```bash
open frontend/index.html      # macOS
```

含：核心指标卡、净值曲线、盈亏拆解（价格/资金费/手续费累计）、以及可**播放/拖动逐周期**的交易过程回放。

## 部署（首尔服务器，git pull）

数据抓取需直连 Binance，跑在首尔腾讯云。服务器目录 `~/crypto-quant` git 跟踪 `origin/main`，用**只读 deploy key** 认证：

```bash
# 本地改完代码: git add -A && git commit -m "..." && git push   (github-personal → Zhang-Shubo)
ssh -i ~/Documents/tecent_seoul.pem ubuntu@43.164.191.143
cd ~/crypto-quant && git pull --ff-only
python3 backtest/short_gainers.py        # 重新抓数+回测, 刷新 results.json / data.js
```

详见服务器笔记《首尔腾讯云服务器》的「已部署:crypto-quant」一节。

## 环境

脚本仅依赖 Python 标准库，无需安装第三方包（`requirements.txt` 为后续扩展预留）。
