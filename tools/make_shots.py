"""批量生成交付用截图(进程内直接抓窗口,不走外部自动化)。

用法:  python make_shots.py
输出:  shot_dark / shot_light / shot_hover / shot_settings / shot_ma_dialog /
       shot_capital / shot_sl_tp / shot_sl_tp_set / shot_chart_only
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import tkinter as tk

from nqsim.uidefs import enable_dpi_awareness
enable_dpi_awareness()

from nqsim.app import App

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- PNG 编码
def _png(width, height, rgba: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def capture(widget, path: Path) -> bool:
    """抓取某个控件所属顶层窗口的位图(支持主窗口与对话框)。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    top = widget.winfo_toplevel()
    top.update_idletasks()
    top.update()
    hwnd = int(top.wm_frame(), 16)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    raw = bytearray(buf.raw)
    for i in range(0, len(raw), 4):
        raw[i], raw[i + 2] = raw[i + 2], raw[i]
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        return False
    path.write_bytes(_png(w, h, bytes(raw)))
    return True


def shot(widget, name: str):
    time.sleep(0.35)
    widget.update()
    out = HERE / f"shot_{name}.png"
    ok = capture(widget, out)
    print(f"  capture {out.name}: {'OK' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------- 主流程
root = tk.Tk()
app = App(root)
app.engine.paused = True
root.update()

# 最大化(与交付截图一致:整屏铺满)
user32 = ctypes.windll.user32
hwnd = int(root.wm_frame(), 16)
user32.ShowWindow(hwnd, 3)          # SW_MAXIMIZE
root.update()
time.sleep(0.6)
root.update()

sim = app.sim


def run(seconds, step=30):
    for _ in range(int(seconds // step)):
        sim.step(step)
        app._refresh()
        root.update_idletasks()


def wait_big_order(max_steps=40):
    """推进几秒仿真,直到分笔成交窗口里出现一笔大单(>=10 手)。"""
    for _ in range(max_steps):
        sim.step(1)
        app._refresh()
        root.update_idletasks()
        tape = list(sim.market.tape)
        if any(t[2] >= 10 for t in tape[-13:]):
            return True
    return False


print("生成截图...")
run(900)                                   # 先跑 15 分钟,让K线与分笔成交有内容
price = sim.price
app.qty_var.set("2")
app.place_order("buy")                     # 开多 2 手
run(240)

# --- 止盈止损编辑模式(双击开仓线) ---
app._chart_edit_request()
root.update()
shot(root, "sl_tp")

# --- 拖动止损线 -> 挂出止损/止盈单 ---
mx = app.chart.winfo_width() // 2
y_sl = int(app.chart.y_at(app.chart.edit_sl))
app.chart.event_generate("<ButtonPress-1>", x=mx, y=y_sl, when="now")
app.chart.event_generate("<B1-Motion>", x=mx, y=y_sl + 46, when="now")
app.chart.event_generate("<ButtonRelease-1>", x=mx, y=y_sl + 46, when="now")
root.update()
run(60)
app._refresh()
root.update()
wait_big_order()
shot(root, "sl_tp_set")

# --- 悬停读数 ---
hx = int(app.chart.winfo_width() * 0.62)
hy = int(app.chart.winfo_height() * 0.36)
app.chart.hover = (hx, hy)
app.chart.redraw()
root.update()
shot(root, "hover")
app.chart.hover = None
app.chart.redraw()

# --- 图表放大布局 ---
app.set_bot_mode("chart")
root.update()
time.sleep(0.3)
root.update()
shot(root, "chart_only")
app.set_bot_mode("full")
root.update()

# --- 夜间主界面 ---
app._refresh()
root.update()
wait_big_order()
shot(root, "dark")

# --- 设置面板 ---
app.open_settings()
root.update()
time.sleep(0.4)
shot(app.settings_win, "settings")
app._close_settings()
root.update()

# --- 均线设置对话框 ---
app.open_ma_dialog()
root.update()
time.sleep(0.4)
ma_win = root.winfo_children()[-1]
for w in root.winfo_children():
    try:
        if w.winfo_class() == "Toplevel" and w.wm_title() == "均线设置":
            ma_win = w
    except tk.TclError:
        pass
shot(ma_win, "ma_dialog")
try:
    ma_win.destroy()
except tk.TclError:
    pass
root.update()

# --- 初始资金对话框 ---
app.open_capital_dialog()
root.update()
time.sleep(0.4)
cap_win = None
for w in root.winfo_children():
    try:
        if w.winfo_class() == "Toplevel" and w.wm_title() == "初始资金设置":
            cap_win = w
    except tk.TclError:
        pass
if cap_win is not None:
    shot(cap_win, "capital")
    try:
        cap_win.destroy()
    except tk.TclError:
        pass
root.update()

# --- 日间主界面 ---
app.toggle_theme()
root.update()
time.sleep(0.6)
root.update()
app._refresh()
root.update()
wait_big_order()
shot(root, "light")

print("权益", round(sim.equity(), 2), "持仓", sim.broker.qty)
app.engine.stop()
root.destroy()
print("完成")
