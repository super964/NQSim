"""
界面 DPI / 缩放支持
==================

Windows 高分屏(125%/150%/175%)下,若程序没有声明 DPI 感知,
系统会按 96 DPI 渲染窗口后再做位图拉伸 —— 结果就是**整体发糊**。
本模块负责:

1. 在创建任何窗口之前声明 **Per-Monitor V2 DPI 感知**;
2. 把 Tk 的 point→像素换算强制对齐真实 DPI(`tk scaling`),
   使字号按 **真实物理尺寸(pt)** 渲染 —— 不管屏幕是 100% 还是 200%,
   9pt 就是 9pt 大小,但始终锐利;
3. 暴露像素级缩放系数,供行高/间距/窗口尺寸这类"像素单位"使用。
"""

from __future__ import annotations

import ctypes
import sys

_state = {"dpi": 96.0, "scale": 1.0}


def enable_dpi_awareness() -> bool:
    """声明 Per-Monitor V2 DPI 感知(必须在创建窗口之前调用)。"""
    if sys.platform != "win32":
        return False
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return True
    except Exception:
        pass
    return False


def detect(root) -> float:
    """按真实 DPI 初始化缩放系数,返回缩放系数(1.0 = 96 DPI)。"""
    dpi = 96.0
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        dpi = 96.0
    if dpi < 48 or dpi > 960:
        dpi = 96.0
    _state["dpi"] = dpi
    _state["scale"] = dpi / 96.0
    try:
        # 强制 Tk 按真实 DPI 换算 point→像素(字体才是物理尺寸且锐利)
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    return _state["scale"]


def scale() -> float:
    """当前缩放系数(1.0 = 96 DPI;150% 屏为 1.5)。"""
    return _state["scale"]


def dpi() -> float:
    return _state["dpi"]


def px(base: int) -> int:
    """把"96 DPI 基准的像素数"换算成当前屏幕的真实像素(用于行高/间距/窗口尺寸)。"""
    return max(1, int(round(base * _state["scale"])))
