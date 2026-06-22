# Web 后端 & API 说明

模块：`cryptoquant.web`（`server.py` + `market.py`）

单进程后端（默认 `127.0.0.1:8800`），一个端口整合三块：市场看板、回测产物、实时监控。
两个后台守护线程分别刷新涨幅榜缓存与采样实时浮盈。

## 启动

```bash
python3 -m cryptoquant.web        # 或: make serve
# 浏览器打开 http://127.0.0.1:8800
```

需直连 Binance（拉行情/标记价）。签名相关（监控建仓）需先 `source .env`（见
[live-trading.md](./live-trading.md)）。

## 配置（环境变量）

由 `cryptoquant/config.py` 的 `MonitorConfig.from_env()` 读取：

| 变量 | 默认 | 含义 |
|------|------|------|
| `APP_PORT` | 8800 | 监听端口 |
| `MON_SIDE` | long | 监控策略方向 short/long |
| `MON_CAPITAL` | 100 | 监控用名义本金 USDT |
| `MON_SL` / `MON_TP` | 0.10 / 0.50 | 止损 / 止盈比例 |
| `MON_TOP` | 5 | 监控持仓数 |
| `MON_MIN_QVOL` | 1000000 | 标的最小 24h 成交额 |
| `MON_UNIVERSE` | 150 | 1h 榜刷新的标的池大小 |
| `SAMPLE_SEC` | 60 | 浮盈采样周期（秒） |
| `MON_REBALANCE_SEC` | 21600 | 滚动调仓周期（秒，默认 6h）；到点重新选币开新快照，上一批到点平仓并入累计已实现；设 `0` 则冻结不调仓 |

```bash
MON_SIDE=short MON_CAPITAL=500 APP_PORT=9000 python3 -m cryptoquant.web
```

## HTTP 接口

所有 JSON 接口返回 `Access-Control-Allow-Origin: *`。

### `GET /api/gainers`

合约涨幅榜（来自后台缓存）。

| 查询参数 | 默认 | 说明 |
|----------|------|------|
| `window` | 24h | `24h` 或 `1h` |
| `order` | desc | `desc` 涨幅榜 / `asc` 跌幅榜 |
| `top` | 30 | 返回条数 |

```jsonc
{ "window": "24h", "updated": 1782026642655,
  "rows": [ { "symbol": "XUSDT", "chg": 42.1, "price": 1.23, "qvol": 5.0e7 }, ... ] }
```

### `GET /api/klines`

单标的 K 线（实时透传 Binance）。

| 参数 | 默认 | 说明 |
|------|------|------|
| `symbol` | （必填） | 如 `BTCUSDT` |
| `interval` | 1h | 15m/1h/4h/1d… |
| `limit` | 200 | ≤ 1000 |

```jsonc
{ "symbol": "BTCUSDT", "interval": "1h",
  "candles": [ { "time": 1700000000, "open":, "high":, "low":, "close":, "volume": }, ... ],
  "price_precision": 2, "tick_size": 0.1 }
```

> `time` 为秒级（适配 lightweight-charts）；`tick_size` 供前端自适应价格精度的下限。

### `GET /api/monitor`

实时监控当前快照（读时会锁定已触发的止损/止盈）。

```jsonc
{ "plan": { "side", "capital", "sl", "tp", "top", "notional_each", "frozen_at",
            "positions": [ { "symbol","entry","qty","sl","tp","chg24",
                             "closed","exit","reason","exit_ts" }, ... ] },
  "prices": { "XUSDT": 1.23, ... },          // 实时标记价
  // summary: 本轮(当前快照) total_pnl/total_notional/hit + 账户级累计
  //   realized_cum=历次清仓/调仓已实现; cum_pnl=realized_cum+本轮浮盈;
  //   equity=initial_capital+cum_pnl(实时总额)
  "summary": { "total_pnl", "total_notional", "hit",
               "realized_cum", "cum_pnl", "equity", "initial_capital" },
  "snapshot_id": 1, "sample_sec": 60,
  "next_rebalance": 1782..., "rebalance_sec": 21600, "now": 1782... }
```

### `GET /api/monitor/history`

连续账户权益（实时总额）采样序列，跨所有快照按时间排序（来自 SQLite，最多最近 5000 点，重启后保留）。

```jsonc
{ "snapshot_id": 1, "frozen_at": 1782..., "initial_capital": 100,
  "samples": [ { "ts", "total_pnl", "hit", "equity" }, ... ] }
```

### 静态文件

其余路径回退到 `frontend/` 目录（`/` → `index.html`），含 `runs/*.json` 回测产物。
路径做了规范化校验，防止越权读取 `frontend/` 外的文件。

## 页面

| 页面 | 路径 | 数据源 |
|------|------|--------|
| 市场看板 | `/` `index.html` | `/api/gainers` + `/api/klines` |
| 策略汇总 | `/backtest.html` | `runs/manifest.json` |
| 回测明细 | `/run.html?id=<id>` | `runs/<id>.json` |
| 实时策略 | `/live.html` | `/api/monitor` + `/api/monitor/history` |

## 存储

`data/live.db`（SQLite）三张表：`snapshots`（每次调仓冻结的持仓计划）、`samples`（逐次采样，含 `equity` 账户权益列）、`account`（账户级 `initial_capital` / `realized_cum` 累计已实现，单行 `id=1`）。
已 gitignore；删除即重置历史与累计。
