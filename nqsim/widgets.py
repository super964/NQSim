"""图表组件:K线图(含成交量、均线、持仓标注)与资金曲线(含回撤)。"""

from __future__ import annotations

import datetime as dt
import math
import tkinter as tk
from tkinter import font as tkfont
from typing import List, Optional

from .fmt import fmt_price
from . import uidefs

FONT_NUM = "Consolas"


def _p(base: int) -> int:
    """图表内边距按屏幕缩放换算(字体变大 → 刻度栏也要留够空间)。"""
    return uidefs.px(int(base * 1.25))


def _mix(c1: str, c2: str, t: float) -> str:
    def rgb(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    a, b = rgb(c1), rgb(c2)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _downsample(pts: list, buckets: int) -> list:
    """按桶取极值降采样,保留形状且大幅降低绘制开销。"""
    n = len(pts)
    if n <= buckets * 2:
        return pts
    out = []
    step = n / buckets
    for i in range(buckets):
        a = int(i * step)
        b = min(n, int((i + 1) * step) + 1)
        if a >= b:
            continue
        chunk = pts[a:b]
        out.append(chunk[0])
        lo = min(chunk, key=lambda p: p[1])
        hi = max(chunk, key=lambda p: p[1])
        if lo is not chunk[0]:
            out.append(lo)
        if hi is not chunk[0] and hi is not lo:
            out.append(hi)
    out.append(pts[-1])
    return out


class CandleChart(tk.Canvas):
    """K线主图。支持滚轮缩放、鼠标拖动平移、K线宽度上限与右对齐。"""

    MAX_SLOT = 15          # 单根K线最大像素宽度(数据不足时不拉伸)
    DRAG_MARGIN = 400      # 拖动时预加载的额外K线根数

    def __init__(self, master, theme: dict, **kw):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, **kw)
        self.theme = theme
        self.data: dict = {}
        self.hover = None
        self.shown = 0
        self.slot = 0
        self.max_slot = _p(self.MAX_SLOT)
        self.offset = 0            # 相对最新K线的偏移根数(向左回看为正)
        self.on_zoom = None        # 回调: +1 放大 / -1 缩小
        self.on_pan = None         # 回调: 新的偏移根数
        self._drag_x = None
        self._drag_px = 0
        # --- 止盈止损拖拽编辑 ---
        self.edit_mode = False     # 是否处于"双击开仓线"后的编辑模式
        self.edit_sl = None        # 编辑中的止损价
        self.edit_tp = None        # 编辑中的止盈价
        self.drag_line = None      # 正在拖动的线: sl / tp / entry
        self.on_edit_request = None   # 回调: 双击开仓线 -> 请求进入编辑模式
        self.on_exit_drag = None      # 回调: (kind, price) 拖动结束
        # 价格标尺缓存(供 y <-> price 换算)
        self._pl_lo = 0.0
        self._pl_rng = 1.0
        self._pl_top = 0.0
        self._pl_h = 1.0
        self._f_lbl = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self._f_lbl_b = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        self._f_num = tkfont.Font(family="Consolas", size=11)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._wheel(e, 1))
        self.bind("<Button-5>", lambda e: self._wheel(e, -1))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)

    # -- 价格标尺换算 ----------------------------------------------------
    def price_at(self, y: float) -> float:
        """屏幕纵坐标 -> 价格。"""
        if self._pl_h <= 0:
            return 0.0
        return self._pl_lo + (self._pl_top + self._pl_h - y) / self._pl_h * self._pl_rng

    def y_at(self, price: float) -> float:
        """价格 -> 屏幕纵坐标。"""
        if self._pl_rng <= 0:
            return 0.0
        return self._pl_top + self._pl_h - (price - self._pl_lo) / self._pl_rng * self._pl_h

    def snap_price(self, price: float) -> float:
        """对齐到最小变动价位。"""
        tick = self.data.get("tick", 0.25) or 0.25
        return round(round(price / tick) * tick, 4)

    # -- 双击开仓线 -> 进入止盈止损编辑 -----------------------------------
    def _on_double_click(self, e):
        # 注意:Tk 在检测到双击时,会用 <Double-Button-1> **替换**第二次的
        # <Button-1>,因此这里必须自己补上"按下"逻辑,否则拖动平移会失效。
        entry = self.data.get("entry")
        if entry and self.data.get("qty") and abs(e.y - self.y_at(entry)) <= _p(9):
            if self.on_edit_request:
                self.on_edit_request()       # 进入 / 退出止盈止损编辑
            return "break"
        # 不在开仓线上:当作普通按下,保证拖动平移照常工作
        self._drag_x = e.x
        self._drag_px = 0

    def _pick_line(self, y: float) -> str:
        """判断鼠标靠近哪条可拖动的线。"""
        tol = _p(9)
        if self.edit_sl is not None and abs(y - self.y_at(self.edit_sl)) <= tol:
            return "sl"
        if self.edit_tp is not None and abs(y - self.y_at(self.edit_tp)) <= tol:
            return "tp"
        entry = self.data.get("entry")
        if entry and abs(y - self.y_at(entry)) <= tol:
            return "entry"
        return ""

    # -- 拖动平移 --------------------------------------------------------
    def _on_press(self, e):
        if self.edit_mode:
            kind = self._pick_line(e.y)
            if kind:
                self.drag_line = kind
                self._drag_x = e.x
                self._drag_px = 0
                try:
                    self.configure(cursor="sb_v_double_arrow")
                except tk.TclError:
                    pass
                return
        self._drag_x = e.x
        self._drag_px = 0
        try:
            self.configure(cursor="fleur")
        except tk.TclError:
            pass

    def _on_drag(self, e):
        if self.drag_line:
            self._drag_line_to(e.y)
            return
        if self._drag_x is None:
            return
        self._drag_px = e.x - self._drag_x
        self.redraw()

    def _drag_line_to(self, y: float):
        """把被拖动的线移到鼠标所在价位。"""
        p = self.snap_price(self.price_at(y))
        if self.drag_line == "sl":
            self.edit_sl = p
        elif self.drag_line == "tp":
            self.edit_tp = p
        elif self.drag_line == "entry":
            # 拖动开仓线:止损/止盈整体跟随(保持相对距离)
            old = self.data.get("entry") or p
            ds = (self.edit_sl - old) if self.edit_sl is not None else None
            dt = (self.edit_tp - old) if self.edit_tp is not None else None
            if ds is not None:
                self.edit_sl = self.snap_price(p + ds)
            if dt is not None:
                self.edit_tp = self.snap_price(p + dt)
        self.redraw()

    def _on_release(self, e):
        if self.drag_line:
            kind = self.drag_line
            self.drag_line = None
            try:
                self.configure(cursor="")
            except tk.TclError:
                pass
            if self.on_exit_drag:
                if kind == "sl":
                    self.on_exit_drag("sl", self.edit_sl)
                elif kind == "tp":
                    self.on_exit_drag("tp", self.edit_tp)
            self.redraw()
            return
        self.commit_pan()

    def commit_pan(self):
        """把像素位移换算成K线根数偏移并提交。"""
        if self._drag_x is None:
            return
        self._drag_x = None
        try:
            self.configure(cursor="")
        except tk.TclError:
            pass
        slot = self.slot or 1.0
        delta = int(round(self._drag_px / slot))
        self._drag_px = 0
        if delta:
            self.offset = max(0, self.offset + delta)
            if self.on_pan:
                self.on_pan(self.offset)
        self.redraw()

    def reset_pan(self):
        self.offset = 0
        self._drag_px = 0
        self._drag_x = None

    def _on_wheel(self, e):
        self._wheel(e, 1 if e.delta > 0 else -1)

    def _wheel(self, e, delta):
        if self.on_zoom:
            self.on_zoom(delta)
        return "break"

    def _on_motion(self, e):
        self.hover = (e.x, e.y)
        if self._drag_x is None:
            self.redraw()

    def _on_leave(self, e):
        self.hover = None
        if self._drag_x is None:
            self.redraw()

    def redraw(self):
        t = self.theme
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 60 or h < 60:
            return
        d = self.data
        all_bars: List[dict] = d.get("bars", [])
        cur = d.get("cur")
        if not all_bars and not cur:
            self.create_text(w / 2, h / 2, text="等待行情…", fill=t["dim"], font=("Microsoft YaHei UI", 13))
            return
        n_visible = int(d.get("visible", 110))
        tick = d.get("tick", 0.25)
        off = max(0, int(self.offset))
        price = d.get("price")
        if price is None:
            price = all_bars[-1]["c"] if all_bars else (cur["c"] if cur else 0.0)

        pad_l, pad_r, pad_t, pad_b = _p(8), _p(74), _p(10), _p(22)
        vol_h = int((h - pad_t - pad_b) * 0.22)
        price_h = h - pad_t - pad_b - vol_h - _p(8)
        pw = w - pad_l - pad_r
        ph = price_h
        pw = max(pw, 10)

        # ---- 视口定位:offset 表示相对最新K线向左回看的根数 ----
        last = len(all_bars) - 1 - off          # 视口最右一根在 all_bars 中的下标
        if last < 0:
            last = 0
        if off == 0 and cur:
            last += 1                          # 正在形成的K线占最右侧一格
        # K线宽度上限:数据不足时右对齐留白,不拉伸成巨块
        slot = pw / max(1, n_visible)
        if slot > self.max_slot:
            slot = self.max_slot
            n_visible = max(1, int(pw / slot))
        first = last - n_visible + 1
        if first < 0:
            first = 0
        n = last - first + 1
        shift = self._drag_px                 # 拖动中的像素级预览位移
        self.shown = n
        self.slot = slot

        def bar_at(i):
            """取第 i 根(虚拟下标, last 可能是正在形成的K线)。"""
            if i < len(all_bars):
                return all_bars[i]
            return cur

        def x_of(i):
            return pad_l + pw - (last - i) * slot + shift

        def in_view(x):
            return pad_l - slot <= x <= pad_l + pw + slot

        # 绘制范围:向左多留 DRAG_MARGIN 根,拖动时能平滑露出更早的K线
        d0 = max(0, first - self.DRAG_MARGIN)
        d1 = last
        bars = []
        for i in range(d0, d1 + 1):
            b = bar_at(i)
            if b is not None:
                bars.append((i, b))
        if not bars:
            self.create_text(w / 2, h / 2, text="等待行情…", fill=t["dim"],
                             font=("Microsoft YaHei UI", 13))
            return

        # ---- 价格区间(只按视口内的K线计算) ----
        vis = [b for i, b in bars if in_view(x_of(i))]
        if not vis:
            vis = [b for _, b in bars]
        hi = max(b["h"] for b in vis)
        lo = min(b["l"] for b in vis)
        extra = []
        for key in ("entry", "stop"):
            v = d.get(key)
            if v:
                extra.append(v)
        for v in extra:
            hi = max(hi, v)
            lo = min(lo, v)
        span = max(hi - lo, tick * 8)
        pad = span * 0.12
        hi += pad
        lo -= pad
        rng = hi - lo

        def y_of(p):
            return pad_t + ph - (p - lo) / rng * ph

        # 缓存价格标尺(供拖拽时 y <-> price 换算)
        self._pl_lo, self._pl_rng, self._pl_top, self._pl_h = lo, rng, pad_t, ph

        # ---- 网格 + 价格刻度 ----
        steps = 6
        for i in range(steps + 1):
            p = lo + rng * i / steps
            y = y_of(p)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["grid"])
            self.create_text(w - pad_r + 6, y, text=fmt_price(p, tick), fill=t["dim"],
                             anchor="w", font=("Consolas", 10))
        # 时间刻度
        tstep = max(1, n // 6)
        for k, (i, b) in enumerate(bars):
            if k % tstep:
                continue
            ts = dt.datetime.fromtimestamp(b["t"], d.get("tz"))
            x = x_of(i)
            if not in_view(x):
                continue
            self.create_text(x - slot / 2, h - pad_b + _p(2), text=ts.strftime("%m-%d %H:%M"),
                             fill=t["faint"], anchor="nw", font=("Consolas", 10))

        # ---- 成交量 ----
        vmax = max(b["v"] for _, b in bars) or 1
        vy0 = pad_t + ph + _p(8)
        bw = max(1.0, slot * 0.66)
        for i, b in bars:
            x = x_of(i)
            if not in_view(x):
                continue
            vh = (b["v"] / vmax) * (vol_h - 4)
            col = t["vol_up"] if b["c"] >= b["o"] else t["vol_down"]
            self.create_rectangle(x - bw / 2, vy0 + vol_h - vh, x + bw / 2, vy0 + vol_h,
                                  fill=col, outline="")

        # ---- 左上角行情读数(仿专业终端) ----
        title = d.get("title")

        # ---- K线 ----
        for i, b in bars:
            x = x_of(i)
            if not in_view(x):
                continue
            up = b["c"] >= b["o"]
            col = t["up"] if up else t["down"]
            self.create_line(x, y_of(b["h"]), x, y_of(b["l"]), fill=col)
            y0, y1 = y_of(max(b["o"], b["c"])), y_of(min(b["o"], b["c"]))
            if abs(y1 - y0) < 1.2:
                y1 = y0 + 1.2
            self.create_rectangle(x - bw / 2, y0, x + bw / 2, y1, fill=col, outline=col)

        # ---- 持仓 / 止损(画在K线之上,标签带底板保证可读) ----
        entry = d.get("entry")
        if entry and d.get("qty"):
            y = y_of(entry)
            col = t["up"] if d["qty"] > 0 else t["down"]
            self.create_line(pad_l, y, pad_l + pw, y, fill=col, dash=(5, 3))
            side = "多" if d["qty"] > 0 else "空"
            txt = f"持仓 {side}{abs(d['qty']):g} @ {fmt_price(entry, tick)}"
            tw = self._f_lbl_b.measure(txt)
            self.create_rectangle(pad_l + _p(2), y - _p(24), pad_l + _p(2) + tw + _p(10),
                                  y - _p(2), fill=t["panel3"], outline=t["border"])
            self.create_text(pad_l + _p(7), y - _p(12), text=txt,
                             fill=col, anchor="w", font=("Microsoft YaHei UI", 10, "bold"))
        stop = d.get("stop")
        if stop and d.get("qty"):
            y = y_of(stop)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["warn"], dash=(2, 4))
            txt = f"止损 {fmt_price(stop, tick)}"
            tw = self._f_lbl.measure(txt)
            self.create_rectangle(pad_l + _p(2), y - _p(24), pad_l + _p(2) + tw + _p(10),
                                  y - _p(2), fill=t["panel3"], outline=t["border"])
            self.create_text(pad_l + _p(7), y - _p(12), text=txt,
                             fill=t["warn"], anchor="w", font=("Microsoft YaHei UI", 10))

        # ---- 止盈线(非编辑模式下也显示当前挂单的止盈价) ----
        tp = d.get("tp")
        if tp and d.get("qty") and not self.edit_mode:
            y = y_of(tp)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["up"], dash=(4, 4))
            txt = f"止盈 {fmt_price(tp, tick)}"
            tw = self._f_lbl.measure(txt)
            self.create_rectangle(pad_l + _p(2), y - _p(24), pad_l + _p(2) + tw + _p(10),
                                  y - _p(2), fill=t["panel3"], outline=t["border"])
            self.create_text(pad_l + _p(7), y - _p(12), text=txt,
                             fill=t["up"], anchor="w", font=("Microsoft YaHei UI", 10))

        # ---- 止盈止损编辑模式:可拖动的止损线 / 止盈线 ----
        # 持仓已平(止盈止损成交)时不再绘制,避免线残留在K线图上
        if self.edit_mode and d.get("qty"):
            for kind, val, col, name in (("sl", self.edit_sl, t["down"], "止损"),
                                         ("tp", self.edit_tp, t["up"], "止盈")):
                if val is None:
                    continue
                y = y_of(val)
                if y < pad_t - _p(4) or y > pad_t + ph + _p(4):
                    continue
                active = (self.drag_line == kind)
                self.create_line(pad_l, y, pad_l + pw, y, fill=col, dash=(6, 3),
                                 width=3 if active else 2)
                txt = f"{name} {fmt_price(val, tick)}"
                tw = self._f_lbl_b.measure(txt)
                lx = min(pad_l + _p(6), pad_l + pw - tw - _p(12))
                self.create_rectangle(lx, y - _p(24), lx + tw + _p(10), y - _p(2),
                                      fill=t["panel3"], outline=col)
                self.create_text(lx + _p(5), y - _p(12), text=txt, fill=col, anchor="w",
                                 font=("Microsoft YaHei UI", 10, "bold"))
                # 右侧价格轴上的价格标签
                self.create_rectangle(w - pad_r + 2, y - _p(11), w - 4, y + _p(11),
                                      fill=t["panel3"], outline=col)
                self.create_text(w - pad_r + 6, y, text=fmt_price(val, tick), fill=col,
                                 anchor="w", font=("Consolas", 11, "bold"))
            # 编辑提示(放在图表右上角,避免压住时间轴)
            self.create_text(pad_l + pw, pad_t + _p(2),
                             text="拖动红线设止损 · 拖动绿线设止盈 · 再次双击开仓线或按 Esc 退出",
                             fill=t["warn"], anchor="ne", font=("Microsoft YaHei UI", 10))

        # ---- 均线(周期可配置,1~2000) ----
        # EMA 在完整历史上递推(只用可见窗口会导致长周期均线严重失真),仅绘制可视范围内
        mas = d.get("mas") or [(20, "accent2"), (60, "accent")]
        for period, ckey in mas:
            if not all_bars:
                continue
            col = t.get(ckey, t["accent"])
            seed_n = min(period, len(all_bars))
            ema = sum(b["c"] for b in all_bars[:seed_n]) / seed_n
            k = 2.0 / (period + 1)
            pts = []
            for i, b in enumerate(all_bars):
                ema = b["c"] * k + ema * (1 - k)
                if i >= seed_n - 1:
                    x = x_of(i)
                    if in_view(x):
                        pts.append((x, y_of(ema)))
            if cur and off == 0:
                ema = cur["c"] * k + ema * (1 - k)
                x = x_of(len(all_bars))
                if in_view(x):
                    pts.append((x, y_of(ema)))
            if len(pts) > 1:
                self.create_line(*[c for p in pts for c in p], fill=col, width=2)

        # ---- 现价线 ----
        y = y_of(price)
        self.create_line(pad_l, y, pad_l + pw, y, fill=t["flat"], dash=(4, 3))
        tag = t["panel3"]
        self.create_rectangle(w - pad_r + 2, y - _p(11), w - 4, y + _p(11), fill=tag, outline=t["flat"])
        self.create_text(w - pad_r + 6, y, text=fmt_price(price, tick), fill=t["text"],
                         anchor="w", font=("Consolas", 11, "bold"))

        # ---- 左上角行情读数(画在最上层,不被K线遮挡) ----
        if title:
            tw = self._f_num.measure(title)
            self.create_rectangle(pad_l, pad_t, pad_l + tw + _p(10), pad_t + _p(22),
                                  fill=t["bg"], outline="")
            self.create_text(pad_l + _p(3), pad_t + _p(2), text=title, fill=t["dim"],
                             anchor="nw", font=("Consolas", 11))
            # 均线图例
            lx = pad_l + tw + _p(18)
            for period, ckey in (d.get("mas") or []):
                col = t.get(ckey, t["accent"])
                lab = f"MA{period}"
                lw = self._f_num.measure(lab)
                self.create_text(lx, pad_t + _p(2), text=lab, fill=col, anchor="nw",
                                 font=("Consolas", 10, "bold"))
                lx += lw + _p(16)

        # ---- 十字光标:该K线的涨跌(点数/比例)+ 右侧价格轴标注该K线价格 ----
        if self.hover and pad_l <= self.hover[0] <= pad_l + pw:
            hx, hy = self.hover
            self.create_line(hx, pad_t, hx, pad_t + ph + vol_h + _p(8), fill=t["cross"], dash=(3, 3))
            self.create_line(pad_l, hy, pad_l + pw, hy, fill=t["cross"], dash=(3, 3))
            # 命中最近的K线
            idx = last - int(round((pad_l + pw + shift - hx) / slot))
            idx = max(d0, min(last, idx))
            b = bar_at(idx)
            if b is None:
                return
            ts = dt.datetime.fromtimestamp(b["t"], d.get("tz"))
            info = (f"{ts.strftime('%m-%d %H:%M')}  O {fmt_price(b['o'], tick)}  "
                    f"H {fmt_price(b['h'], tick)}  L {fmt_price(b['l'], tick)}  "
                    f"C {fmt_price(b['c'], tick)}  V {b['v']}")
            tw = max(_p(300), self._f_num.measure(info) + _p(14))
            tx = min(max(hx - tw / 2, pad_l), max(pad_l, pad_l + pw - tw))
            self.create_rectangle(tx, pad_t + ph + _p(2), tx + tw, pad_t + ph + _p(22),
                                  fill=t["panel3"], outline=t["border"])
            self.create_text(tx + 6, pad_t + ph + _p(12), text=info, fill=t["text"], anchor="w",
                             font=("Consolas", 10))

            # ---- 涨跌:相对前一根K线收盘价(第一根用开盘价) ----
            prev = bar_at(idx - 1) if idx - 1 >= 0 else None
            base_c = prev["c"] if prev else b["o"]
            chg = b["c"] - base_c
            pct = (chg / base_c * 100.0) if base_c else 0.0
            ccol = t["up"] if chg >= 0 else t["down"]
            ctxt = f"涨跌 {chg:+.2f} 点   {pct:+.2f}%"
            cw = self._f_lbl_b.measure(ctxt) + _p(16)
            cx = min(max(hx + _p(10), pad_l), max(pad_l, pad_l + pw - cw))
            cy = pad_t + _p(30)
            self.create_rectangle(cx, cy, cx + cw, cy + _p(22), fill=t["panel3"], outline=ccol)
            self.create_text(cx + _p(8), cy + _p(11), text=ctxt, fill=ccol, anchor="w",
                             font=("Microsoft YaHei UI", 10, "bold"))

            # ---- 右侧价格轴:标出鼠标所指K线的开/收盘价 ----
            y_c = y_of(b["c"])
            y_o = y_of(b["o"])
            if abs(y_c - y_o) > _p(16):          # 实体够高才画开盘价虚线
                self.create_line(pad_l, y_o, pad_l + pw, y_o, fill=t["cross"], dash=(1, 4))
            self.create_line(pad_l, y_c, pad_l + pw, y_c, fill=t["cross"])
            self.create_rectangle(w - pad_r + 2, y_c - _p(11), w - 4, y_c + _p(11),
                                  fill=t["panel3"], outline=t["cross"])
            self.create_text(w - pad_r + 6, y_c, text=fmt_price(b["c"], tick), fill=t["text"],
                             anchor="w", font=("Consolas", 11, "bold"))


