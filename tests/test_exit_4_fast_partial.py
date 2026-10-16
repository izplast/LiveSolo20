"""Item 4: IOC исполнил 40%, цена откатилась выше стопа -> следующий такт
добивает остаток маркетом IOC reduceOnly (maker-путь для fast не используется)."""
from exit_fake import FakeClient, make_bot, cyc, check


class PartialClient(FakeClient):
    """Первый маркет заливает 40%, остальные — полностью."""

    def __init__(self):
        super().__init__()
        self.markets = 0

    def create_order(self, **kw):
        if kw.get("order_type") == "Market":
            self.markets += 1
            n = self.markets
            self.calls.append(kw)
            qty = float(kw.get("qty"))
            if n == 1:
                part = round(qty * 0.4, 12)
                o = {"orderId": f"f{n}", "orderStatus": "PartiallyFilled",
                     "cumExecQty": str(part), "avgPrice": str(self.mark),
                     "cumExecFee": "0.01", "price": str(kw.get("price") or 0)}
                self.pos_qty = max(0.0, self.pos_qty - part)
            else:
                o = {"orderId": f"f{n}", "orderStatus": "Filled",
                     "cumExecQty": str(qty), "avgPrice": str(self.mark),
                     "cumExecFee": "0.01", "price": str(kw.get("price") or 0)}
                self.pos_qty = max(0.0, self.pos_qty - qty)
            self.by_link[kw.get("order_link_id")] = o
            return o
        return super().create_order(**kw)


client = PartialClient()
client.mark = 97.0                      # стоп 97.5 пробит для Buy
client.pos_qty = 1.0
bot = make_bot(client)
c = cyc(qty=1.0, avg_entry=100.0, stop_level=97.5)
bot.cycles["t1"] = c

bot._close_fast(c, "hard_sl")

check("closing взведён fast=True",
      c.get("closing") == {"reason": "hard_sl", "fast": True}, c.get("closing"))
check("частичный филл списан (остаток 0.6)", abs(c["qty"] - 0.6) < 1e-9, c["qty"])
check("цикл не финализирован", c.get("closed") is not True)
mkts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Market"]
check("первый выход — один маркет", len(mkts) == 1, mkts)
check("маркет IOC reduceOnly",
      mkts[0]["reduce_only"] is True and mkts[0]["time_in_force"] == "IOC", mkts)

# Цена откатилась выше стопа — медленный путь мог бы ждать, fast добивает.
client.mark = 100.5
bot._drive_closing(c)

mkts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Market"]
check("второй такт — добивочный маркет", len(mkts) == 2, mkts)
check("добивка на остаток 0.6", abs(float(mkts[1]["qty"]) - 0.6) < 1e-9, mkts)
check("добивка IOC reduceOnly",
      mkts[1]["reduce_only"] is True and mkts[1]["time_in_force"] == "IOC")
check("уникальные линки (без дубля orderLinkId)",
      mkts[0]["order_link_id"] != mkts[1]["order_link_id"],
      [m["order_link_id"] for m in mkts])
lmts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Limit"]
check("maker-лимиток не было", lmts == [], lmts)
check("позиция вышла в ноль", abs(c.get("qty", -1)) < 1e-9, c.get("qty"))
check("цикл закрыт как hard_sl", c.get("closed") is True)
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("journal cycle_closed/hard_sl",
      len(closed) == 1 and closed[0].get("exit_reason") == "hard_sl", closed)
print("ITEM4 OK")
