"""Общая фейковая обвязка для тестов выходов (без сети, без pytest).

Запуск:  python3 tests/test_exit_*.py   (код 0 = ок)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor as E


class FakeClient:
    """Минимальный клиент: маркеты филятся мгновенно, книга и тикер задаются."""

    def __init__(self):
        self.calls = []
        self.by_link = {}
        self.mark = 100.0
        self.pos_qty = 0.0
        self.pos_symbol = "TSTUSDT"
        self.pos_side = "Buy"
        self.history = []
        self.pnl = []
        self.book = {"bids": [[99.9, 10]], "asks": [[100.1, 10]]}
        self.executions = []
        self.instruments_info = {
            "symbol": "TSTUSDT",
            "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1",
                              "maxOrderQty": "100000",
                              "maxNotionalValue": "200"},
            "priceFilter": {"tickSize": "0.01"},
            "leverageFilter": {"maxLeverage": "20"},
        }

    def ticker(self, symbol):
        return {"markPrice": self.mark, "lastPrice": self.mark}

    def orderbook(self, symbol, limit=1):
        return self.book

    def instruments(self, symbol=None):
        row = dict(self.instruments_info)
        if symbol:
            row["symbol"] = symbol
        return [row]

    def set_leverage(self, symbol, leverage):
        self.calls.append({"set_leverage": leverage, "symbol": symbol})
        return {}

    def create_order(self, **kw):
        self.calls.append(kw)
        o = {"orderId": f"f{len(self.calls)}", "orderStatus": "New",
             "orderLinkId": kw.get("order_link_id"),
             "cumExecQty": "0", "price": str(kw.get("price") or 0)}
        if kw.get("order_type") == "Market":
            o.update({"orderStatus": "Filled",
                      "cumExecQty": str(kw.get("qty")),
                      "avgPrice": str(self.mark), "cumExecFee": "0.01"})
            if kw.get("reduce_only"):
                self.pos_qty = max(0.0, self.pos_qty - kw["qty"])
        self.by_link[kw.get("order_link_id")] = o
        return o

    def cancel_order(self, symbol, order_link_id):
        self.calls.append({"cancel": order_link_id})
        return {}

    def get_order(self, symbol, link, order_id=None):
        return self.by_link.get(link, {"orderStatus": "New",
                                       "cumExecQty": "0", "price": "100"})

    def open_orders(self, symbol):
        return []

    def get_position(self, symbol):
        return {"symbol": symbol, "size": str(self.pos_qty),
                "side": self.pos_side, "avgPrice": str(self.mark)}

    def all_positions(self, settle_coin="USDT", limit=200):
        if self.pos_qty <= 0:
            return []
        return [{"symbol": self.pos_symbol, "size": str(self.pos_qty),
                 "side": self.pos_side, "avgPrice": str(self.mark)}]

    def closed_pnl(self, symbol, limit=20):
        return list(self.pnl)

    def order_history(self, symbol, order_id="", order_link_id="", limit=50):
        rows = [o for o in self.by_link.values()
                if (order_id and str(o.get("orderId")) == str(order_id))
                or (order_link_id
                    and str(o.get("orderLinkId")) == str(order_link_id))]
        return rows[:limit]

    def set_trading_stop(self, symbol, stop_loss, trigger="MarkPrice"):
        self.calls.append({"trading_stop": stop_loss, "trigger": trigger})
        return {}

    def closed_executions(self, symbol, since_ms):
        return [e for e in self.executions
                if e.get("symbol") == symbol and e.get("execTime", 0) >= since_ms]


class FakeJournal:
    def __init__(self):
        self.rows = []

    def __getattr__(self, name):
        def w(*a, **kw):
            self.rows.append((name, a, kw))
        return w

    def reasons(self, kind):
        return [kw.get("exit_reason") or kw.get("reason")
                for n, a, kw in self.rows if n == kind]


def make_bot(client=None, **kw):
    from types import SimpleNamespace
    bot = E.DcaBot.__new__(E.DcaBot)
    bot.p = SimpleNamespace(max_hold_minutes=90, max_cycle_loss_usdt=5.0,
                            max_docups=2, take_profit_pct=1.2)
    bot.bp = SimpleNamespace(fill_timeout_ms=100)
    bot.client = client or FakeClient()
    bot.journal = FakeJournal()
    bot.trailing_enabled = True
    bot.trail_trigger, bot.trail_step = 1.5, 0.5
    bot.exit_retry_delays = ()
    bot.exit_maker_enabled, bot.exit_maker_timeout_sec = True, 15.0
    bot.max_exit_slippage_pct, bot.exit_force_after_sec = 0.5, 180.0
    bot.cycles = {}
    bot._instruments = {}
    bot._last_prices = {}
    bot.processed = set()
    bot.offset = 0
    bot.orphan_recheck_sec = 60.0
    bot._last_reconcile_at = None
    bot._positions_on_exchange = 0
    bot._orphans_total = 0
    bot.state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "_tmp_state.json")
    for k, v in kw.items():
        setattr(bot, k, v)
    return bot


def cyc(**kw):
    now = int(time.time() * 1000)
    c = {"cycle_id": "t1", "signal_id": "t1", "symbol": "TSTUSDT",
         "side": "Buy", "qty": 1.0, "avg_entry": 100.0, "docups": 0,
         "fee": 0.0, "last_fill_price": 100.0, "qty_step": 0.001,
         "tick_size": 0.01, "min_qty": 0.001, "base_link": "t1",
         "links": {}, "fills": {}, "tp_seq": 0, "stop_level": 97.5,
         "trail_active": False, "trail_peak": None, "closed": False,
         "open_ts": now, "signal_ts": now, "tp_price": 101.2,
         "realized_pnl": 0.0, "natr": 1.0}
    c.update(kw)
    return c


def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name + (" — " + str(detail) if detail else ""))
    if not cond:
        raise SystemExit(1)