class PanMixin:
    """鼠标拖动平移:按住图表左右拖动可回看更早的数据。"""

    def _init_pan(self):
        self.offset = 0            # 相对最新数据的偏移点数(向左回看为正)
        self.on_pan = None
        self._drag_x = None
        self._drag_px = 0
        self.bind("<ButtonPress-1>", self._pan_press)
        self.bind("<B1-Motion>", self._pan_drag)
        self.bind("<ButtonRelease-1>", self._pan_release)

    def _pan_press(self, e):
        self._drag_x = e.x
        self._drag_px = 0
        try:
            self.configure(cursor="fleur")
        except tk.TclError:
            pass

    def _pan_drag(self, e):
        if self._drag_x is None:
            return
        self._drag_px = e.x - self._drag_x
        self.redraw()

    def _pan_release(self, e):
        self.commit_pan()

    def commit_pan(self):
        if self._drag_x is None:
            return
        self._drag_x = None
        try:
            self.configure(cursor="")
        except tk.TclError:
            pass
        slot = getattr(self, "slot", 0) or 1.0
        delta = int(round(self._drag_px / slot))
        self._drag_px = 0
        if delta:
            self.offset = max(0, self.offset + delta)
            if self.on_pan:
                self.on_pan(self.offset)
        self.redraw()

    def reset_pan(self):
        self.offset = 0
        self._drag_px = 0
        self._drag_x = None


