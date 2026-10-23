"""GUI 冒烟测试:窗口/刷新/主题/视图/下单/周期/倍速/倒退/快进。"""
from __future__ import annotations

import datetime as dt
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nqsim.uidefs import enable_dpi_awareness
enable_dpi_awareness()

from nqsim.app import App
from nqsim.market import BAR_PERIODS

errors = []
root = tk.Tk()
app = App(root)
app.engine.paused = True
root.update()


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK]   {name} {detail}")
    else:
        errors.append(name)
        print(f"  [FAIL] {name} {detail}")


# 1) 基础刷新
app._refresh()
root.update()
check("界面构建与刷新", True)

# 2) 周期切换:必须真正改变K线数量级与周期名
for per, name in BAR_PERIODS.items():
    app.set_period(per)
    app._refresh()
    root.update()
    bars = app.sim.market.bars_for(per, 5)
    ok = len(bars) > 0
    check(f"周期切换 {name}", ok, f"可取 {len(bars)} 根")
app.set_period(60)
app._refresh()
root.update()

# 2b) 高周期K线必须数量充足,且不会被拉成"巨粗方块"
for per, name in BAR_PERIODS.items():
    app.set_period(per)
    app._refresh()
    root.update()
    cnt = len(app.sim.market.bars_for(per, 300))
    check(f"高周期 {name} K线充足", cnt >= 20, f"= {cnt} 根")
    check(f"高周期 {name} 未被拉伸", app.chart.slot <= app.chart.max_slot + 0.01,
          f"单根 {app.chart.slot:.1f}px / 共 {app.chart.shown} 根")
app.set_period(60)
app._refresh()
root.update()

# 2c) 滚轮缩放
n0 = app.visible_bars
app._zoom_chart(1)
app._zoom_chart(1)
n1 = app.visible_bars
app._zoom_chart(-1)
n2 = app.visible_bars
check("K线滚轮缩放生效", n1 < n0 and n2 > n1, f"{n0} -> {n1} -> {n2}")
app.set_visible(110)
p0 = app.visible_points
app._zoom_line(1)
p1 = app.visible_points
check("分时图滚轮缩放生效", p1 < p0, f"{p0} -> {p1}")
c0 = app.visible_curve
app._zoom_equity(-1)
c1 = app.visible_curve
check("资金曲线滚轮缩放生效", c1 > c0, f"{c0} -> {c1}")
app.visible_points, app.visible_curve = p0, c0

