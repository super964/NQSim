"""
模拟主控
========

* 时钟:仿真时间独立于真实时间,倍速播放(1x~600x),可暂停、单步
* 快照:每 30 仿真秒保留一份完整状态(行情+账户+净值曲线+操作日志)
* 倒退:回退到任意历史时刻并**确定性重演**(行情 RNG 状态随快照保存)
  - 复盘模式:保留历史操作,回到过去继续播放可完整再现当时行情与账户
  - 重来模式:丢弃倒退点之后的操作,从该点重新交易(训练重做用)
* 净值:每 5 仿真秒采样一次,计算回撤、夏普、胜率等统计
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from collections import deque
from typing import Callable, List, Optional

from .account import Broker
from .market import INSTRUMENTS, MarketSim

SNAPSHOT_INTERVAL = 30.0      # 仿真秒
EQUITY_SAMPLE_INTERVAL = 5.0   # 仿真秒
MAX_SNAPSHOTS = 1200           # 约 10 小时仿真历史


class Action:
    """用户操作(可回放)。"""

    __slots__ = ("ts", "kind", "data")

    def __init__(self, ts: dt.datetime, kind: str, data: dict):
        self.ts = ts
        self.kind = kind
        self.data = data


class Simulation:
    def __init__(self, instr: str = "NQ", seed: int = 20260101,
                 balance: float = 100_000.0, leverage: int = 20,
                 bar_period: int = 60, start: dt.datetime | None = None,
                 base_price: float | None = None, daily_sigma: float = 0.0125):
        self.instr = instr
        self.seed = seed
        self.bar_period = bar_period
        self.market = MarketSim(instr, seed, start, base_price, daily_sigma, bar_period)
        self.broker = Broker(instr, balance, leverage, seed=seed + 101)
        self.lock = threading.RLock()
        self.action_log: List[Action] = []
        self._cursor = 0                # 下一个待执行的操作在 action_log 中的下标
        self.snapshots = deque(maxlen=MAX_SNAPSHOTS)
        self.equity_curve: List[tuple] = []
        self._next_sample = 0.0
        self.speed = 10
        self.paused = True
        self.rewind_mode = "replay"     # replay | redo
        self.last_action_label = ""
        self.peak_equity = balance
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self._snapshot_clock = 0.0
        self.start_time = self.market.now
        self._push_snapshot()
        self._sample_equity(force=True)

    # ------------------------------------------------------------------
    # 启动预填
    # ------------------------------------------------------------------
    def fast_prefill(self, days: int = 190, recent_hours: float = 72.0):
        """快速生成历史行情并铺满各周期K线。

        逐笔引擎跑满数月历史需要数分钟,因此启动阶段用快速合成器一次性生成:
        远期按小时粒度、近端按底仓粒度,与逐笔引擎同参数同形态。
        生成后重建快照,保证后续倒退/快进在可回退区间内正常工作。
        """
        with self.lock:
            self.market.fast_history(days=days, recent_hours=recent_hours)
            # 历史不含持仓与成交,净值曲线保持起始值
            self.action_log.clear()
            self._cursor = 0
            self.snapshots.clear()
            self.equity_curve = [(self.market.now, self.broker.equity(self.market.price))]
            self._next_sample = self.market.seconds_simulated + EQUITY_SAMPLE_INTERVAL
            self._snapshot_clock = self.market.seconds_simulated
            self.peak_equity = self.broker.equity(self.market.price)
            self.max_dd = 0.0
            self.max_dd_pct = 0.0
            self._push_snapshot()

    # ------------------------------------------------------------------
    # 基础状态
    # ------------------------------------------------------------------
    @property
    def instr_key(self) -> str:
        return self.instr

    @property
    def now(self) -> dt.datetime:
        return self.market.now

    @property
    def price(self) -> float:
        return self.market.price

    def bid_ask(self):
        half = self.market.spread_ticks() * self.market.tick / 2.0
        return self.market.price - half, self.market.price + half

    def equity(self) -> float:
        return self.broker.equity(self.market.price)

    # ------------------------------------------------------------------
    # 用户操作
    # ------------------------------------------------------------------
    def submit(self, kind: str, data: dict) -> Action:
        """提交操作(线程安全)。会在下一笔成交时生效。"""
        with self.lock:
            a = Action(self.market.now, kind, data)
            self.action_log.append(a)
            self.last_action_label = f"{kind} {data}"
            return a

    def _exec_action(self, a: Action):
        b = self.broker
        d = a.data
        if a.kind == "order":
            oco = None
            if d.get("oco_stop") is not None and d.get("oco_limit") is not None:
                oco = b.place_order(d["side"], "stop", d["oco_stop"], d["oco_stop_price"],
                                    now=a.ts, note="括号-止损")
                b.place_order(d["side"], "limit", d["oco_limit"], d["oco_limit_price"],
                              now=a.ts, note="括号-止盈", oco_with=oco)
            else:
                b.place_order(d["side"], d.get("otype", "market"), d["qty"],
                              limit=d.get("limit"), stop=d.get("stop"),
                              now=a.ts, note=d.get("note", ""),
                              protect=d.get("protect"))
        elif a.kind == "cancel":
            b.cancel(d["id"])
        elif a.kind == "cancel_all":
            b.cancel_all()
        elif a.kind == "close":
            b.close_position(now=a.ts, fraction=d.get("fraction", 1.0))
        elif a.kind == "flip":
            b.flip_position(now=a.ts)
        elif a.kind == "leverage":
            b.set_leverage(d["leverage"], self.market.price)
        elif a.kind == "exits":
            # 在K线图上拖拽设置止盈/止损(两者互为 OCO)
            b.set_position_exits(stop=d.get("stop"), tp=d.get("tp"), now=a.ts)

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------
    def _on_print(self, ts: dt.datetime, px: float):
        # 执行到期的用户操作
        while self._cursor < len(self.action_log) and self.action_log[self._cursor].ts <= ts:
            self._exec_action(self.action_log[self._cursor])
            self._cursor += 1
        spread = self.market.spread_ticks()
        cap = max(1.0, 0.25 * self.market.cur_prints_per_sec)
        self.broker.process_print(ts, px, spread, cap, self.market.cur_prints_per_sec)
        # 强平检查
        if self.broker.qty != 0:
            eq = self.broker.equity(px)
            if eq <= self.broker.margin_maintenance(px):
                self.broker.force_liquidate(px, "维持保证金击穿")
                self.broker.cancel_all("强平后撤单")

    def step(self, seconds: float):
        """推进仿真 seconds 秒。"""
        with self.lock:
            start = self.market.now
            for ts, px in self.market.advance(seconds):
                self._on_print(ts, px)
            # 隔夜结算:跨 18:00 美东时计提隔夜利息
            if start.date() != self.market.now.date() or start.hour != self.market.now.hour:
                self._check_settlement(start, self.market.now)
            self._sample_equity()
            self._maybe_snapshot()

    def _check_settlement(self, start: dt.datetime, end: dt.datetime):
        """检测是否跨过 18:00 美东结算点 -> 计提隔夜利息。"""
        d = start.date()
        for _ in range(5):
            settle = dt.datetime(d.year, d.month, d.day, 18, 0, tzinfo=start.tzinfo)
            if settle <= start:
                d += dt.timedelta(days=1)
                continue
            if settle > end:
                break
            notional = self.broker.notional(self.market.price)
            self.broker.charge_funding(settle, notional)
            d += dt.timedelta(days=1)

    def _sample_equity(self, force: bool = False):
        """按固定 5 仿真秒的节奏采样净值曲线(步长较大时线性插值补点)。"""
        t = self.market.seconds_simulated
        eq = self.broker.equity(self.market.price)
        if force:
            self.equity_curve.append((self.market.now, eq))
            self._next_sample = t + EQUITY_SAMPLE_INTERVAL
            self._track_equity(eq)
            return
        if t < self._next_sample:
            return
        if self.equity_curve:
            prev_ts, prev_eq = self.equity_curve[-1]
            span = max(1e-9, (self.market.now - prev_ts).total_seconds())
            prev_secs = t - span
        else:
            prev_eq, span, prev_secs = eq, 1.0, t
        count = 0
        while self._next_sample <= t and count < 3000:
            frac = min(1.0, max(0.0, (self._next_sample - prev_secs) / span))
            e = prev_eq + (eq - prev_eq) * frac
            ts = self.market.now - dt.timedelta(seconds=t - self._next_sample)
            self.equity_curve.append((ts, e))
            self._track_equity(e)
            self._next_sample += EQUITY_SAMPLE_INTERVAL
            count += 1
        if len(self.equity_curve) > 200000:
            del self.equity_curve[:50000]

    def _track_equity(self, eq: float):
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_dd:
            self.max_dd = dd
        if self.peak_equity > 0:
            self.max_dd_pct = max(self.max_dd_pct, dd / self.peak_equity)

    def _maybe_snapshot(self):
        t = self.market.seconds_simulated
        if t - self._snapshot_clock >= SNAPSHOT_INTERVAL:
            self._snapshot_clock = t
            self._push_snapshot()

    def _push_snapshot(self):
        self.snapshots.append({
            "t": self.market.now,
            "secs": self.market.seconds_simulated,
            "market": self.market.snapshot(),
            "broker": self.broker.snapshot(),
            "eq_len": len(self.equity_curve),
            "act_len": len(self.action_log),
            "cursor": self._cursor,
            "peak_equity": self.peak_equity,
            "max_dd": self.max_dd,
            "max_dd_pct": self.max_dd_pct,
        })

    # ------------------------------------------------------------------
    # 前进 / 后退
    # ------------------------------------------------------------------
    def _find_snapshot(self, target: dt.datetime):
        best = None
        for s in self.snapshots:
            if s["t"] <= target:
                best = s
            else:
                break
        return best

    def rewind_to(self, target: dt.datetime, mode: str = "replay",
                  progress: Optional[Callable[[float, str], None]] = None) -> bool:
        """回退到 target 时刻并确定性重演。"""
        with self.lock:
            snap = self._find_snapshot(target)
            if snap is None:
                return False
            self._restore_snapshot(snap)
            # 截断操作日志到快照点
            del self.action_log[snap["act_len"]:]
            self._cursor = snap["cursor"]
            # 重演到目标时刻
            total = (target - self.market.now).total_seconds()
            if total > 0:
                chunk = 60.0
                done = 0.0
                while done < total:
                    self.step(min(chunk, total - done))
                    done += chunk
                    if progress:
                        progress(done / total, "重演历史行情…")
            if mode == "redo":
                # 丢弃目标时刻之后的操作 -> 回到该点重新交易
                keep = 0
                for i, a in enumerate(self.action_log):
                    if a.ts <= target:
                        keep = i + 1
                del self.action_log[keep:]
                self._cursor = min(self._cursor, keep)
            self.rewind_mode = mode
            self._push_snapshot()
            return True

    def _restore_snapshot(self, snap: dict):
        self.market.restore(snap["market"])
        self.broker.restore(snap["broker"])
        if len(self.equity_curve) > snap["eq_len"]:
            del self.equity_curve[snap["eq_len"]:]
        self.peak_equity = snap["peak_equity"]
        self.max_dd = snap["max_dd"]
        self.max_dd_pct = snap["max_dd_pct"]
        self._snapshot_clock = snap["secs"]
        self._next_sample = snap["secs"] + EQUITY_SAMPLE_INTERVAL

    def jump_forward(self, target: dt.datetime,
                     progress: Optional[Callable[[float, str], None]] = None) -> bool:
        """快进到 target(必须晚于当前时刻)。"""
        with self.lock:
            total = (target - self.market.now).total_seconds()
            if total <= 0:
                return False
            chunk = 120.0
            done = 0.0
            while done < total:
                self.step(min(chunk, total - done))
                done += chunk
                if progress:
                    progress(done / total, "快进中…")
            return True

    def rewind_seconds(self, seconds: float, mode: str = "replay") -> bool:
        return self.rewind_to(self.market.now - dt.timedelta(seconds=seconds), mode)

    def history_span(self) -> float:
        """可回退的历史跨度(仿真秒)。"""
        if not self.snapshots:
            return 0.0
        return (self.snapshots[-1]["secs"] - self.snapshots[0]["secs"])

    def history_start(self) -> dt.datetime:
        return self.snapshots[0]["t"] if self.snapshots else self.market.now

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        b = self.broker
        eq = self.broker.equity(self.market.price)
        initial = b.initial_balance
        trades = b.trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (float("inf") if wins else 0.0)
        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (-gross_loss / len(losses)) if losses else 0.0
        rr = (avg_win / abs(avg_loss)) if avg_loss > 1e-9 else 0.0
        win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
        # 最大连亏
        streak = 0
        max_streak = 0
        for t in trades:
            if t.pnl < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        # 持仓时长
        hold_secs = 0.0
        for t in trades:
            if t.close_t and t.open_t:
                hold_secs += (t.close_t - t.open_t).total_seconds()
        elapsed = max(1.0, self.market.seconds_simulated)
        # 夏普:基于净值采样收益(仅取最近样本,避免长回放时卡顿)
        sharpe = 0.0
        curve = self.equity_curve[-3000:]
        if len(curve) > 30:
            rets = []
            for i in range(1, len(curve)):
                p = curve[i - 1][1]
                if abs(p) > 1e-9:
                    rets.append(curve[i][1] / p - 1.0)
            if len(rets) > 10:
                mu = sum(rets) / len(rets)
                var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
                sd = math.sqrt(var)
                if sd > 1e-12:
                    per_year = 252 * 23 * 3600 / EQUITY_SAMPLE_INTERVAL
                    sharpe = mu / sd * math.sqrt(per_year)
        return {
            "equity": eq,
            "initial": initial,
            "net_pnl": eq - initial,
            "return_pct": (eq / initial - 1) * 100 if initial else 0.0,
            "realized": b.realized,
            "unrealized": b.unrealized(self.market.price),
            "commission": b.commission_paid,
            "funding": b.funding_paid,
            "max_dd": self.max_dd,
            "max_dd_pct": self.max_dd_pct * 100,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "profit_factor": pf,
            "payoff": rr,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "max_streak_loss": max_streak,
            "largest_win": max((t.pnl for t in wins), default=0.0),
            "largest_loss": min((t.pnl for t in losses), default=0.0),
            "avg_hold_sec": (hold_secs / len(trades)) if trades else 0.0,
            "time_in_market": min(100.0, hold_secs / elapsed * 100),
            "sharpe": sharpe,
            "liquidations": b.liquidations,
            "margin_calls": b.margin_calls,
        }

    # ------------------------------------------------------------------
    def reset(self, seed: Optional[int] = None, balance: Optional[float] = None,
              leverage: Optional[int] = None, base_price: Optional[float] = None,
              start: Optional[dt.datetime] = None):
        with self.lock:
            if seed is not None:
                self.seed = seed
            if balance is not None:
                self.broker.initial_balance = balance
            if leverage is not None:
                self.broker.leverage = leverage
            self.market = MarketSim(self.instr, self.seed, start or self.market.now,
                                     base_price if base_price is not None else self.market.price,
                                     self.market.daily_sigma, self.bar_period)
            self.broker = Broker(self.instr, self.broker.initial_balance,
                                 self.broker.leverage, seed=self.seed + 101)
            self.action_log.clear()
            self._cursor = 0
            self.snapshots.clear()
            self.equity_curve.clear()
            self.peak_equity = self.broker.initial_balance
            self.max_dd = 0.0
            self.max_dd_pct = 0.0
            self._snapshot_clock = 0.0
            self._next_sample = 0.0
            self._push_snapshot()
            self._sample_equity(force=True)
