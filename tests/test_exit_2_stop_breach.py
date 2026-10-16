"""Item 2: пробитый стоп -> ровно один маркет reduceOnly, без постановки на биржу."""
from exit_fake import FakeClient, FakeJournal, make_bot, cyc, check

# --- breached stop: докупка подняла stop_level выше mark ---
client = FakeClient()
client.mark = 97.0                    # stop 97.5 уже пробит для Buy
client.pos_qty = 1.0
bot = make_bot(client)
c = cyc(stop_level=97.5)
bot.cycles["t1"] = c

bot._sync_exchange_stop(c)

mkts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Market"]
stops = [k for k in client.calls if "trading_stop" in k]
check("ровно один маркет", len(mkts) == 1, mkts)
check("маркет reduceOnly", mkts[0]["reduce_only"] is True)
check("стоп на биржу не ставился", stops == [], stops)
check("цикл закрыт как hard_sl",
      c.get("closed") is True and c.get("pnl") is not None, c.get("pnl"))
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("journal cycle_closed/hard_sl",
      len(closed) == 1 and closed[0].get("exit_reason") == "hard_sl", closed)

# --- прочая ошибка постановки при НЕпробитом стопе -> stop_arm_failed ---
client2 = FakeClient()
client2.mark = 100.5                  # стоп 97.5 не пробит


def boom(symbol, stop_loss, trigger="MarkPrice"):
    raise RuntimeError("Bybit retCode 34036")


client2.set_trading_stop = boom
bot2 = make_bot(client2)
c2 = cyc(stop_level=97.5)
bot2._sync_exchange_stop(c2)
check("stop_armed остался False", not c2.get("stop_armed"))
fails = [kw for n, a, kw in bot2.journal.rows
         if n == "write" and a and a[0] == "stop_arm_failed"]
check("journal stop_arm_failed", len(fails) == 1, fails)
mkts2 = [k for k in client2.calls
         if isinstance(k, dict) and k.get("order_type") == "Market"]
check("маркета не было", mkts2 == [], mkts2)

# --- slTriggerBy передаётся явно ---
client3 = FakeClient()
client3.mark = 100.5
bot3 = make_bot(client3)
bot3._sync_exchange_stop(cyc(stop_level=97.5))
stops3 = [k for k in client3.calls if "trading_stop" in k]
check("slTriggerBy=MarkPrice явно",
      stops3 and stops3[0].get("trigger") == "MarkPrice", stops3)
print("ITEM2 OK")