class LineChart(PanMixin, tk.Canvas):
    """分时图:日内价格走势 + 均价线。支持滚轮缩放与拖动平移。"""

    def __init__(self, master, theme: dict, **kw):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, **kw)
        self.theme = theme
        self.data: dict = {}
        self.on_zoom = None
        self.slot = 0
        self._init_pan()
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._wheel(e, 1))
        self.bind("<Button-5>", lambda e: self._wheel(e, -1))

    def _on_wheel(self, e):
        self._wheel(e, 1 if e.delta > 0 else -1)

    def _wheel(self, e, delta):
        if self.on_zoom:
            self.on_zoom(delta)
        return "break"

    def redraw(self):
        t = self.theme
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 60 or h < 40:
            return
        d = self.data
        all_pts = d.get("points", [])
        all_avg = list(d.get("avg") or [])
        vis = int(d.get("visible", 0) or 0)
        off = max(0, int(self.offset))
        n_all = len(all_pts)
        end = n_all - off
        if end < 2:
            end = min(n_all, 2)
        start = max(0, end - vis) if vis else 0
        pts = all_pts[start:end]
        avg = all_avg[start:end] if len(all_avg) >= end else all_avg
        if len(pts) < 2:
            self.create_text(w / 2, h / 2, text="等待分时数据…", fill=t["dim"],
                             font=("Microsoft YaHei UI", 12))
            return
        prev_close = d.get("prev_close", pts[0][1])
        pad_l, pad_r, pad_t, pad_b = _p(56), _p(68), _p(10), _p(20)
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        vals = [p[1] for p in pts]
        hi, lo = max(vals), min(vals)
        hi = max(hi, prev_close)
        lo = min(lo, prev_close)
        span = max(hi - lo, 0.25)
        hi += span * 0.12
        lo -= span * 0.12
        rng = hi - lo
        shift = self._drag_px

        def y_of(v):
            return pad_t + ph - (v - lo) / rng * ph

        def x_of(i):
            return pad_l + (i / (len(pts) - 1)) * pw + shift

        self.slot = pw / max(1, len(pts) - 1)

        for i in range(5):
            v = lo + rng * i / 4
            y = y_of(v)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["grid"])
            self.create_text(w - pad_r + 6, y, text=f"{v:,.2f}", fill=t["dim"], anchor="w",
                             font=("Consolas", 10))
            pct = (v / prev_close - 1) * 100 if prev_close else 0
            self.create_text(pad_l - 6, y, text=f"{pct:+.2f}%", fill=t["faint"], anchor="e",
                             font=("Consolas", 10))
        y0 = y_of(prev_close)
        self.create_line(pad_l, y0, pad_l + pw, y0, fill=t["faint"], dash=(4, 4))
        self.create_text(pad_l + 4, y0 - 8, text=f"昨结 {prev_close:,.2f}", fill=t["faint"],
                         anchor="w", font=("Consolas", 10))
        # 均价线
        if len(avg) > 1 and len(avg) != len(pts):
            # 均价点数与价格点数不一致时按比例重采样,保证横轴对齐
            new_avg = []
            for i in range(len(pts)):
                f = i / max(1, len(pts) - 1) * (len(avg) - 1)
                j = int(f)
                k = min(j + 1, len(avg) - 1)
                new_avg.append(avg[j] + (avg[k] - avg[j]) * (f - j))
            avg = new_avg
        if len(avg) > 1:
            self.create_line(*[c for i, v in enumerate(avg) for c in (x_of(i), y_of(v))],
                             fill=t["accent2"], width=1)
        # 价格线
        col = t["up"] if pts[-1][1] >= prev_close else t["down"]
        area = [(x_of(0), y_of(lo))] + [(x_of(i), y_of(p[1])) for i, p in enumerate(pts)] + \
               [(x_of(len(pts) - 1), y_of(lo))]
        self.create_polygon(area, fill=_mix(col, t["bg"], 0.85), outline="")
        self.create_line(*[c for i, p in enumerate(pts) for c in (x_of(i), y_of(p[1]))],
                         fill=col, width=1)
        ts0 = d.get("t0")
        if ts0:
            self.create_text(pad_l, h - pad_b + _p(2), text=ts0.strftime("%H:%M"), fill=t["faint"],
                             anchor="nw", font=("Consolas", 10))
            self.create_text(pad_l + pw, h - pad_b + _p(2), text=d.get("t1", "").strftime("%H:%M"),
                             fill=t["faint"], anchor="ne", font=("Consolas", 10))


