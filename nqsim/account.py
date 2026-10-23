"""
券商 / 账户模型
===============

* 订单类型:市价、限价、止损市价、止损限价,支持 OCO 括号单
* 成交撮合:市价单下一笔成交(真实延迟),限价单需价格穿越并排队,
  吃单能力受当笔成交容量限制 -> 大单自动分笔、产生 VWAP 滑点
* 滑点模型:半点差 + 冲击成本(平方根律) + 随机噪声,方向性作用于成交价
* 成本:经纪商佣金 + 交易所/清算费(按手按边)
* 保证金:名义价值/杠杆,维持保证金 25%,支持动态调整杠杆并触发追保
* 强平:权益击穿维持保证金时市价强平(额外惩罚性滑点)
* 隔夜成本:按占用保证金计提隔夜利息(每日 18:00 美东结算)
"""

from __future__ import annotations

import datetime as dt
import math
import random
from typing import Dict, List, Optional

from .market import INSTRUMENTS


class Order:
    __slots__ = ("id", "code", "side", "otype", "qty", "filled", "avg_fill", "limit", "stop",
                 "status", "created", "note", "oco", "triggered", "reason", "fills", "protect")

    def __init__(self, oid: int, side: str, otype: str, qty: float,
                 limit: Optional[float] = None, stop: Optional[float] = None,
                 created: dt.datetime | None = None, note: str = "",
                 protect: Optional[float] = None, code: str = ""):
        self.id = oid
        self.code = code or f"#{oid}"   # 仿真实平台的订单编号(交易所代码-日期-流水)
        self.side = side                  # buy / sell
        self.otype = otype                # market / limit / stop / stop_limit
        self.qty = qty
        self.filled = 0.0
        self.avg_fill = 0.0
        self.limit = limit
        self.stop = stop
        self.status = "working"           # working / triggered / filled / cancelled / rejected
        self.created = created
        self.note = note
        self.oco: Optional[int] = None
        self.triggered = False
        self.reason = ""
        self.fills: List[tuple] = []
        self.protect = protect            # 开仓成交后自动挂保护性止损的价格

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled)

    @property
    def type_cn(self) -> str:
        return {"market": "市价", "limit": "限价", "stop": "止损市价",
                "stop_limit": "止损限价"}.get(self.otype, self.otype)

    def describe(self) -> str:
        side = "买入" if self.side == "buy" else "卖出"
        extra = ""
        if self.limit is not None:
            extra += f" 限价 {self.limit:,.2f}"
        if self.stop is not None:
            extra += f" 止损 {self.stop:,.2f}"
        return f"{self.code} {side} {self.type_cn} {self.qty:g}{extra}"


class Trade:
    """一笔完整开平仓。"""

    __slots__ = ("side", "qty", "entry", "exit", "pnl", "open_t", "close_t", "bars", "commission")

    def __init__(self, side, qty, entry, exit_, pnl, open_t, close_t, commission=0.0):
        self.side = side
        self.qty = qty
        self.entry = entry
        self.exit = exit_
        self.pnl = pnl
        self.open_t = open_t
        self.close_t = close_t
        self.commission = commission
        self.bars = 0


