# crypto-quant —— 常用命令 (从仓库根目录运行)
# 所有脚本仅依赖 Python 标准库; 数据抓取需直连 Binance (首尔服务器)。

PY ?= python3

.PHONY: help gainers backtest backtest-batch live serve test clean

help:           ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

gainers:        ## 现货 24h 涨幅榜 Top 20
	$(PY) -m cryptoquant.gainers

backtest:       ## 单配置回测 (默认 做空 止损10%/止盈50%)
	$(PY) -m cryptoquant.backtest

backtest-batch: ## 一次抓数, 跑预设策略矩阵 (生成 runs/ + manifest)
	$(PY) -m cryptoquant.backtest --batch

live:           ## 实盘执行器 (默认 testnet + dry-run, 只打印不下单)
	$(PY) -m cryptoquant.live

serve:          ## 启动统一后端 (市场/回测/实时), http://127.0.0.1:8800
	$(PY) -m cryptoquant.web

test:           ## 运行离线单元测试
	$(PY) -m unittest discover -s tests -v

clean:          ## 清理本地生成产物与缓存
	rm -rf data/live.db data/last_run.json frontend/runs
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
