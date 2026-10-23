"""格式化工具。"""

from __future__ import annotations


def fmt_price(p: float, tick: float = 0.25) -> str:
    if p is None:
        return "--"
    dec = 0 if tick >= 1 else (1 if tick < 0.25 else 2)
    return f"{p:,.{dec}f}"


def fmt_money(v: float, dec: int = 2) -> str:
    if v is None:
        return "--"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.{dec}f}"


def fmt_signed_money(v: float, dec: int = 2) -> str:
    if v is None:
        return "--"
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.{dec}f}"


def fmt_qty(q: float) -> str:
    if q is None:
        return "--"
    if abs(q - round(q)) < 1e-9:
        return f"{int(round(q))}"
    return f"{q:,.2f}".rstrip("0").rstrip(".")


def fmt_time(ts, with_date: bool = True) -> str:
    if ts is None:
        return "--"
    return ts.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_clock(ts) -> str:
    if ts is None:
        return "--:--:--"
    return ts.strftime("%H:%M:%S")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"
