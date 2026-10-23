"""无头自测:验证日历、行情、撮合、保证金与倒带重演的确定性。"""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nqsim.market import (Calendar, ET, INSTRUMENTS, MarketSim, generate_macro_events,
                          holidays_for, early_closes_for, is_holiday, is_early_close)
from nqsim.account import Broker
from nqsim.sim import Simulation

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


def section(t):
    print(f"\n=== {t} ===")


# ---------------------------------------------------------------- 日历
section("交易日历 / 时区")
cal = Calendar(dt.datetime.now(ET))
mon = dt.datetime(2026, 1, 5, 10, 0, tzinfo=ET)      # 周一
sun = dt.datetime(2026, 1, 4, 19, 0, tzinfo=ET)      # 周日 19:00
fri = dt.datetime(2026, 1, 9, 19, 0, tzinfo=ET)      # 周五 19:00
sat = dt.datetime(2026, 1, 10, 12, 0, tzinfo=ET)
check("周一 10:00 开盘", cal.is_open(mon))
check("周日 18:00 后开盘", cal.is_open(sun))
check("周日 10:00 休市", not cal.is_open(dt.datetime(2026, 1, 4, 10, 0, tzinfo=ET)))
check("周五 19:00 无夜盘(休市)", not cal.is_open(fri))
check("周六休市", not cal.is_open(sat))
check("周一 17:00 收盘", not cal.is_open(dt.datetime(2026, 1, 5, 17, 30, tzinfo=ET)))
check("元旦休市", not cal.is_open(dt.datetime(2026, 1, 1, 10, 0, tzinfo=ET)))
check("夏令时切换(3月第二周日前为EST)",
      dt.datetime(2026, 3, 5, 12, 0, tzinfo=ET).tzname() == "EST")
check("夏令时(7月为EDT)", dt.datetime(2026, 7, 5, 12, 0, tzinfo=ET).tzname() == "EDT")
check("感恩节次日提前收盘", is_early_close(dt.date(2026, 11, 27)))
check("假期前一日提前收盘", is_early_close(dt.date(2026, 12, 24)))
check("提前收盘时段13:00后休市",
      not cal.is_open(dt.datetime(2026, 12, 24, 13, 30, tzinfo=ET)))
check("提前收盘时段12:00仍开盘",
      cal.is_open(dt.datetime(2026, 12, 24, 12, 0, tzinfo=ET)))
check("圣诞当日休市",
      not cal.is_open(dt.datetime(2026, 12, 25, 11, 0, tzinfo=ET)))

# ---------------------------------------------------------------- 事件
section("宏观事件")
evs = generate_macro_events(2026, __import__("random").Random(1))
kinds = {e[1].split(" ")[0] for e in evs}
check("NFP 存在", any("非农" in e[1] for e in evs))
check("FOMC 存在", any("FOMC" in e[1] for e in evs))
check("CPI 存在", any("CPI" in e[1] for e in evs))
check("事件数量合理(80~200)", 80 <= len(evs) <= 200, f"= {len(evs)}")
check("FOMC 固定在美东14:00", all(e[0].hour == 14 for e in evs if "FOMC" in e[1]))
check("非农固定在美东08:30", all(e[0].hour == 8 and e[0].minute == 30 for e in evs if "非农" in e[1]))
check("事件按时间排序", all(evs[i][0] <= evs[i + 1][0] for i in range(len(evs) - 1)))

