# 回测引擎说明

模块：`cryptoquant.backtest`（引擎 `engine.py` + CLI `__main__.py`）

## 策略定义

「涨幅榜动量/逆动量 + 止损/止盈」：

- **标的池**：Binance USDT 永续合约中 24h 成交额最高的 N 个。
- **调仓**：每 `--hours`（默认 6）小时，按「过去 24h 涨幅」排序取前 `--top`（默认 5），
  等权建仓（各占净值名义的 1/top）。
- **方向**：`--side short` 做空涨幅榜（逆动量）/ `--side long` 做多涨幅榜（顺动量）。
- **每仓独立离场**：
  - short：价格涨 `--stop-loss` 止损 / 跌 `--take-profit` 止盈 / 否则到点平仓。
  - long：价格跌 `--stop-loss` 止损 / 涨 `--take-profit` 止盈 / 否则到点平仓。
  - 触发用逐根 1h K 线的最高/最低价判定；同一根内若止损止盈都触发，**保守按止损先成交**。
- **成本建模**：
  - taker 手续费 `--fee`（默认 0.05%）逐笔开平按名义收取。
  - 资金费每 8h 结算，仅持仓期间累计（short 费率为正时收取、long 为正时支付）。
  - 离场后资金闲置到下次调仓。

## 时点动态池（默认，消除前视偏差）

朴素做法「用今天的成交额排名去选过去的标的」会引入前视偏差——当时还不活跃的币不该入选。

引擎默认使用**时点动态池**：先抓一个更大的候选集（`--candidates`，默认 400），回测中每个
调仓点按「截至当时的滚动 24h 成交额」重新取前 `--universe`（默认 120）。这样每个决策只用
当时可得的信息。

`--fixed-universe` 可退回固定池（用当下成交额前 N 选标的，含前视偏差），仅用于对比。

## 运行

```bash
# 单配置 (默认: 做空, 45天, 池120, top5, 止损10%/止盈50%, fee 0.05%)
python3 -m cryptoquant.backtest

# 指定方向与止损止盈
python3 -m cryptoquant.backtest --side long --stop-loss 0.2 --take-profit 0.5

# 一次抓数, 跑预设矩阵 (4 个配置, 见 engine.PRESETS)
python3 -m cryptoquant.backtest --batch

# 退回固定池 (对比用)
python3 -m cryptoquant.backtest --fixed-universe
```

### 主要参数

| 参数 | 默认 | 含义 |
|------|------|------|
| `--days` | 45 | 回测窗口天数 |
| `--universe` | 120 | 实际交易的池子大小（每期选前 N） |
| `--candidates` | 400 | 抓取的候选集（动态池从中按时点成交额选） |
| `--top` | 5 | 每期持仓数 |
| `--side` | short | 方向 short/long |
| `--hours` | 6 | 调仓周期（小时） |
| `--stop-loss` | 0.10 | 止损比例 |
| `--take-profit` | 0.50 | 止盈比例 |
| `--fee` | 0.0005 | taker 单边费率 |
| `--capital` | 10000 | 初始净值 USDT |
| `--batch` | — | 跑预设矩阵 |
| `--fixed-universe` | — | 用固定池（含前视偏差） |

## 产物

每个 `(side, 止损, 止盈)` 组合是一个 **run**，`id` 形如 `long_sl20_tp50`：

- `frontend/runs/<id>.json` — 单个 run 的完整结果（`meta` / `summary` / `curve` / `cycles`），
  供 `run.html?id=<id>` 回放。
- `frontend/runs/manifest.json` — 所有 run 的汇总索引，按总收益排序，供 `backtest.html` 列表。
- `data/last_run.json` — 最近一个 run（调试/兼容，已 gitignore）。

### run JSON 结构

```jsonc
{
  "meta":   { "id", "label", "side", "days", "rebalance_hours", "top_n",
              "stop_loss", "take_profit", "universe_mode", "fee_rate",
              "start_equity", "period_start", "period_end", ... },
  "summary":{ "final_equity", "total_return_pct", "cagr_pct", "max_drawdown_pct",
              "sharpe", "win_rate_pct", "num_cycles", "total_fees",
              "total_funding", "exits": {SL,TP,TIME,NA}, "exit_*_pct" },
  "curve":  [ { "time", "equity" }, ... ],          // 净值曲线
  "cycles": [ { "time", "equity", "pnl_price", "pnl_funding", "fee", "net",
                "shorts": [ { "symbol", "ret24_pct", "entry", "exit",
                              "reason", "hold_h", "pnl" } ] }, ... ]
}
```

> `cycles[].shorts` 沿用历史字段名，表示「该周期各持仓腿」，方向 long 时也复用此键。

## 指标口径

- **CAGR**：`(期末/期初)^(365/天数) − 1`。短窗口（如 45 天）外推到年化会被极度放大，
  仅作参考，勿当真实年化。
- **Sharpe**：逐周期收益的均值/标准差，按每年周期数 `365×24/hours` 开方年化；无风险利率取 0。
- **最大回撤**：净值曲线峰值到谷值的最大跌幅。
- **胜率**：净额为正的周期数 / 净额非零的周期数。
- **离场构成**：止损/止盈/到时间各占比，反映策略实际是被止损截断还是吃满持有期。

## 重要免责

⚠️ 仅供研究演示，非投资建议。未计**滑点 / 借币费 / 爆仓**；等权 1x 无杠杆；单一历史窗口。
高收益大概率含 regime 偏差，需更长样本与基准（如买入持有 BTC）对比才能判断 edge 是否稳健。

> 实测结论：朴素「追空涨幅榜」在加密牛市/强趋势中是**负 edge**——做空暴涨币等于逆动量，
> 会被持续碾压；止损止盈只截断单笔极端，不扭转方向本身。
