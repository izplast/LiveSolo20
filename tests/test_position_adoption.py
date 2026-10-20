"""П.2: ордер не найден, но позиция на бирже есть -> цикл принят, не reject.

Запуск:  python3 tests/test_position_adoption.py   (код 0 = ок)
"""
from exit_fake import FakeClient, make_bot, check
import executor as E


class NoOrderClient(FakeClient):
    """Ордер исполнился, но realtime/history/executions его не отдают."""

    def get_order(self, symbol, link, order_id=None):
        return None

    def create_order(self, **kw):
        o = super().create_order(**kw)
        return {"orderId": o.get("orderId"),
                "orderLinkId": kw.get("order_link_id"),
                "orderStatus": "New"}


client = NoOrderClient()
client.pos_qty = 0.2
client.pos_symbol = "TSTUSDT"
client.pos_side = "Buy"
client.mark = 100.0

p = E.DcaParams(entry_usdt=20.0, max_docups=2, stop_pct=1.0,
                dca_step_pct=0.8, multiplier=2.0, leverage=4.0, tp_pct=1.2)
bot = make_bot(client, p=p)
sig = {"symbol": "TSTUSDT", "side": "Buy", "signal_id": "t1", "price": 100.0,
       "ts": 1792478880000, "diagnostics": {"natr": 1.0}}
bot._open_cycle(sig, 1792478880000)

check("цикл не брошен, открыт", "t1" in bot.cycles, list(bot.cycles.keys()))
c = bot.cycles.get("t1", {})
check("qty принят с биржи", c.get("qty") == 0.2, c.get("qty"))
check("avgPrice принят с биржи", c.get("avg_entry") == 100.0, c.get("avg_entry"))
adopted = [kw for n, a, kw in bot.journal.rows
           if n == "write" and a and a[0] == "position_adopted"]
check("journal position_adopted", len(adopted) == 1, adopted)
opened = [kw for n, a, kw in bot.journal.rows if n == "cycle_opened"]
check("journal cycle_opened", len(opened) == 1, opened)
rejected = [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "signal_rejected"]
check("signal_rejected нет", not rejected, rejected)
stops = [kw for n, a, kw in bot.journal.rows
         if n == "write" and a and a[0] == "stop_armed"]
check("биржевой стоп выставлен сразу", len(stops) == 1, stops)


class FlatClient(NoOrderClient):
    def get_position(self, symbol):
        return {"symbol": symbol, "size": "0", "side": "", "avgPrice": "0"}


client2 = FlatClient()
client2.pos_qty = 0.0
bot2 = make_bot(client2, p=p)
bot2._open_cycle({"symbol": "TSTUSDT", "side": "Buy", "signal_id": "t2",
                  "price": 100.0, "ts": 1792478880000,
                  "diagnostics": {"natr": 1.0}}, 1792478880000)
check("нет позиции -> сигнал всё ещё реджектится", "t2" not in bot2.cycles,
      list(bot2.cycles.keys()))
print("ADOPT OK")


class FlakyCreateClient(FakeClient):
    """create_order упал (таймаут), но ордер дошёл и позиция открыта."""

    def __init__(self):
        super().__init__()
        self.failed = False

    def create_order(self, **kw):
        if not self.failed:
            self.failed = True
            raise RuntimeError("timeout")
        return super().create_order(**kw)


client3 = FlakyCreateClient()
client3.pos_qty = 0.2
client3.pos_symbol = "TSTUSDT"
client3.pos_side = "Buy"
client3.mark = 100.0
bot3 = make_bot(client3, p=p)
bot3._open_cycle({"symbol": "TSTUSDT", "side": "Buy", "signal_id": "t3",
                  "price": 100.0, "ts": 1792478880000,
                  "diagnostics": {"natr": 1.0}}, 1792478880000)
check("create_order упал, но позиция есть -> цикл открыт", "t3" in bot3.cycles,
      list(bot3.cycles.keys()))
rejected3 = [kw for n, a, kw in bot3.journal.rows
             if n == "write" and a and a[0] == "signal_rejected"]
check("нет signal_rejected при позиции", not rejected3, rejected3)
print("ADOPT-EXC OK")
