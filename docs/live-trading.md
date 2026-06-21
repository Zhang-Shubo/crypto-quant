# 实盘执行 & 实时监控说明

模块：`cryptoquant.live`（执行器 `executor.py` + 监控 `monitor.py`）

涉及真实资金，请先通读本页的**安全分层**。

## 凭证配置

执行器与监控走签名接口需要 API Key/Secret。**永不写入仓库**：

```bash
cp .env.example .env          # 填入 BINANCE_KEY / BINANCE_SECRET (.env 已 gitignore)
set -a && source .env && set +a
```

建议给 API Key **仅开「合约交易」权限、关闭提现，并绑定服务器出口 IP 白名单**。
Testnet（测试币，零风险）注册：<https://testnet.binancefuture.com>。

## 实盘执行器 `cryptoquant.live`

把回测策略的「一次调仓」落到真实下单：选 24h 涨幅榜前 N → 按方向市价开仓 →
挂 reduceOnly 止损 + 止盈（整仓平）。配合 cron 每 6h 跑一次即成一个简单机器人。

### 三档安全分层（默认最安全）

| 命令 | 环境 | 行为 |
|------|------|------|
| `python3 -m cryptoquant.live` | testnet | **dry-run**，只打印将下的单，不调用下单接口 |
| `python3 -m cryptoquant.live --execute` | testnet | 真下单，但用**测试币**，零真金风险 |
| `python3 -m cryptoquant.live --live --execute` | **实盘** | ⚠️ **真金**下单（会二次确认打印警告） |

### 示例

```bash
python3 -m cryptoquant.live                                  # testnet dry-run
python3 -m cryptoquant.live --side long --sl 0.2 --tp 0.5    # 做多, 调参
python3 -m cryptoquant.live --execute                        # testnet 真下单
python3 -m cryptoquant.live --signal live --execute          # 用实盘涨幅榜选标的, testnet 执行
python3 -m cryptoquant.live --live --execute                 # ⚠️ 实盘真金
```

### 参数

| 参数 | 默认 | 含义 |
|------|------|------|
| `--side` | short | 方向 short/long |
| `--top` | 5 | 持仓数 |
| `--sl` / `--tp` | 0.10 / 0.50 | 止损 / 止盈比例 |
| `--leverage` | 1 | 杠杆 |
| `--capital` | 读余额 | 部署总额 USDT；不填则读账户 availableBalance |
| `--fraction` | 1.0 | 动用资金比例 |
| `--min-qvol` | 1e6 | 标的最小 24h 成交额过滤 |
| `--live` | — | 用实盘（默认 testnet） |
| `--execute` | — | 真下单（默认 dry-run） |
| `--signal` | venue | 涨幅榜来源：`venue` 执行所自身 / `live` 实盘行情 |

> `--signal live`：testnet 的行情是模拟的，用它选标的没意义。该选项让排榜用**实盘真实行情**、
> 仍在 testnet 执行，并自动跳过 testnet 不存在的合约——更接近真实信号的演练。

## 实时监控（dry-run，不下单）

`cryptoquant.live.monitor.Monitor` 由 Web 后端在后台线程驱动（见 [web-api.md](./web-api.md)），
也可单独实例化用于测试。它**不下任何单**，只做「假设按当前涨幅榜建仓后会怎样」的实时跟踪：

1. **冻结快照**：启动时按当前涨幅榜选出一组持仓（入场价/止损/止盈/数量），写入
   `data/live.db` 的 `snapshots` 表。
2. **周期采样**：每 `SAMPLE_SEC`（默认 60s）用实时**标记价**计算浮盈，写入 `samples` 表。
   重启后历史保留。
3. **单调触发**：标记价一旦触到止损/止盈，该仓**永久平仓**、盈亏定格在触发价（模拟
   STOP_MARKET 成交），不会因价格回落而翻回持仓——避免前端在边界反复闪烁。

前端 `live.html` 展示合计浮盈、浮盈率、各仓的入场/现价/距止损止盈、以及浮盈历史曲线。

## 风险提示

⚠️ 策略**未经样本外验证**，回测中朴素追空涨幅榜为负 edge（见 [backtest.md](./backtest.md)）。
实盘风险自负，非投资建议。强烈建议长期只在 testnet 或 dry-run 监控下观察，真金实盘前务必
用极小资金、小 `--fraction` 试运行。