class EquityChart(PanMixin, tk.Canvas):
    """资金曲线 + 回撤。支持滚轮缩放与拖动平移。"""

    def __init__(self, master, theme: dict, **kw):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, **kw)
        self.theme = theme
        self.data: dict = {}
        self.on_zoom = None
        self.slot = 0
        self._init_pan()
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._wheel(e, 1))
        self.bind("<Button-5>", lambda e: self._wheel(e, -1))

    def _on_wheel(self, e):
        self._wheel(e, 1 if e.delta > 0 else -1)

    def _wheel(self, e, delta):
        if self.on_zoom:
            self.on_zoom(delta)
        return "break"

    def redraw(self):
        t = self.theme
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 60 or h < 40:
            return
        d = self.data
        all_pts: List[tuple] = d.get("curve", [])
        initial = d.get("initial", 0.0)
        vis = int(d.get("visible", 0) or 0)
        off = max(0, int(self.offset))
        end = len(all_pts) - off
        if end < 2:
            end = min(len(all_pts), 2)
        start = max(0, end - vis) if vis else 0
        pts = all_pts[start:end]
        if len(pts) < 2:
            self.create_text(w / 2, h / 2, text="净值采样中…", fill=t["dim"], font=("Microsoft YaHei UI", 12))
            return
        pts = _downsample(pts, 260)
        pad_l, pad_r, pad_t, pad_b = _p(8), _p(74), _p(8), _p(16)
        dd_h = _p(34)
        ph = h - pad_t - pad_b - dd_h - 6
        pw = w - pad_l - pad_r
        shift = self._drag_px

        vals = [p[1] for p in pts]
        hi, lo = max(vals), min(vals)
        if initial:
            hi = max(hi, initial)
            lo = min(lo, initial)
        if hi - lo < 1e-6:
            hi, lo = hi + 1, lo - 1
        pad = (hi - lo) * 0.15
        hi += pad
        lo -= pad
        rng = hi - lo
        self.slot = pw / max(1, len(pts) - 1)

        def x_of(i):
            return pad_l + (i / (len(pts) - 1)) * pw + shift

        def y_of(v):
            return pad_t + ph - (v - lo) / rng * ph

        for i in range(5):
            v = lo + rng * i / 4
            y = y_of(v)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["grid"])
            self.create_text(w - pad_r + 6, y, text=f"${v:,.0f}", fill=t["dim"], anchor="w",
                             font=("Consolas", 10))

        # 初始资金线
        if initial:
            y = y_of(initial)
            self.create_line(pad_l, y, pad_l + pw, y, fill=t["faint"], dash=(4, 4))
            self.create_text(pad_l + 4, y - 8, text=f"初始 ${initial:,.0f}", fill=t["faint"],
                             anchor="w", font=("Consolas", 10))

        # 净值面积 + 折线
        fill_pts = [x_of(0), y_of(lo)]
        fill_pts += [(x_of(i), y_of(v)) for i, (_, v) in enumerate(pts)]
        fill_pts += [x_of(len(pts) - 1), y_of(lo)]
        self.create_polygon(fill_pts, fill=_mix(t["equity"], t["bg"], 0.82), outline="")
        self.create_line(*[c for i, (_, v) in enumerate(pts) for c in (x_of(i), y_of(v))],
                         fill=t["equity"], width=2)

        # 交易标记(按时间就近定位)
        marks = d.get("marks", [])
        if marks:
            import bisect
            ts_list = [p[0] for p in pts]
            for mts, good in marks:
                i = bisect.bisect_left(ts_list, mts)
                if i >= len(pts):
                    i = len(pts) - 1
                x = x_of(i)
                y = y_of(pts[i][1])
                col = t["good"] if good else t["bad"]
                self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=col, outline=t["bg"])

        # 回撤区(水下曲线:峰值与净值之间填充,独立比例尺)
        peak = pts[0][1]
        dds = []
        for _, v in pts:
            peak = max(peak, v)
            dds.append(peak - v)
        dd_max = max(dds) if dds else 0.0
        base_y = pad_t + ph + dd_h
        band_top = base_y - dd_h
        self.create_rectangle(pad_l, band_top, pad_l + pw, base_y, fill=t["panel2"], outline="")
        self.create_line(pad_l, base_y, pad_l + pw, base_y, fill=t["border"])
        if dd_max > 1e-9:
            def y_dd(d):
                return band_top + 2 + (d / dd_max) * (dd_h - 6)
            peak_line = [(x_of(i), band_top + 1) for i in range(len(pts))]
            val_line = [(x_of(i), y_dd(d)) for i, d in enumerate(dds)]
            area = peak_line + list(reversed(val_line))
            self.create_polygon(area, fill=_mix(t["dd"], t["panel2"], 0.62), outline="")
            self.create_line(*[c for p in val_line for c in p], fill=t["dd"], width=1)
        else:
            self.create_line(pad_l, band_top + 1, pad_l + pw, band_top + 1, fill=t["faint"])
        self.create_text(pad_l + 4, band_top + 2, text=f"回撤", fill=t["dim"], anchor="nw",
                         font=("Microsoft YaHei UI", 10))
        if dd_max > 1e-9:
            self.create_text(pad_l + pw - 4, band_top + 2, text=f"最大 ${dd_max:,.0f}", fill=t["dd"],
                             anchor="ne", font=(FONT_NUM, 10))


