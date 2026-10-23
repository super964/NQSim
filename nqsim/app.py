"""
纳斯达克100 期货交易终端 (NQ Trading Terminal)
==============================================

专业期货交易终端布局:
  * 顶部:周期按钮组 / 缩放 / 播放 / 倍速 / 倒退重演 / 主题
  * 中部:左侧功能导航 + 居中行情图(铺满) + 右侧盘口报价与分笔成交
  * 底部:资金状态栏 + 交易下单区 + 持仓/委托/成交/统计

仿真内核:CME 真实交易日历、逐笔行情与K线、真实撮合滑点、
保证金与杠杆风控、可回退重演的资金曲线。
"""

from __future__ import annotations

import datetime as dt
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from .account import Order
from .fmt import (fmt_duration, fmt_money, fmt_price, fmt_qty, fmt_signed_money,
                  fmt_time)
from .market import BAR_PERIODS, INSTRUMENTS, ET
from .sim import Simulation
from .theme import THEMES, get_theme
from . import uidefs
from .widgets import CandleChart, EquityChart, LineChart, PosTable

FONT = "Microsoft YaHei UI"
FONT_NUM = "Consolas"

NAV_ITEMS = ["报价", "自选", "分时图", "K线图", "多图同列"]
# K线图拖动回看时额外预取的历史K线根数
PAN_WINDOW = 600