class Broker:
    def __init__(self, instr_key: str = "NQ", balance: float = 100_000.0,
                 leverage: int = 20, seed: int = 7, overnight_rate: float = 0.06):
        self.instr_key = instr_key
        self.instr = INSTRUMENTS[instr_key]
        self.mult = self.instr["mult"]
        self.tick = self.instr["tick"]
        self.initial_balance = balance
        self.cash = balance
        self.qty = 0.0
        self.avg = 0.0
        self.realized = 0.0
        self.commission_paid = 0.0
        self.funding_paid = 0.0
        self.leverage = leverage
        self.maint_ratio = 0.25
        self.exec_rng = random.Random(seed)
        self.orders: Dict[int, Order] = {}
        self.order_seq = 0
        self.protective: Dict[int, int] = {}   # 保护性止损单 id -> 对应开仓单 id
        self.exit_orders: Dict[str, int] = {}  # "stop" / "tp" -> 当前持仓的止盈止损挂单 id
        self.position_order_code = ""          # 当前持仓的开仓单编号
        self.trades: List[Trade] = []
        self.events: List[dict] = []          # 成交/系统事件日志
        self.liquidations = 0
        self.margin_calls = 0
        self.open_trade: Optional[Trade] = None
        self.last_price = self.instr["base_price"]

    # -- 基础计算 ---------------------------------------------------------
    def unrealized(self, price: Optional[float] = None) -> float:
        p = self.last_price if price is None else price
        if self.qty == 0:
            return 0.0
        return (p - self.avg) * self.qty * self.mult

    def equity(self, price: Optional[float] = None) -> float:
        return self.cash + self.unrealized(price)

    def notional(self, price: Optional[float] = None) -> float:
        p = self.last_price if price is None else price
        return abs(self.qty) * p * self.mult

    def margin_required(self, price: Optional[float] = None) -> float:
        return self.notional(price) / max(1, self.leverage)

    def margin_maintenance(self, price: Optional[float] = None) -> float:
        return self.margin_required(price) * self.maint_ratio

    def free_margin(self, price: Optional[float] = None) -> float:
        return self.equity(price) - self.margin_required(price)

    def liquidation_price(self) -> Optional[float]:
        """强平价:权益 = 维持保证金。"""
        if self.qty == 0:
            return None
        ma = self.maint_ratio / max(1, self.leverage)
        eq = self.equity(self.last_price)
        if self.qty > 0:
            denom = 1.0 - ma
            if denom <= 0:
                return None
            p = (self.avg - eq / (self.qty * self.mult)) / denom
        else:
            p = (self.avg + eq / (abs(self.qty) * self.mult)) / (1.0 + ma)
        return p

    def round_trip_cost(self, qty: float = 1.0) -> float:
        return (self.instr["comm"] + self.instr["fee"]) * 2 * qty

    # -- 下单 -------------------------------------------------------------
    def place_order(self, side: str, otype: str, qty: float,
                    limit: Optional[float] = None, stop: Optional[float] = None,
                    now: dt.datetime | None = None, note: str = "",
                    oco_with: Optional[Order] = None,
                    protect: Optional[float] = None) -> Order:
        self.order_seq += 1
        o = Order(self.order_seq, side, otype, qty, limit, stop, now, note, protect,
                  code=self._make_code(now))
        if oco_with is not None:
            o.oco = oco_with.id
            oco_with.oco = o.id
        self.orders[o.id] = o
        self.log("order", f"下单 {o.describe()}")
        return o

    def _make_code(self, now: dt.datetime | None = None) -> str:
        """仿真实平台的订单编号:纯数字长串(交易日 + 交易所流水号)。

        真实纳指期货的报单号就是一串很长的数字,形如:
            202610220000000001 / 202610220000000002
        前 8 位为交易日(便于按日归档与排序),后 10 位为当日递增流水。
        """
        ts = (now or dt.datetime.now())
        return f"{ts.strftime('%Y%m%d')}{self.order_seq:010d}"

    # -- 持仓的止盈 / 止损(可在K线图上拖拽设置) --------------------------
    def set_position_exits(self, stop: Optional[float] = None, tp: Optional[float] = None,
                           now: dt.datetime | None = None) -> dict:
        """为当前持仓设置止损 / 止盈,两者互为 OCO(先成交者撤销另一单)。

        再次调用会先撤销旧的挂单,实现"拖动改价"。
        """
        if self.qty == 0:
            return {}
        for kind in ("stop", "tp"):
            oid = self.exit_orders.pop(kind, None)
            if oid is not None:
                o = self.orders.get(oid)
                if o and o.status in ("working", "triggered"):
                    self.cancel(oid, "重新设置")
        side = "sell" if self.qty > 0 else "buy"
        qty = abs(self.qty)
        out = {}
        o_stop = o_tp = None
        if stop is not None:
            o_stop = self.place_order(side, "stop", qty, stop=stop, now=now, note="止损")
            self.exit_orders["stop"] = o_stop.id
            out["stop"] = o_stop
        if tp is not None:
            o_tp = self.place_order(side, "limit", qty, limit=tp, now=now, note="止盈")
            self.exit_orders["tp"] = o_tp.id
            out["tp"] = o_tp
        if o_stop is not None and o_tp is not None:
            o_stop.oco = o_tp.id
            o_tp.oco = o_stop.id
        return out

    def position_exit_prices(self) -> tuple:
        """返回当前持仓挂着的 (止损价, 止盈价),没有则为 None。"""
        stop = tp = None
        for kind, attr in (("stop", "stop"), ("tp", "limit")):
            oid = self.exit_orders.get(kind)
            o = self.orders.get(oid) if oid else None
            if o and o.status in ("working", "triggered"):
                v = getattr(o, attr)
                if v is not None:
                    if kind == "stop":
                        stop = v
                    else:
                        tp = v
        if stop is None and self.qty != 0:
            # 兼容"开仓自动挂止损"生成的保护性止损单
            want = "sell" if self.qty > 0 else "buy"
            for o in self.orders.values():
                if (o.otype == "stop" and o.side == want and o.stop
                        and o.status in ("working", "triggered")):
                    stop = o.stop
                    break
        return stop, tp

    def cancel(self, oid: int, reason: str = "用户撤单") -> bool:
        o = self.orders.get(oid)
        if not o or o.status in ("filled", "cancelled"):
            return False
        o.status = "cancelled"
        o.reason = reason
        if o.oco:
            sib = self.orders.get(o.oco)
            if sib and sib.status == "working":
                sib.status = "cancelled"
                sib.reason = "OCO 联动"
        self.log("cancel", f"撤单 {o.describe()}")
        return True

    def cancel_all(self, reason: str = "批量撤单"):
        for o in list(self.orders.values()):
            if o.status in ("working", "triggered"):
                self.cancel(o.id, reason)

    def close_position(self, now: dt.datetime | None = None, fraction: float = 1.0,
                       note: str = "平仓") -> Optional[Order]:
        if self.qty == 0:
            return None
        qty = abs(self.qty) * max(0.0, min(1.0, fraction))
        side = "sell" if self.qty > 0 else "buy"
        return self.place_order(side, "market", qty, now=now, note=note)

    def flip_position(self, now: dt.datetime | None = None) -> Optional[Order]:
        if self.qty == 0:
            return None
        qty = abs(self.qty) * 2
        side = "sell" if self.qty > 0 else "buy"
        return self.place_order(side, "market", qty, now=now, note="反手")

    def set_leverage(self, lev: int, price: Optional[float] = None):
        self.leverage = max(1, int(lev))
        self.log("leverage", f"杠杆调整为 {self.leverage}x")
        self.check_margin(price)

    def check_margin(self, price: Optional[float] = None):
        """调整杠杆/行情跳变后的保证金检查。"""
        if self.qty == 0:
            return
        p = self.last_price if price is None else price
        eq = self.equity(p)
        maint = self.margin_maintenance(p)
        req = self.margin_required(p)
        if eq <= maint:
            self.force_liquidate(p, reason="维持保证金不足")
        elif eq < req:
            # 追保:按比例减仓直到保证金充足
            need = req - eq
            price_drop = abs(p - self.avg) * self.mult + 1e-9
            reduce_qty = min(abs(self.qty), need / price_drop)
            self.margin_calls += 1
            self.log("margin", f"追保减仓 {reduce_qty:.2f} 手 (权益 ${eq:,.0f} < 保证金 ${req:,.0f})")
            if reduce_qty > 0:
                self.close_position(price=p, fraction=reduce_qty / abs(self.qty), note="追保减仓")

    # -- 撮合 -------------------------------------------------------------
    def _slippage(self, side: str, price: float, spread_ticks: float, qty: float,
                  capacity: float) -> float:
        half = spread_ticks * 0.5
        impact = 0.0
        if capacity > 0:
            impact = 0.25 * math.sqrt(max(qty, 1e-9) / capacity)
        impact = min(impact, 8.0)
        noise = self.exec_rng.uniform(0.0, 0.30)
        ticks = half + impact + noise
        adj = ticks * self.tick
        return price + adj if side == "buy" else price - adj

    def process_print(self, ts: dt.datetime, price: float, spread_ticks: float,
                      capacity: float, prints_per_sec: float = 4.0):
        """处理一笔成交:撮合挂单并更新持仓。"""
        self.last_price = price
        working = [o for o in self.orders.values() if o.status in ("working", "triggered")]
        for o in working:
            if o.remaining <= 0:
                continue
            fill_price = None
            if o.otype == "market":
                fill_price = self._slippage(o.side, price, spread_ticks, o.remaining, capacity)
            elif o.otype == "limit":
                if o.side == "buy" and price <= o.limit:
                    fill_price = min(o.limit, price)
                elif o.side == "sell" and price >= o.limit:
                    fill_price = max(o.limit, price)
            elif o.otype == "stop":
                if not o.triggered:
                    # 买入止损:价格上穿触发;卖出止损:价格下穿触发
                    if o.side == "buy" and o.stop is not None and price >= o.stop:
                        o.triggered = True
                        o.status = "triggered"
                    elif o.side == "sell" and o.stop is not None and price <= o.stop:
                        o.triggered = True
                        o.status = "triggered"
                if o.triggered:
                    fill_price = self._slippage(o.side, price, spread_ticks, o.remaining, capacity)
            elif o.otype == "stop_limit":
                if not o.triggered:
                    if o.side == "buy" and o.stop is not None and price >= o.stop:
                        o.triggered = True
                        o.status = "triggered"
                    elif o.side == "sell" and o.stop is not None and price <= o.stop:
                        o.triggered = True
                        o.status = "triggered"
                if o.triggered:
                    if o.side == "buy" and price <= o.limit:
                        fill_price = min(o.limit, price)
                    elif o.side == "sell" and price >= o.limit:
                        fill_price = max(o.limit, price)

            if fill_price is None:
                continue
            qty = min(o.remaining, capacity)
            if qty <= 0:
                continue
            fill_price = round(fill_price / self.tick) * self.tick
            self._apply_fill(o, qty, fill_price, ts)
            if o.remaining <= 1e-9:
                o.status = "filled"
                o.filled = o.qty
                if o.oco:
                    sib = self.orders.get(o.oco)
                    if sib and sib.status in ("working", "triggered"):
                        self.cancel(sib.id, "OCO 联动")
        # 强平检查(维持保证金击穿)
        if self.qty != 0:
            eq = self.equity(price)
            if eq <= self.margin_maintenance(price):
                self.force_liquidate(price, "维持保证金击穿")
                self.cancel_all("强平后撤单")

    def _apply_fill(self, order: Order, qty: float, price: float, ts: dt.datetime):
        side = order.side
        prev_qty = self.qty
        if self.qty == 0 or (self.qty > 0) == (side == "buy"):
            new_abs = abs(self.qty) + qty
            self.avg = (self.avg * abs(self.qty) + price * qty) / new_abs if new_abs else price
            # 同向成交要累加仓位(分笔成交时尤其重要)
            self.qty = new_abs if side == "buy" else -new_abs
        else:
            close_qty = min(abs(self.qty), qty)
            pnl = (price - self.avg) * close_qty * self.mult * (1 if self.qty > 0 else -1)
            self.realized += pnl
            self.cash += pnl
            remaining = qty - close_qty
            if remaining <= 1e-9:
                self.qty = 0.0
                self.avg = 0.0
                if self.open_trade is not None:
                    self.open_trade.exit = price
                    self.open_trade.close_t = ts
                    self.open_trade.pnl = pnl
                    self.trades.append(self.open_trade)
                    self.open_trade = None
            else:
                self.qty = -remaining if side == "buy" else remaining
                self.avg = price
                if self.open_trade is not None:
                    self.open_trade.exit = price
                    self.open_trade.close_t = ts
                    self.open_trade.pnl = pnl
                    self.trades.append(self.open_trade)
                    self.open_trade = None
                self.open_trade = Trade("long" if side == "buy" else "short", remaining,
                                        price, price, 0.0, ts, ts)
        comm = (self.instr["comm"] + self.instr["fee"]) * qty
        self.cash -= comm
        self.commission_paid += comm
        if self.open_trade is None and self.qty != 0 and prev_qty == 0:
            self.open_trade = Trade("long" if self.qty > 0 else "short", abs(self.qty),
                                    self.avg, self.avg, 0.0, ts, ts)
            # 记录开仓单编号(持仓表展示)
            self.position_order_code = order.code
        elif self.open_trade is not None:
            self.open_trade.qty = abs(self.qty)
            self.open_trade.entry = self.avg
        order.filled += qty
        order.avg_fill = ((order.avg_fill * (order.filled - qty)) + price * qty) / order.filled
        order.fills.append((ts, qty, price))
        self.log("fill", f"成交 {('买入' if side=='buy' else '卖出')} {qty:g} @ {price:,.2f} "
                         f"({order.type_cn} {order.code})")
        # 开仓成交后自动挂保护性止损(真实平台的括号单行为)
        if order.remaining <= 1e-9 and order.protect:
            prot_side = "sell" if side == "buy" else "buy"
            po = self.place_order(prot_side, "stop", order.filled, stop=order.protect,
                                  now=ts, note="保护止损")
            self.protective[po.id] = order.id
        # 持仓被平掉时,撤销所有保护性止损与止盈挂单,避免反向开仓
        if abs(self.qty) < 1e-9:
            for pid in list(self.protective.keys()):
                if self.orders.get(pid) and self.orders[pid].status in ("working", "triggered"):
                    self.cancel(pid, "持仓已平")
            for kind in list(self.exit_orders.keys()):
                oid = self.exit_orders.pop(kind)
                o = self.orders.get(oid)
                if o and o.status in ("working", "triggered"):
                    self.cancel(oid, "持仓已平")
            self.position_order_code = ""

    def force_liquidate(self, price: float, reason: str = "强制平仓"):
        """维持保证金击穿:市价强平 + 惩罚性滑点。"""
        if self.qty == 0:
            return
        side = "sell" if self.qty > 0 else "buy"
        qty = abs(self.qty)
        fill = self._slippage(side, price, 3.0, qty, 1.0) - (0.02 * self.tick if side == "buy" else -0.02 * self.tick)
        self.order_seq += 1
        o = Order(-1, side, "market", qty, created=None, note="强平",
                  code=self._make_code(dt.datetime.now()))
        self._apply_fill(o, qty, fill, dt.datetime.now())
        self.liquidations += 1
        self.log("liquidate", f"⚠ 强制平仓 {qty:g} 手 @ {fill:,.2f} —— {reason}")

    def charge_funding(self, ts: dt.datetime, notional: float):
        """隔夜利息:按占用保证金年化 6% 计提。"""
        if notional <= 0:
            return
        interest = notional * 0.06 / 252
        self.cash -= interest
        self.funding_paid += interest
        if interest > 0.005:
            self.log("funding", f"隔夜利息 -${interest:,.2f} (名义 ${notional:,.0f})")

    def log(self, kind: str, text: str):
        self.events.append({"kind": kind, "text": text})
        if len(self.events) > 500:
            del self.events[0]

    # -- 快照 -------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "instr_key": self.instr_key,
            "initial_balance": self.initial_balance,
            "cash": self.cash,
            "qty": self.qty,
            "avg": self.avg,
            "realized": self.realized,
            "commission_paid": self.commission_paid,
            "funding_paid": self.funding_paid,
            "leverage": self.leverage,
            "order_seq": self.order_seq,
            "orders": {k: (v.side, v.otype, v.qty, v.filled, v.avg_fill, v.limit, v.stop,
                           v.status, v.created, v.note, v.oco, v.triggered, v.reason, v.protect)
                       for k, v in self.orders.items()},
            "protective": dict(self.protective),
            "exit_orders": dict(self.exit_orders),
            "position_order_code": self.position_order_code,
            "trades": [(t.side, t.qty, t.entry, t.exit, t.pnl, t.open_t, t.close_t)
                       for t in self.trades],
            "events": list(self.events),
            "liquidations": self.liquidations,
            "margin_calls": self.margin_calls,
            "last_price": self.last_price,
            "rng_state": self.exec_rng.getstate(),
        }

    def restore(self, s: dict):
        self.instr_key = s["instr_key"]
        self.instr = INSTRUMENTS[self.instr_key]
        self.mult = self.instr["mult"]
        self.tick = self.instr["tick"]
        self.initial_balance = s["initial_balance"]
        self.cash = s["cash"]
        self.qty = s["qty"]
        self.avg = s["avg"]
        self.realized = s["realized"]
        self.commission_paid = s["commission_paid"]
        self.funding_paid = s["funding_paid"]
        self.leverage = s["leverage"]
        self.order_seq = s["order_seq"]
        self.orders = {}
        for k, v in s["orders"].items():
            o = Order(k, v[0], v[1], v[2], v[5], v[6], v[8], v[9], v[13] if len(v) > 13 else None)
            o.filled, o.avg_fill, o.status, o.oco, o.triggered, o.reason = v[3], v[4], v[7], v[10], v[11], v[12]
            self.orders[k] = o
        self.protective = dict(s.get("protective", {}))
        self.exit_orders = dict(s.get("exit_orders", {}))
        self.position_order_code = s.get("position_order_code", "")
        self.trades = [Trade(t[0], t[1], t[2], t[3], t[4], t[5], t[6]) for t in s["trades"]]
        self.events = list(s["events"])
        self.liquidations = s["liquidations"]
        self.margin_calls = s["margin_calls"]
        self.last_price = s["last_price"]
        self.exec_rng.setstate(s["rng_state"])
        self.open_trade = None