class PosTable(tk.Frame):
    """持仓明细表(自绘网格)。

    ttk.Treeview 的 tag 只能整行着色,做不到"只把盈亏数字变色",
    因此这里改用 Label 拼一张可逐格精确控制的表格。
    净持仓模型下最多一行,列宽固定、按屏幕缩放换算。
    """

    # (键, 表头, 基准列宽, 对齐方式, 字体类型)
    COLS = (
        ("oid",    "订单号",   190, "w",     "num"),
        ("dir",    "方向",      56, "center", "cn"),
        ("qty",    "数量",      56, "center", "num"),
        ("avg",    "开仓均价",  92, "e",     "num"),
        ("last",   "现价",      92, "e",     "num"),
        ("diff",   "价差",      70, "e",     "num"),
        ("pts",    "盈利点数",  84, "e",     "num"),
        ("pnl",    "浮动盈亏", 104, "e",     "num"),
        ("margin", "保证金",    96, "e",     "num"),
        ("liq",    "强平价",    92, "e",     "num"),
    )

    def __init__(self, master, theme: dict, rows: int = 1, **kw):
        super().__init__(master, bg=theme["panel"], **kw)
        self.theme = theme
        self._rows = rows
        self._header = []
        self._cells = []
        self._build()

    # -- 构建 -------------------------------------------------------------
    def _build(self):
        t = self.theme
        for i, (key, txt, w, anchor, ftype) in enumerate(self.COLS):
            self.grid_columnconfigure(i, minsize=uidefs.px(w))
            h = tk.Label(self, text=txt, bg=t["panel3"], fg=t["dim"],
                         font=("Microsoft YaHei UI", 11), anchor="center")
            h.grid(row=0, column=i, sticky="ew", padx=1, pady=(0, 1))
            self._header.append(h)
        for r in range(self._rows):
            row = []
            for i, (key, txt, w, anchor, ftype) in enumerate(self.COLS):
                lbl = tk.Label(self, text="--", bg=t["panel"], fg=t["text"],
                               font=self._font(ftype), anchor=anchor)
                lbl.grid(row=r + 1, column=i, sticky="ew", padx=1, pady=2)
                row.append(lbl)
            self._cells.append(row)

    @staticmethod
    def _font(ftype: str, bold: bool = False):
        if ftype == "cn":
            return ("Microsoft YaHei UI", 12, "bold") if bold else ("Microsoft YaHei UI", 12)
        return (FONT_NUM, 12, "bold") if bold else (FONT_NUM, 12)

    # -- 更新 -------------------------------------------------------------
    def set_row(self, r: int, values: dict, pnl_color: str = None):
        """更新第 r 行。pnl_color 只作用于"浮动盈亏"这一格。"""
        t = self.theme
        if r >= len(self._cells):
            return
        row = self._cells[r]
        for i, (key, txt, w, anchor, ftype) in enumerate(self.COLS):
            lbl = row[i]
            fg = t["text"]
            bold = (key == "pnl")
            if key == "pnl" and pnl_color:
                fg = pnl_color
            lbl.configure(text=values.get(key, "--"), fg=fg,
                          font=self._font(ftype, bold), bg=t["panel"])

    def clear(self):
        for r in range(len(self._cells)):
            self.set_row(r, {})

    def apply_theme(self, theme: dict):
        self.theme = theme
        self.configure(bg=theme["panel"])
        for h in self._header:
            h.configure(bg=theme["panel3"], fg=theme["dim"])
        for row in self._cells:
            for lbl in row:
                lbl.configure(bg=theme["panel"], fg=theme["text"])
