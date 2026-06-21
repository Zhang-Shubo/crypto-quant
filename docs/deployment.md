# 部署说明（首尔服务器）

数据抓取需直连 Binance（境内/部分地区会被 451 拒绝），因此跑在能直连的**首尔腾讯云**上。
服务器目录 `~/crypto-quant` 跟踪 `origin/main`，用**只读 deploy key** 认证。

## 更新代码并跑回测

```bash
# 本地: 改完代码提交推送 (github-personal → Zhang-Shubo)
git add -A && git commit -m "..." && git push

# 服务器:
ssh -i ~/Documents/tecent_seoul.pem ubuntu@43.164.191.143
cd ~/crypto-quant && git pull --ff-only
python3 -m cryptoquant.backtest --batch     # 重新抓数+回测, 刷新 runs/ + manifest
```

> 回测产物（`frontend/runs/`、`data/last_run.json`、`data/live.db`）均已 gitignore，
> 不进版本库，所以 `git pull --ff-only` 不会因本地生成文件冲突。

## 起后端服务

```bash
cd ~/crypto-quant
set -a && source .env && set +a            # 监控建仓需要签名凭证 (见 live-trading.md)
python3 -m cryptoquant.web                  # 默认 127.0.0.1:8800
```

后台常驻可用 `nohup` / `systemd` / `tmux`，例如：

```bash
nohup python3 -m cryptoquant.web > ~/crypto-quant-web.log 2>&1 &
```

如需公网访问，用 Nginx 反代到 `127.0.0.1:8800`，并自行加 TLS 与访问控制（后端默认只听
回环地址）。

## 定时实盘 / 定时回测（可选）

用 cron 把执行器或回测定时化。**真金实盘务必先在 testnet 充分验证**（见
[live-trading.md](./live-trading.md)）。示例：每 6 小时一次 testnet 调仓演练 +
每日刷新回测：

```cron
0 */6 * * *  cd ~/crypto-quant && set -a && . ./.env && set +a && python3 -m cryptoquant.live --execute >> ~/cq-live.log 2>&1
30 0  * * *  cd ~/crypto-quant && python3 -m cryptoquant.backtest --batch >> ~/cq-bt.log 2>&1
```

## 环境

- Python 3.10+（仅标准库；无需 `pip install`）。
- 出口 IP 需能直连 `api.binance.com` / `fapi.binance.com`；若用实盘签名接口，将该 IP
  加入 Binance API Key 的 IP 白名单。

## 从旧结构迁移（v0.1 → v0.2）

旧的扁平脚本路径已重组为 `cryptoquant/` 包，命令对应关系：

| 旧命令 | 新命令 |
|--------|--------|
| `python3 data/binance_gainers.py` | `python3 -m cryptoquant.gainers` |
| `python3 backtest/short_gainers.py` | `python3 -m cryptoquant.backtest` |
| `python3 execution/live_trade.py` | `python3 -m cryptoquant.live` |
| `python3 webapp/server.py` | `python3 -m cryptoquant.web` |
| `execution/.env` | `.env`（仓库根目录） |

服务器首次 `git pull` 到 v0.2 后，记得把旧的 `execution/.env` 复制到根目录 `.env`，
并更新任何 cron / 启动脚本里的命令。