class Engine(threading.Thread):
    """后台推进仿真时钟的线程。"""

    def __init__(self, sim: Simulation):
        super().__init__(daemon=True)
        self.sim = sim
        self.running = True
        self.paused = True
        self.speed = 10
        self.cmd: "queue.Queue" = queue.Queue()
        self.busy = ""
        self.progress = 0.0
        self.error = ""

    def stop(self):
        self.running = False

    def run(self):
        last = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            dt_real = now - last
            last = now
            while True:
                try:
                    cmd, args = self.cmd.get_nowait()
                except queue.Empty:
                    break
                self._handle(cmd, args)
            if not self.paused and not self.busy:
                try:
                    self.sim.step(min(120.0, dt_real * self.speed))
                except Exception as e:              # pragma: no cover
                    self.error = f"{type(e).__name__}: {e}"
                    self.paused = True
            time.sleep(0.006)

    def _handle(self, cmd, args):
        self.busy = ""
        self.progress = 0.0
        try:
            if cmd == "pause":
                self.paused = args
            elif cmd == "speed":
                self.speed = args
            elif cmd == "step":
                self.sim.step(args)
            elif cmd == "rewind":
                secs, mode = args
                self.busy = "倒退重演…"
                target = self.sim.now - dt.timedelta(seconds=secs)
                ok = self.sim.rewind_to(target, mode,
                                        progress=lambda p, s: setattr(self, "progress", p))
                if not ok:
                    self.error = f"可回退历史不足({int(self.sim.history_span()//60)}分钟)"
            elif cmd == "jump":
                target, mode = args
                self.busy = "快进…"
                if target > self.sim.now:
                    self.sim.jump_forward(target, progress=lambda p, s: setattr(self, "progress", p))
                else:
                    ok = self.sim.rewind_to(target, mode,
                                            progress=lambda p, s: setattr(self, "progress", p))
                    if not ok:
                        self.error = f"可回退历史不足({int(self.sim.history_span()//60)}分钟)"
            elif cmd == "reset":
                self.busy = "重建行情…"
                self.sim.reset(**args)
                self.sim.fast_prefill(days=190, recent_hours=72.0)
                self.sim.step(1800)
                self.paused = True
        except Exception as e:                  # pragma: no cover
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.busy = ""
            self.progress = 0.0


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.theme_mode = "dark"
        self.theme = get_theme(self.theme_mode)
        self.instr_key = "NQ"
        self.initial_balance = 100_000.0   # 初始资金(可调 $1,000 ~ $1亿)
        self.sim = Simulation(self.instr_key, seed=20260101, balance=self.initial_balance,
                              leverage=20, bar_period=60, start=default_start())
        self.engine = Engine(self.sim)
        self.visible_bars = 110
        self.visible_points = 240      # 分时图可见点数
        self.visible_curve = 600       # 资金曲线可见点数
        self.period = 60
        self.ma_list = [20, 60]        # 均线周期(1~2000)
        self.ma_colors = ["accent2", "accent", "warn", "up", "down", "flat"]
        self.order_side = "buy"
        self.order_type = "market"
        self.view = "K线图"
        self.bot_mode = "full"         # 底部区布局: full | compact | chart | custom
        self._cfg_cache = {}
        # 设置面板(顶部工具条只留核心图表控制,其余集中在此)
        self.settings_win = None
        self.pause_btn = None
        self.mode_btn = None
        self.ma_btn = None
        self.cap_btn = None
        self.lev_scale = None
        self.lev_lbl = None
        self.visible_lbl = None
        self.speed_btns = []
        self.layout_btns = []
        self.theme_btns = []
        self.lev_var = tk.DoubleVar(value=20)   # 杠杆(滑块在设置面板里)
        # 预填真实历史行情:数月历史,保证任意周期都有完整K线形态
        self.sim.fast_prefill(days=190, recent_hours=72.0)
        # 再跑 30 分钟逐笔行情:盘口/分笔即时可用,倒退功能开局即有可回退区间
        self.sim.step(1800)
        self.engine.speed = 10
        self.engine.paused = False
        self.engine.start()
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._build()
        self._apply_theme()
        self.set_speed(10)
        self._refresh()
        self.root.after(80, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F2>", lambda e: self.toggle_theme())
        self.root.bind("<F3>", lambda e: self.toggle_pause())
        self.root.bind("<Escape>", lambda e: self._chart_edit_exit())
        # 在图表外松开鼠标也要提交拖动结果
        self.root.bind("<ButtonRelease-1>", self._commit_all_pans)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build(self):
        t = self.theme
        self.root.title("纳指期货交易终端  |  NQ Trading Terminal")
        self.root.configure(bg=t["bg"])
        # 按真实 DPI 布局:逻辑尺寸固定,物理像素随屏幕缩放放大(高分屏不再被拉伸变糊)
        uidefs.detect(self.root)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(uidefs.px(1580), sw - uidefs.px(20))
        h = min(uidefs.px(990), sh - uidefs.px(40))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(min(uidefs.px(1200), sw - uidefs.px(40)),
                          min(uidefs.px(760), sh - uidefs.px(60)))
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.container = tk.Frame(self.root, bg=t["bg"])
        self.container.grid(row=0, column=0, sticky="nsew")
        self._build_all(self.container)

    def _build_all(self, p):
        t = self.theme
        # ---------------- 顶部工具条 ----------------
        top = tk.Frame(p, bg=t["panel"], highlightthickness=1, highlightbackground=t["border"])
        top.grid(row=0, column=0, sticky="ew")
        self.top = top
        self._build_toolbar(top)
        # ---------------- 主体 ----------------
        mid = tk.Frame(p, bg=t["bg"])
        mid.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        p.grid_rowconfigure(1, weight=1)
        p.grid_columnconfigure(0, weight=1)

        nav = tk.Frame(mid, bg=t["panel"], highlightthickness=1, highlightbackground=t["border"])
        nav.grid(row=0, column=0, sticky="nsw")
        self.nav = nav
        self._build_nav(nav)

        center = tk.Frame(mid, bg=t["bg"])
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_rowconfigure(0, weight=1)
        self.center = center
        self._build_center(center)

        quote = tk.Frame(mid, bg=t["panel"], highlightthickness=1, highlightbackground=t["border"],
                         width=uidefs.px(318))
        quote.grid(row=0, column=2, sticky="nse")
        quote.grid_propagate(False)
        self.quote = quote
        self._build_quote(quote)

        # ---------------- 底部交易区 ----------------
        # 可拖动分隔条:上下自由调整"图表区 / 底部交易区"的比例
        sash = tk.Frame(p, bg=t["border"], height=uidefs.px(7), cursor="sb_v_double_arrow")
        sash.grid(row=2, column=0, sticky="ew", padx=4)
        self.sash = sash
        for i in range(3):
            tk.Frame(sash, bg=t["panel3"], height=uidefs.px(1),
                     width=uidefs.px(26)).grid(row=i, column=0, pady=uidefs.px(1))
        sash.bind("<ButtonPress-1>", self._sash_press)
        sash.bind("<B1-Motion>", self._sash_drag)
        sash.bind("<ButtonRelease-1>", self._sash_release)

        bot = tk.Frame(p, bg=t["panel"], highlightthickness=1, highlightbackground=t["border"])
        bot.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 4))
        p.grid_rowconfigure(3, weight=0)
        self.bot = bot
        self._build_bottom(bot)
        bot.grid_propagate(False)
        self.bot_h = 0                   # 稍后按实际内容测量
        self.bot_h_full = 0
        self._sash_drag_y = 0
        self._sash_drag_h = 0
        # 测量各区块高度并套用当前布局模式
        self.root.update_idletasks()
        self._measure_bot()
        # 工具条上的初始资金按钮(重建界面后也要同步)
        self._update_cap_btn()

    # ------------------------------------------------------------------
    # 布局:图表区 / 底部交易区 的比例
    # ------------------------------------------------------------------
    def _measure_bot(self):
        """测量底部各区块高度,并套用当前布局模式。"""
        self.h_acct = self.acct_bar.winfo_reqheight()
        self.h_form = self.form.winfo_reqheight()
        self.h_nb = self.nb.winfo_reqheight()
        self.h_nb_min = uidefs.px(96)          # 紧凑档:表格只露 3 行
        self.bot_h_full = self.h_acct + self.h_nb + self.h_form + uidefs.px(8)
        self.bot_h_min = self.h_acct + self.h_form + uidefs.px(8)
        self._apply_bot_mode()

    def _apply_bot_mode(self):
        """底部区布局档位。

        full    : 资金条 + 持仓/委托/成交表格(自然高度) + 底部下单条
        compact : 资金条 + 矮表格(约 3 行)              + 底部下单条
        chart   : 资金条 + 底部下单条(表格隐藏,图表最大化)
        custom  : 拖动分隔条得到的自定义高度
        """
        mode = self.bot_mode
        if mode == "full":
            self.nb.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
            self.form.grid(row=2, column=0, sticky="ew")
            h = self.bot_h_full
        elif mode == "compact":
            self.nb.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
            self.form.grid(row=2, column=0, sticky="ew")
            h = self.h_acct + self.h_nb_min + self.h_form + uidefs.px(8)
        elif mode == "chart":
            self.nb.grid_remove()
            self.form.grid(row=2, column=0, sticky="ew")
            h = self.bot_h_min
        else:                              # custom:拖动分隔条得到
            h = max(self.bot_h_min, self.bot_h)
        self.bot_h = h
        try:
            self.bot.configure(height=h)
        except tk.TclError:
            pass
        self._sync_settings()

    def cycle_bot_mode(self):
        """一键切换图表区占比:完整 -> 紧凑 -> 图表 -> 完整。"""
        order = ["full", "compact", "chart"]
        cur = self.bot_mode if self.bot_mode in order else "full"
        self.bot_mode = order[(order.index(cur) + 1) % len(order)]
        self._apply_bot_mode()
        names = {"full": "完整(表格+下单条)", "compact": "紧凑(矮表格+下单条)",
                 "chart": "图表(仅下单条,表格隐藏)"}
        self._set_status("图表布局:" + names[self.bot_mode])

    def set_bot_height(self, h: int):
        lo = getattr(self, "bot_h_min", uidefs.px(60))
        hi = uidefs.px(1400)
        h = max(lo, min(hi, int(h)))
        self.bot_h = h
        try:
            self.bot.configure(height=h)
        except tk.TclError:
            pass

    def _sash_press(self, e):
        self._sash_drag_y = e.y
        self._sash_drag_h = self.bot_h

    def _sash_drag(self, e):
        self.set_bot_height(self._sash_drag_h - (e.y - self._sash_drag_y))

    def _sash_release(self, e):
        self.bot_mode = "custom"
        # 拖动即恢复完整底部(表格 + 下单条),再按新高度重排
        self.nb.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.form.grid(row=2, column=0, sticky="ew")
        self._sync_settings()
        self._set_status(f"底部区高度 {self.bot_h}px(可拖动分隔条调整)")

    # ---------------- 顶部 ----------------
    def _build_toolbar(self, p):
        t = self.theme
        # 控件区与报价条分开(用 pack 而非同一 grid,避免报价条宽度挤压按钮)
        self.ctrls = tk.Frame(p, bg=t["panel"])
        self.ctrls.pack(fill="x")
        p = self.ctrls
        self.title_lbl = tk.Label(p, text="纳指期货交易终端", bg=t["panel"], fg=t["text"],
                                  font=(FONT, 15, "bold"))
        self.title_lbl.grid(row=0, column=0, sticky="w", padx=10, pady=6)
        # 周期按钮组
        self.period_btns = []
        for per, name in BAR_PERIODS.items():
            b = tk.Button(p, text=name, width=4, bg=t["panel3"], fg=t["text"], relief="flat",
                          bd=0, font=(FONT, 11), command=lambda s=per: self.set_period(s))
            b.grid(row=0, column=1 + len(self.period_btns), padx=1)
            self.period_btns.append((b, per))
        # 缩放
        tk.Button(p, text="－", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12), command=lambda: self.set_visible(self.visible_bars - 20)).grid(
            row=0, column=11, padx=(10, 1))
        tk.Button(p, text="＋", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12), command=lambda: self.set_visible(self.visible_bars + 20)).grid(
            row=0, column=12, padx=(1, 6))
        # 设置面板入口:播放控制 / 历史重演 / 均线 / 布局 / 主题 / 资金 / 杠杆 全部收在这里
        self.settings_btn = tk.Button(p, text="⚙ 设置", width=9, bg=t["accent"], fg="#ffffff",
                                      relief="flat", bd=0, font=(FONT, 12, "bold"),
                                      command=self.open_settings)
        self.settings_btn.grid(row=0, column=13, padx=(14, 2))
        self.status_lbl = tk.Label(p, text="就绪", bg=t["panel"], fg=t["dim"], font=(FONT, 11))
        self.status_lbl.grid(row=0, column=14, sticky="e", padx=6)
        p.grid_columnconfigure(14, weight=1)

        # 报价条(独立 pack,不影响控件区布局)
        self.quote_bar = tk.Frame(self.top, bg=t["panel"])
        self.quote_bar.pack(fill="x")
        qb = self.quote_bar
        self.price_lbl = tk.Label(qb, text="--", bg=t["panel"], fg=t["text"],
                                  font=(FONT_NUM, 26, "bold"))
        self.price_lbl.pack(side="left", padx=(10, 4), pady=2)
        self.chg_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["dim"],
                                font=(FONT_NUM, 13, "bold"))
        self.chg_lbl.pack(side="left", padx=2)
        self.bid_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["bid"],
                                font=(FONT_NUM, 13))
        self.bid_lbl.pack(side="left", padx=(16, 2))
        self.ask_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["ask"],
                                font=(FONT_NUM, 13))
        self.ask_lbl.pack(side="left", padx=2)
        self.spread_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["dim"],
                                   font=(FONT_NUM, 11))
        self.spread_lbl.pack(side="left", padx=(4, 0))
        self.time_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["text"],
                                 font=(FONT_NUM, 15, "bold"))
        self.time_lbl.pack(side="right", padx=(10, 4))
        self.session_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["dim"], font=(FONT, 11))
        self.session_lbl.pack(side="right", padx=2)
        self.theme_btn = tk.Button(qb, text="主题: 夜间", bg=t["panel3"], fg=t["text"],
                                   activebackground=t["border"], relief="flat", bd=0,
                                   font=(FONT, 11), padx=10, pady=2, command=self.toggle_theme)
        self.theme_btn.pack(side="right", padx=6)
        self.event_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["warn"], font=(FONT, 11))
        self.event_lbl.pack(side="right", padx=6)
        self.busy_lbl = tk.Label(qb, text="", bg=t["panel"], fg=t["warn"], font=(FONT, 11))
        self.busy_lbl.pack(side="right", padx=4)

    # ---------------- 左侧导航 ----------------
    def _build_nav(self, p):
        t = self.theme
        self.nav_btns = []
        for i, name in enumerate(NAV_ITEMS):
            b = tk.Button(p, text=name, width=9, bg=t["panel"], fg=t["dim"], relief="flat",
                          bd=0, font=(FONT, 11), command=lambda n=name: self.set_view(n))
            b.grid(row=i, column=0, sticky="ew", padx=2, pady=1)
            self.nav_btns.append((b, name))
        p.grid_columnconfigure(0, weight=1)

    # ---------------- 中部行情区 ----------------
    def _build_center(self, p):
        t = self.theme
        self.chart = CandleChart(p, t)
        self.line_chart = LineChart(p, t)
        self.eq_chart = EquityChart(p, t, height=200)
        self.view_quote = tk.Frame(p, bg=t["bg"])
        self.view_watch = tk.Frame(p, bg=t["bg"])
        self._build_watch(self.view_watch)
        self._build_quote_view(self.view_quote)
        self.center_panes = [self.chart, self.line_chart, self.view_quote, self.view_watch]
        # 滚轮缩放
        self.chart.on_zoom = self._zoom_chart
        self.line_chart.on_zoom = self._zoom_line
        self.eq_chart.on_zoom = self._zoom_equity
        # 鼠标拖动平移(向左回看历史)
        self.chart.on_pan = self._pan_chart
        self.line_chart.on_pan = self._pan_line
        self.eq_chart.on_pan = self._pan_equity
        # 双击开仓线 -> 在K线图上拖拽设置止盈止损
        self.chart.on_edit_request = self._chart_edit_request
        self.chart.on_exit_drag = self._chart_exit_set
        self.set_view("K线图")

    def _build_quote_view(self, p):
        t = self.theme
        self.qv_labels = {}
        rows = [("最新", "last"), ("涨跌", "chg"), ("涨幅", "pct"), ("开盘", "open"),
                ("最高", "high"), ("最低", "low"), ("昨结", "prev"), ("总量", "vol"),
                ("现手", "last_vol"), ("持仓", "oi"), ("均价", "avg"), ("买一", "bid"),
                ("卖一", "ask"), ("价差", "spread")]
        for i, (name, key) in enumerate(rows):
            r, c = divmod(i, 2)
            tk.Label(p, text=name, bg=t["bg"], fg=t["dim"], font=(FONT, 12)).grid(
                row=r * 2, column=c * 2, sticky="w",
                padx=(uidefs.px(20 + c * 150), uidefs.px(4)), pady=(uidefs.px(8 if r == 0 else 2), 0))
            v = tk.Label(p, text="--", bg=t["bg"], fg=t["text"], font=(FONT_NUM, 15, "bold"),
                         anchor="e")
            v.grid(row=r * 2, column=c * 2 + 1, sticky="e",
                   padx=(uidefs.px(4), uidefs.px(20 + (1 - c) * 120)),
                   pady=(uidefs.px(8 if r == 0 else 2), 0))
            self.qv_labels[key] = v

    def _build_watch(self, p):
        t = self.theme
        tk.Label(p, text="自选列表", bg=t["bg"], fg=t["text"], font=(FONT, 15, "bold")).pack(
            anchor="w", padx=20, pady=(14, 8))
        self.watch_tree = ttk.Treeview(p, columns=("code", "last", "chg", "vol"),
                                       show="headings", height=8, style="Dark.Treeview")
        for c, w, txt in (("code", 130, "代码"), ("last", 110, "最新"),
                          ("chg", 90, "涨跌"), ("vol", 110, "总量")):
            self.watch_tree.heading(c, text=txt)
            self.watch_tree.column(c, width=uidefs.px(int(w * 1.15)), anchor="center")
        self.watch_tree.pack(fill="x", padx=20)

    # ---------------- 右侧盘口 ----------------
    def _hline(self, parent, row: int) -> int:
        """一条细分隔线,返回下一可用行号。"""
        f = tk.Frame(parent, bg=self.theme["border"], height=uidefs.px(1))
        f.grid(row=row, column=0, sticky="ew", padx=10, pady=uidefs.px(3))
        return row + 1

    def _build_quote(self, p):
        """右侧纳指期货盘口面板(按真实交易面板排版)。

        结构:合约抬头 / 交易时段 / 卖出·买入 / 两列对齐的行情统计 / 分笔成交。
        统计区用 4 列网格(名称-数值-名称-数值),数值列右对齐,读数不歪。
        """
        t = self.theme
        p.grid_columnconfigure(0, weight=1)
        r = 0
        self.q_name = tk.Label(p, text="", bg=t["panel"], fg=t["warn"],
                               font=(FONT, 15, "bold"), anchor="w")
        self.q_name.grid(row=r, column=0, sticky="ew", padx=12, pady=(8, 0))
        r += 1
        self.q_sub = tk.Label(p, text="", bg=t["panel"], fg=t["dim"],
                              font=(FONT, 10), anchor="w")
        self.q_sub.grid(row=r, column=0, sticky="ew", padx=12)
        r += 1
        self.q_session = tk.Label(p, text="", bg=t["panel"], fg=t["dim"],
                                  font=(FONT_NUM, 11), anchor="w")
        self.q_session.grid(row=r, column=0, sticky="ew", padx=12, pady=(1, 0))
        r += 1
        r = self._hline(p, r)
        # ---- 卖一 / 买一 ----
        self.q_ask = tk.Label(p, text="", bg=t["panel"], fg=t["ask"],
                              font=(FONT_NUM, 16, "bold"), anchor="w")
        self.q_ask.grid(row=r, column=0, sticky="ew", padx=12, pady=(1, 0))
        r += 1
        self.q_bid = tk.Label(p, text="", bg=t["panel"], fg=t["bid"],
                              font=(FONT_NUM, 16, "bold"), anchor="w")
        self.q_bid.grid(row=r, column=0, sticky="ew", padx=12, pady=(1, 0))
        r += 1
        r = self._hline(p, r)
        # ---- 行情统计(两列,数值右对齐) ----
        stat = tk.Frame(p, bg=t["panel"])
        stat.grid(row=r, column=0, sticky="ew", padx=4, pady=(0, 0))
        stat.grid_columnconfigure(1, weight=1)
        stat.grid_columnconfigure(3, weight=1)
        self.q_stats = {}
        rows = [("最新", "last"), ("涨跌", "chg"), ("涨幅", "pct"), ("开盘", "open"),
                ("最高", "high"), ("最低", "low"), ("总量", "vol"), ("持仓", "oi"),
                ("昨结", "prev"), ("均价", "avg"), ("买一", "bid"), ("卖一", "ask"),
                ("价差", "spread")]
        for i, (name, key) in enumerate(rows):
            rr, cc = divmod(i, 2)
            lab = tk.Label(stat, text=name, bg=t["panel"], fg=t["dim"],
                           font=(FONT, 11), anchor="w")
            val = tk.Label(stat, text="--", bg=t["panel"], fg=t["text"],
                           font=(FONT_NUM, 12), anchor="e")
            if cc == 0:
                lab.grid(row=rr, column=0, sticky="w", padx=(8, 3), pady=1)
                val.grid(row=rr, column=1, sticky="e", padx=(3, 14), pady=1)
            else:
                lab.grid(row=rr, column=2, sticky="w", padx=(2, 3), pady=1)
                val.grid(row=rr, column=3, sticky="e", padx=(3, 8), pady=1)
            self.q_stats[key] = val
        r += 1
        r = self._hline(p, r)
        # ---- 分笔成交 ----
        tk.Label(p, text="分笔成交", bg=t["panel"], fg=t["text"],
                 font=(FONT, 12, "bold"), anchor="w").grid(
            row=r, column=0, sticky="ew", padx=12, pady=(0, 2))
        r += 1
        self.tape_tree = ttk.Treeview(p, columns=("t", "px", "qty", "dir"), show="headings",
                                      height=18, style="Tape.Treeview")
        for c, w, txt in (("t", 56, "时间"), ("px", 78, "价格"), ("qty", 44, "数量"),
                          ("dir", 44, "方向")):
            self.tape_tree.heading(c, text=txt)
            self.tape_tree.column(c, width=uidefs.px(int(w * 1.2)), anchor="center")
        self.tape_tree.grid(row=r, column=0, sticky="nsew", padx=8, pady=(0, 8))
        p.grid_rowconfigure(r, weight=1)

    # ---------------- 底部交易区 ----------------
    def _build_bottom(self, p):
        t = self.theme
        p.grid_columnconfigure(0, weight=1)
        # --- 资金状态栏 ---
        self.acct_bar = tk.Frame(p, bg=t["panel3"])
        self.acct_bar.grid(row=0, column=0, sticky="ew")
        self.acct_lbl = tk.Label(self.acct_bar, text="", bg=t["panel3"], fg=t["text"],
                                 font=(FONT_NUM, 12), anchor="w")
        self.acct_lbl.pack(side="left", padx=10, pady=3)
        # 单手名义 / 保证金 / 往返成本(放在资金条右端,给下方单行下单条腾出宽度)
        self.risk_lbl = tk.Label(self.acct_bar, text="", bg=t["panel3"], fg=t["dim"],
                                 font=(FONT_NUM, 11), anchor="e")
        self.risk_lbl.pack(side="right", padx=10, pady=3)
        # --- 持仓/委托/成交/统计 ---
        nb = ttk.Notebook(p)
        nb.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.nb = nb
        p.grid_rowconfigure(1, weight=1)
        # 持仓明细:自绘网格表格(只有"浮动盈亏"那一格按盈亏着色,整行不变色)
        pos_frame = tk.Frame(nb, bg=t["panel"])
        nb.add(pos_frame, text="持仓")
        self.pos_table = PosTable(pos_frame, t)
        self.pos_table.pack(fill="both", expand=True, padx=6, pady=6)
        self.orders_tree = self._mk_tree(nb, "委托", (("id", 118, "订单号"), ("type", 70, "类型"),
                                                      ("dir", 50, "方向"), ("qty", 60, "数量"),
                                                      ("price", 80, "价格"), ("status", 70, "状态")))
        self.trades_tree = self._mk_tree(nb, "成交", (("t", 80, "时间"), ("dir", 60, "方向"),
                                                       ("qty", 60, "数量"), ("px", 80, "价格"),
                                                       ("amt", 90, "金额")))
        eq_frame = tk.Frame(nb, bg=t["panel"])
        nb.add(eq_frame, text="资金曲线")
        self.eq_tab_chart = EquityChart(eq_frame, t, height=190)
        self.eq_tab_chart.on_zoom = self._zoom_equity
        self.eq_tab_chart.pack(fill="both", expand=True, padx=4, pady=4)
        self.eq_tab_info = tk.Label(eq_frame, text="", bg=t["panel"], fg=t["dim"], font=(FONT_NUM, 11))
        self.eq_tab_info.pack(anchor="e", padx=8)
        stats_frame = tk.Frame(nb, bg=t["panel"])
        nb.add(stats_frame, text="交易统计")
        self.stat_labels = {}
        rows = [("净值", "equity"), ("净盈亏", "net"), ("收益率", "ret"), ("已实现", "realized"),
                ("浮动盈亏", "unreal"), ("手续费", "comm"), ("隔夜利息", "fund"), ("最大回撤", "dd"),
                ("交易笔数", "trades"), ("胜率", "winrate"), ("盈亏比", "pf"), ("平均持仓", "hold"),
                ("在市时间", "tim"), ("年化夏普", "sharpe"), ("强平次数", "liq")]
        for i, (name, key) in enumerate(rows):
            rr, cc = divmod(i, 3)
            tk.Label(stats_frame, text=name, bg=t["panel"], fg=t["dim"], font=(FONT, 11)).grid(
                row=rr * 2, column=cc * 2, sticky="w", padx=(14, 2), pady=(6 if rr == 0 else 1))
            v = tk.Label(stats_frame, text="--", bg=t["panel"], fg=t["text"], font=(FONT_NUM, 12, "bold"),
                         anchor="e")
            v.grid(row=rr * 2, column=cc * 2 + 1, sticky="e", padx=(2, 14), pady=(6 if rr == 0 else 1))
            self.stat_labels[key] = v
        # --- 交易下单条:整幅界面最底部的一栏(横向排开) ---
        form = tk.Frame(p, bg=t["panel"])
        form.grid(row=2, column=0, sticky="ew")
        self.form = form
        self._build_form(form)

    def _mk_tree(self, parent, title, cols):
        f = tk.Frame(parent, bg=self.theme["panel"])
        tree = ttk.Treeview(f, columns=[c[0] for c in cols], show="headings", height=6,
                            style="Dark.Treeview")
        for key, w, txt in cols:
            tree.heading(key, text=txt)
            tree.column(key, width=uidefs.px(int(w * 1.18)), anchor="center",
                        stretch=(key in ("px", "amt", "price", "pnl", "diff", "pts")))
        # 盈利红色 / 亏损绿色(中文行情习惯)
        tree.tag_configure("win", foreground=self.theme["down"])
        tree.tag_configure("lose", foreground=self.theme["up"])
        tree.pack(fill="both", expand=True, padx=4, pady=4)
        parent.add(f, text=title)
        return tree

    def _build_form(self, p):
        """交易下单条:整幅界面最下方的一栏,所有下单控件横向排在一行里。

        竖排的表单会吃掉大量图表高度,因此改为单行;控件宽度经过收敛,
        在 150% 缩放的 2560 宽屏上也能完整排下,不会被裁切。
        """
        t = self.theme
        FL = (FONT, 11)
        FN = (FONT_NUM, 13)

        def sep():
            tk.Frame(p, bg=t["border"], width=uidefs.px(1), height=uidefs.px(24)).pack(
                side="left", fill="y", padx=uidefs.px(7))

        def lbl(txt, padx=(0, 4)):
            tk.Label(p, text=txt, bg=t["panel"], fg=t["dim"], font=FL).pack(side="left", padx=padx)

        # ---- 合约 ----
        lbl("合约", (10, 4))
        self.instr_var = tk.StringVar(value=self.instr_key)
        cb = ttk.Combobox(p, textvariable=self.instr_var, values=list(INSTRUMENTS.keys()),
                          state="readonly", width=5, style="Dark.TCombobox")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._on_instr)
        sep()

        # ---- 委托价 + 委托价格 ----
        lbl("委托价")
        self.price_var = tk.StringVar(value="市价")
        self.type_cb = ttk.Combobox(p, textvariable=self.price_var, state="readonly", width=7,
                                    values=["市价", "限价", "止损市价", "止损限价"],
                                    style="Dark.TCombobox")
        self.type_cb.pack(side="left")
        self.type_cb.bind("<<ComboboxSelected>>", self._on_type)
        self.limit_var = tk.StringVar(value="")
        self.limit_entry = tk.Entry(p, textvariable=self.limit_var, width=8, bg=t["panel3"],
                                    fg=t["dim"], insertbackground=t["text"], relief="flat",
                                    font=FN, justify="right")
        self.limit_entry.pack(side="left", padx=(6, 0))
        sep()

        # ---- 数量(手) ----
        lbl("数量")
        qrow = tk.Frame(p, bg=t["panel"])
        qrow.pack(side="left")
        tk.Button(qrow, text="－", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12), command=lambda: self.bump_qty(-1)).pack(side="left")
        self.qty_var = tk.StringVar(value="1")
        self.qty_entry = tk.Entry(qrow, textvariable=self.qty_var, justify="center", width=4,
                                  bg=t["panel3"], fg=t["text"], insertbackground=t["text"],
                                  relief="flat", font=FN)
        self.qty_entry.pack(side="left", padx=2)
        tk.Button(qrow, text="＋", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12), command=lambda: self.bump_qty(1)).pack(side="left")
        sep()

        # ---- 止损价 / 止盈价 ----
        lbl("止损价")
        self.stop_var = tk.StringVar(value="")
        self.stop_entry = tk.Entry(p, textvariable=self.stop_var, width=9, bg=t["panel3"],
                                   fg=t["text"], insertbackground=t["text"], relief="flat",
                                   font=FN, justify="right")
        self.stop_entry.pack(side="left")
        lbl("止盈价", (8, 4))
        self.tp_var = tk.StringVar(value="")
        self.tp_entry = tk.Entry(p, textvariable=self.tp_var, width=9, bg=t["panel3"], fg=t["text"],
                                 insertbackground=t["text"], relief="flat", font=FN, justify="right")
        self.tp_entry.pack(side="left")
        sep()

        # ---- 自动挂止损 ----
        self.protect_var = tk.IntVar(value=0)
        self.protect_chk = tk.Checkbutton(p, text="自动挂止损", variable=self.protect_var,
                                          bg=t["panel"], fg=t["dim"], selectcolor=t["panel3"],
                                          activebackground=t["panel"], activeforeground=t["text"],
                                          font=FL, anchor="w", highlightthickness=0, bd=0)
        self.protect_chk.pack(side="left", padx=(0, 2))
        sep()

        # ---- 买卖按钮(仿专业终端:按钮上带盘口价) ----
        self.sell_btn = tk.Button(p, text="卖出", bg=t["down"], fg="#ffffff", relief="flat",
                                  bd=0, width=9, font=(FONT, 12, "bold"), height=1, pady=1,
                                  command=lambda: self.place_order("sell"))
        self.sell_btn.pack(side="left", padx=(0, 2))
        self.buy_btn = tk.Button(p, text="买入", bg=t["up"], fg="#ffffff", relief="flat",
                                 bd=0, width=9, font=(FONT, 12, "bold"), height=1, pady=1,
                                 command=lambda: self.place_order("buy"))
        self.buy_btn.pack(side="left", padx=(2, 0))
        sep()

        # ---- 平仓(反手 / 全撤 / 风险算手 已按需移除) ----
        tk.Button(p, text="平仓", bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 13), height=1, pady=5,
                  command=self.close_position).pack(side="left", padx=1)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def toggle_theme(self):
        self.set_theme("light" if self.theme_mode == "dark" else "dark")

    def _apply_theme(self):
        t = self.theme
        st = self.style
        st.configure(".", background=t["panel"], foreground=t["text"], fieldbackground=t["panel3"],
                     borderwidth=0, font=(FONT, 11))
        st.configure("TFrame", background=t["panel"])
        st.configure("TLabel", background=t["panel"], foreground=t["text"])
        st.configure("TButton", background=t["panel3"], foreground=t["text"], borderwidth=0,
                     focuscolor=t["panel3"], padding=(6, 3))
        st.map("TButton", background=[("active", t["border"]), ("pressed", t["border"])])
        st.configure("TNotebook", background=t["panel"], borderwidth=0, tabmargins=0)
        st.configure("TNotebook.Tab", background=t["panel3"], foreground=t["dim"], padding=(10, 3))
        st.map("TNotebook.Tab", background=[("selected", t["panel"])], foreground=[("selected", t["text"])])
        st.configure("Dark.TCombobox", fieldbackground=t["panel3"], background=t["panel3"],
                     foreground=t["text"], arrowcolor=t["text"], bordercolor=t["border"])
        st.map("Dark.TCombobox", fieldbackground=[("readonly", t["panel3"])],
               foreground=[("readonly", t["text"])], background=[("readonly", t["panel3"])],
               selectbackground=[("readonly", t["panel3"])], selectforeground=[("readonly", t["text"])])
        st.configure("Dark.Treeview", background=t["panel2"], fieldbackground=t["panel2"],
                     foreground=t["text"], bordercolor=t["border"], rowheight=uidefs.px(26))
        st.map("Dark.Treeview", background=[("selected", t["accent"])],
               foreground=[("selected", "#ffffff")])
        # 分笔成交用更紧凑的行高(真实盘口成交列表更密)
        st.configure("Tape.Treeview", background=t["panel2"], fieldbackground=t["panel2"],
                     foreground=t["text"], bordercolor=t["border"], rowheight=uidefs.px(21))
        st.map("Tape.Treeview", background=[("selected", t["accent"])],
               foreground=[("selected", "#ffffff")])
        st.configure("Dark.Treeview.Heading", background=t["panel3"], foreground=t["dim"],
                     relief="flat", borderwidth=0, padding=(2, 3))
        st.map("Dark.Treeview.Heading", background=[("active", t["border"])])
        st.configure("Tape.Treeview.Heading", background=t["panel3"], foreground=t["dim"],
                     relief="flat", borderwidth=0, padding=(2, 2))
        st.map("Tape.Treeview.Heading", background=[("active", t["border"])])
        self.root.configure(bg=t["bg"])
        for w in (self.top, self.quote_bar, self.acct_bar, self.form, self.nav, self.quote):
            try:
                w.configure(bg=t["panel"] if w is not self.acct_bar else t["panel3"])
            except tk.TclError:
                pass
        self.chart.configure(bg=t["bg"])
        self.line_chart.configure(bg=t["bg"])
        self.eq_chart.configure(bg=t["bg"])
        self.view_quote.configure(bg=t["bg"])
        self.view_watch.configure(bg=t["bg"])
        self.theme_btn.configure(text="主题: 夜间" if self.theme_mode == "dark" else "主题: 日间",
                                 bg=t["panel3"], fg=t["text"], activebackground=t["border"])
        for w in (self.title_lbl, self.time_lbl, self.session_lbl, self.status_lbl,
                  self.busy_lbl, self.event_lbl, self.spread_lbl):
            try:
                w.configure(bg=t["panel"], fg=t["text"] if w in (self.title_lbl, self.time_lbl)
                            else t["dim"])
            except tk.TclError:
                pass
        self.price_lbl.configure(bg=t["panel"])
        self.chg_lbl.configure(bg=t["panel"])
        self.bid_lbl.configure(bg=t["panel"], fg=t["bid"])
        self.ask_lbl.configure(bg=t["panel"], fg=t["ask"])
        self.acct_lbl.configure(bg=t["panel3"], fg=t["text"])
        for w in (self.limit_entry, self.stop_entry, self.tp_entry, self.qty_entry):
            w.configure(bg=t["panel3"], fg=t["text"], insertbackground=t["text"])
        if self.lev_scale is not None:
            self.lev_scale.configure(bg=t["accent"], fg=t["dim"], troughcolor=t["panel3"],
                                     activebackground=t["accent"],
                                     highlightbackground=t["panel2"])
        self.protect_chk.configure(bg=t["panel"], fg=t["dim"], activebackground=t["panel"],
                                   selectcolor=t["panel3"], activeforeground=t["text"])
        self.sell_btn.configure(bg=t["down"], fg="#ffffff", activebackground=t["down"])
        self.buy_btn.configure(bg=t["up"], fg="#ffffff", activebackground=t["up"])
        self.q_name.configure(bg=t["panel"], fg=t["warn"])
        self.q_sub.configure(bg=t["panel"], fg=t["dim"])
        self.q_session.configure(bg=t["panel"], fg=t["dim"])
        for lbl in self.q_stats.values():
            lbl.configure(bg=t["panel"], fg=t["text"])
        self.q_ask.configure(bg=t["panel"], fg=t["ask"])
        self.q_bid.configure(bg=t["panel"], fg=t["bid"])
        self.pos_table.apply_theme(t)

    def toggle_pause(self):
        self.engine.paused = not self.engine.paused
        self._sync_settings()
        self._set_status("已暂停" if self.engine.paused else f"运行中 ×{self.engine.speed}")

    def set_speed(self, sp: int):
        self.engine.speed = sp
        self._sync_settings()
        self._set_status(f"倍速 ×{sp}")

    def set_period(self, per: int):
        self.period = per
        self.sim.bar_period = per
        self.sim.market.bar_period = per
        self.chart.reset_pan()          # 切换周期回到最新
        for b, val in self.period_btns:
            b.configure(bg=self.theme["accent"] if val == per else self.theme["panel3"],
                        fg="#ffffff" if val == per else self.theme["text"])
        self._set_status(f"周期 {BAR_PERIODS.get(per, '-')}")

    def set_visible(self, n: int):
        self.visible_bars = max(20, min(600, n))
        self.chart.redraw()

    # -- 滚轮缩放 ---------------------------------------------------------
    @staticmethod
    def _zoom_factor(delta: int) -> float:
        """向上滚(放大)可见数量减少,向下滚(缩小)可见数量增加。"""
        return 0.82 if delta > 0 else 1.22

    def _zoom_chart(self, delta: int):
        """K线图缩放:滚轮向上放大(少显示几根),向下缩小(多显示几根)。"""
        n = int(round(self.visible_bars * self._zoom_factor(delta)))
        self.set_visible(max(20, min(600, n)))

    def _zoom_line(self, delta: int):
        """分时图缩放。"""
        self.visible_points = max(30, min(1500, int(round(self.visible_points *
                                                         self._zoom_factor(delta)))))
        self.line_chart.redraw()
        self._set_status(f"分时可见 {self.visible_points} 点")

    def _zoom_equity(self, delta: int):
        """资金曲线缩放。"""
        self.visible_curve = max(30, min(5000, int(round(self.visible_curve *
                                                        self._zoom_factor(delta)))))
        self.eq_chart.redraw()
        self.eq_tab_chart.redraw()
        self._set_status(f"资金曲线可见 {self.visible_curve} 点")

    def _pan_chart(self, offset: int):
        """K线图拖动平移后回调:扩展历史窗口并刷新。"""
        self.chart.offset = max(0, offset)
        self._refresh()
        if self.chart.offset:
            self._set_status(f"K线已向左回看 {self.chart.offset} 根(拖回右侧回到最新)")
        else:
            self._set_status("K线已回到最新")

    def _pan_line(self, offset: int):
        """分时图拖动平移。"""
        self.line_chart.offset = max(0, offset)
        self._set_status(f"分时已向左回看 {self.line_chart.offset} 点"
                         if self.line_chart.offset else "分时已回到最新")

    def _pan_equity(self, offset: int):
        """资金曲线拖动平移。"""
        self.eq_chart.offset = max(0, offset)
        self.eq_tab_chart.offset = max(0, offset)
        self._set_status(f"资金曲线已向左回看 {self.eq_chart.offset} 点"
                         if self.eq_chart.offset else "资金曲线已回到最新")

    def set_view(self, name: str):
        self.view = name
        for w in self.center_panes:
            try:
                w.pack_forget()
            except tk.TclError:
                pass
        if name == "K线图":
            self.chart.pack(fill="both", expand=True)
        elif name == "分时图":
            self.line_chart.pack(fill="both", expand=True)
        elif name == "报价":
            self.view_quote.pack(fill="both", expand=True)
        elif name == "自选":
            self.view_watch.pack(fill="both", expand=True)
        else:   # 多图同列
            self.chart.pack(fill="both", expand=True, side="top")
            self.eq_chart.pack(fill="both", expand=True, side="top")
        for b, nm in self.nav_btns:
            b.configure(bg=self.theme["accent"] if nm == name else self.theme["panel"],
                        fg="#ffffff" if nm == name else self.theme["dim"])

    def toggle_mode(self):
        self.sim.rewind_mode = "redo" if self.sim.rewind_mode == "replay" else "replay"
        self._sync_settings()

    def step_bar(self):
        self.engine.cmd.put(("step", float(self.period)))

    def _on_type(self, *_):
        m = {"市价": "market", "限价": "limit", "止损市价": "stop", "止损限价": "stop_limit"}
        self.order_type = m.get(self.price_var.get(), "market")
        # 只有限价类委托才需要填委托价格,其余情况置灰提示
        need = self.order_type in ("limit", "stop_limit")
        try:
            self.limit_entry.configure(fg=self.theme["text"] if need else self.theme["dim"])
        except tk.TclError:
            pass

    def _on_lev_change(self, val):
        lev = max(1, int(round(float(val))))
        self.lev_var.set(lev)
        if self.lev_lbl is not None:
            self.lev_lbl.configure(text=f"{lev}x")
        self.sim.submit("leverage", {"leverage": lev})

    def _on_instr(self, *_):
        key = self.instr_var.get()
        if key == self.instr_key:
            return
        self.instr_key = key
        self.sim.instr = key
        self.engine.cmd.put(("reset", {"seed": int(time.time()) % 100000,
                                       "base_price": INSTRUMENTS[key]["base_price"]}))

    def bump_qty(self, d: int):
        try:
            q = float(self.qty_var.get())
        except ValueError:
            q = 1.0
        self.qty_var.set(str(max(1, int(q + d))))

    def _num(self, var, default=0.0) -> float:
        try:
            return float(str(var.get()).replace(",", "").strip())
        except (ValueError, tk.TclError):
            return default

    def place_order(self, side: Optional[str] = None):
        side = side or self.order_side
        qty = max(1, int(self._num(self.qty_var, 1)))
        otype = self.order_type
        limit = self._num(self.limit_var, 0.0) or None
        stop = self._num(self.stop_var, 0.0) or None
        tp = self._num(self.tp_var, 0.0) or None
        if otype in ("limit", "stop_limit") and not limit:
            self._set_status("请填写委托价")
            return
        if otype in ("stop", "stop_limit") and not stop:
            self._set_status("请填写止损价")
            return
        protect = stop if self.protect_var.get() else None
        if self.protect_var.get() and not stop:
            self._set_status("请先填写止损价")
            return
        data = {"side": side, "otype": otype, "qty": qty, "limit": limit, "stop": stop,
                "protect": protect}
        if tp:
            # 止盈:成交后挂一张限价平仓单(OCO 联动)
            data["oco_limit"] = qty
            data["oco_limit_price"] = tp
        self.sim.submit("order", data)
        self._set_status(f"已提交 {'买入' if side=='buy' else '卖出'} {qty} 手 {self.price_var.get()}")

    def close_position(self):
        self.sim.submit("close", {"fraction": 1.0})
        self._set_status("已提交平仓")

    # ------------------------------------------------------------------
    # K线图上拖拽设置止盈 / 止损
    # ------------------------------------------------------------------
    def _chart_edit_request(self):
        """双击开仓线 -> 进入止盈止损拖拽编辑模式(再次双击退出)。"""
        if self.chart.edit_mode:
            self._chart_edit_exit()
            return
        b = self.sim.broker
        if b.qty == 0:
            self._set_status("当前没有持仓,无法设置止盈止损")
            return
        stop, tp = b.position_exit_prices()
        price = self.sim.price
        tick = b.tick
        if stop is None:
            stop = round(price * 0.997 / tick) * tick
        if tp is None:
            tp = round(price * 1.003 / tick) * tick
        self.chart.edit_sl = stop
        self.chart.edit_tp = tp
        self.chart.edit_mode = True
        self.chart.redraw()
        self._set_status("拖动红线设止损 · 拖动绿线设止盈 · 再次双击开仓线退出")

    def _chart_exit_set(self, kind: str, price: float):
        """拖动止盈/止损线结束 -> 提交挂单。"""
        b = self.sim.broker
        if b.qty == 0:
            return
        sl = self.chart.edit_sl
        tp = self.chart.edit_tp
        if sl is None and tp is None:
            return
        # 方向校验:多头止损在下、止盈在上;空头相反
        price_now = self.sim.price
        if sl is not None and tp is not None:
            if b.qty > 0 and sl >= tp:
                sl = min(sl, tp - b.tick)
            elif b.qty < 0 and sl <= tp:
                sl = max(sl, tp + b.tick)
        self.sim.submit("exits", {"stop": sl, "tp": tp})
        name = "止损" if kind == "sl" else "止盈"
        self._set_status(f"已设置{name} {price:,.2f}(与另一单互为 OCO)")

    def _chart_edit_exit(self):
        """退出止盈止损编辑模式。"""
        self.chart.edit_mode = False
        self.chart.edit_sl = None
        self.chart.edit_tp = None
        self.chart.redraw()
        self._set_status("已退出止盈止损编辑")

    # ------------------------------------------------------------------
    # 初始资金设置
    # ------------------------------------------------------------------
    CAP_MIN = 1_000.0
    CAP_MAX = 100_000_000.0
    CAP_PRESETS = [1_000.0, 10_000.0, 100_000.0, 500_000.0,
                   1_000_000.0, 10_000_000.0, 100_000_000.0]

    @staticmethod
    def _cap_text(v: float) -> str:
        """把金额压缩成按钮上显示的短文本。"""
        v = float(v)
        if v >= 100_000_000:
            return f"资金:${v/100_000_000:g}亿"
        if v >= 10_000:
            return f"资金:${v/10_000:g}万"
        return f"资金:${v:,.0f}"

    def _update_cap_btn(self):
        if self.cap_btn is None:
            return
        try:
            self.cap_btn.configure(text=self._cap_text(self.initial_balance))
        except tk.TclError:
            pass

    def open_capital_dialog(self):
        """设置初始资金:$1,000 ~ $1亿,可手动输入或点选常用额度。"""
        win = tk.Toplevel(self.root)
        win.title("初始资金设置")
        t = self.theme
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        var = tk.StringVar(value=f"{self.initial_balance:.0f}")

        tk.Label(win, text="初始资金 (USD)", bg=t["panel"], fg=t["text"],
                 font=(FONT, 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w",
                                               padx=14, pady=(12, 4))
        ent = tk.Entry(win, textvariable=var, justify="right", width=20, bg=t["panel3"],
                       fg=t["text"], insertbackground=t["text"], relief="flat",
                       font=(FONT_NUM, 14))
        ent.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 2))
        ent.select_range(0, "end")
        tk.Label(win, text="可手动输入 · 范围 $1,000 ~ $100,000,000",
                 bg=t["panel"], fg=t["dim"], font=(FONT, 10)).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        tk.Label(win, text="常用额度", bg=t["panel"], fg=t["dim"], font=(FONT, 11)).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 4))
        pf = tk.Frame(win, bg=t["panel"])
        pf.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14)
        for i, v in enumerate(self.CAP_PRESETS):
            rr, cc = divmod(i, 4)
            tk.Button(pf, text=f"${v:,.0f}", bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                      font=(FONT_NUM, 10),
                      command=lambda x=v: var.set(f"{x:.0f}")).grid(
                row=rr, column=cc, sticky="ew", padx=1, pady=1)
            pf.grid_columnconfigure(cc, weight=1)

        msg = tk.Label(win, text="", bg=t["panel"], fg=t["bad"], font=(FONT, 10))
        msg.grid(row=5, column=0, columnspan=2, sticky="w", padx=14, pady=(6, 0))

        def apply():
            raw = var.get().replace(",", "").replace("$", "").strip()
            try:
                v = float(raw)
            except ValueError:
                msg.configure(text="请输入有效数字")
                return
            if v < self.CAP_MIN or v > self.CAP_MAX:
                msg.configure(text=f"超出范围:${self.CAP_MIN:,.0f} ~ ${self.CAP_MAX:,.0f}")
                return
            self.initial_balance = v
            self._update_cap_btn()
            self.engine.cmd.put(("reset", {"balance": v}))
            self._set_status(f"初始资金已设为 ${v:,.0f},账户已重置")
            win.destroy()

        brow = tk.Frame(win, bg=t["panel"])
        brow.grid(row=6, column=0, columnspan=2, sticky="e", padx=14, pady=(10, 12))
        tk.Button(brow, text="取消", bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12), command=win.destroy).pack(side="left", padx=(0, 6))
        tk.Button(brow, text="确定", bg=t["accent"], fg="#ffffff", relief="flat", bd=0,
                  font=(FONT, 12, "bold"), command=apply).pack(side="left")
        win.grab_set()
        ent.focus_set()

    # ------------------------------------------------------------------
    # 设置面板(顶部工具条只保留核心图表控制,其余全部收进这里)
    # ------------------------------------------------------------------
    def _close_settings(self):
        win = getattr(self, "settings_win", None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.settings_win = None
        self.speed_btns = []
        self.layout_btns = []
        self.theme_btns = []
        self.pause_btn = None
        self.mode_btn = None
        self.ma_btn = None
        self.cap_btn = None
        self.lev_scale = None
        self.lev_lbl = None
        self.visible_lbl = None

    def open_settings(self):
        """打开设置面板:播放控制 / 历史重演 / 图表 / 外观与账户。"""
        self._close_settings()
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("设置")
        t = self.theme
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        outer = tk.Frame(win, bg=t["panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        def section(row, title):
            f = tk.Frame(outer, bg=t["panel2"], highlightthickness=1,
                         highlightbackground=t["border"])
            f.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            tk.Label(f, text=title, bg=t["panel2"], fg=t["accent"],
                     font=(FONT, 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
            body = tk.Frame(f, bg=t["panel2"])
            body.pack(fill="x", padx=12, pady=(0, 10))
            return body

        def cap(txt, cmd, width=6, accent=False):
            return tk.Button(body_of[0], text=txt, width=width,
                             bg=t["accent"] if accent else t["panel3"],
                             fg="#ffffff" if accent else t["text"],
                             relief="flat", bd=0, font=(FONT, 12), command=cmd)

        body_of = [None]

        # ---------- 播放控制 ----------
        body = section(0, "▶   播放控制")
        body_of[0] = body
        self.pause_btn = cap("▶ 播放", self.toggle_pause, width=9, accent=True)
        self.pause_btn.pack(side="left", padx=(0, 4))
        cap("⏭ 单步", self.step_bar, width=9).pack(side="left", padx=4)
        tk.Label(body, text="倍速", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(16, 4))
        self.speed_btns = []
        for sp in (1, 5, 10, 30, 60, 120, 300):
            bb = tk.Button(body, text=f"×{sp}", width=4, bg=t["panel3"], fg=t["text"],
                           relief="flat", bd=0, font=(FONT_NUM, 11),
                           command=lambda s=sp: self.set_speed(s))
            bb.pack(side="left", padx=1)
            self.speed_btns.append((bb, sp))

        # ---------- 历史重演 ----------
        body = section(1, "⟲   历史重演")
        body_of[0] = body
        tk.Label(body, text="倒退", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(0, 4))
        for lab, secs in (("-1分", 60), ("-5分", 300), ("-15分", 900), ("-1时", 3600)):
            tk.Button(body, text=lab, width=5, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                      font=(FONT_NUM, 11),
                      command=lambda s=secs: self.engine.cmd.put(("rewind", (s, self.sim.rewind_mode)))
                      ).pack(side="left", padx=1)
        cap("跳转", self.open_jump, width=7).pack(side="left", padx=(14, 2))
        self.mode_btn = cap("复盘", self.toggle_mode, width=7)
        self.mode_btn.pack(side="left", padx=2)
        cap("重置", self.reset_sim, width=7).pack(side="left", padx=2)

        # ---------- 图表 ----------
        body = section(2, "▤   图表")
        body_of[0] = body
        self.ma_btn = cap("均线设置", self.open_ma_dialog, width=9)
        self.ma_btn.pack(side="left", padx=(0, 4))
        tk.Label(body, text="布局", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(14, 4))
        self.layout_btns = []
        for mode, name in (("full", "完整"), ("compact", "紧凑"), ("chart", "图表")):
            bb = tk.Button(body, text=name, width=6, bg=t["panel3"], fg=t["text"], relief="flat",
                           bd=0, font=(FONT, 12), command=lambda m=mode: self.set_bot_mode(m))
            bb.pack(side="left", padx=1)
            self.layout_btns.append((bb, mode))
        tk.Label(body, text="K线根数", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(16, 4))
        tk.Button(body, text="－", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12),
                  command=lambda: self.set_visible(self.visible_bars - 20)).pack(side="left")
        self.visible_lbl = tk.Label(body, text=str(self.visible_bars), width=4, bg=t["panel2"],
                                    fg=t["text"], font=(FONT_NUM, 12, "bold"))
        self.visible_lbl.pack(side="left")
        tk.Button(body, text="＋", width=2, bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 12),
                  command=lambda: self.set_visible(self.visible_bars + 20)).pack(side="left")

        # ---------- 外观与账户 ----------
        body = section(3, "◑   外观与账户")
        body_of[0] = body
        tk.Label(body, text="主题", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(0, 4))
        self.theme_btns = []
        for mode, name in (("dark", "夜间"), ("light", "日间")):
            bb = tk.Button(body, text=name, width=6, bg=t["panel3"], fg=t["text"], relief="flat",
                           bd=0, font=(FONT, 12), command=lambda m=mode: self.set_theme(m))
            bb.pack(side="left", padx=1)
            self.theme_btns.append((bb, mode))
        self.cap_btn = tk.Button(body, text="资金:$10万", width=11, bg=t["panel3"], fg=t["text"],
                                 relief="flat", bd=0, font=(FONT_NUM, 12),
                                 command=self.open_capital_dialog)
        self.cap_btn.pack(side="left", padx=(18, 4))
        tk.Label(body, text="杠杆", bg=t["panel2"], fg=t["dim"], font=(FONT, 11)).pack(
            side="left", padx=(6, 4))
        self.lev_scale = tk.Scale(body, from_=1, to=100, orient="horizontal",
                                  variable=self.lev_var, showvalue=False, bg=t["accent"],
                                  fg=t["dim"], troughcolor=t["panel3"], activebackground=t["accent"],
                                  highlightthickness=0, bd=0, sliderrelief="flat",
                                  highlightbackground=t["panel2"], length=uidefs.px(150),
                                  command=self._on_lev_change, font=(FONT_NUM, 10))
        self.lev_scale.pack(side="left")
        self.lev_lbl = tk.Label(body, text="", bg=t["panel2"], fg=t["accent"],
                                font=(FONT_NUM, 12, "bold"), width=6, anchor="w")
        self.lev_lbl.pack(side="left", padx=(6, 0))

        self._sync_settings()

    def _sync_settings(self):
        """把设置面板里的控件状态同步为当前实际状态。"""
        t = self.theme
        try:
            self._sync_settings_inner(t)
        except tk.TclError:      # 面板已被关闭(控件失效)
            pass

    def _sync_settings_inner(self, t):
        if self.pause_btn is not None:
            playing = not self.engine.paused
            self.pause_btn.configure(text="⏸ 暂停" if playing else "▶ 播放",
                                     bg=t["accent"] if playing else t["panel3"],
                                     fg="#ffffff" if playing else t["text"])
        for b, sp in self.speed_btns:
            b.configure(bg=t["accent"] if self.engine.speed == sp else t["panel3"],
                        fg="#ffffff" if self.engine.speed == sp else t["text"])
        if self.mode_btn is not None:
            redo = self.sim.rewind_mode == "redo"
            self.mode_btn.configure(text="重来" if redo else "复盘",
                                    bg=t["accent"] if redo else t["panel3"],
                                    fg="#ffffff" if redo else t["text"])
        for b, mode in self.layout_btns:
            cur = self.bot_mode if self.bot_mode != "custom" else "full"
            b.configure(bg=t["accent"] if mode == cur else t["panel3"],
                        fg="#ffffff" if mode == cur else t["text"])
        for b, mode in self.theme_btns:
            cur = self.theme_mode
            b.configure(bg=t["accent"] if mode == cur else t["panel3"],
                        fg="#ffffff" if mode == cur else t["text"])
        if self.cap_btn is not None:
            self.cap_btn.configure(text=self._cap_text(self.initial_balance))
        if self.lev_lbl is not None:
            self.lev_lbl.configure(text=f"{self.lev_var.get():g}x")
        if getattr(self, "visible_lbl", None) is not None:
            self.visible_lbl.configure(text=str(self.visible_bars))

    def set_bot_mode(self, mode: str):
        """直接切换底部区布局档位。"""
        self.bot_mode = mode
        self._apply_bot_mode()
        self._sync_settings()
        names = {"full": "完整(表格+下单条)", "compact": "紧凑(矮表格+下单条)",
                 "chart": "图表(仅下单条,表格隐藏)"}
        self._set_status("图表布局:" + names.get(mode, mode))

    def set_theme(self, mode: str):
        """切换日间 / 夜间主题(整体重建界面)。"""
        if mode == self.theme_mode:
            return
        reopen = getattr(self, "settings_win", None) is not None
        self._close_settings()
        self.theme_mode = mode
        self.theme = get_theme(self.theme_mode)
        form = {"qty": self.qty_var.get(), "limit": self.limit_var.get(),
                "stop": self.stop_var.get(), "tp": self.tp_var.get(),
                "type": self.price_var.get(), "lev": self.lev_var.get(),
                "protect": self.protect_var.get(), "instr": self.instr_var.get()}
        for child in self.container.winfo_children():
            child.destroy()
        self._cfg_cache.clear()
        self._build_all(self.container)
        self._apply_theme()
        self.qty_var.set(form["qty"])
        self.limit_var.set(form["limit"])
        self.stop_var.set(form["stop"])
        self.tp_var.set(form["tp"])
        self.price_var.set(form["type"])
        self.lev_var.set(form["lev"])
        self.protect_var.set(form["protect"])
        self.instr_var.set(form["instr"])
        self._on_type()
        self.set_speed(self.engine.speed)
        self.set_period(self.period)
        self.set_view(self.view)
        self._refresh()
        if reopen:
            self.open_settings()

    # ------------------------------------------------------------------
    # 均线设置
    # ------------------------------------------------------------------
    def open_ma_dialog(self):
        """均线参数设置:周期 1~2000,可增删。"""
        win = tk.Toplevel(self.root)
        win.title("均线设置")
        t = self.theme
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="均线周期(1 ~ 2000)", bg=t["panel"], fg=t["text"],
                 font=(FONT, 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w",
                                               padx=14, pady=(12, 6))
        listf = tk.Frame(win, bg=t["panel"])
        listf.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14)
        rows = {"items": []}

        def refresh_rows():
            for w in rows["items"]:
                w.destroy()
            rows["items"] = []
            if not self.ma_list:
                tk.Label(listf, text="(未启用均线)", bg=t["panel"], fg=t["faint"],
                         font=(FONT, 11)).grid(row=0, column=0, columnspan=3, sticky="w")
                return
            for i, per in enumerate(self.ma_list):
                col = self.ma_colors[i % len(self.ma_colors)]
                tk.Label(listf, text=f"MA{per}", bg=t["panel"], fg=t.get(col, t["text"]),
                         font=(FONT_NUM, 12, "bold")).grid(row=i, column=0, sticky="w", padx=(0, 14))
                # 上移/下移
                tk.Button(listf, text="↑", width=2, bg=t["panel3"], fg=t["text"], relief="flat",
                          bd=0, font=(FONT, 11),
                          command=lambda i=i: self._move_ma(i, -1, refresh_rows)).grid(
                              row=i, column=1, padx=1)
                tk.Button(listf, text="↓", width=2, bg=t["panel3"], fg=t["text"], relief="flat",
                          bd=0, font=(FONT, 11),
                          command=lambda i=i: self._move_ma(i, 1, refresh_rows)).grid(
                              row=i, column=2, padx=1)
                tk.Button(listf, text="删除", width=4, bg=t["panel3"], fg=t["text"], relief="flat",
                          bd=0, font=(FONT, 11),
                          command=lambda i=i: self._del_ma(i, refresh_rows)).grid(
                              row=i, column=3, padx=(6, 0))

        refresh_rows()

        addf = tk.Frame(win, bg=t["panel"])
        addf.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(10, 0))
        tk.Label(addf, text="新增周期", bg=t["panel"], fg=t["dim"], font=(FONT, 11)).pack(side="left")
        var = tk.StringVar()
        ent = tk.Entry(addf, textvariable=var, width=8, justify="center", bg=t["panel3"],
                       fg=t["text"], insertbackground=t["text"], relief="flat", font=(FONT_NUM, 13))
        ent.pack(side="left", padx=6)
        ent.bind("<Return>", lambda e: do_add())

        def do_add():
            raw = var.get().strip()
            try:
                n = int(raw)
            except ValueError:
                messagebox.showerror("错误", "请输入 1~2000 的整数", parent=win)
                return
            if n < 1 or n > 2000:
                messagebox.showerror("错误", "均线周期范围为 1 ~ 2000", parent=win)
                return
            if n in self.ma_list:
                messagebox.showinfo("提示", f"MA{n} 已存在", parent=win)
                return
            if len(self.ma_list) >= 6:
                messagebox.showinfo("提示", "最多同时显示 6 条均线", parent=win)
                return
            self.ma_list.append(n)
            self.ma_list.sort()
            var.set("")
            refresh_rows()
            self._refresh()
            self._set_status(f"已添加 MA{n}")

        tk.Button(addf, text="添加", bg=t["accent"], fg="#ffffff", relief="flat", bd=0,
                  font=(FONT, 11), command=do_add).pack(side="left", padx=2)
        tk.Button(addf, text="清空", bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 11),
                  command=lambda: (self.ma_list.clear(), refresh_rows(), self._refresh())).pack(
                      side="left", padx=2)
        tk.Button(win, text="完成", bg=t["accent"], fg="#ffffff", relief="flat", bd=0,
                  font=(FONT, 12), command=win.destroy).grid(row=3, column=0, columnspan=3,
                                                             sticky="ew", padx=14, pady=(12, 12))
        ent.focus_set()

    def _move_ma(self, i: int, d: int, refresh):
        j = i + d
        if 0 <= j < len(self.ma_list):
            self.ma_list[i], self.ma_list[j] = self.ma_list[j], self.ma_list[i]
            refresh()
            self._refresh()

    def _del_ma(self, i: int, refresh):
        if 0 <= i < len(self.ma_list):
            self.ma_list.pop(i)
            refresh()
            self._refresh()
            self._set_status("已删除均线")

    def open_jump(self):
        win = tk.Toplevel(self.root)
        win.title("跳转时间")
        t = self.theme
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        tk.Label(win, text="目标时间(美东 HH:MM,早于当前=倒退,晚于=快进)",
                 bg=t["panel"], fg=t["dim"], font=(FONT, 11)).grid(row=0, column=0, columnspan=2,
                                                                   padx=12, pady=(12, 6))
        span = int(self.sim.history_span() // 60)
        tk.Label(win, text=f"当前 {self.sim.now.strftime('%m-%d %H:%M')} · 可回退 {span} 分钟",
                 bg=t["panel"], fg=t["faint"], font=(FONT, 11)).grid(row=1, column=0, columnspan=2,
                                                                    padx=12, pady=(0, 4))
        var = tk.StringVar(value=self.sim.now.strftime("%H:%M"))
        e = tk.Entry(win, textvariable=var, font=(FONT_NUM, 16), justify="center", bg=t["panel3"],
                     fg=t["text"], insertbackground=t["text"], relief="flat", width=12)
        e.grid(row=2, column=0, columnspan=2, padx=12, pady=6)
        e.focus_set()

        def do_jump():
            try:
                hh, mm = [int(x) for x in var.get().split(":")[:2]]
            except Exception:
                messagebox.showerror("错误", "时间格式应为 HH:MM", parent=win)
                return
            now = self.sim.now
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target > now:
                target -= dt.timedelta(days=1)
            self.engine.cmd.put(("jump", (target, self.sim.rewind_mode)))
            win.destroy()

        tk.Button(win, text="跳转", bg=t["accent"], fg="#ffffff", relief="flat", bd=0,
                  font=(FONT, 13), command=do_jump).grid(row=3, column=0, sticky="ew", padx=(12, 4),
                                                         pady=(4, 12))
        tk.Button(win, text="取消", bg=t["panel3"], fg=t["text"], relief="flat", bd=0,
                  font=(FONT, 13), command=win.destroy).grid(row=3, column=1, sticky="ew",
                                                            padx=(4, 12), pady=(4, 12))
        win.grab_set()

    def reset_sim(self):
        if not messagebox.askokcancel("重置", "重置账户并重新生成行情?历史记录将清空。"):
            return
        self.engine.cmd.put(("reset", {"seed": int(time.time()) % 100000,
                                       "base_price": INSTRUMENTS[self.instr_key]["base_price"]}))
        self._set_status("已重置")

    def _set_status(self, text: str):
        self.status_lbl.configure(text=text)

    def _cfg(self, widget, **kw):
        key = id(widget)
        last = self._cfg_cache.get(key)
        if last is not None and all(last.get(k) == v for k, v in kw.items()):
            return
        self._cfg_cache[key] = dict(kw)
        try:
            widget.configure(**kw)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def _tick(self):
        try:
            self._refresh()
        except Exception as e:
            self._set_status(f"界面异常: {e}")
        self.root.after(80, self._tick)

    def _refresh(self):
        sim = self.sim
        t = self.theme
        with sim.lock:
            m = sim.market
            b = sim.broker
            price = m.price
            now = m.now
            bid, ask = sim.bid_ask()
            equity = b.equity(price)
            per = self.period
            # 供拖动回看的额外历史(平移时按需延展窗口)
            need = self.visible_bars + self.chart.offset + PAN_WINDOW
            # 均线需要更长历史才能起势(上限 1200 根,避免高周期取数过重)
            if self.ma_list:
                need = max(need, min(max(self.ma_list), 1200))
            bars = m.bars_for(per, need)
            cur = m.current_bar_for(per)
            unreal = b.unrealized(price)
            margin = b.margin_required(price)
            maint = b.margin_maintenance(price)
            liq = b.liquidation_price()
            pos_qty, pos_avg = b.qty, b.avg
            # 当前持仓挂着的止损 / 止盈(K线图上会画出来)
            exit_stop, exit_tp = b.position_exit_prices()
            orders = [o for o in b.orders.values() if o.status in ("working", "triggered")]
            curve = sim.equity_curve[-2500:]
            trades = list(b.trades[-120:])
            session = m.calendar.session_label(now)
            last_event = getattr(m, "last_event", None)
            tape = list(m.tape)[-40:]
            sess_high, sess_low = m.session_high, m.session_low
            sess_open, sess_vol = m.session_open, m.session_vol
            prev_close = m.prev_close
            sess1 = m.session_bars(60) if self.view == "分时图" else []

        now_ts = time.perf_counter()
        if not hasattr(self, "_stats_cache") or now_ts - self._stats_ts > 0.6:
            with sim.lock:
                stats = sim.stats()
            self._stats_cache = stats
            self._stats_ts = now_ts
        else:
            stats = self._stats_cache

        # ---- 顶部报价条 ----
        chg = price - prev_close
        pct = chg / prev_close * 100 if prev_close else 0.0
        self._cfg(self.price_lbl, text=fmt_price(price, m.tick),
                  fg=t["up"] if chg >= 0 else t["down"])
        self._cfg(self.chg_lbl, text=f"{chg:+,.2f}  {pct:+.2f}%",
                  fg=t["up"] if chg >= 0 else t["down"])
        self._cfg(self.bid_lbl, text=f"买 {fmt_price(bid, m.tick)}")
        self._cfg(self.ask_lbl, text=f"卖 {fmt_price(ask, m.tick)}")
        self._cfg(self.spread_lbl, text=f"价差 {m.spread_ticks():.0f}跳")
        self._cfg(self.time_lbl, text=now.strftime("%m-%d %H:%M:%S"))
        self._cfg(self.session_lbl, text=f"美东 {now.tzname()} · {session}")

        nxt = m.events[m.event_idx] if m.event_idx < len(m.events) else None
        if last_event and (now - last_event[0]).total_seconds() < 90 * 60:
            ets, title, sev = last_event
            self._cfg(self.event_lbl, text=f"⚡ {title} · {(now - ets).total_seconds()/60:.0f}分钟前")
        elif nxt:
            ets, title, sev = nxt
            self._cfg(self.event_lbl, text=f"下一事件:{title} ({(ets-now).total_seconds()/3600:.1f}小时后)")
        else:
            self._cfg(self.event_lbl, text="")

        # ---- K线图 ----
        # 止盈/止损成交后持仓已平:自动退出编辑模式,
        # 图上的开仓线 / 止损线 / 止盈线随之消失,对应挂单也已自动撤销
        if pos_qty == 0 and (self.chart.edit_mode or self.chart.drag_line):
            self.chart.edit_mode = False
            self.chart.edit_sl = None
            self.chart.edit_tp = None
            self.chart.drag_line = None
        o = cur["o"] if cur else price
        h = cur["h"] if cur else price
        l = cur["l"] if cur else price
        c = cur["c"] if cur else price
        self.chart.data = {
            "bars": bars, "cur": cur, "price": price, "tick": m.tick,
            "entry": pos_avg if pos_qty else None, "qty": pos_qty,
            "stop": exit_stop, "tp": exit_tp,
            "visible": self.visible_bars, "tz": ET,
            "mas": [(p, self.ma_colors[i % len(self.ma_colors)])
                    for i, p in enumerate(self.ma_list)],
            "title": (f"{BAR_PERIODS.get(per,'-')}  {m.instr_key}   "
                      f"O {fmt_price(o, m.tick)}  H {fmt_price(h, m.tick)}  "
                      f"L {fmt_price(l, m.tick)}  C {fmt_price(c, m.tick)}"),
        }
        if self.view in ("K线图", "多图同列"):
            self.chart.redraw()

        # ---- 分时图 ----
        if self.view == "分时图":
            pts = [(x["t"], x["c"]) for x in sess1]
            avg = []
            run = 0.0
            for i, x in enumerate(sess1):
                run += x["c"]
                avg.append(run / (i + 1))
            self.line_chart.data = {
                "points": pts, "avg": avg, "prev_close": prev_close,
                "visible": self.visible_points,
                "t0": dt.datetime.fromtimestamp(pts[0][0], ET) if pts else None,
                "t1": now,
            }
            self.line_chart.redraw()

        # ---- 报价视图 / 自选 ----
        if self.view == "报价":
            qv = self.qv_labels
            self._cfg(qv["last"], text=fmt_price(price, m.tick))
            self._cfg(qv["chg"], text=f"{chg:+,.2f}", fg=t["up"] if chg >= 0 else t["down"])
            self._cfg(qv["pct"], text=f"{pct:+.2f}%", fg=t["up"] if chg >= 0 else t["down"])
            self._cfg(qv["open"], text=fmt_price(sess_open, m.tick))
            self._cfg(qv["high"], text=fmt_price(sess_high, m.tick))
            self._cfg(qv["low"], text=fmt_price(sess_low, m.tick))
            self._cfg(qv["prev"], text=fmt_price(prev_close, m.tick))
            self._cfg(qv["vol"], text=f"{sess_vol:,}")
            self._cfg(qv["last_vol"], text=str(tape[-1][2] if tape else 0))
            self._cfg(qv["oi"], text=f"{int(sess_vol * 2.3):,}")
            self._cfg(qv["avg"], text=fmt_price(m.vwap, m.tick))
            self._cfg(qv["bid"], text=fmt_price(bid, m.tick), fg=t["bid"])
            self._cfg(qv["ask"], text=fmt_price(ask, m.tick), fg=t["ask"])
            self._cfg(qv["spread"], text=f"{m.spread_ticks():.0f}跳")
        if self.view == "自选":
            self.watch_tree.delete(*self.watch_tree.get_children())
            for key, spec in INSTRUMENTS.items():
                self.watch_tree.insert("", "end", values=(
                    f"{key} 纳指", fmt_price(price, m.tick),
                    f"{chg:+,.2f}", f"{sess_vol:,}"))

        # ---- 资金曲线(多图同列 + 资金曲线标签页) ----
        marks = []
        for tr in trades[-60:]:
            if tr.close_t:
                marks.append((tr.close_t, tr.pnl > 0))
        eq_data = {"curve": curve, "initial": b.initial_balance, "marks": marks,
                   "visible": self.visible_curve}
        self.eq_chart.data = eq_data
        self.eq_tab_chart.data = eq_data
        if self.view == "多图同列":
            self.eq_chart.redraw()
        try:
            if self.nb.select() == self.nb.tabs()[3]:
                self.eq_tab_chart.redraw()
        except (tk.TclError, IndexError):
            pass
        self._cfg(self.eq_tab_info, text=f"起始 {fmt_money(b.initial_balance)} → 当前 {fmt_money(equity)}"
                                         f"  ({stats['return_pct']:+.2f}%)  最大回撤 "
                                         f"-${stats['max_dd']:,.0f} ({stats['max_dd_pct']:.2f}%)")

        # ---- 右侧盘口 ----
        self._cfg(self.q_name, text=f"{m.instr_key}  纳指期货")
        self._cfg(self.q_sub, text="E-mini Nasdaq-100 · CME")
        self._cfg(self.q_session, text="交易时段 06:00~05:00 (美东)")
        self._cfg(self.q_ask, text=f"卖一   {fmt_price(ask, m.tick)}")
        self._cfg(self.q_bid, text=f"买一   {fmt_price(bid, m.tick)}")
        qs = self.q_stats
        pcol = t["up"] if chg >= 0 else t["down"]
        self._cfg(qs["last"], text=fmt_price(price, m.tick), fg=pcol)
        self._cfg(qs["chg"], text=f"{chg:+,.2f}", fg=pcol)
        self._cfg(qs["pct"], text=f"{pct:+.2f}%", fg=pcol)
        self._cfg(qs["open"], text=fmt_price(sess_open, m.tick))
        self._cfg(qs["high"], text=fmt_price(sess_high, m.tick))
        self._cfg(qs["low"], text=fmt_price(sess_low, m.tick))
        self._cfg(qs["vol"], text=f"{sess_vol:,}")
        self._cfg(qs["oi"], text=f"{int(sess_vol * 2.3):,}")
        self._cfg(qs["prev"], text=fmt_price(prev_close, m.tick))
        self._cfg(qs["avg"], text=fmt_price(m.vwap, m.tick))
        self._cfg(qs["bid"], text=fmt_price(bid, m.tick), fg=t["bid"])
        self._cfg(qs["ask"], text=fmt_price(ask, m.tick), fg=t["ask"])
        self._cfg(qs["spread"], text=f"{m.spread_ticks():.0f}跳")
        tape_sig = (tape[-1][0], tape[-1][1]) if tape else None
        if tape_sig != getattr(self, "_tape_sig", None):
            self._tape_sig = tape_sig
            self.tape_tree.delete(*self.tape_tree.get_children())
            for ts, px, q, d in reversed(tape):
                self.tape_tree.insert("", "end", values=(
                    ts.strftime("%H:%M:%S"), fmt_price(px, m.tick), q,
                    "B" if d == "B" else ("S" if d == "S" else "—")))

        # ---- 资金状态栏 ----
        risk_rate = (abs(unreal) / equity * 100) if equity else 0.0
        self._cfg(self.acct_lbl, text=(
            f"{m.instr_key} 纳指期货    权益: {fmt_money(equity)}    占用保证金: {fmt_money(margin)}    "
            f"可用: {fmt_money(equity - margin)}    费用: {fmt_money(b.commission_paid + b.funding_paid)}    "
            f"风险率: {risk_rate:.2f}%    平盈: {fmt_signed_money(b.realized)}    "
            f"浮盈: {fmt_signed_money(unreal)}"))

        # ---- 持仓 / 委托 / 成交 ----
        # 持仓表:整行保持统一配色,只把"浮动盈亏"这一格按盈亏着色(盈利红 / 亏损绿)
        if pos_qty:
            # 价差:现价与开仓均价的价格差(点)   盈利点数:价差 × 数量(总点数)
            diff = (price - pos_avg) * (1 if pos_qty > 0 else -1)
            pts = diff * abs(pos_qty)
            pnl_color = t["down"] if unreal >= 0 else t["up"]   # 盈利红 / 亏损绿
            self.pos_table.set_row(0, {
                "oid": b.position_order_code or "--",
                "dir": "多头" if pos_qty > 0 else "空头",
                "qty": f"{abs(pos_qty):g}",
                "avg": fmt_price(pos_avg, m.tick),
                "last": fmt_price(price, m.tick),
                "diff": f"{diff:+.2f}",
                "pts": f"{pts:+.2f}",
                "pnl": fmt_signed_money(unreal),
                "margin": fmt_money(margin),
                "liq": fmt_price(liq, m.tick) if liq else "--",
            }, pnl_color=pnl_color)
        else:
            self.pos_table.clear()
        sig = tuple((o.id, o.status, o.remaining, o.limit, o.stop) for o in orders)
        if sig != getattr(self, "_order_sig", None):
            self._order_sig = sig
            self.orders_tree.delete(*self.orders_tree.get_children())
            for o in orders:
                px_txt = fmt_price(o.limit, m.tick) if o.limit else (
                    fmt_price(o.stop, m.tick) if o.stop else "市价")
                self.orders_tree.insert("", "end", values=(
                    o.code, o.type_cn, "买" if o.side == "buy" else "卖",
                    f"{o.remaining:g}", px_txt,
                    "已触发" if o.status == "triggered" else "挂单中"))
        self.trades_tree.delete(*self.trades_tree.get_children())
        for tr in list(reversed(b.trades[-40:])):
            self.trades_tree.insert("", "end", values=(
                tr.close_t.strftime("%m-%d %H:%M:%S") if tr.close_t else "",
                "多" if tr.side == "long" else "空", f"{tr.qty:g}",
                fmt_price(tr.exit, m.tick), fmt_signed_money(tr.pnl)))

        # ---- 统计 ----
        sl = self.stat_labels
        self._cfg(sl["equity"], text=fmt_money(stats["equity"]))
        self._cfg(sl["net"], text=fmt_signed_money(stats["net_pnl"]),
                  fg=t["good"] if stats["net_pnl"] >= 0 else t["bad"])
        self._cfg(sl["ret"], text=f"{stats['return_pct']:+.2f}%",
                  fg=t["good"] if stats["return_pct"] >= 0 else t["bad"])
        self._cfg(sl["realized"], text=fmt_signed_money(stats["realized"]))
        self._cfg(sl["unreal"], text=fmt_signed_money(stats["unrealized"]))
        self._cfg(sl["comm"], text=f"-${stats['commission']:,.0f}")
        self._cfg(sl["fund"], text=f"-${stats['funding']:,.0f}")
        self._cfg(sl["dd"], text=f"-${stats['max_dd']:,.0f} ({stats['max_dd_pct']:.2f}%)", fg=t["bad"])
        self._cfg(sl["trades"], text=f"{stats['trades']} ({stats['wins']}胜/{stats['losses']}负)")
        self._cfg(sl["winrate"], text=f"{stats['win_rate']:.1f}%")
        pf = stats["profit_factor"]
        self._cfg(sl["pf"], text=("∞" if pf == float("inf") else f"{pf:.2f}"))
        self._cfg(sl["hold"], text=fmt_duration(stats["avg_hold_sec"]))
        self._cfg(sl["tim"], text=f"{stats['time_in_market']:.1f}%")
        self._cfg(sl["sharpe"], text=f"{stats['sharpe']:.2f}")
        self._cfg(sl["liq"], text=str(stats["liquidations"]))

        # ---- 买卖按钮带盘口价 ----
        self._cfg(self.sell_btn, text=f"卖出\n{fmt_price(ask, m.tick)}")
        self._cfg(self.buy_btn, text=f"买入\n{fmt_price(bid, m.tick)}")
        # 滑块位置跟随实际杠杆(程序化赋值不会触发 -command,不会回灌)
        try:
            if abs(self.lev_var.get() - b.leverage) > 1e-9:
                self.lev_var.set(b.leverage)
        except tk.TclError:
            pass
        self._cfg(self.risk_lbl, text=f"1手 {fmt_money(price * b.mult, 0)} · "
                                     f"保证金 {fmt_money(price * b.mult / max(1, b.leverage), 0)} · "
                                     f"往返 {fmt_money(b.round_trip_cost(1))}")

        if self.engine.busy:
            self._cfg(self.busy_lbl, text=f"{self.engine.busy} {self.engine.progress*100:.0f}%")
        else:
            self._cfg(self.busy_lbl, text="")
        if self.engine.error:
            self._cfg(self.status_lbl, text=self.engine.error)

    def _commit_all_pans(self, _e=None):
        """统一提交三个图表的拖动结果。"""
        for c in (self.chart, self.line_chart, self.eq_chart, self.eq_tab_chart):
            try:
                c.commit_pan()
            except tk.TclError:
                pass

    def _on_close(self):
        self.engine.stop()
        self.root.destroy()


def default_start() -> dt.datetime:
    """默认起始时间:最近一个交易日的 09:30 美东开盘。"""
    from .market import Calendar, is_holiday
    now = dt.datetime.now(ET)
    for i in range(10):
        d = (now - dt.timedelta(days=i)).date()
        if d.weekday() >= 5 or is_holiday(d):
            continue
        t = dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
        if t < now:
            return t
    return now


def main():
    from .uidefs import enable_dpi_awareness
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
