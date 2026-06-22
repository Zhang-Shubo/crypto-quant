"""实时策略监控 (dry-run, 不下单)。

冻结一组「按当前涨幅榜选出的」持仓 (入场价/止损/止盈), 之后周期性用实时标记价
采样浮盈并写 SQLite, 重启后历史保留。触发判定是单调的: 标记价一旦触到止损/止盈,
该仓永久平仓、盈亏定格在触发价 (模拟 STOP_MARKET 成交), 不再翻回持仓。

滚动调仓: 每隔 cfg.rebalance_sec (默认 6h) 重新选币、开新快照。调仓时把上一快照
「到点平仓」(已平仓用出场价, 持仓用当前标记价), 盈亏并入账户级「累计已实现」,
并持久化到 account 表。账户权益(实时总额) = 起始本金 + 累计已实现 + 当前快照浮盈,
因此止盈/止损清仓的盈亏会一路累计、跨快照不丢。cfg.rebalance_sec=0 则冻结不调仓。

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
        self.next_rebalance = None              # 下次调仓时刻 (ms); None=不调仓
        self.initial_capital = cfg.capital      # 账户起始本金 (持久化于 account 表)
        self.realized_cum = 0.0                 # 累计已实现盈亏 (跨快照, 含历次清仓/调仓)

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
        CREATE TABLE IF NOT EXISTS account(
          id INTEGER PRIMARY KEY CHECK(id=1),
          initial_capital REAL, realized_cum REAL, updated_at INTEGER);
        """)
        # 老库迁移: samples 增加 equity (账户权益快照) 列
        try:
            con.execute("ALTER TABLE samples ADD COLUMN equity REAL")
        except sqlite3.OperationalError:
            pass
        con.commit()
        con.close()

    def _load_account(self):
        """读取(或初始化)账户级累计状态。"""
        con = self._db()
        row = con.execute(
            "SELECT initial_capital, realized_cum FROM account WHERE id=1").fetchone()
        if row is None:
            con.execute("INSERT INTO account(id,initial_capital,realized_cum,updated_at) "
                        "VALUES(1,?,?,?)",
                        (self.cfg.capital, 0.0, int(time.time() * 1000)))
            con.commit()
            self.initial_capital, self.realized_cum = self.cfg.capital, 0.0
        else:
            self.initial_capital = row["initial_capital"]
            self.realized_cum = row["realized_cum"]
        con.close()

    def _persist_account(self):
        con = self._db()
        con.execute("UPDATE account SET realized_cum=?, updated_at=? WHERE id=1",
                    (self.realized_cum, int(time.time() * 1000)))
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

    @staticmethod
    def realized_value(plan, px) -> float:
        """把 plan 全部仓位实现为盈亏: 已平仓用出场价, 持仓用当前标记价 (模拟到点平仓)。
        无行情的仓跳过。用于调仓时把上一快照并入累计已实现。"""
        dr = 1 if plan["side"] == "long" else -1
        tot = 0.0
        for p in plan["positions"]:
            price = p["exit"] if p.get("closed") else px.get(p["symbol"])
            if price is None:
                continue
            tot += p["qty"] * (price - p["entry"]) * dr
        return tot

    # ---------- 实时取数 (供 HTTP /api/monitor) ----------
    def snapshot(self) -> dict:
        """读时锁定触发并算盈亏, 返回深拷贝快照 + 账户级汇总 (含实时总额/累计盈亏)。"""
        syms = {p["symbol"] for p in self.plan["positions"]}
        px = self.mark_prices(syms) if syms else {}
        with self.lock:
            self.apply_triggers(self.plan, px)
            pnl, notional, hit = self.compute_pnl(self.plan, px)
            realized_cum, init_cap = self.realized_cum, self.initial_capital
            plan = json.loads(json.dumps(self.plan))   # 深拷贝
        cum_pnl = realized_cum + pnl                   # 账户累计盈亏 (历次清仓 + 本轮浮盈)
        equity = init_cap + cum_pnl                    # 实时总额 (账户权益)
        return {"plan": plan, "prices": px, "now": int(time.time() * 1000),
                "summary": {"total_pnl": round(pnl, 4), "total_notional": round(notional, 2),
                            "hit": hit, "realized_cum": round(realized_cum, 4),
                            "cum_pnl": round(cum_pnl, 4), "equity": round(equity, 4),
                            "initial_capital": round(init_cap, 2)},
                "snapshot_id": self.snapshot_id, "sample_sec": self.cfg.sample_sec,
                "next_rebalance": self.next_rebalance, "rebalance_sec": self.cfg.rebalance_sec}

    def history(self) -> dict:
        """连续账户权益曲线: 跨所有快照按时间排序 (老样本无 equity 时回退 本金+浮盈)。"""
        con = self._db()
        rows = con.execute(
            "SELECT ts, total_pnl, hit, COALESCE(equity, ? + total_pnl) AS equity "
            "FROM samples ORDER BY ts DESC LIMIT 5000", (self.initial_capital,)).fetchall()
        con.close()
        return {"snapshot_id": self.snapshot_id, "frozen_at": self.plan.get("frozen_at", 0),
                "initial_capital": round(self.initial_capital, 2),
                "samples": [dict(r) for r in rows[::-1]]}

    # ---------- 调仓 (开新快照, 并入累计) ----------
    def _open_snapshot(self):
        """重新选币、开新快照。若已有上一快照, 先到点平仓并入累计已实现。"""
        with self.lock:
            prev = self.plan if self.plan.get("positions") else None
        if prev:
            syms = {p["symbol"] for p in prev["positions"]}
            px = self.mark_prices(syms) if syms else {}
            banked = self.realized_value(prev, px)
            with self.lock:
                self.realized_cum += banked
            self._persist_account()
            print(f"[live] 调仓: 快照#{self.snapshot_id} 到点平仓, 并入已实现 {banked:+.4f}U "
                  f"(累计 {self.realized_cum:+.4f}U)", file=sys.stderr)
        plan = self.build_plan()
        con = self._db()
        cur = con.execute(
            "INSERT INTO snapshots(created_at,side,capital,sl,tp,top,plan_json) VALUES(?,?,?,?,?,?,?)",
            (plan["frozen_at"], self.cfg.side, self.cfg.capital, self.cfg.stop_loss,
             self.cfg.take_profit, self.cfg.top, json.dumps(plan, ensure_ascii=False)))
        sid = cur.lastrowid
        con.commit()
        con.close()
        nxt = (plan["frozen_at"] + self.cfg.rebalance_sec * 1000) if self.cfg.rebalance_sec else None
        with self.lock:
            self.plan = plan
            self.snapshot_id = sid
            self.next_rebalance = nxt
        print(f"[live] 冻结快照 #{sid}: {len(plan['positions'])} 仓"
              + (f", 下次调仓 +{self.cfg.rebalance_sec}s" if nxt else " (不调仓)"), file=sys.stderr)

    # ---------- 后台采样循环 ----------
    def run_forever(self):
        cfg = self.cfg
        while not self.get_prec():           # 等行情元数据就绪
            time.sleep(1)
        self._load_account()
        self._open_snapshot()
        while True:
            try:
                if self.next_rebalance and int(time.time() * 1000) >= self.next_rebalance:
                    self._open_snapshot()    # 到点: 并入累计 + 重新选币
                syms = {p["symbol"] for p in self.plan["positions"]}
                px = self.mark_prices(syms) if syms else {}
                with self.lock:
                    self.apply_triggers(self.plan, px)
                    pnl, notional, hit = self.compute_pnl(self.plan, px)
                    equity = self.initial_capital + self.realized_cum + pnl
                    con = self._db()
                    con.execute(
                        "INSERT INTO samples(snapshot_id,ts,total_pnl,total_notional,hit,prices_json,equity) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (self.snapshot_id, int(time.time() * 1000), round(pnl, 4),
                         round(notional, 2), hit, json.dumps(px), round(equity, 4)))
                    con.commit()
                    con.close()
            except Exception as e:
                print(f"[sample] {e}", file=sys.stderr)
            time.sleep(cfg.sample_sec)
