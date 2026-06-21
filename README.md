# crypto-quant

加密货币量化研究项目 —— **市场看板 · 策略回测 · 实时运行** 三合一。数据取自 Binance 公共行情
与 USDⓈ-M 永续合约，围绕同一个策略族（**涨幅榜多空 + 止损/止盈**）打通数据、回测与实盘。

> 仅依赖 Python 标准库（无需 `pip install`）。抓取需直连 Binance，跑在能直连的首尔服务器上。
> ⚠️ 仅供研究演示，非投资建议。

## 快速上手

```bash
# 1. 现货 24h 涨幅榜
python3 -m cryptoquant.gainers --top 10

# 2. 跑策略回测 (一次抓数, 预设矩阵) → 生成 frontend/runs/
python3 -m cryptoquant.backtest --batch

# 3. 起统一后端 (市场/回测/实时), 浏览器开 http://127.0.0.1:8800
python3 -m cryptoquant.web

# 4. 实盘执行器 (默认 testnet + dry-run, 只打印不下单)
python3 -m cryptoquant.live
```

也可用 `make help` 查看快捷命令，`make test` 跑离线单测。

## 三大模块

| 模块 | 入口 | 说明 | 文档 |
|------|------|------|------|
| 市场看板 | `python3 -m cryptoquant.web` | 实时涨幅榜（1h/24h）+ 点选 K 线 | [web-api](docs/web-api.md) |
| 策略回测 | `python3 -m cryptoquant.backtest` | 涨幅榜多空 + 止损/止盈，含资金费/手续费、时点动态池 | [backtest](docs/backtest.md) |
| 实时运行 | `python3 -m cryptoquant.live` / Web | 单次实盘调仓执行器 + dry-run 监控采样 | [live-trading](docs/live-trading.md) |

后端单进程整合三块：市场 API、回测静态产物、实时监控 API，并直接服务 `frontend/`。
四个页面：市场 `index.html`、策略汇总 `backtest.html`、回测明细 `run.html?id=<id>`、实时 `live.html`。

## 项目结构

```
crypto-quant/
├── cryptoquant/              # 核心库 (可导入, 纯标准库)
│   ├── config.py             #   集中配置: 路径 / endpoint / 常量 / MonitorConfig
│   ├── exchange/             #   交易所 I/O
│   │   ├── http.py           #     统一 GET-JSON + 重试
│   │   ├── public.py         #     免签公共行情 (涨幅榜/K线/资金费/标的池)
│   │   └── futures.py        #     签名版 USDⓈ-M 客户端
│   ├── strategy/gainers.py   #   选标的 + 止损止盈价位 (纯逻辑, 易测)
│   ├── backtest/             #   回测引擎 + CLI (-m cryptoquant.backtest)
│   ├── live/                 #   executor 实盘执行 + monitor 监控采样
│   ├── web/                  #   server 后端 + market 榜单缓存
│   └── gainers.py            #   现货涨幅榜 CLI
├── frontend/                 # 静态前端 (index/backtest/run/live + app.css)
│   └── runs/                 #   回测产物 (gitignored)
├── docs/                     # 架构 / 回测 / 实盘 / API / 部署 文档
├── tests/                    # 离线单元测试 (无网络)
├── data/                     # 运行期数据: live.db / last_run.json (gitignored)
├── Makefile  .env.example  requirements.txt
```

详见 [docs/architecture.md](docs/architecture.md)。

## 部署

服务器 `git pull --ff-only` 后跑 `python3 -m cryptoquant.backtest --batch` 刷新回测、
`python3 -m cryptoquant.web` 起服务。凭证放仓库根 `.env`（从 `.env.example` 复制，已 gitignore）。
完整步骤与旧命令迁移表见 [docs/deployment.md](docs/deployment.md)。

## 免责

⚠️ 仅供研究演示，非投资建议。回测未计滑点/借币费/爆仓，等权 1x；单一历史窗口，高收益大概率
含 regime 偏差。朴素「追空涨幅榜」实测为**负 edge**。实盘风险自负，真金前务必在 testnet 充分验证。
