"""П.1: PostOnly-выход не пересекает стакан: Sell -> лучший ask, Buy -> лучший bid."""
from exit_fake import FakeClient, make_bot, cyc, check
import executor as E

ob = {"bids": [[99.9, 10]], "asks": [[100.1, 10]]}
check("maker Sell == best ask",
      abs(E.DcaBot._maker_exit_price(ob, "Sell") - 100.1) < 1e-9)
check("maker Buy == best bid",
      abs(E.DcaBot._maker_exit_price(ob, "Buy") - 99.9) < 1e-9)
check("maker Sell не пересекает (цена >= best ask)",
      E.DcaBot._maker_exit_price(ob, "Sell") >= ob["asks"][0][0])
check("пустой стакан -> 0.0",
      E.DcaBot._maker_exit_price({"bids": [], "asks": []}, "Sell") == 0.0)
# _best_exit_price не тронут: встречная цена для маркета/спреда
check("best Sell == best bid (встречная)",
      abs(E.DcaBot._best_exit_price(ob, "Sell") - 99.9) < 1e-9)
check("best Buy == best ask (встречная)",
      abs(E.DcaBot._best_exit_price(ob, "Buy") - 100.1) < 1e-9)

# Сквозной путь: широкий спред -> maker-лимитка по ask для Sell
client = FakeClient()
client.book = {"bids": [[90.0, 10]], "asks": [[110.0, 10]]}
bot = make_bot(client)
c = cyc(qty=1.0, avg_entry=100.0)
bot.cycles["t1"] = c
bot._close_slow(c, "time_exit")
lmts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Limit"]
check("одна maker-лимитка", len(lmts) == 1, lmts)
check("PostOnly reduceOnly",
      lmts[0]["time_in_force"] == "PostOnly" and lmts[0]["reduce_only"] is True,
      lmts)
check("цена Sell == ask (110, квант tick 0.01)",
      abs(float(lmts[0]["price"]) - 110.0) < 1e-9, lmts)
check("не пересекает: цена >= best ask",
      float(lmts[0]["price"]) >= 110.0)
mkts = [k for k in client.calls
        if isinstance(k, dict) and k.get("order_type") == "Market"]
check("маркета не было", mkts == [], mkts)
print("ITEM6 OK")
