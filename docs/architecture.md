# 架构说明

crypto-quant 是一个「数据 → 回测 → 实盘」一体的加密货币量化研究项目，围绕同一个策略族
（**涨幅榜多空 + 止损/止盈**）打通三个环节：

| 环节 | 模块 | 入口 | 产物 |
|------|------|------|------|
| 市场看板 | `cryptoquant.web` | `python3 -m cryptoquant.web` | 实时涨幅榜 + K 线网页 |
| 策略回测 | `cryptoquant.backtest` | `python3 -m cryptoquant.backtest --batch` | `frontend/runs/*.json` |
| 实时运行 | `cryptoquant.live` | `python3 -m cryptoquant.live` / Web 监控 | 下单 / SQLite 采样 |

设计原则：**仅依赖 Python 标准库**（无 pandas/ccxt），数据取自 Binance 公共接口与
USDⓈ-M 永续合约。抓取需直连 Binance，跑在能直连的首尔服务器上。

## 分层

代码组织成一个可导入的包 `cryptoquant/`，自底向上分层，上层只依赖下层：

```
                ┌─────────────────────────────────────────────┐
   入口 (CLI)   │  -m cryptoquant.{gainers,backtest,live,web}   │
                └───────┬───────────┬───────────┬──────────────┘
                        │           │           │
   应用层        backtest/      live/        web/
                 engine        executor      server + market
                                monitor
                        │           │           │
                        └─────┬─────┴─────┬─────┘
   策略层 (纯逻辑)       strategy/gainers  (选标的 / 止损止盈价位 / 取整)
                              │
   I/O 层               exchange/  http (重试) · public (公共行情) · futures (签名客户端)
                              │
   基础                 config  (路径 · endpoint · 常量 · MonitorConfig)
```

### `cryptoquant/config.py`

单点集中：仓库路径（`FRONTEND_DIR` / `RUNS_DIR` / `DATA_DIR` / `DB_PATH`）、交易所
endpoint（现货 / FAPI / testnet）、时间常量、杠杆代币后缀、以及 Web 后端的
`MonitorConfig`（从环境变量构造）。各模块从这里取值，避免常量与路径散落。

### `cryptoquant/exchange/`（I/O 层）

所有与 Binance 的网络交互集中在此：

- `http.py` — 统一的 `get_json(base, path, params)`，含指数退避重试；对 429 限频加长退避，
  对 418/451 封禁立即抛出。**消除了原先三处各写一遍的 urlopen 逻辑。**
- `public.py` — 免签公共行情：现货 ticker、合约 `select_universe` / `fetch_klines` /
  `fetch_funding` / `funding_between`，以及统一 schema 的 `rank_tickers`。
- `futures.py` — 签名版 `BinanceFutures` 客户端（HMAC-SHA256），testnet/实盘切换，
  含公共便捷方法与私有下单方法，附带 `selftest_signature()` 离线自校验。

### `cryptoquant/strategy/gainers.py`（策略层）

与网络/状态无关的**纯逻辑**，便于单测，被实盘执行与监控共用：
`pick_gainers`（选涨幅榜前 N，可按可交易符号集过滤）、`stop_take_levels`
（按方向算止损/止盈价位）、`round_down`（下单精度向下取整）、`is_leveraged`。
**消除了 live_trade / server / gainers 三处重复的选标的与取整逻辑。**

### 应用层

- `backtest/engine.py` — 回测核心：抓数 → 时点动态池 → 逐周期模拟 → 指标汇总；
  写 `runs/<id>.json` + `manifest.json`。详见 [backtest.md](./backtest.md)。
- `live/executor.py` — 单次实盘调仓（dry-run/testnet/实盘三档安全分层）。
- `live/monitor.py` — dry-run 监控：冻结快照 + 单调触发 + 周期采样写 SQLite。
- `web/market.py` — 涨幅榜缓存（24h / 1h）。
- `web/server.py` — 单进程后端，整合市场 API、回测静态产物、实时监控 API。
  详见 [web-api.md](./web-api.md)。

## 数据流

```
回测:  Binance FAPI ──load_data──▶ 内存行情 ──run_config──▶ runs/<id>.json ──▶ backtest.html / run.html
市场:  Binance FAPI ──Market.run_forever──▶ 内存缓存 ──/api/gainers──▶ index.html
实时:  Binance FAPI ──Monitor.build_plan(冻结)──▶ 采样 ──SQLite──▶ /api/monitor ──▶ live.html
```

回测产物（`runs/`、`last_run.json`、`live.db`）均为运行期生成、已 gitignore，
不进版本库，避免服务器 `git pull` 冲突。

## 为什么只用标准库

项目定位是「轻量、可在任意能直连 Binance 的机器上 `git pull` 即跑」。所有 HTTP 用
`urllib`、签名用 `hmac/hashlib`、存储用 `sqlite3`、前端图表用 CDN 的 lightweight-charts /
Chart.js。`requirements.txt` 中的第三方包仅为后续扩展（如引入 pandas 做更复杂分析）预留，
当前运行不需要安装。
