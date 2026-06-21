"""实时策略监控 (dry-run, 不下单)。

冻结一组「按当前涨幅榜选出的」持仓 (入场价/止损/止盈), 之后周期性用实时标记价
采样浮盈并写 SQLite, 重启后历史保留。触发判定是单调的: 标记价一旦触到止损/止盈,
该仓永久平仓、盈亏定格在触发价 (模拟 STOP_MARKET 成交), 不再翻回持仓。

被 cryptoquant.web 后端在后台线程驱动; 也可单独实例化用于测试。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time

from ..config import DATA_DIR, DB_PATH, MonitorConfig
from ..strategy import pick_gainers, round_down, stop_take_levels


class Monitor:
    def __init__(self, client, cfg: MonitorConfig, get_prec):
        """client: BinanceFutures(实盘只读); cfg: MonitorConfig; get_prec: ()->precision_map。"""
        self.client = client
        self.cfg = cfg
        self.get_prec = get_prec
        self.lock = threading.Lock()
        self.plan: dict = {"positions": []}
        self.snapshot_id = None

    # ---------- SQLite ----------
    def _db(self):
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def init_db(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        con = self._db()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at INTEGER, side TEXT, capital REAL, sl REAL, tp REAL, top INTEGER,
          plan_json TEXT);
        CREATE TABLE IF NOT EXISTS samples(
          snapshot_id INTEGER, ts INTEGER,
          total_pnl REAL, total_notional REAL, hit INTEGER, prices_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_samples ON samples(snapshot_id, ts);
        """)
        con.commit()
        con.close()

    # ---------- 计划 (冻结快照) ----------
    def _quality_allowed(self, prec) -> set:
        """质量池可交易集: 在 prec(可交易) 基础上, 仅保留上市≥quality_age_months月的标的。
        成交额门槛由 pick_gainers 的 min_quote_volume 处理。"""
        from ..exchange import onboard_dates
        from ..config import DAY_MS
        onboard = onboard_dates()
        now = int(time.time() * 1000)
        cutoff = self.cfg.quality_age_months * 30 * DAY_MS
        return {s for s in prec if onboard.get(s, 0) and (now - onboard[s]) >= cutoff}

    def build_plan(self) -> dict:
        cfg, prec = self.cfg, self.get_prec()
        if cfg.quality_pool:
            allowed = self._quality_allowed(prec)
            min_qv = cfg.quality_vol
            pool = "quality"
        else:
            allowed = prec
            min_qv = cfg.min_quote_volume
            pool = "gainers"
        picks = pick_gainers(self.client.ticker_24hr(), cfg.top, allowed=allowed,
                             min_quote_volume=min_qv)
        notional = cfg.capital / cfg.top if picks else 0.0
        pos = []
        for g in picks:
            sym, price, pp = g["symbol"], g["price"], prec[g["symbol"]]
            qty = round_down(notional / price, pp["qty_prec"])
            sl_raw, tp_raw = stop_take_levels(cfg.side, price, cfg.stop_loss, cfg.take_profit)
            sl = round_down(sl_raw, pp["price_prec"])
            tp = round_down(tp_raw, pp["price_prec"])
            pos.append({"symbol": sym, "entry": price, "qty": qty, "sl": sl, "tp": tp,
                        "chg24": g["change_pct"], "closed": False, "exit": None,
                        "reason": None, "exit_ts": None})
        return {"side": cfg.side, "capital": cfg.capital, "sl": cfg.stop_loss,
                "tp": cfg.take_profit, "top": cfg.top, "notional_each": round(notional, 2),
                "pool": pool, "quality_age_months": cfg.quality_age_months if cfg.quality_pool else None,
                "quality_vol": cfg.quality_vol if cfg.quality_pool else None,
                "frozen_at": int(time.time() * 1000), "positions": pos}

    # ---------- 盈亏 ----------
    def mark_prices(self, symbols) -> dict:
        return {d["symbol"]: float(d["markPrice"])
                for d in self.client.premium_index() if d["symbol"] in symbols}

    @staticmethod
    def apply_triggers(plan, px):
        """锁定触发 (单调): 标记价触到止损/止盈即永久平仓, 盈亏定格在触发价。
        需在外层 lock 内调用 (原地改 plan)。"""
        now = int(time.time() * 1000)
        for p in plan["positions"]:
            if p.get("closed"):
                continue
            cur = px.get(p["symbol"])
            if cur is None:
                continue
            reason = exit_px = None
            if plan["side"] == "long":
                if cur <= p["sl"]:
                    reason, exit_px = "SL", p["sl"]
                elif cur >= p["tp"]:
                    reason, exit_px = "TP", p["tp"]
            else:
                if cur >= p["sl"]:
                    reason, exit_px = "SL", p["sl"]
                elif cur <= p["tp"]:
                    reason, exit_px = "TP", p["tp"]
            if reason:
                p.update(closed=True, exit=exit_px, reason=reason, exit_ts=now)

    @staticmethod
    def compute_pnl(plan, px):
        dr = 1 if plan["side"] == "long" else -1
        tot_pnl = tot_notional = 0.0
        closed = 0
        for p in plan["positions"]:
            # 已平仓: 盈亏定格在出场价; 持仓中: 用实时标记价
            price = p["exit"] if p.get("closed") else px.get(p["symbol"])
            if price is None:
                continue
            tot_pnl += p["qty"] * (price - p["entry"]) * dr
            tot_notional += p["qty"] * p["entry"]
            if p.get("closed"):
                closed += 1
        return tot_pnl, tot_notional, closed

    # ---------- 实时取数 (供 HTTP /api/monitor) ----------
    def snapshot(self) -> dict:
        """读时锁定触发并算盈亏, 返回深拷贝快照 + 汇总。"""
        syms = {p["symbol"] for p in self.plan["positions"]}
        px = self.mark_prices(syms) if syms else {}
        with self.lock:
            self.apply_triggers(self.plan, px)
            pnl, notional, hit = self.compute_pnl(self.plan, px)
            plan = json.loads(json.dumps(self.plan))   # 深拷贝
        return {"plan": plan, "prices": px, "now": int(time.time() * 1000),
                "summary": {"total_pnl": round(pnl, 4), "total_notional": round(notional, 2), "hit": hit},
                "snapshot_id": self.snapshot_id, "sample_sec": self.cfg.sample_sec}

    def history(self) -> dict:
        con = self._db()
        rows = con.execute(
            "SELECT ts,total_pnl,total_notional,hit FROM samples WHERE snapshot_id=? ORDER BY ts",
            (self.snapshot_id,)).fetchall()
        con.close()
        return {"snapshot_id": self.snapshot_id, "frozen_at": self.plan.get("frozen_at", 0),
                "samples": [dict(r) for r in rows]}

    # ---------- 后台采样循环 ----------
    def run_forever(self):
        cfg = self.cfg
        while not self.get_prec():           # 等行情元数据就绪
            time.sleep(1)
        self.plan = self.build_plan()
        con = self._db()
        cur = con.execute(
            "INSERT INTO snapshots(created_at,side,capital,sl,tp,top,plan_json) VALUES(?,?,?,?,?,?,?)",
            (self.plan["frozen_at"], cfg.side, cfg.capital, cfg.stop_loss, cfg.take_profit,
             cfg.top, json.dumps(self.plan, ensure_ascii=False)))
        self.snapshot_id = cur.lastrowid
        con.commit()
        con.close()
        print(f"[live] 冻结快照 #{self.snapshot_id}: {len(self.plan['positions'])} 仓", file=sys.stderr)
        while True:
            try:
                syms = {p["symbol"] for p in self.plan["positions"]}
                px = self.mark_prices(syms) if syms else {}
                with self.lock:
                    self.apply_triggers(self.plan, px)
                    pnl, notional, hit = self.compute_pnl(self.plan, px)
                    con = self._db()
                    con.execute(
                        "INSERT INTO samples(snapshot_id,ts,total_pnl,total_notional,hit,prices_json) "
                        "VALUES(?,?,?,?,?,?)",
                        (self.snapshot_id, int(time.time() * 1000), round(pnl, 4),
                         round(notional, 2), hit, json.dumps(px)))
                    con.commit()
                    con.close()
            except Exception as e:
                print(f"[sample] {e}", file=sys.stderr)
            time.sleep(cfg.sample_sec)
