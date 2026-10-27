"""Item 3: TP filled + size 0 -> take_profit, а не exchange_sl."""
import time

from exit_fake import FakeClient, make_bot, cyc, check

# --- TP исполнился, биржа пуста ---
client = FakeClient()
client.pos_qty = 0.0
bot = make_bot(client)
c = cyc(qty=1.0, avg_entry=100.0)
c["tp_link"] = "t1_tp1"
c["links"] = {"t1_tp1": {"role": "tp", "side": "Sell", "qty": 1.0,
                         "price": 101.2, "reduce_only": True}}
client.by_link["t1_tp1"] = {"orderStatus": "Filled", "cumExecQty": "1.0",
                            "avgPrice": "101.2", "cumExecFee": "0.05",
                            "price": "101.2"}
bot.cycles["t1"] = c

bot._sync_position(c)

check("цикл закрыт", c.get("closed") is True)
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("reason=take_profit", len(closed) == 1
      and closed[0].get("exit_reason") == "take_profit", closed)
check("pnl по TP-цене", abs(c.get("pnl", 0) - (1.2 - 0.05)) < 1e-9, c.get("pnl"))

# --- контроль: TP не исполнялся -> exchange_sl (оценка по stop_price) ---
client2 = FakeClient()
client2.pos_qty = 0.0
bot2 = make_bot(client2)
c2 = cyc(qty=1.0, avg_entry=100.0)
c2["stop_armed"] = True
c2["stop_price"] = 97.5
c2["tp_link"] = "t1_tp1"
c2["links"] = {"t1_tp1": {"role": "tp", "side": "Sell", "qty": 1.0,
                          "price": 101.2, "reduce_only": True}}
client2.by_link["t1_tp1"] = {"orderStatus": "New", "cumExecQty": "0",
                             "price": "101.2"}
now2 = int(time.time() * 1000)
client2.executions = [{"symbol": "TSTUSDT", "execTime": now2,
                       "execPrice": "97.5", "execQty": "1.0",
                       "stopOrderType": "StopLoss", "orderType": "Market"}]
client2.pnl = [{"symbol": "TSTUSDT", "avgExitPrice": "97.5",
                "closedPnl": "-2.55", "updatedTime": str(now2)}]
bot2._sync_position(c2)
closed2 = [kw for n, a, kw in bot2.journal.rows if n == "cycle_closed"]
check("без TP-филла reason=exchange_sl", len(closed2) == 1
      and closed2[0].get("exit_reason") == "exchange_sl", closed2)
check("exchange_sl pnl из closed-pnl",
      closed2 and abs(closed2[0].get("pnl", 0) + 2.55) < 1e-9
      and closed2[0].get("pnl_source") == "closed_pnl", closed2)
print("ITEM3 OK")