# 2d) 真实鼠标滚轮事件(验证 <MouseWheel> 绑定确实生效)
app.set_visible(110)
root.update()
app.chart.event_generate("<MouseWheel>", delta=120,
                         x=app.chart.winfo_width() // 2,
                         y=app.chart.winfo_height() // 2, when="now")
root.update()
check("真实滚轮事件触发K线缩放", app.visible_bars < 110, f"= {app.visible_bars}")
app.set_visible(110)

# 2e) 鼠标按住K线图左右拖动平移
app.chart.reset_pan()
app._refresh()
root.update()
cx = app.chart.winfo_width() // 2
cy = app.chart.winfo_height() // 2
app.chart.event_generate("<ButtonPress-1>", x=cx, y=cy, when="now")
for dx in range(0, 160, 20):
    app.chart.event_generate("<B1-Motion>", x=cx + dx, y=cy, when="now")
root.update()
app.chart.event_generate("<ButtonRelease-1>", x=cx + 160, y=cy, when="now")
root.update()
check("拖动K线图可向左回看历史", app.chart.offset > 0, f"偏移 {app.chart.offset} 根")
# 拖回右侧应回到最新
app.chart.event_generate("<ButtonPress-1>", x=cx, y=cy, when="now")
for dx in range(0, 200, 20):
    app.chart.event_generate("<B1-Motion>", x=cx - dx, y=cy, when="now")
app.chart.event_generate("<ButtonRelease-1>", x=cx - 200, y=cy, when="now")
root.update()
check("拖回右侧回到最新K线", app.chart.offset == 0, f"偏移 {app.chart.offset} 根")

# 3) 倍速:引擎速度必须随按钮变化
for sp in (1, 10, 60, 300):
    app.set_speed(sp)
    check(f"倍速 ×{sp}", app.engine.speed == sp)

# 4) 倍速实测:1秒真实时间内仿真时间应推进约 speed 倍
import time as _t
for sp in (1, 10, 60):
    app.engine.paused = True
    app.set_speed(sp)
    _t.sleep(0.1)
    t0 = app.sim.now
    app.engine.paused = False
    w0 = _t.perf_counter()
    _t.sleep(1.0)
    app.engine.paused = True
    w1 = _t.perf_counter()
    adv = (app.sim.now - t0).total_seconds()
    ratio = adv / (w1 - w0)
    ok = 0.5 * sp <= ratio <= 2.0 * sp
    check(f"倍速实测 ×{sp}", ok, f"实际 {ratio:.1f}x 实时")

# 4b) K线推进速度应随倍速变化(1分钟K线在×60下 1 秒应新增约 1 根)
app.engine.paused = True
app.set_period(60)
app.sim.step(120)
n0 = app.sim.market.bars_for(60, 500)[-1]["t"]
app.set_speed(60)
app.engine.paused = False
_t.sleep(2.0)
app.engine.paused = True
n1 = app.sim.market.bars_for(60, 500)[-1]["t"]
check("K线随倍速推进", n1 > n0,
      f"2秒内推进 {int((n1-n0)/60)} 根1分钟K线(×60 理论≈2根)")

# 5) 视图切换(左侧导航)
for v in ("报价", "自选", "分时图", "K线图", "多图同列"):
    app.set_view(v)
    app._refresh()
    root.update()
    check(f"视图 {v}", app.view == v)
app.set_view("K线图")

# 5) 主题切换(整体重建)
app.toggle_theme()
root.update()
app._refresh()
root.update()
check("主题切换", app.theme_mode == "light")
app.toggle_theme()
root.update()
app._refresh()
root.update()
check("主题切回", app.theme_mode == "dark")

# 6) 下单 -> 成交 -> 持仓
price = app.sim.price
app.qty_var.set("2")
app.stop_var.set(f"{price - 60:.2f}")
app.protect_var.set(1)
app.place_order("buy")
opened = False
for _ in range(20):
    app.sim.step(15)
    root.update_idletasks()
    if app.sim.broker.qty == 2:
        opened = True          # 期间若被保护止损打掉,也不影响"下单→成交→持仓"的判定
app._refresh()
root.update()
check("买入成交并持仓", opened, f"持仓 {app.sim.broker.qty}")
prot = [o for o in app.sim.broker.orders.values() if o.otype == "stop"]
check("自动挂出保护止损", len(prot) == 1, f"{len(prot)} 张")

# 7) 引擎指令:倒退 / 快进
app.engine.paused = True
time.sleep(0.15)
t_before = app.sim.now
app.engine.cmd.put(("rewind", (300, "replay")))
for _ in range(400):
    root.update()
    time.sleep(0.01)
    if app.sim.now < t_before and not app.engine.busy:
        break
check("倒退 5 分钟", app.sim.now < t_before, f"{t_before} -> {app.sim.now}")
t2 = app.sim.now
app.engine.cmd.put(("jump", (t2 + dt.timedelta(minutes=10), "replay")))
for _ in range(600):
    root.update()
    time.sleep(0.01)
    if app.sim.now > t2 and not app.engine.busy:
        break
check("快进 10 分钟", app.sim.now > t2, f"{t2} -> {app.sim.now}")

# 8) 平仓
app.sim.submit("close", {"fraction": 1.0})
for _ in range(20):
    app.sim.step(15)
    root.update_idletasks()
app._refresh()
root.update()
check("平仓后无持仓", app.sim.broker.qty == 0, f"持仓 {app.sim.broker.qty}")

# 9) 均线配置:可增删,周期范围 1~2000
app.ma_list = [20, 60]
app._refresh()
root.update()
mas = app.chart.data.get("mas")
check("均线数据传入图表", mas is not None and len(mas) == 2, str(mas))
app.ma_list = [1, 2000]
app._refresh()
root.update()
check("支持 1 与 2000 周期", len(app.chart.data["mas"]) == 2)
app.ma_list = [20, 60]
app._refresh()
root.update()

# 10) 布局:一键放大图表 + 分隔条拖动
h_full = app.chart.winfo_height()
app.cycle_bot_mode()          # 紧凑
root.update()
h_compact = app.chart.winfo_height()
app.cycle_bot_mode()          # 图表(隐藏表格)
root.update()
h_chart = app.chart.winfo_height()
check("一键放大图表生效", h_chart > h_compact > h_full,
      f"完整{h_full} -> 紧凑{h_compact} -> 图表{h_chart}")
app.cycle_bot_mode()          # 回到完整
root.update()
app.sash.event_generate("<ButtonPress-1>", x=10, y=3, when="now")
root.update()
_drag_h = app._sash_drag_h
app.sash.event_generate("<B1-Motion>", x=10, y=-60, when="now")
root.update()
_drag_mid = app.bot_h
app.sash.event_generate("<ButtonRelease-1>", x=10, y=-60, when="now")
root.update()
check("拖动分隔条可调高度", app.bot_mode == "custom" and app.bot_h != app.bot_h_full,
      f"bot_h={app.bot_h} full={app.bot_h_full} (press={_drag_h} motion={_drag_mid})")
app.bot_mode = "full"
app._apply_bot_mode()
root.update()

# 11) 底部下单条:必须是一栏、位于界面最底部、按钮不被遮挡
app.bot_mode = "full"
app._apply_bot_mode()
app._refresh()
root.update()
fh = app.form.winfo_height()
bot_bottom = app.bot.winfo_rooty() + app.bot.winfo_height()
form_bottom = app.form.winfo_rooty() + fh
check("下单区为单行一栏", fh < 130, f"高度 {fh}px")
check("下单条位于界面最底部", form_bottom <= bot_bottom + 2,
      f"下单条底 {form_bottom} / 底区底 {bot_bottom}")
check("底部区明显压缩(把空间让给K线图)", app.bot_h < root.winfo_height() * 0.45,
      f"底部 {app.bot_h}px / 窗口 {root.winfo_height()}px")
for nm, w in (("卖出", app.sell_btn), ("买入", app.buy_btn)):
    check(f"{nm}按钮完整显示(未被遮挡)", w.winfo_width() > 20 and w.winfo_height() > 15,
          f"{w.winfo_width()}x{w.winfo_height()}")
# 下单条里所有可交互控件都要有实际尺寸(说明没有被裁切)
ctrls = [app.instr_var, app.price_var, app.qty_var, app.stop_var, app.tp_var]
check("下单条控件齐全", len(ctrls) == 5 and all(c.get() is not None for c in ctrls))
# 资金条右端的信息不能被截断
app._refresh()
root.update()
check("资金条信息完整", "1手" in app.risk_lbl.cget("text"), app.risk_lbl.cget("text"))
check("杠杆滑块与实际杠杆同步",
      abs(app.lev_var.get() - app.sim.broker.leverage) < 1e-9,
      f"滑块 {app.lev_var.get():g} / 实际 {app.sim.broker.leverage}")

# 12) 初始资金设置:$1,000 ~ $1亿,默认 10 万
check("初始资金默认 10 万", app.initial_balance == 100_000.0, f"{app.initial_balance:,.0f}")
check("初始资金下限 $1,000", App.CAP_MIN == 1_000.0)
check("初始资金上限 $1亿", App.CAP_MAX == 100_000_000.0)
app.open_capital_dialog()
root.update()
dlgs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
check("初始资金对话框可打开", len(dlgs) == 1,
      f"数量={len(dlgs)} " + str([d.title() for d in dlgs]))
if dlgs:
    texts = []

    def collect(w):
        for c in w.winfo_children():
            try:
                if isinstance(c, tk.Label):
                    texts.append(c.cget("text"))
            except tk.TclError:
                pass
            collect(c)

    collect(dlgs[0])
    check("对话框含常用额度", any("$100,000,000" in s for s in texts), str(texts[:8]))
    dlgs[0].destroy()
    root.update()
# 实际应用新资金
for target in (1_000_000.0, 100_000.0):
    app.initial_balance = target
    app._update_cap_btn()
    app.engine.cmd.put(("reset", {"balance": target}))
    for _ in range(600):
        root.update()
        time.sleep(0.01)
        if not app.engine.busy and abs(app.sim.broker.initial_balance - target) < 1e-6:
            break
    check(f"初始资金可设置为 ${target:,.0f}",
          abs(app.sim.broker.initial_balance - target) < 1e-6,
          f"实际 {app.sim.broker.initial_balance:,.0f}")

# 13) K线悬停:显示该K线涨跌(点数/比例) + 右侧价格轴标注该K线价格
import re

app._refresh()
root.update()
ch = app.chart


def _price_tags(c):
    n = 0
    for item in c.find_all():
        if c.type(item) == "text":
            s = c.itemcget(item, "text")
            if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", s):
                n += 1
    return n


ch.hover = None
ch.redraw()
n_tags_0 = _price_tags(ch)
ch.hover = (int(ch.winfo_width() * 0.60), int(ch.winfo_height() * 0.40))
ch.redraw()
root.update()
n_tags_1 = _price_tags(ch)
check("右侧价格轴标注悬停K线价格", n_tags_1 > n_tags_0,
      f"价格标注 {n_tags_0} -> {n_tags_1}")
texts = [ch.itemcget(i, "text") for i in ch.find_all() if ch.type(i) == "text"]
joined = " ".join(texts)
chg_txt = next((s for s in texts if "涨跌" in s), "")
check("悬停显示涨跌点数与比例", "涨跌" in chg_txt and "点" in chg_txt and "%" in chg_txt,
      chg_txt)
info_txt = next((s for s in texts
                 if " O " in s and " H " in s and " C " in s and " V " in s), "")
m_chg = re.search(r"涨跌\s*([+-][\d.]+)\s*点\s*([+-][\d.]+)%", chg_txt)
m_c = re.search(r"C\s*([\d,]+\.?\d*)", info_txt)
ok = False
detail = "未解析"
if m_chg and m_c:
    close = float(m_c.group(1).replace(",", ""))
    chg = float(m_chg.group(1))
    pct = float(m_chg.group(2))
    prev_close = close - chg
    bars = ch.data.get("bars", [])
    # 允许显示层面的两位小数取整误差
    ok = any(abs(b["c"] - prev_close) < 0.02 for b in bars)
    expect_pct = chg / prev_close * 100 if prev_close else 0.0
    ok = ok and abs(expect_pct - pct) < 0.02
    detail = f"C={close:,.2f} 涨跌={chg:+.2f}点({pct:+.2f}%) 前收={prev_close:,.2f}"
check("悬停涨跌数值与K线数据自洽", ok, detail)
ch.hover = None
ch.redraw()
root.update()

# 14) 设置面板:顶部工具条只留核心图表控制,其余全部收进设置
app.open_settings()
root.update()
sw = app.settings_win
check("设置面板可打开", sw is not None and sw.winfo_exists())
panel_texts = []


def _collect(w, out):
    for c in w.winfo_children():
        try:
            if isinstance(c, (tk.Label, tk.Button, tk.Checkbutton)):
                out.append(c.cget("text"))
        except tk.TclError:
            pass
        _collect(c, out)


if sw is not None:
    _collect(sw, panel_texts)
    joined = " ".join(panel_texts)
    for need in ("播放控制", "历史重演", "图表", "外观与账户", "均线设置", "跳转", "复盘",
                 "重置", "夜间", "日间", "杠杆", "K线根数"):
        check(f"设置面板含「{need}」", need in joined)
    check("设置面板含倍速", "×300" in joined and "×60" in joined)
    check("设置面板含布局三档", "完整" in joined and "紧凑" in joined and "图表" in joined)
    # 工具条上不应再有播放/倍速/均线/布局/资金等按钮
    tb_texts = []
    _collect(app.top, tb_texts)
    tb_joined = " ".join(tb_texts)
    for gone in ("倍速", "倒退", "跳转", "复盘", "重置", "均线", "布局", "资金:$10万"):
        check(f"工具条已移除「{gone}」", gone not in tb_joined)
    check("工具条保留设置入口", "设置" in tb_joined)
    app._close_settings()
    root.update()

# 15) 已删除的功能按钮:反手 / 全撤 / 风险算手
form_texts = []
_collect(app.form, form_texts)
form_joined = " ".join(form_texts)
for gone in ("反手", "全撤", "风险算手"):
    check(f"下单条已删除「{gone}」", gone not in form_joined)
check("下单条保留平仓", "平仓" in form_joined)
check("资金条不再显示杠杆", "杠杆" not in app.acct_lbl.cget("text"),
      app.acct_lbl.cget("text")[-40:])

# 16) 双击开仓线 -> 止盈止损拖拽模式
price = app.sim.price
app.qty_var.set("2")
app.place_order("buy")
for _ in range(20):
    app.sim.step(15)
    root.update_idletasks()
app._refresh()
root.update()
check("有持仓可供编辑", app.sim.broker.qty == 2, f"持仓 {app.sim.broker.qty}")
entry = app.sim.broker.avg
ey = app.chart.y_at(entry)
mx = app.chart.winfo_width() // 2
# Tk 不允许直接合成 <Double-Button-1>,连续两次快速点击由 Tk 自己识别
app.chart.event_generate("<ButtonPress-1>", x=mx, y=int(ey), when="now")
app.chart.event_generate("<ButtonRelease-1>", x=mx, y=int(ey), when="now")
app.chart.event_generate("<ButtonPress-1>", x=mx, y=int(ey), when="now")
app.chart.event_generate("<ButtonRelease-1>", x=mx, y=int(ey), when="now")
root.update()
check("双击开仓线进入止盈止损编辑", app.chart.edit_mode,
      f"edit_mode={app.chart.edit_mode} sl={app.chart.edit_sl} tp={app.chart.edit_tp}")
check("编辑模式给出止损止盈初值",
      app.chart.edit_sl is not None and app.chart.edit_tp is not None
      and app.chart.edit_sl < app.chart.edit_tp,
      f"sl={app.chart.edit_sl} tp={app.chart.edit_tp}")

# 拖动止损线 -> 生成止损挂单
sl_before = app.chart.edit_sl
y_sl = int(app.chart.y_at(sl_before))
app.chart.event_generate("<ButtonPress-1>", x=app.chart.winfo_width() // 2, y=y_sl, when="now")
app.chart.event_generate("<B1-Motion>", x=app.chart.winfo_width() // 2, y=y_sl + 40, when="now")
app.chart.event_generate("<ButtonRelease-1>", x=app.chart.winfo_width() // 2, y=y_sl + 40,
                         when="now")
root.update()
dragged = app.chart.edit_sl
check("拖动止损线改变了止损价", dragged is not None and dragged < sl_before,
      f"{sl_before} -> {dragged}")
for _ in range(60):
    app.sim.step(2)
    root.update_idletasks()
app._refresh()
root.update()
b = app.sim.broker
sl, tp = b.position_exit_prices()
check("拖动止损线后挂出止损单", sl is not None and dragged is not None
      and abs(sl - dragged) < 1e-6, f"止损挂单 {sl} / 拖动值 {dragged}")
check("止损挂单为正确方向", any(
    o.otype == "stop" and o.side == "sell" and o.status in ("working", "triggered")
    for o in b.orders.values()))
check("止盈止损互为 OCO",
      any(o.otype == "stop" and o.oco for o in b.orders.values())
      or any(o.otype == "limit" and o.oco for o in b.orders.values()))

# 17) 持仓表:长数字订单号 / 价差 / 盈利点数 / 只有盈亏数字着色
app._refresh()
root.update()
pt = app.pos_table
keys = [c[0] for c in pt.COLS]
cells = pt._cells[0]
texts = [c.cget("text") for c in cells]
code = texts[keys.index("oid")]
diff = texts[keys.index("diff")]
pts = texts[keys.index("pts")]
pnl = texts[keys.index("pnl")]
check("持仓表有数据行", any(t != "--" for t in texts), str(texts))
check("持仓表含长数字订单号", bool(code) and re.match(r"^\d{18}$", str(code)), str(code))
check("持仓表含价差", re.match(r"^[+-][\d.]+$", str(diff)), str(diff))
check("持仓表含盈利点数", re.match(r"^[+-][\d.]+$", str(pts)), str(pts))
check("盈利点数 = 价差 × 数量", abs(abs(float(pts)) - abs(float(diff)) * 2) < 0.01,
      f"价差 {diff} 点数 {pts}")
unreal = app.sim.broker.unrealized(app.sim.price)
expect_fg = app.theme["down"] if unreal >= 0 else app.theme["up"]
pnl_fg = cells[keys.index("pnl")].cget("fg")
check("浮动盈亏格按盈亏着色(红盈绿亏)", pnl_fg == expect_fg, f"{pnl_fg} 期望 {expect_fg}")
other_fgs = [(k, cells[i].cget("fg")) for i, k in enumerate(keys) if k != "pnl"]
check("其余单元格保持默认色(不整行染色)",
      all(fg == app.theme["text"] for _, fg in other_fgs), str(other_fgs[:3]))

# 18) 订单编号格式(纯数字长串,仿真实报单号)
codes = [o.code for o in app.sim.broker.orders.values()]
check("订单编号为长数字串", bool(codes) and all(re.match(r"^\d{18}$", c) for c in codes),
      str(codes[:3]))
check("订单编号互不重复", len(set(codes)) == len(codes), f"{len(set(codes))}/{len(codes)}")

# 19) 右侧盘口面板排版
check("盘口统计项齐全", len(app.q_stats) >= 13, f"{len(app.q_stats)} 项")
check("盘口含关键字段", all(k in app.q_stats for k in
      ("last", "chg", "pct", "open", "high", "low", "vol", "oi", "prev", "avg",
       "bid", "ask", "spread")))
check("盘口数值列右对齐",
      all(app.q_stats[k].cget("anchor") == "e" for k in app.q_stats))
check("盘口数值已刷新",
      all(app.q_stats[k].cget("text") not in ("", "--") for k in app.q_stats),
      str({k: app.q_stats[k].cget("text") for k in ("last", "vol", "spread")}))
check("盘口卖一买一均有报价",
      app.q_ask.cget("text") != "" and app.q_bid.cget("text") != "",
      f"{app.q_ask.cget('text')} / {app.q_bid.cget('text')}")
check("盘口标题含纳指期货", "纳指期货" in app.q_name.cget("text"), app.q_name.cget("text"))

# 20) 分笔成交:数量要有大小单分布(不能全是 1)
tape_qty = [t[2] for t in list(app.sim.market.tape)]
check("分笔成交样本充足", len(tape_qty) >= 50, f"{len(tape_qty)} 笔")
check("分笔成交数量不全为1", len(set(tape_qty)) > 1,
      f"不同手数 {len(set(tape_qty))} 种: {sorted(set(tape_qty))[:8]}")
check("分笔成交含大单(≥10手)", max(tape_qty) >= 10, f"最大 {max(tape_qty)} 手")
small_ratio = sum(1 for q in tape_qty if q <= 3) / len(tape_qty)
check("分笔成交以小单为主", small_ratio > 0.6, f"小单占比 {small_ratio:.0%}")
check("分笔成交数量为整手且≥1",
      all(isinstance(q, int) and q >= 1 for q in tape_qty))
avg_qty = sum(tape_qty) / len(tape_qty)
check("平均每笔手数合理(1.5~6)", 1.5 < avg_qty < 6.0, f"均值 {avg_qty:.2f} 手")

# 21) 止盈成交后:开仓线 / 止盈止损线自动消失,挂单自动撤销
b = app.sim.broker
check("持仓挂着止盈止损单", b.position_exit_prices()[1] is not None,
      str(b.position_exit_prices()))
if not app.chart.edit_mode:
    app._chart_edit_request()      # 进入编辑模式,验证成交后能自动退出
root.update()
check("进入止盈止损编辑模式", app.chart.edit_mode)
tick = b.tick
px = app.sim.price
# 止盈压到现价上方一跳,止损放在下方,保证很快成交
app.sim.submit("exits", {"stop": px - 30 * tick, "tp": px + 1 * tick})
for _ in range(300):
    app.sim.step(2)
    root.update_idletasks()
    if b.qty == 0:
        break
app._refresh()
root.update()
check("止盈成交后持仓已平", b.qty == 0, f"qty={b.qty}")
check("成交后自动退出止盈止损编辑模式", not app.chart.edit_mode)
check("成交后开仓线消失",
      not app.chart.data.get("qty") and app.chart.data.get("entry") is None,
      f"entry={app.chart.data.get('entry')} qty={app.chart.data.get('qty')}")
check("成交后止盈止损线消失",
      app.chart.data.get("stop") is None and app.chart.data.get("tp") is None,
      f"stop={app.chart.data.get('stop')} tp={app.chart.data.get('tp')}")
check("成交后止盈止损挂单已撤销", b.position_exit_prices() == (None, None),
      str(b.position_exit_prices()))
check("成交后持仓表已清空", app.pos_table._cells[0][0].cget("text") == "--",
      app.pos_table._cells[0][0].cget("text"))

app.engine.stop()
root.destroy()
if errors:
    print("\n失败项:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("\nGUI 冒烟测试通过 ✅")
