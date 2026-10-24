"""Item 5 + П.2: цена выхода exchange_sl — фактическая из closed-pnl.

По докам Bybit v5 closedPnl чистый (Position P&L - openFee - closeFee -
funding), поэтому в realized он идёт как есть, без повторного вычета fee;
openFee+closeFee пишутся в журнал только информационно.
"""
import time
from exit_fake import FakeClient, make_bot, cyc, check


class PnlClient(FakeClient):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def closed_pnl(self, symbol, limit=20):
        return [r for r in self._rows if r.get("symbol") == symbol]


now = int(time.time() * 1000)
# Buy 1.0 @100, выход 97.1: position P&L -2.9, openFee 0.05, closeFee 0.07,
# funding 0 -> чистый closedPnl = -3.02
rows = [{"symbol": "TSTUSDT", "avgExitPrice": "97.1", "closedPnl": "-3.02",
         "openFee": "0.05", "closeFee": "0.07",
         "stopOrderType": "StopLoss", "updatedTime": str(now)}]
client = PnlClient(rows)
client.pos_qty = 0.0
bot = make_bot(client)
c = cyc(qty=1.0, avg_entry=100.0, fee=0.05, stop_level=97.5)
c["open_ts"] = now - 60_000
c["signal_ts"] = now - 60_000
c["stop_armed"] = True
c["stop_price"] = 97.5
c["tp_link"] = "t1_tp1"
c["links"] = {"t1_tp1": {"role": "tp", "side": "Sell", "qty": 1.0,
                         "price": 101.2, "reduce_only": True}}
client.by_link["t1_tp1"] = {"orderStatus": "New", "cumExecQty": "0",
                            "price": "101.2"}
bot.cycles["t1"] = c

bot._sync_position(c)

check("цикл закрыт", c.get("closed") is True)
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("reason=exchange_sl", len(closed) == 1
      and closed[0].get("exit_reason") == "exchange_sl", closed)
fills = [kw for n, a, kw in bot.journal.rows
         if n == "write" and a and a[0] == "order_filled"
         and kw.get("mode") == "exchange_stop"]
check("один order_filled exchange_stop", len(fills) == 1, fills)
check("цена из closed-pnl (97.1, не stop 97.5)",
      abs(fills[0].get("avg_fill_price", 0) - 97.1) < 1e-9, fills)
check("не estimated", fills[0].get("estimated") is False, fills)
check("price_source=closed_pnl", fills[0].get("price_source") == "closed_pnl")
check("fee в журнале = openFee+closeFee (инфо, 0.12)",
      abs(fills[0].get("fee", 0) - 0.12) < 1e-9, fills)
check("pnl = чистый closedPnl без повторного вычета (-3.02)",
      abs(c.get("pnl", 0) - (-3.02)) < 1e-9, c.get("pnl"))
check("c[fee] обнулён (net уже включает комиссии)",
      abs(c.get("fee", -1) - 0.0) < 1e-9, c.get("fee"))

# --- фолбэк: API молчит -> оценка по stop_price + estimated=True ---
client2 = FakeClient()
client2.pos_qty = 0.0
bot2 = make_bot(client2)
c2 = cyc(qty=1.0, avg_entry=100.0, fee=0.0, stop_level=97.5)
c2["stop_armed"] = True
c2["stop_price"] = 97.5
c2["tp_link"] = None
c2["links"] = {}
bot2._sync_position(c2)
fills2 = [kw for n, a, kw in bot2.journal.rows
          if n == "write" and a and a[0] == "order_filled"
          and kw.get("estimated") is True]
check("фолбэк пишет estimated=True", len(fills2) == 1
      and fills2[0].get("estimated") is True, fills2)
check("фолбэк цена = stop_price", abs(fills2[0].get("avg_fill_price", 0)
                                      - 97.5) < 1e-9, fills2)
print("ITEM5 OK")