# ---------------------------------------------------------------- 行情
section("行情引擎")
m = MarketSim("NQ", seed=42, start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
start_price = m.price
prices = []
for ts, px in m.advance(60 * 60 * 6):     # 6 小时
    prices.append(px)
ret = (prices[-1] / start_price - 1) * 100
bars = m.bars
rng = max(b["h"] for b in bars) - min(b["l"] for b in bars)
check("价格有波动", len(set(prices)) > 1000, f"不同价位 {len(set(prices))}")
check("6小时涨跌幅合理(-2.5%~+2.5%)", -2.5 < ret < 2.5, f"= {ret:+.3f}%")
check("K线已生成", len(bars) > 100, f"= {len(bars)} 根(1分钟)")
check("K线 OHLC 自洽", all(b["h"] >= max(b["o"], b["c"]) and b["l"] <= min(b["o"], b["c"])
                          and b["h"] >= b["l"] for b in bars))
check("K线按时间递增", all(bars[i]["t"] < bars[i + 1]["t"] for i in range(len(bars) - 1)))
check("价格贴合最小变动单位", all(abs(round(p / 0.25) * 0.25 - p) < 1e-6 for p in prices[:5000]))
check("无价格穿越零值", min(prices) > 1000)
spread = m.spread_ticks()
check("盘口价差为 1~4 跳", 1 <= spread <= 4, f"= {spread}")
# 波动率标定:全天(23h)日内波幅 ~ 1.25%
check("日内区间合理(0.4%~3.5%)", 0.004 < rng / m.price < 0.035, f"= {rng/m.price*100:.2f}%")

# ---------------------------------------------------------------- 撮合
section("撮合 / 滑点 / 保证金")
b = Broker("NQ", 100_000.0, leverage=20, seed=5)
px = 24500.0
b.last_price = px
o = b.place_order("buy", "market", 1, now=dt.datetime.now(ET))
b.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
filled = b.orders[o.id]
check("市价单已成交", filled.status == "filled" and filled.filled == 1)
check("成交价高于市价(买方滑点)", filled.avg_fill > px, f"= {filled.avg_fill:.2f}")
check("滑点在 1~3 跳内", 1 <= (filled.avg_fill - px) / 0.25 <= 3.5,
      f"= {(filled.avg_fill-px)/0.25:.2f} 跳")
check("持仓 = 1 手", b.qty == 1)
check("收取佣金(4.38/手)", abs(b.commission_paid - 4.38) < 0.01, f"= {b.commission_paid:.2f}")
eq = b.equity(px)
check("权益 = 现金 + 浮盈", abs(eq - (b.cash + b.unrealized(px))) < 1e-6)
check("强平价低于现价(多头)", b.liquidation_price() < px, f"= {b.liquidation_price():.2f}")
b2 = Broker("NQ", 100_000.0, leverage=20, seed=5)
b2.last_price = px
b2.place_order("sell", "market", 1, now=dt.datetime.now(ET))
b2.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
check("空头强平价高于现价", b2.liquidation_price() > px, f"= {b2.liquidation_price():.2f}")

# 限价单:价格未穿越不成交
b3 = Broker("NQ", 100_000.0, 20, seed=1)
b3.last_price = px
lo = b3.place_order("buy", "limit", 1, limit=px - 20, now=dt.datetime.now(ET))
b3.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
check("限价单未穿越则不成交", b3.orders[lo.id].status == "working")
b3.process_print(dt.datetime.now(ET), px - 25, 1.0, 1.0)
check("限价单穿越后成交", b3.orders[lo.id].status == "filled",
      f"@ {b3.orders[lo.id].avg_fill:.2f}")
check("限价成交价不劣于限价", b3.orders[lo.id].avg_fill <= px - 20)

# 止损单:卖出止损应设在现价下方,价格下穿才触发
b4 = Broker("NQ", 100_000.0, 20, seed=1)
b4.last_price = px
so = b4.place_order("sell", "stop", 1, stop=px - 30, now=dt.datetime.now(ET))
b4.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
check("止损单未触发则挂单", b4.orders[so.id].status == "working")
b4.process_print(dt.datetime.now(ET), px - 35, 1.0, 1.0)
check("止损单触发并成交", b4.orders[so.id].status == "filled",
      f"@ {b4.orders[so.id].avg_fill:.2f}")
check("止损成交价低于止损价(含滑点)", b4.orders[so.id].avg_fill < px - 30)

# 强平
b5 = Broker("NQ", 10_000.0, 50, seed=1)
b5.last_price = px
b5.place_order("buy", "market", 1, now=dt.datetime.now(ET))
b5.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
b5.process_print(dt.datetime.now(ET), px - 1200, 3.0, 1.0)
check("大幅不利行情触发强平", b5.liquidations == 1 and b5.qty == 0,
      f"权益 {b5.equity(px-1200):,.0f}")

# 括号单:开仓成交后自动挂保护性止损,平仓后自动撤销
b6 = Broker("NQ", 100_000.0, 20, seed=3)
b6.last_price = px
b6.place_order("buy", "market", 2, now=dt.datetime.now(ET), protect=px - 30)
b6.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
b6.process_print(dt.datetime.now(ET), px, 1.0, 1.0)
check("开仓成交", b6.qty == 2, f"= {b6.qty}")
prot = [o for o in b6.orders.values() if o.otype == "stop" and o.side == "sell"]
check("自动挂出保护性止损", len(prot) == 1 and prot[0].stop == px - 30,
      f"= {prot[0].stop if prot else None}")
b6.close_position(now=dt.datetime.now(ET))
for _ in range(4):
    b6.process_print(dt.datetime.now(ET), px, 1.0, 2.0)
check("平仓后保护止损被撤销",
      all(o.status == "cancelled" for o in prot), f"= {prot[0].status if prot else 'n/a'}")

# ---------------------------------------------------------------- 模拟主控
section("模拟主控 / 倒带重演")
sim = Simulation("NQ", seed=777, balance=100_000.0, leverage=20, bar_period=60,
                 start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
path = []
for _ in range(20):
    sim.step(30)
    path.append(sim.price)
mid_time = sim.now
mid_price = sim.price
sim.step(300)
after_price = sim.price
after_eq = sim.equity()

ok = sim.rewind_to(mid_time, "replay")
check("倒退到历史点成功", ok)
check("倒退后时间正确", sim.now == mid_time, f"= {sim.now}")
check("倒退后价格一致", abs(sim.price - mid_price) < 1e-6)
for _ in range(10):
    sim.step(30)
check("重演后价格与原始一致(确定性)", abs(sim.price - after_price) < 1e-6,
      f"{sim.price:.2f} vs {after_price:.2f}")
check("重演后净值一致", abs(sim.equity() - after_eq) < 1e-6)

# 重来模式:丢弃之后的操作
sim2 = Simulation("NQ", seed=778, balance=100_000.0, leverage=20, bar_period=60,
                  start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
for _ in range(10):
    sim2.step(30)
t_cut = sim2.now
sim2.submit("order", {"side": "buy", "otype": "market", "qty": 2, "limit": None, "stop": None})
for _ in range(6):
    sim2.step(30)
check("下单后有持仓", sim2.broker.qty == 2, f"= {sim2.broker.qty}")
sim2.rewind_to(t_cut, "redo")
check("重来模式清空持仓", sim2.broker.qty == 0, f"= {sim2.broker.qty}")
check("重来模式丢弃后续操作日志", len(sim2.action_log) == 0, f"= {len(sim2.action_log)}")

# 统计
sim3 = Simulation("NQ", seed=999, balance=100_000.0, leverage=20, bar_period=60,
                  start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
for i in range(40):
    sim3.step(60)
    if i % 7 == 0:
        sim3.submit("order", {"side": "buy" if i % 14 == 0 else "sell",
                              "otype": "market", "qty": 1, "limit": None, "stop": None})
st = sim3.stats()
check("统计可用", st["equity"] > 0)
check("有交易记录", st["trades"] >= 1, f"= {st['trades']}")
check("最大回撤非负", st["max_dd"] >= 0, f"= ${st['max_dd']:,.0f}")
check("胜率在 0~100", 0 <= st["win_rate"] <= 100, f"= {st['win_rate']:.1f}%")
check("净值曲线有采样", len(sim3.equity_curve) > 50, f"= {len(sim3.equity_curve)}")
check("快照历史可用", sim3.history_span() > 300, f"= {sim3.history_span():.0f} 秒")

# 性能
import time as _t
sim4 = Simulation("NQ", seed=1234, balance=100_000.0, leverage=20, bar_period=60,
                  start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
t0 = _t.perf_counter()
sim4.step(1800)          # 30 分钟仿真
dtp = _t.perf_counter() - t0
rate = 1800 / dtp
check("仿真速度 > 100x 实时", rate > 100, f"= {rate:.0f}x 实时(单线程)")

# ---------------------------------------------------------------- 多周期聚合
section("多周期K线聚合 / 高周期形态")
PERIODS = (60, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 86400)
# 跨收盘(17:00)与次日开盘推进,底仓出现时间缺口
m3 = MarketSim("NQ", seed=7, start=dt.datetime(2026, 1, 5, 14, 0, tzinfo=ET))
for _ts, _px in m3.advance(60 * 60 * 12):
    pass
base = m3.aggs[10].bars
gaps = sum(1 for i in range(1, len(base)) if base[i]["t"] - base[i - 1]["t"] != 10)
check("跨收盘后底仓出现时间缺口(休市)", gaps >= 1, f"= {gaps} 处")
ok, counts = True, {}
for p in PERIODS:
    try:
        counts[p] = len(m3.bars_for(p, 100))
        m3.current_bar_for(p)
    except Exception as e:                       # pragma: no cover
        ok = False
        counts[p] = -1
check("跨休市后各周期分组不崩溃", ok, str(counts))
h1 = m3.bars_for(3600, 50)
check("小时线按整点对齐", all(b["t"] % 3600 == 0 for b in h1), f"= {len(h1)} 根")
d1 = m3.bars_for(86400, 30)
check("日线数量合理(仅12小时推进)", 1 <= len(d1) <= 5, f"= {len(d1)} 根")
# 分组一致性:底仓手工分组的 10 分钟K线 必须与 bars_for(600) 完全一致
manual = []
b10 = m3.aggs[10].bars
i = len(b10)
while i > 0 and len(manual) < 12:
    g = (b10[i - 1]["t"] // 600) * 600
    j = i - 1
    while j > 0 and b10[j - 1]["t"] >= g:
        j -= 1
    chunk = b10[j:i]
    manual.append({"t": chunk[0]["t"], "o": chunk[0]["o"],
                   "h": max(x["h"] for x in chunk), "l": min(x["l"] for x in chunk),
                   "c": chunk[-1]["c"], "v": sum(x["v"] for x in chunk)})
    i = j
manual.reverse()
auto = m3.bars_for(600, 12)
same = len(auto) == len(manual) and all(
    a["t"] == b["t"] and abs(a["h"] - b["h"]) < 1e-9 and abs(a["l"] - b["l"]) < 1e-9
    and abs(a["c"] - b["c"]) < 1e-9 and a["v"] == b["v"] for a, b in zip(auto, manual))
check("10分钟分组与手工统计一致", same, f"= {len(auto)} 根")
# 小时线与底仓分组合并后的最新一根一致
cur_h = m3.current_bar_for(3600)
check("当前小时K线自洽", cur_h is None or (cur_h["h"] >= cur_h["l"] and cur_h["v"] > 0))

# ---------------------------------------------------------------- 快速预填
section("快速历史预填 / 高周期铺满")
sim5 = Simulation("NQ", seed=4321, balance=100_000.0, leverage=20, bar_period=60,
                  start=dt.datetime(2026, 1, 5, 9, 30, tzinfo=ET))
t0 = _t0 = __import__("time").perf_counter()
sim5.fast_prefill(days=60, recent_hours=48.0)
elapsed = __import__("time").perf_counter() - t0
cnt = {p: len(sim5.market.bars_for(p, 200)) for p in PERIODS}
check("预填耗时 < 6 秒", elapsed < 6.0, f"= {elapsed:.2f}s")
check("各周期均有充足K线(>=30根)", all(v >= 30 for v in cnt.values()), str(cnt))
check("日线历史覆盖多日", cnt[86400] >= 20, f"= {cnt[86400]} 根")
check("10秒底仓历史充足", len(sim5.market.aggs[10].bars) > 5000,
      f"= {len(sim5.market.aggs[10].bars)} 根")
check("预填后价格仍为基准价", abs(sim5.market.price - 24500.0) < 1e-6,
      f"= {sim5.market.price}")
check("预填后昨结算合理", 20000 < sim5.market.prev_close < 30000,
      f"= {sim5.market.prev_close}")
# 预填后继续推进,行情与K线必须正常
sim5.step(600)
check("预填后继续推进正常", len(sim5.market.bars_for(60, 50)) >= 50)
check("预填后快照可回退", sim5.history_span() >= 0)

print(f"\n{'='*56}")
print(f"通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✅")
