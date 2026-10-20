"""П.1: исполнение ищется realtime → history → executions; orderId из create.

Запуск:  python3 tests/test_order_lookup_fallback.py   (код 0 = ок)
"""
from exit_fake import FakeClient, make_bot, check
import executor as E


class RouteClient(E.BybitClient):
    """BybitClient без сети: _get маршрутизируется по path, executions — стаб."""

    def __init__(self):
        super().__init__(E.BybitConfig(api_key="k", api_secret="s", demo=True))
        self.realtime = []
        self.history = []
        self.execs = []
        self.paths = []

    def _get(self, path, params=None, signed=False):
        self.paths.append(path)
        if path == "/v5/order/realtime":
            return {"list": list(self.realtime)}
        if path == "/v5/order/history":
            return {"list": list(self.history)}
        return {"list": []}

    def executions(self, symbol, start_time_ms=None, limit=50):
        return list(self.execs)


FILLED = {"orderId": "ord1", "orderLinkId": "L1", "orderStatus": "Filled",
          "cumExecQty": "8300", "avgPrice": "0.002403",
          "cumExecFee": "0.0219"}

# 1) realtime пуст, в history Filled -> возвращаем history
c1 = RouteClient()
c1.history = [dict(FILLED)]
o1 = c1.get_order("JCTUSDT", "L1", order_id="ord1")
check("realtime пуст + Filled в history -> order найден", o1 is not None, o1)
check("статус Filled из history", o1 and o1.get("orderStatus") == "Filled", o1)
check("history спрошен после realtime",
      c1.paths[:2] == ["/v5/order/realtime", "/v5/order/history"], c1.paths)

# 2) пусто и там, и там, но есть execution -> синтезируем Filled
c2 = RouteClient()
c2.execs = [{"orderId": "ord2", "orderLinkId": "L2", "execType": "Trade",
             "side": "Buy", "execQty": "8300", "execPrice": "0.002403",
             "execFee": "0.0219"}]
o2 = c2.get_order("JCTUSDT", "L2", order_id="ord2")
check("realtime/history пусты + execution -> order найден", o2 is not None, o2)
check("execution синтезирован как Filled", o2 and o2["orderStatus"] == "Filled",
      o2)
check("cumExecQty из execution", o2 and abs(float(o2["cumExecQty"]) - 8300) < 1e-6,
      o2)
check("avgPrice из execution", o2 and abs(float(o2["avgPrice"]) - 0.002403) < 1e-9,
      o2)

# 3) realtime содержит ордер -> history/executions не нужны
c3 = RouteClient()
c3.realtime = [dict(FILLED)]
c3.history = [{"orderStatus": "Cancelled"}]
o3 = c3.get_order("JCTUSDT", "L1")
check("realtime отдаёт ордер", o3 and o3["orderStatus"] == "Filled", o3)
check("history не спрашивался при непустом realtime",
      c3.paths == ["/v5/order/realtime"], c3.paths)


# 4) бот: realtime пуст, Filled в history -> цикл открыт
class HistoryFallbackClient(FakeClient):
    def get_order(self, symbol, link, order_id=None):
        for o in self.history:
            if o.get("orderLinkId") == link:
                return o
        return None

    def create_order(self, **kw):
        o = super().create_order(**kw)
        if kw.get("order_type") == "Market":
            self.history.append(dict(o))
        return o


p = E.DcaParams(entry_usdt=20.0, max_docups=2, stop_pct=1.0,
                dca_step_pct=0.8, multiplier=2.0, leverage=4.0, tp_pct=1.2)
bot = make_bot(HistoryFallbackClient(), p=p)
sig = {"symbol": "TSTUSDT", "side": "Buy", "signal_id": "t1", "price": 100.0,
       "ts": 1792478880000, "diagnostics": {"natr": 1.0}}
bot._open_cycle(sig, 1792478880000)
check("цикл открыт по history-фолбэку", "t1" in bot.cycles,
      list(bot.cycles.keys()))
opened = [kw for n, a, kw in bot.journal.rows if n == "cycle_opened"]
check("journal cycle_opened", len(opened) == 1, opened)
rejected = [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "signal_rejected"]
check("signal_rejected нет", not rejected, rejected)
print("ORDER_LOOKUP OK")
