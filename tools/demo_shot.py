"""演示脚本:在真实运行的界面里走一遍完整交易流程并抓取窗口截图。"""

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

WS = sys.argv[1] if len(sys.argv) > 1 else "dark"
VIEW = sys.argv[2] if len(sys.argv) > 2 else "K线图"


# ---------------------------------------------------------------- 截图
def _png(width, height, rgba: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def capture(root: tk.Tk, path: str) -> bool:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = int(root.wm_frame(), 16)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

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
    Path(path).write_bytes(_png(w, h, bytes(raw)))
    return True


# ---------------------------------------------------------------- 主流程
root = tk.Tk()
app = App(root)
app.engine.paused = True          # 演示期间由脚本控制时间
root.update()
root.geometry("1600x950")
root.update()

if WS == "light":
    app.toggle_theme()
    root.update_idletasks()

sim = app.sim


def run(seconds, step=30):
    for _ in range(int(seconds // step)):
        sim.step(step)
        app._refresh()
        root.update_idletasks()


run(600)                                    # 先跑 10 分钟
price = sim.price
app.qty_var.set("3")
app.stop_var.set(f"{price - 55:.2f}")
app.protect_var.set(1)
app.place_order("buy")                      # 开多 + 自动止损
run(120)
app.price_var.set("限价")
app.limit_var.set(f"{sim.price + 22:.2f}")
app.qty_var.set("2")
app.place_order("sell")                     # 限价止盈挂单
run(240)
app.sim.submit("close", {"fraction": 0.5})  # 部分平仓
run(180)
app.sim.submit("leverage", {"leverage": 30})
run(240)
app.sim.submit("order", {"side": "sell", "otype": "market", "qty": 1,
                          "limit": None, "stop": None, "protect": None})
run(120)

app.set_view(VIEW)
app._refresh()
root.update()
time.sleep(0.8)
root.update()

out = Path(__file__).resolve().parent / f"shot_{WS}.png"
print("capture ->", out, capture(root, str(out)))
print("权益", round(sim.equity(), 2), "持仓", sim.broker.qty,
      "委托", [o.describe() for o in sim.broker.orders.values() if o.status == "working"])
app.engine.stop()
root.destroy()
