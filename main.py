"""程序入口 —— 打包为 exe 时的启动文件。"""

from __future__ import annotations

import multiprocessing
import sys


def main():
    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()
    # 高分屏下必须最先声明 DPI 感知,否则窗口会被系统按 96DPI 拉伸 → 整体发糊
    from nqsim.uidefs import enable_dpi_awareness
    enable_dpi_awareness()
    from nqsim.app import main as run
    run()


if __name__ == "__main__":
    main()
