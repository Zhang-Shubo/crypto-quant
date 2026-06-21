"""10 个经典 BTC 单标的策略 (仅做多 long/flat) + 元信息注册表。

每个策略的信号函数 fn(bars) 返回:
  ("pos", positions)              — positions[t] ∈ {0,1}: 截至 close[t] 决定的目标仓位,
                                    引擎滞后 1 根执行(收盘算信号、次根成交)。
  ("intraday", grets, traded)     — 仅波动率突破: 当根已实现毛收益 + 是否成交,
                                    入场/出场都在当根内(开盘挂 stop, 收盘平), 引擎按成交扣费。

REGISTRY 同时携带中文介绍(原理/逻辑/优劣/适用 regime), 供报告生成器写 Obsidian 文档。
"""
from __future__ import annotations

from . import indicators as ind


# --------------------------- 状态机辅助 ---------------------------
def _state_machine(n, enter_ok, exit_ok):
    """通用「持有/空仓」状态机: enter_ok(i)/exit_ok(i) 为 None 时该根不动作。
    返回 positions(0/1), pos[i] 表示截至 close[i] 应持有的目标仓位。"""
    pos = [0] * n
    holding = False
    for i in range(n):
        if holding:
            if exit_ok(i):
                holding = False
        else:
            if enter_ok(i):
                holding = True
        pos[i] = 1 if holding else 0
    return pos


# --------------------------- 1. 买入持有 ---------------------------
def s_buyhold(bars):
    return "pos", [1] * len(bars)


# --------------------------- 2. 双均线交叉 20/100 ---------------------------
def s_sma_cross(bars, fast=20, slow=100):
    c = [b["c"] for b in bars]
    f, s = ind.sma(c, fast), ind.sma(c, slow)
    pos = [1 if (f[i] is not None and s[i] is not None and f[i] > s[i]) else 0
           for i in range(len(c))]
    return "pos", pos


# --------------------------- 3. 200 日均线趋势过滤 ---------------------------
def s_ma200(bars, n=200):
    c = [b["c"] for b in bars]
    m = ind.sma(c, n)
    pos = [1 if (m[i] is not None and c[i] > m[i]) else 0 for i in range(len(c))]
    return "pos", pos


# --------------------------- 4. 唐奇安通道突破 (海龟 20/10) ---------------------------
def s_donchian(bars, entry=20, exit=10):
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    ph, _ = ind.donchian(h, l, entry)
    _, pl = ind.donchian(h, l, exit)
    enter_ok = lambda i: ph[i] is not None and c[i] > ph[i]
    exit_ok = lambda i: pl[i] is not None and c[i] < pl[i]
    return "pos", _state_machine(len(c), enter_ok, exit_ok)


# --------------------------- 5. 布林带突破 (顺势) ---------------------------
def s_boll_break(bars, n=20, k=2.0):
    c = [b["c"] for b in bars]
    mid, up, _ = ind.bollinger(c, n, k)
    enter_ok = lambda i: up[i] is not None and c[i] > up[i]
    exit_ok = lambda i: mid[i] is not None and c[i] < mid[i]
    return "pos", _state_machine(len(c), enter_ok, exit_ok)


# --------------------------- 6. 布林带均值回归 (抄底) ---------------------------
def s_boll_revert(bars, n=20, k=2.0):
    c = [b["c"] for b in bars]
    mid, _, lo = ind.bollinger(c, n, k)
    enter_ok = lambda i: lo[i] is not None and c[i] < lo[i]
    exit_ok = lambda i: mid[i] is not None and c[i] >= mid[i]
    return "pos", _state_machine(len(c), enter_ok, exit_ok)


# --------------------------- 7. RSI 均值回归 ---------------------------
def s_rsi_revert(bars, n=14, buy=30, sell=50):
    c = [b["c"] for b in bars]
    r = ind.rsi(c, n)
    enter_ok = lambda i: r[i] is not None and r[i] < buy
    exit_ok = lambda i: r[i] is not None and r[i] > sell
    return "pos", _state_machine(len(c), enter_ok, exit_ok)


# --------------------------- 8. MACD 趋势 ---------------------------
def s_macd(bars, fast=12, slow=26, signal=9):
    c = [b["c"] for b in bars]
    line, sig, _ = ind.macd(c, fast, slow, signal)
    pos = [1 if (line[i] is not None and sig[i] is not None and line[i] > sig[i]) else 0
           for i in range(len(c))]
    return "pos", pos


# --------------------------- 9. 时间序列动量 (90 日绝对动量) ---------------------------
def s_tsmom(bars, lookback=90):
    c = [b["c"] for b in bars]
    pos = [0] * len(c)
    for i in range(lookback, len(c)):
        pos[i] = 1 if c[i] > c[i - lookback] else 0
    return "pos", pos


