"""
市场模拟引擎
============

模拟 E-mini 纳斯达克100 期货(NQ) / Micro(MNQ) / 纳斯达克100 CFD 的真实交易环境:

* 真实交易日历:CME Globex 周日18:00(美东)开盘 -> 周五17:00收盘,每日17:00-18:00维护休市,
  周末休市,美国法定假日全天休市,节假日前一个交易日13:00提前收盘。
* 真实美东时区:自行实现 EST/EDT 夏令时规则(不依赖系统 tzdata)。
* 真实时间周期:逐笔成交带亚秒时间戳,1秒一根节拍,K线按周期聚合。
* 真实波动结构:日内 U 型波动率分布 + OU 型波动聚集(类GARCH) + 事件驱动跳空。
* 真实宏观事件:非农、CPI、PPI、零售销售、PCE、PMI、ISM、初请失业金、FOMC、GDP、
  巨头财报,均带真实发布时刻(美东)与波动冲击。
* 真实盘口:买卖价差随流动性/事件变化,成交量随时间与事件变化。
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections import deque
from typing import Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------
# 时区:美东时间(自行实现夏令时,避免 Windows 缺少 tzdata)
# --------------------------------------------------------------------------


class EasternTime(dt.tzinfo):
    """美国东部时间 EST/EDT。"""

    _dst_cache: dict = {}

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> dt.date:
        d = dt.date(year, month, 1)
        first_sunday = 1 + (6 - d.weekday()) % 7
        return dt.date(year, month, first_sunday + 7 * (n - 1))

    def _dst_bounds(self, year: int):
        """夏令时起止时刻(每年固定,缓存以避免高频重复计算)。"""
        b = EasternTime._dst_cache.get(year)
        if b is None:
            start = dt.datetime.combine(self._nth_sunday(year, 3, 2), dt.time(2, 0))
            end = dt.datetime.combine(self._nth_sunday(year, 11, 1), dt.time(2, 0))
            b = (start, end)
            EasternTime._dst_cache[year] = b
        return b

    def _is_dst(self, d: dt.datetime) -> bool:
        # 3月第二个周日 02:00 -> 11月第一个周日 02:00 (2007 年后美国规则)
        start, end = self._dst_bounds(d.year)
        naive = d.replace(tzinfo=None)
        return start <= naive < end

    def utcoffset(self, d):
        return dt.timedelta(hours=-4) if self._is_dst(d) else dt.timedelta(hours=-5)

    def dst(self, d):
        return dt.timedelta(hours=1) if self._is_dst(d) else dt.timedelta(0)

    def tzname(self, d):
        return "EDT" if self._is_dst(d) else "EST"


ET = EasternTime()

# --------------------------------------------------------------------------
# 合约规格(真实参数)
# --------------------------------------------------------------------------

INSTRUMENTS = {
    "NQ": {
        "label": "NQ  E-mini 纳斯达克100期货",
        "mult": 20.0,        # 合约乘数 $/点
        "tick": 0.25,        # 最小变动价位
        "comm": 2.09,        # 经纪商佣金 $/手/边
        "fee": 2.29,         # 交易所+清算费 $/手/边
        "lot": 1.0,
        "base_price": 24500.0,
    },
    "MNQ": {
        "label": "MNQ  Micro E-mini 纳斯达克100",
        "mult": 2.0,
        "tick": 0.25,
        "comm": 0.62,
        "fee": 0.62,
        "lot": 1.0,
        "base_price": 24500.0,
    },
    "NAS100": {
        "label": "NAS100  纳斯达克100 CFD",
        "mult": 1.0,
        "tick": 0.1,
        "comm": 0.0,
        "fee": 0.0,
        "lot": 0.1,
        "base_price": 24500.0,
    },
}

BAR_PERIODS = {
    60: "1分", 180: "3分", 300: "5分", 600: "10分", 900: "15分",
    1800: "30分", 3600: "1时", 7200: "2时", 14400: "4时", 86400: "日",
}
# 每秒基准成交笔数(再乘以时段活跃度)
BASE_PRINT_RATE = 14.0
# 分笔成交的手数分布(仿真实纳指盘口):小单为主、夹杂中单、偶发机构大单
# (累计概率上界, 手数区间)
PRINT_SIZE_BUCKETS = (
    (0.790, (1, 1)),        # 79%   1 手(最小成交)
    (0.910, (2, 3)),        # 12%   2~3 手
    (0.970, (4, 9)),        # 6%    4~9 手
    (0.994, (10, 25)),      # 2.4%  10~25 手
    (1.010, (26, 120)),     # 0.6%  26~120 手(机构大单)
)
# 平均每笔手数:把"成交笔数"折算成"成交手数",保证历史合成与实时行情口径一致
AVG_PRINT_SIZE = 2.338
# 底仓K线粒度(秒):所有显示周期都由它分组而来,粒度越细高周期历史越完整
BASE_PERIOD = 10
# 小时线粒度:1时/2时/4时由它分组而来,提供数月历史
HOUR_PERIOD = 3600
# 日线粒度:按美东自然日对齐
DAY_PERIOD = 86400
# 各聚合层的历史容量(根数)
MAX_BASE_BARS = 36000      # 10秒线 ≈ 100 小时
MAX_HOUR_BARS = 6000       # 小时线 ≈ 250 天
MAX_DAY_BARS = 800         # 日线 ≈ 2 年

# --------------------------------------------------------------------------
# 假日与提前收盘
# --------------------------------------------------------------------------

_HOLIDAY_CACHE: dict = {}
_EARLY_CACHE: dict = {}


def _easter(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """weekday: 0=周一 ... 6=周日"""
    d = dt.date(year, month, 1)
    first = 1 + (weekday - d.weekday()) % 7
    return dt.date(year, month, first + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        d = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    last = d - dt.timedelta(days=(d.weekday() - weekday) % 7)
    return last


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 6:
        return d - dt.timedelta(days=1)
    if d.weekday() == 5:
        return d + dt.timedelta(days=1)
    return d


def holidays_for(year: int) -> set:
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    hol = set()
    hol.add(_observed(dt.date(year, 1, 1)))                     # 元旦
    hol.add(_nth_weekday(year, 1, 0, 3))                        # 马丁路德金日
    hol.add(_nth_weekday(year, 2, 0, 3))                        # 总统日
    hol.add(_easter(year) - dt.timedelta(days=2))               # 耶稣受难日
    hol.add(_last_weekday(year, 5, 0))                          # 阵亡将士纪念日
    hol.add(_observed(dt.date(year, 6, 19)))                    # 六月节
    hol.add(_observed(dt.date(year, 7, 4)))                     # 独立日
    hol.add(_nth_weekday(year, 9, 0, 1))                        # 劳动节
    hol.add(_nth_weekday(year, 11, 3, 4))                       # 感恩节
    hol.add(_observed(dt.date(year, 12, 25)))                   # 圣诞
    _HOLIDAY_CACHE[year] = hol
    return hol


def early_closes_for(year: int) -> set:
    """13:00(美东)提前收盘的交易日。"""
    if year in _EARLY_CACHE:
        return _EARLY_CACHE[year]
    hol = holidays_for(year)
    ec = set()

    def prev_business_day(d: dt.date) -> dt.date:
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5 or d in hol:
            d -= dt.timedelta(days=1)
        return d

    # 法定假日前一个交易日提前收盘
    for h in hol:
        ec.add(prev_business_day(h))
    # 感恩节次日
    ec.add(_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1))
    # 平安夜(周一至周四)
    ce = dt.date(year, 12, 24)
    if ce.weekday() <= 3 and ce not in hol:
        ec.add(ce)
    # 独立日前夕
    j3 = dt.date(year, 7, 3)
    if j3.weekday() <= 4 and j3 not in hol:
        ec.add(j3)
        if j3.weekday() == 4:      # 7月3日是周五 -> 周四也提前收盘
            ec.add(j3 - dt.timedelta(days=1))
    _EARLY_CACHE[year] = ec
    return ec


def is_holiday(d: dt.date) -> bool:
    return d in holidays_for(d.year)


def is_early_close(d: dt.date) -> bool:
    return d in early_closes_for(d.year)


# --------------------------------------------------------------------------
# 交易日历
# --------------------------------------------------------------------------

SESSION_START_HOUR = 18    # 美东 18:00 开盘
SESSION_END_HOUR = 17      # 美东 17:00 收盘
EARLY_CLOSE_HOUR = 13      # 提前收盘时间
RTH_OPEN = (9, 30)         # 美东常规交易时段开始
RTH_CLOSE = (16, 0)        # 美东常规交易时段结束


class Calendar:
    """CME Globex 日历。"""

    def __init__(self, now: dt.datetime):
        self.now = now

    # -- 会话起点/终点 ---------------------------------------------------
    def session_start(self, day: dt.date) -> dt.datetime:
        return dt.datetime(day.year, day.month, day.day, SESSION_START_HOUR, 0, tzinfo=ET)

    def session_end(self, start_day: dt.date) -> dt.datetime:
        """给定开盘日,返回收盘时刻(含提前收盘/假日规则)。"""
        nxt = start_day + dt.timedelta(days=1)
        if is_holiday(nxt):
            return dt.datetime(start_day.year, start_day.month, start_day.day,
                               EARLY_CLOSE_HOUR, 0, tzinfo=ET)
        if is_early_close(nxt):
            return dt.datetime(nxt.year, nxt.month, nxt.day, EARLY_CLOSE_HOUR, 0, tzinfo=ET)
        return dt.datetime(nxt.year, nxt.month, nxt.day, SESSION_END_HOUR, 0, tzinfo=ET)

    def can_start_session(self, day: dt.date) -> bool:
        """该日18:00是否可以开盘(周五无夜盘,假日/提前收盘日不再开新盘)。"""
        if day.weekday() == 4:            # 周五:无夜盘
            return False
        if day.weekday() == 5:            # 周六
            return False
        if is_holiday(day):
            return False
        if is_early_close(day):
            return False
        return True

    def next_session_start(self, after: dt.datetime) -> dt.datetime:
        day = after.date()
        for _ in range(20):
            if self.can_start_session(day):
                cand = self.session_start(day)
                if cand > after:
                    return cand
            day += dt.timedelta(days=1)
        return after + dt.timedelta(days=1)

    def session_start_day_for(self, t: dt.datetime) -> dt.date:
        """t 所属会话的开盘日(18:00 那天)。"""
        day = t.date()
        return day if t.hour >= SESSION_START_HOUR else day - dt.timedelta(days=1)

    def is_open(self, t: dt.datetime) -> bool:
        """是否处于交易时段:属于某个有效会话 [18:00开盘日, 收盘时刻] 内。"""
        if t.date().weekday() == 5:          # 周六
            return False
        if is_holiday(t.date()):
            return False
        sd = self.session_start_day_for(t)
        if not self.can_start_session(sd):
            return False
        return t < self.session_end(sd)

    def session_label(self, t: dt.datetime) -> str:
        if not self.is_open(t):
            return "休市"
        if t.hour >= SESSION_START_HOUR:
            return "夜盘"
        if (t.hour, t.minute) >= RTH_CLOSE:
            return "盘后"
        if (t.hour, t.minute) >= RTH_OPEN:
            return "常规交易时段"
        return "盘前"


# --------------------------------------------------------------------------
# 宏观事件日历
# --------------------------------------------------------------------------

def _first_weekday(year, month, weekday):
    d = dt.date(year, month, 1)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    return d


def _last_business_day(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    while d.weekday() >= 5 or is_holiday(d):
        d -= dt.timedelta(days=1)
    return d


def _nearest_weekday(year, month, day):
    d = dt.date(year, month, day)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def generate_macro_events(year: int, rng: random.Random) -> List[Tuple[dt.datetime, str, int]]:
    """生成整年的宏观事件表:(美东时间, 标题, 强度1-3)。"""
    ev: List[Tuple[dt.datetime, str, int]] = []

    def add(d: dt.date, h: int, m: int, title: str, sev: int):
        if is_holiday(d):
            return
        ev.append((dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ET), title, sev))

    for mo in range(1, 13):
        # 非农:当月第一个周五 8:30
        nfp = _first_weekday(year, mo, 4)
        add(nfp, 8, 30, "美国非农就业报告 (NFP)", 3)
        # CPI:约13日
        add(_nearest_weekday(year, mo, 13), 8, 30, "美国消费者物价指数 (CPI)", 3)
        # PPI:约16日
        add(_nearest_weekday(year, mo, 16), 8, 30, "美国生产者物价指数 (PPI)", 1)
        # 零售销售:约17日
        add(_nearest_weekday(year, mo, 17), 8, 30, "美国零售销售", 2)
        # PCE 物价指数:月末最后一个工作日
        add(_last_business_day(year, mo), 8, 30, "美国核心PCE物价指数", 2)
        # ISM 制造业/服务业:当月第一个工作日 10:00
        b1 = _first_weekday(year, mo, 0)
        add(b1, 10, 0, "美国ISM制造业PMI", 1)
        add(b1, 10, 0, "美国ISM服务业PMI", 1)
        # 初请失业金:每周四 8:30
        d = _first_weekday(year, mo, 3)
        while d.month == mo:
            add(d, 8, 30, "美国初请失业金人数", 1)
            d += dt.timedelta(days=7)

    # FOMC 利率决议:约8次/年,美东 14:00
    fomc_months = {1: 3, 3: 3, 4: 4, 5: 3, 6: 3, 7: 4, 9: 3, 10: 4, 11: 3, 12: 3}
    for mo, n in fomc_months.items():
        d = _nth_weekday(year, mo, 2, n)   # 第三个/第四个周三
        add(d, 14, 0, "FOMC 利率决议与新闻发布会", 3)

    # GDP 季度
    for mo in (1, 4, 7, 10):
        d = _nearest_weekday(year, mo, 26 if mo in (1, 10) else 28)
        add(d, 8, 30, "美国季度GDP初值", 2)

    # 巨头财报(盘后 16:30)
    earnings = {
        1: [("AAPL", 2), ("MSFT", 2), ("META", 2)],
        2: [("AMZN", 2)],
        4: [("AAPL", 2), ("MSFT", 2), ("TSLA", 3), ("META", 2)],
        5: [("NVDA", 3), ("AMZN", 2)],
        7: [("AAPL", 2), ("MSFT", 2), ("TSLA", 3), ("META", 2)],
        8: [("NVDA", 3), ("AMZN", 2)],
        10: [("AAPL", 2), ("MSFT", 2), ("TSLA", 3), ("META", 2)],
        11: [("NVDA", 3), ("AMZN", 2)],
    }
    for mo, lst in earnings.items():
        d = _nearest_weekday(year, mo, 22 if mo in (1, 10) else 24)
        for ticker, sev in lst:
            add(d, 16, 30, f"{ticker} 财报(盘后)", sev)

    ev.sort(key=lambda x: x[0])
    return ev


# --------------------------------------------------------------------------
# 波动率 / 成交量 日内形态
# --------------------------------------------------------------------------

# 每个美东小时的相对波动率(平方根尺度)与相对成交活跃度
_VOL_SHAPE = {
    0: 0.72, 1: 0.70, 2: 0.68, 3: 0.70, 4: 0.75, 5: 0.80, 6: 0.85, 7: 0.95,
    8: 1.10, 9: 1.45, 10: 1.60, 11: 1.45, 12: 1.15, 13: 1.25, 14: 1.30,
    15: 1.45, 16: 1.55, 17: 1.45, 18: 1.90, 19: 1.55, 20: 1.25, 21: 1.05,
    22: 0.90, 23: 0.80,
}
_ACT_SHAPE = {
    0: 0.45, 1: 0.38, 2: 0.35, 3: 0.36, 4: 0.40, 5: 0.45, 6: 0.55, 7: 0.75,
    8: 1.10, 9: 3.20, 10: 2.10, 11: 1.55, 12: 1.15, 13: 1.25, 14: 1.40,
    15: 1.60, 16: 1.80, 17: 1.20, 18: 3.00, 19: 1.70, 20: 1.25, 21: 1.00,
    22: 0.80, 23: 0.60,
}
# 09:30 开盘瞬间的尖峰
_OPEN_SPIKE = {9: {30: 3.0, 31: 2.2, 32: 1.7, 33: 1.4, 34: 1.2, 35: 1.1}}
_CLOSE_SPIKE = {15: {50: 1.6, 51: 1.8, 52: 2.0, 53: 2.2, 54: 2.4, 55: 2.6, 56: 2.8, 57: 3.0, 58: 3.2, 59: 3.4}}
_NIGHT_SPIKE = {18: {0: 3.0, 1: 2.2, 2: 1.7, 3: 1.4, 4: 1.2, 5: 1.1}}


def _shape_factor(hour: int, minute: int) -> float:
    v = _VOL_SHAPE.get(hour, 1.0)
    if hour in _OPEN_SPIKE and minute in _OPEN_SPIKE[hour]:
        v *= _OPEN_SPIKE[hour][minute]
    if hour in _CLOSE_SPIKE and minute in _CLOSE_SPIKE[hour]:
        v *= _CLOSE_SPIKE[hour][minute]
    if hour in _NIGHT_SPIKE and minute in _NIGHT_SPIKE[hour]:
        v *= _NIGHT_SPIKE[hour][minute]
    return v


def _activity_factor(hour: int, minute: int) -> float:
    a = _ACT_SHAPE.get(hour, 1.0)
    if hour == 9 and 30 <= minute <= 34:
        a *= 1.8
    if hour == 18 and minute <= 10:
        a *= 2.2
    if hour == 15 and minute >= 50:
        a *= 1.5
    return a


# --------------------------------------------------------------------------
# 行情模拟器
# --------------------------------------------------------------------------

def _group_start(ts: float, period: int, align: str = "epoch") -> float:
    """分组起点(epoch 秒)。align='et_day' 时按美东自然日对齐。"""
    if align == "et_day":
        d = dt.datetime.fromtimestamp(ts, ET).date()
        return dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()
    return (int(ts) // period) * period


class BarAgg:
    """单周期K线聚合器。所有周期并行维护,切换周期即时且历史完整。"""

    __slots__ = ("period", "max_bars", "bars", "bar", "base", "align")

    def __init__(self, period: int, max_bars: int = 1200, align: str = "epoch"):
        self.period = period
        self.max_bars = max_bars
        self.bars: List[dict] = []
        self.bar: Optional[dict] = None
        self.base = 0          # 已从头部丢弃的K线数(用于快照回退)
        self.align = align     # epoch | et_day

    def _ensure(self, ts: dt.datetime, px: float, epoch: float = None) -> dict:
        """确保有一根覆盖 ts 的当前K线,返回它。"""
        b = self.bar
        if b is None or (epoch if epoch is not None else ts.timestamp()) >= b["end"]:
            self.flush()
            ep = epoch if epoch is not None else ts.timestamp()
            start = _group_start(ep, self.period, self.align)
            self.bar = {"t": start, "end": start + self.period,
                        "o": px, "h": px, "l": px, "c": px, "v": 0}
            b = self.bar
        return b

    def update(self, ts: dt.datetime, px: float, epoch: float = None, qty: float = 1):
        b = self._ensure(ts, px, epoch)
        if px > b["h"]:
            b["h"] = px
        if px < b["l"]:
            b["l"] = px
        b["c"] = px
        b["v"] += qty

    def merge(self, ts: dt.datetime, o: float, h: float, l: float, c: float, v: int,
              epoch: float = None):
        """并入一根已合成的K线(用于快速历史生成)。"""
        b = self._ensure(ts, o, epoch)
        if h > b["h"]:
            b["h"] = h
        if l < b["l"]:
            b["l"] = l
        b["c"] = c
        b["v"] += v

    def flush(self):
        if self.bar is not None:
            self.bars.append(self.bar)
            if len(self.bars) > self.max_bars:
                del self.bars[0]
                self.base += 1
            self.bar = None

    def snapshot(self):
        return (self.base, len(self.bars), dict(self.bar) if self.bar else None)

    def restore(self, s):
        base_s, ln, cur = s
        self.base = base_s
        if len(self.bars) > ln:
            del self.bars[ln:]
        self.bar = dict(cur) if cur else None


class MarketSim:
    """逐笔行情生成器。状态可快照/恢复(用于倒退重放)。"""

    def __init__(self, instr_key: str = "NQ", seed: int = 20240117,
                 start: dt.datetime | None = None,
                 base_price: float | None = None,
                 daily_sigma: float = 0.0125,
                 bar_period: int = 60):
        self.instr_key = instr_key
        self.instr = INSTRUMENTS[instr_key]
        self.tick = self.instr["tick"]
        self.rng = random.Random(seed)
        # 成交手数用独立的随机流:不干扰价格路径的随机序列,
        # 保证行情波动率标定与倒带重演完全可复现
        self.size_rng = random.Random() if seed is None else random.Random(seed * 7919 + 104729)
        self.seed = seed
        self.daily_sigma = daily_sigma
        self.bar_period = bar_period
        self.calendar = Calendar(start or dt.datetime.now(ET))
        self.events = generate_macro_events((start or dt.datetime.now(ET)).year, self.rng)
        self.event_idx = 0
        self.now = start or dt.datetime.now(ET)
        self.price = base_price if base_price is not None else self.instr["base_price"]
        self.open_price = self.price
        self.prev_close = self.price
        self.log_vol = 0.0            # OU 波动聚集状态
        self.vwap_num = 0.0
        self.vwap_den = 0.0
        # 多级K线底仓:细周期用 10 秒线,中周期用小时线,长周期用日线。
        # 每层各自保留足够长的历史,切换到任意周期都能立即铺满K线。
        self.aggs = {
            BASE_PERIOD: BarAgg(BASE_PERIOD, max_bars=MAX_BASE_BARS),
            HOUR_PERIOD: BarAgg(HOUR_PERIOD, max_bars=MAX_HOUR_BARS),
            DAY_PERIOD: BarAgg(DAY_PERIOD, max_bars=MAX_DAY_BARS, align="et_day"),
        }
        self.max_bars = MAX_BASE_BARS
        self.cur_prints_per_sec = 4
        self.tape: deque = deque(maxlen=300)   # 分笔成交:(时间, 价格, 手数, 方向)
        self.tape_max = 300
        # 会话统计
        self.session_high = self.price
        self.session_low = self.price
        self.session_open = self.price
        self.session_vol = 0
        self.last_event = None
        self.last_event_price = None
        self.event_vol_mult = 1.0
        self.event_vol_until = None
        self.seconds_simulated = 0
        self.total_prints = 0
        self._session_start_day = self.now.date()
        self.session_start_price = self.price
        self._roll_session_if_needed()

    # -- 当前周期K线 -----------------------------------------------------
    @property
    def bars(self) -> List[dict]:
        """10 秒底仓K线。"""
        return self.aggs[BASE_PERIOD].bars

    @property
    def bar(self) -> Optional[dict]:
        return self.aggs[BASE_PERIOD].bar

    def _source_for(self, period: int):
        """选择该周期的数据来源聚合层。

        细周期走 10 秒底仓,中周期走小时线,日线走日线层——
        每层都保留足够长的历史,切换周期不会出现"只有几根K线"的情况。
        """
        if period <= 1800:
            return self.aggs[BASE_PERIOD], "epoch"
        if period <= 14400:
            return self.aggs[HOUR_PERIOD], "epoch"
        return self.aggs[DAY_PERIOD], "et_day"

    @staticmethod
    def _merge_chunk(chunk: List[dict]) -> dict:
        return {
            "t": chunk[0]["t"],
            "o": chunk[0]["o"],
            "h": max(b["h"] for b in chunk),
            "l": min(b["l"] for b in chunk),
            "c": chunk[-1]["c"],
            "v": sum(b["v"] for b in chunk),
        }

    def bars_for(self, period: int, n: int = 120) -> List[dict]:
        """按指定周期返回已收盘K线(按时间戳分组,休市缺口安全)。"""
        src, align = self._source_for(period)
        base = src.bars
        out: List[dict] = []
        hi = len(base)
        while hi > 0 and len(out) < n:
            t_last = base[hi - 1]["t"]
            gstart = _group_start(t_last, period, align)
            # 向前收集属于同一分组的基础K线(时间戳 >= 分组起点)
            lo = hi - 1
            while lo > 0 and base[lo - 1]["t"] >= gstart:
                lo -= 1
            chunk = base[lo:hi]
            if chunk:
                out.append(self._merge_chunk(chunk))
            hi = lo
        out.reverse()
        return out

    def current_bar_for(self, period: int) -> Optional[dict]:
        """指定周期下正在形成的K线。"""
        src, align = self._source_for(period)
        cur = src.bar
        if cur is None:
            return None
        if src.period == period:
            return dict(cur)
        base = src.bars
        gstart = _group_start(cur["t"], period, align)
        lo = len(base)
        while lo > 0 and base[lo - 1]["t"] >= gstart:
            lo -= 1
        chunk = base[lo:] + [cur]
        if not chunk:
            return None
        return self._merge_chunk(chunk)

    def session_bars(self, period: int = 60) -> List[dict]:
        """当前交易时段内的K线(分时图用)。"""
        if not self.calendar.is_open(self.now):
            return []
        sd = self.calendar.session_start_day_for(self.now)
        start_ts = self.calendar.session_start(sd).timestamp()
        src, align = self._source_for(period)
        base = src.bars
        out: List[dict] = []
        hi = len(base)
        while hi > 0:
            t_last = base[hi - 1]["t"]
            if t_last < start_ts:
                break
            gstart = _group_start(t_last, period, align)
            lo = hi - 1
            while lo > 0 and base[lo - 1]["t"] >= gstart:
                lo -= 1
            chunk = base[lo:hi]
            if chunk:
                out.append(self._merge_chunk(chunk))
            hi = lo
        out.reverse()
        return out

    def _roll_session_if_needed(self):
        cal = self.calendar
        if cal.is_open(self.now):
            start_day = cal.session_start_day_for(self.now)
            self._session_start_day = start_day
            self.session_end = cal.session_end(start_day)
        else:
            nxt = cal.next_session_start(self.now)
            self.now = nxt
            self._session_start_day = nxt.date()
            self.session_end = cal.session_end(nxt.date())
            self._apply_session_gap()

    def _apply_session_gap(self):
        """夜盘开盘跳空(隔夜消息面)。"""
        r = self.rng
        gap = r.gauss(0.0, 0.0011)
        if r.random() < 0.10:
            gap += r.choice([-1, 1]) * r.uniform(0.0018, 0.0045)
        self.prev_close = self.price
        self.price = self._round(self.price * (1.0 + gap))
        self.open_price = self.price
        self.session_start_price = self.price
        self.session_open = self.price
        self.session_high = self.price
        self.session_low = self.price
        self.session_vol = 0
        self.vwap_num = 0.0
        self.vwap_den = 0.0

    def _round(self, p: float) -> float:
        return round(round(p / self.tick) * self.tick, 4)

    # ------------------------------------------------------------------
    # 快速历史生成(启动预填)
    # ------------------------------------------------------------------
    def fast_history(self, days: int = 190, recent_hours: float = 66.0):
        """一次性生成历史行情,用于启动时铺满长周期K线。

        远期按小时粒度合成、近端按底仓(10秒)粒度合成,速度比逐笔快两个数量级。
        生成的K线与逐笔引擎同源同参数(波动聚集、日内形态、事件冲击、隔夜跳空),
        统计特征一致,因此高周期(10分/15分/1时/日)首次打开即有完整形态。
        """
        cal = self.calendar
        end = self.now
        split = (end - dt.timedelta(hours=recent_hours)).replace(minute=0, second=0, microsecond=0)
        start = split - dt.timedelta(days=days)

        # 事件表覆盖整个历史区间
        self.events = self._events_for_years(start.year, end.year)
        self.event_idx = 0
        self.last_event = None
        self.last_event_price = None

        # 1) 切分连续交易时段
        seg_h, seg_b = self._split_segments(start, split), self._split_segments(split, end)

        # 2) 合成K线(绝对价空间,最后统一缩放到当前价)
        state = {"price": self.price, "log_vol": self.log_vol,
                 "event_mult": 1.0, "event_until": None, "prev_close": self.prev_close,
                 "ev_epochs": [(e[0].timestamp(), e[1], e[2], e[0]) for e in self.events]}
        bars_h = self._fast_segment(seg_h, HOUR_PERIOD, state)
        bars_b = self._fast_segment(seg_b, BASE_PERIOD, state)

        # 3) 缩放,使最后一根K线的收盘价 == 当前价
        last_abs = state["price"]
        scale = (self.price / last_abs) if last_abs > 0 else 1.0

        # 4) 写入各聚合层
        agg_h = self.aggs[HOUR_PERIOD]
        agg_d = self.aggs[DAY_PERIOD]
        agg_b = self.aggs[BASE_PERIOD]
        last_close = self.price

        def scaled(bar):
            """缩放到当前价后重新贴合最小变动价位,保证历史价同样是整跳价格。"""
            ep, o, h, l, c, v = bar
            o2 = self._round(o * scale)
            c2 = self._round(c * scale)
            h2 = self._round(h * scale)
            l2 = self._round(l * scale)
            h2 = max(h2, o2, c2)
            l2 = min(l2, o2, c2)
            return ep, o2, h2, l2, c2, v

        for bar in bars_h:
            ep, o2, h2, l2, c2, v = scaled(bar)
            agg_h.merge(None, o2, h2, l2, c2, v, epoch=ep)
            agg_d.merge(None, o2, h2, l2, c2, v, epoch=ep)
            last_close = c2
        for bar in bars_b:
            ep, o2, h2, l2, c2, v = scaled(bar)
            agg_b.merge(None, o2, h2, l2, c2, v, epoch=ep)
            agg_h.merge(None, o2, h2, l2, c2, v, epoch=ep)
            agg_d.merge(None, o2, h2, l2, c2, v, epoch=ep)
            last_close = c2

        # 5) 收尾:结算当前K线,恢复会话统计
        # 只收 10 秒底仓(其分组在 now 处正好断开),小时/日线保持"正在形成"状态
        self.aggs[BASE_PERIOD].flush()
        self.price = self._round(last_close)
        self.prev_close = self._round(state["prev_close"] * scale)
        self.session_end = cal.session_end(self._session_start_day)
        self.total_prints += sum(b[5] for b in bars_h) + sum(b[5] for b in bars_b)
        # 当前会话统计
        sd = cal.session_start_day_for(self.now)
        sess_start_ts = cal.session_start(sd).timestamp()
        sess = [b for b in self.aggs[BASE_PERIOD].bars if b["t"] >= sess_start_ts]
        if sess:
            self.session_open = sess[0]["o"]
            self.session_high = max(b["h"] for b in sess)
            self.session_low = min(b["l"] for b in sess)
            self.session_vol = sum(b["v"] for b in sess)
            self.open_price = self.session_open
            w = self.session_vol or 1
            self.vwap_num = sum(b["c"] * b["v"] for b in sess)
            self.vwap_den = w
        if self.event_idx > 0:
            self.last_event = self.events[self.event_idx - 1]
        return len(bars_h), len(bars_b)

    def _events_for_years(self, y0: int, y1: int) -> List[Tuple[dt.datetime, str, int]]:
        ev: List[Tuple[dt.datetime, str, int]] = []
        for y in range(y0, y1 + 1):
            ev.extend(generate_macro_events(y, self.rng))
        ev.sort(key=lambda e: e[0])
        return ev

    def _split_segments(self, t0: dt.datetime, t1: dt.datetime):
        """把 [t0,t1) 切成若干连续交易时段(休市自动跳过)。"""
        cal = self.calendar
        segs = []
        t = t0
        guard = 0
        while t < t1 and guard < 4000:
            guard += 1
            if not cal.is_open(t):
                nxt = cal.next_session_start(t)
                t = nxt if nxt > t else t + dt.timedelta(hours=1)
                continue
            sd = cal.session_start_day_for(t)
            se = cal.session_end(sd)
            end = se if se < t1 else t1
            if end > t:
                segs.append((t, end))
            t = end
        return segs

    def _fast_segment(self, segs, gran: int, state: dict):
        """按 gran 粒度合成K线(绝对价格空间),返回 [(ts,o,h,l,c,v), ...]。"""
        rng = self.rng
        kappa = 0.0006
        eta = 0.45 * math.sqrt(2 * kappa)
        out = []
        base_sigma = self.daily_sigma / math.sqrt(1380 * 60)
        evs = state["ev_epochs"]
        for seg_t0, seg_t1 in segs:
            # 隔夜跳空
            gap = rng.gauss(0.0, 0.0011)
            if rng.random() < 0.10:
                gap += rng.choice([-1, 1]) * rng.uniform(0.0018, 0.0045)
            state["prev_close"] = state["price"]
            state["price"] = state["price"] * math.exp(gap)
            # 本时段内美东偏移固定(夏令时切换发生在休市时段),用算术推算本地时间
            s_epoch = seg_t0.timestamp()
            loc = dt.datetime.fromtimestamp(s_epoch, ET)
            naive0 = loc.replace(tzinfo=None)
            # 对齐到 gran 边界
            ts_epoch = math.ceil(s_epoch / gran) * gran
            while ts_epoch + gran <= seg_t1.timestamp() + 1e-6:
                loc = naive0 + dt.timedelta(seconds=ts_epoch - s_epoch)
                hour, minute = loc.hour, loc.minute
                # 宏观事件冲击(落在本根K线内则作用在开盘)
                jump = 0.0
                while self.event_idx < len(evs) and evs[self.event_idx][0] < ts_epoch + gran:
                    e_epoch, title, sev, ets = evs[self.event_idx]
                    self.event_idx += 1
                    if e_epoch < ts_epoch:
                        continue
                    escale = {1: 0.0008, 2: 0.0022, 3: 0.0042}[sev]
                    jump += rng.gauss(0.0, escale)
                    state["event_mult"] = 1.0 + 0.55 * sev
                    state["event_until"] = e_epoch + {1: 1200, 2: 2700, 3: 5400}[sev]
                if state["event_until"] and ts_epoch > state["event_until"]:
                    state["event_mult"] = 1.0
                    state["event_until"] = None
                mult = state["event_mult"]
                # 波动聚集(OU,精确离散化,长时间步长也稳定)
                phi = math.exp(-kappa * gran)
                nsd_lv = eta * math.sqrt(max(0.0, (1.0 - phi * phi) / (2.0 * kappa)))
                state["log_vol"] = phi * state["log_vol"] + rng.gauss(0.0, nsd_lv)
                lv = min(4.0, math.exp(state["log_vol"]))
                shape = _shape_factor(hour, minute)
                sigma = state["price"] * base_sigma * shape * lv * mult * math.sqrt(gran)
                rate = BASE_PRINT_RATE * _activity_factor(hour, minute) * (1.0 + 0.4 * lv)
                if mult > 1.5:
                    rate *= 1.6
                n = max(1, int(rate * gran))
                # 布朗运动子步 -> 最高/最低
                steps = 6
                path = [0.0]
                for _ in range(steps):
                    path.append(path[-1] + rng.gauss(0.0, sigma / math.sqrt(steps)))
                o = self._round(state["price"] * math.exp(jump))
                pts = [o + p for p in path]
                # 微观结构噪声(逐笔随机游走,绝对价位量纲)
                nsd = self.tick * 0.18 * math.sqrt(n)
                h = self._round(max(pts) + abs(rng.gauss(0.0, nsd)) * 0.9)
                l = self._round(min(pts) - abs(rng.gauss(0.0, nsd)) * 0.9)
                c = self._round(o + path[-1] + rng.gauss(0.0, nsd))
                # 历史价同样贴合最小变动价位(否则涨跌幅会出现非整跳的点数)
                h = max(h, o, c)
                l = min(l, o, c)
                # 历史成交量按"手数"计(与实时行情同一口径:笔数 × 平均每笔手数)
                prints = rate * gran * math.exp(rng.gauss(0.0, 0.22))
                vol = max(0, int(prints * AVG_PRINT_SIZE))
                out.append((ts_epoch, o, h, l, c, vol))
                state["price"] = c
                ts_epoch += gran
        return out

    @property
    def vwap(self) -> float:
        return self.vwap_num / self.vwap_den if self.vwap_den > 0 else self.price

    def spread_ticks(self) -> float:
        """盘口价差(跳)。事件/开盘时段更宽。"""
        t = self.now
        s = 1.0
        if t.hour == 9 and 30 <= t.minute <= 40:
            s = 2.0
        if t.hour == 18 and t.minute <= 15:
            s = 2.0
        if t.hour == 15 and t.minute >= 55:
            s = 1.8
        if self.event_vol_mult > 1.8:
            s += 1.0
        if t.hour <= 6:
            s = max(s - 0.0, 1.0)
        return s

    def _sigma_sec(self) -> float:
        """当前每秒波动率(价格绝对值)。"""
        base = self.daily_sigma / math.sqrt(1380 * 60)     # 23小时会话
        shape = _shape_factor(self.now.hour, self.now.minute)
        vol_state = math.exp(self.log_vol)
        return self.price * base * shape * vol_state * self.event_vol_mult

    def _update_events(self):
        while self.event_idx < len(self.events):
            ets, title, sev = self.events[self.event_idx]
            if ets > self.now:
                break
            self.event_idx += 1
            self._apply_event(ets, title, sev)

    def _apply_event(self, ets: dt.datetime, title: str, sev: int):
        r = self.rng
        # 事件冲击:方向与幅度随机
        if sev >= 3:
            jump = r.gauss(0.0, 0.0042)
        elif sev == 2:
            jump = r.gauss(0.0, 0.0022)
        else:
            jump = r.gauss(0.0, 0.0008)
        self.price = self._round(self.price * (1.0 + jump))
        # 事件窗口内波动放大
        window = {1: 20 * 60, 2: 45 * 60, 3: 90 * 60}[sev]
        self.event_vol_mult = 1.0 + 0.55 * sev
        self.event_vol_until = ets + dt.timedelta(seconds=window)
        self.last_event = (ets, title, sev)
        self.last_event_price = self.price

    # -- 主循环 -----------------------------------------------------------
    def _print_size(self) -> int:
        """单笔成交的手数:小单为主、偶有大单(仿真实纳指分笔成交形态)。"""
        u = self.size_rng.random()
        for lim, (lo, hi) in PRINT_SIZE_BUCKETS:
            if u < lim:
                return self.size_rng.randint(lo, hi)
        return self.size_rng.randint(26, 120)

    def advance(self, seconds: float) -> Iterator[Tuple[dt.datetime, float]]:
        """推进仿真时间,逐笔产出 (时间戳, 价格)。"""
        n = int(seconds)
        frac = seconds - n
        for _ in range(n):
            for ts, px in self._advance_one_second():
                yield ts, px
        if frac > 0:
            for ts, px in self._advance_one_second(frac):
                yield ts, px

    def _advance_one_second(self, dur: float = 1.0) -> Iterator[Tuple[dt.datetime, float]]:
        # 休市 -> 跳到下一开盘
        if not self.calendar.is_open(self.now):
            self._roll_session_if_needed()
        elif self.now >= self.session_end:
            self._roll_session_if_needed()

        self._update_events()
        if self.event_vol_until and self.now > self.event_vol_until:
            self.event_vol_mult = 1.0
            self.event_vol_until = None

        t = self.now
        # 波动聚集 OU 过程
        kappa = 0.0006
        eta = 0.45 * math.sqrt(2 * kappa)
        self.log_vol += -kappa * self.log_vol + self.rng.gauss(0.0, eta)

        # 成交笔数必须与推进的仿真时间成正比(否则倍速失效、K线失真)
        rate = BASE_PRINT_RATE * _activity_factor(t.hour, t.minute) * (1.0 + 0.4 * math.exp(self.log_vol))
        if self.event_vol_mult > 1.5:
            rate *= 1.6
        self.cur_prints_per_sec = rate
        expected = rate * dur
        n_prints = int(expected)
        if self.rng.random() < (expected - n_prints):
            n_prints += 1
        if n_prints <= 0:
            self.now = t + dt.timedelta(seconds=dur)
            self.seconds_simulated += dur
            return

        # 波动与 sqrt(时间) 成正比
        sigma = self._sigma_sec() * math.sqrt(dur)
        step_total = 0.0
        for i in range(n_prints):
            step_total += self.rng.gauss(0.0, 1.0)
        step_total *= sigma / math.sqrt(n_prints)
        per_print = step_total / n_prints

        prev_px = self.price
        last_dir = "N"
        for i in range(n_prints):
            frac = (i + 1) / n_prints
            ts = t + dt.timedelta(seconds=dur * frac)
            px = self.price + per_print
            # 微观结构噪声(逐笔跳动),量纲取最小变动价位,避免随机游走累积
            px += self.rng.gauss(0.0, self.tick * 0.18)
            # 向日内均价轻微回归
            px += 2.0e-7 * (self.vwap - px)
            px = self._round(px)
            if px <= self.tick * 10:
                px = self._round(self.open_price * 0.5)
            self.price = px
            # 本笔成交手数(小单为主、偶有大单),成交量/均价/VWAP 全部按手数计
            qty = self._print_size()
            self.vwap_num += px * qty
            self.vwap_den += qty
            # 多级底仓聚合(10秒 / 小时 / 日线)
            ep = ts.timestamp()
            self.aggs[BASE_PERIOD].update(ts, px, ep, qty)
            self.aggs[HOUR_PERIOD].update(ts, px, ep, qty)
            self.aggs[DAY_PERIOD].update(ts, px, ep, qty)
            # 会话统计
            if px > self.session_high:
                self.session_high = px
            if px < self.session_low:
                self.session_low = px
            self.session_vol += qty
            # 分笔成交记录(涨跌方向继承,同价延续前一笔方向)
            if px > prev_px:
                last_dir = "B"
            elif px < prev_px:
                last_dir = "S"
            self.tape.append((ts, px, qty, last_dir))
            prev_px = px
            self.total_prints += 1
            yield ts, px

        self.now = t + dt.timedelta(seconds=dur)
        self.seconds_simulated += dur

    # -- 快照 -------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "instr_key": self.instr_key,
            "seed": self.seed,
            "now": self.now,
            "price": self.price,
            "open_price": self.open_price,
            "prev_close": self.prev_close,
            "log_vol": self.log_vol,
            "vwap_num": self.vwap_num,
            "vwap_den": self.vwap_den,
            "aggs": {p: a.snapshot() for p, a in self.aggs.items()},
            "tape": list(self.tape)[-80:],
            "session_high": self.session_high,
            "session_low": self.session_low,
            "session_open": self.session_open,
            "session_vol": self.session_vol,
            "event_idx": self.event_idx,
            "event_vol_mult": self.event_vol_mult,
            "event_vol_until": self.event_vol_until,
            "last_event": getattr(self, "last_event", None),
            "last_event_price": getattr(self, "last_event_price", None),
            "seconds_simulated": self.seconds_simulated,
            "total_prints": self.total_prints,
            "session_start_day": self._session_start_day,
            "session_end": self.session_end,
            "rng_state": self.rng.getstate(),
            "size_rng_state": self.size_rng.getstate(),
        }

    def restore(self, s: dict):
        self.now = s["now"]
        self.price = s["price"]
        self.open_price = s["open_price"]
        self.prev_close = s["prev_close"]
        self.log_vol = s["log_vol"]
        self.vwap_num = s["vwap_num"]
        self.vwap_den = s["vwap_den"]
        for p, snap in s["aggs"].items():
            if p in self.aggs:
                self.aggs[p].restore(snap)
        self.tape = deque(list(s["tape"]), maxlen=300)
        self.session_high = s["session_high"]
        self.session_low = s["session_low"]
        self.session_open = s["session_open"]
        self.session_vol = s["session_vol"]
        self.event_idx = s["event_idx"]
        self.event_vol_mult = s["event_vol_mult"]
        self.event_vol_until = s["event_vol_until"]
        self.last_event = s["last_event"]
        self.last_event_price = s["last_event_price"]
        self.seconds_simulated = s["seconds_simulated"]
        self.total_prints = s["total_prints"]
        self._session_start_day = s["session_start_day"]
        self.session_end = s["session_end"]
        self.rng.setstate(s["rng_state"])
        if s.get("size_rng_state") is not None:
            self.size_rng.setstate(s["size_rng_state"])
        self.instr_key = s["instr_key"]
        self.instr = INSTRUMENTS[self.instr_key]
        self.tick = self.instr["tick"]