# --------------------------- 10. 波动率突破 (Larry Williams, 日内) ---------------------------
def s_vol_break(bars, k=0.5):
    """开盘价 + k×(昨日振幅) 挂买入 stop; 当根最高触及则成交, 当根收盘平仓 (不留隔夜)。"""
    n = len(bars)
    grets = [0.0] * n
    traded = [False] * n
    for i in range(1, n):
        rng = bars[i - 1]["h"] - bars[i - 1]["l"]
        buystop = bars[i]["o"] + k * rng
        if bars[i]["h"] >= buystop and buystop > 0:
            grets[i] = bars[i]["c"] / buystop - 1.0
            traded[i] = True
    return "intraday", grets, traded


# ============================= 注册表 =============================
REGISTRY = [
    {
        "num": "01", "id": "buyhold", "name": "买入持有", "en": "Buy & Hold",
        "cat": "基准", "params": "无", "fn": s_buyhold,
        "idea": "首日满仓买入并一直持有,不择时。",
        "logic": "全程仓位=1。它不是「策略」,而是其余 9 个择时策略要超越的**基准**——任何策略若长期跑不赢买入持有(且回撤更小/夏普更高),择时就没创造价值。",
        "pros": "零换手、零择时成本;吃满 BTC 长期 beta;实现极简。",
        "cons": "全程暴露,熊市完整承受 70%~80% 回撤;无任何风险控制。",
        "regime": "长牛市无敌。震荡/熊市回撤巨大。",
    },
    {
        "num": "02", "id": "sma_cross", "name": "双均线交叉 (20/100)", "en": "SMA Crossover",
        "cat": "趋势", "params": "快线 SMA20 / 慢线 SMA100", "fn": s_sma_cross,
        "idea": "短期均线上穿长期均线(金叉)做多,下穿(死叉)平仓。",
        "logic": "用两条不同周期的简单均线刻画趋势方向:快线>慢线视为上升趋势成立则持有,反之离场。是最经典的趋势跟踪入门信号。",
        "pros": "逻辑直观、参数少;能吃到大段主升浪;熊市自动空仓躲开深跌。",
        "cons": "震荡市被反复金叉/死叉「打脸」(whipsaw),持续小亏;信号滞后,顶部/底部都让出一截。",
        "regime": "适合单边趋势(无论牛熊);最怕宽幅震荡。",
    },
    {
        "num": "03", "id": "ma200", "name": "200 日均线趋势过滤", "en": "MA200 Trend Filter",
        "cat": "趋势", "params": "SMA200", "fn": s_ma200,
        "idea": "收盘价在 200 日均线上方持有,跌破则空仓。",
        "logic": "200 日线是市场公认的牛熊分界。价格在其上=多头市场,享受上行;跌破=进入风险区,清仓避险。比双均线更「慢」更钝,但躲大熊极有效。",
        "pros": "极简、稳健;历史上能完整躲过加密大熊市的主跌段;换手很低。",
        "cons": "极度滞后,顶部回撤一截才出、底部反弹一截才进;长震荡中在均线上下反复被洗。",
        "regime": "牛熊切换明显时表现最好;盘整期容易来回挨刀。",
    },
    {
        "num": "04", "id": "donchian", "name": "唐奇安通道突破 (海龟 20/10)", "en": "Donchian Breakout",
        "cat": "突破", "params": "入场=20日新高 / 出场=10日新低", "fn": s_donchian,
        "idea": "突破前 20 日最高价做多,跌破前 10 日最低价离场。",
        "logic": "经典「海龟交易法则」核心:价格创出近 20 日新高,意味着突破阻力、趋势启动;用更短的 10 日新低作为离场,锁定趋势利润、及时止损。",
        "pros": "纯价格、无参数拟合味;趋势行情能咬住大波段;规则机械、可严格执行。",
        "cons": "假突破多,震荡市频繁止损;突破时已离起点有距离,入场偏贵。",
        "regime": "强趋势/高波动行情最佳;低波震荡区假突破吃手续费。",
    },
    {
        "num": "05", "id": "boll_break", "name": "布林带突破 (顺势)", "en": "Bollinger Breakout",
        "cat": "突破", "params": "BBands(20, 2σ),上轨入场 / 中轨出场", "fn": s_boll_break,
        "idea": "收盘突破布林带上轨(均线+2σ)做多,跌回中轨平仓。",
        "logic": "突破上轨说明价格相对近期波动出现「异常强」的上行动能,视为趋势加速;跌回中轨(均线)说明动能衰竭,离场。把波动率自适应地嵌进了通道宽度。",
        "pros": "通道随波动自适应,比固定百分比更灵活;能抓动量爆发。",
        "cons": "上轨买在相对高位,回撤风险大;均值回归型行情里「追高即套」。",
        "regime": "趋势加速/突破行情好;均值回归的横盘里最难受。",
    },
    {
        "num": "06", "id": "boll_revert", "name": "布林带均值回归 (抄底)", "en": "Bollinger Reversion",
        "cat": "均值回归", "params": "BBands(20, 2σ),下轨入场 / 中轨出场", "fn": s_boll_revert,
        "idea": "收盘跌破布林带下轨做多(超卖反弹),涨回中轨止盈。",
        "logic": "假设价格短期偏离均值后会回归:跌破下轨=过度超卖,博反弹;回到中轨=均值修复完成,离场。与第 5 个策略方向完全相反,是「低买」而非「追高」。",
        "pros": "震荡市里低买高卖,胜率通常较高;入场点相对便宜。",
        "cons": "趋势下跌中「抄底抄在半山腰」,接连接刀;无硬止损时尾部风险大。",
        "regime": "区间震荡市最佳;单边下跌行情是其天敌。",
    },
    {
        "num": "07", "id": "rsi_revert", "name": "RSI 均值回归 (14)", "en": "RSI Mean Reversion",
        "cat": "均值回归", "params": "RSI14 < 30 入场 / > 50 出场", "fn": s_rsi_revert,
        "idea": "RSI 跌破 30(超卖)买入,回升过 50 卖出。",
        "logic": "RSI 衡量近期涨跌动能的强弱。<30 视为超卖、反弹概率上升;回到 50(中性)即兑现。经典的振荡指标逆势用法。",
        "pros": "对短期超跌反弹敏感;震荡市胜率高、持仓时间短、资金周转快。",
        "cons": "强趋势里 RSI 可长期钝化(跌势中一直<30 越买越亏 / 涨势里早早卖飞);阈值对结果敏感。",
        "regime": "无趋势的来回震荡最佳;趋势行情会持续误导。",
    },
    {
        "num": "08", "id": "macd", "name": "MACD 趋势 (12/26/9)", "en": "MACD Trend",
        "cat": "趋势", "params": "MACD(12,26,9),DIF 上穿 DEA 持有", "fn": s_macd,
        "idea": "MACD 快线(DIF)在信号线(DEA)上方持有,下方空仓。",
        "logic": "MACD 是两条 EMA 之差再平滑,刻画动量的方向与拐点。DIF>DEA 表示中短期动量转强,持有;反之离场。比 SMA 交叉更平滑、更早响应拐点。",
        "pros": "EMA 加权更灵敏、拐点略领先 SMA;趋势段表现稳。",
        "cons": "本质仍是趋势跟踪,震荡市同样反复假信号;参数多。",
        "regime": "趋势市好;高频震荡里假交叉多。",
    },
    {
        "num": "09", "id": "tsmom", "name": "时间序列动量 (90 日)", "en": "Time-Series Momentum",
        "cat": "趋势", "params": "过去 90 日收益 > 0 持有", "fn": s_tsmom,
        "idea": "若过去 90 天 BTC 收益为正则持有,否则空仓。",
        "logic": "学术界验证最广的「绝对动量」:资产自身过去 N 月收益的符号能预测未来短期收益。正动量持有、负动量离场,是跨资产类别普遍有效的趋势因子。",
        "pros": "极简、稳健、参数单一,过拟合风险低;有大量跨市场实证支持;熊市自动离场。",
        "cons": "中等滞后(90 天回看);拐点附近来回切换;绝对收益不如顶部精确择时。",
        "regime": "中长期趋势行情最佳;频繁牛熊快速切换时易反复。",
    },
    {
        "num": "10", "id": "vol_break", "name": "波动率突破 (日内)", "en": "Volatility Breakout",
        "cat": "波动率", "params": "开盘价 + 0.5×昨日振幅 挂买入 stop,收盘平", "fn": s_vol_break,
        "idea": "当日价格突破「开盘价+0.5×昨日振幅」即买入,当日收盘平仓,不留隔夜。",
        "logic": "Larry Williams 经典日内法:用昨日振幅度量「今日该有的波动」,价格向上突破这个阈值说明多头发力,顺势追入,日内了结规避隔夜风险。与其余 9 个隔夜持仓策略的执行方式完全不同。",
        "pros": "只在波动放大、方向明确时入场;日内平仓规避隔夜跳空;现金利用率高(多数时间空仓)。",
        "cons": "日线近似(无分钟数据)会高估成交质量;换手高、手续费/滑点侵蚀大;k 值敏感。",
        "regime": "高波动、日内冲高行情好;低波动磨人行情几乎不触发或假突破。",
    },
]
