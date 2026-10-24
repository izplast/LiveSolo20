"""110017 / reconcile: цикл без позиции на бирже финализируется.

Два бага с прода:
- reduce-only закрытие в нулевую позицию возвращает 110017, цикл не
  финализируется и занимает слот (KATUSDT);
- reconcile искал только сирот (позиция без цикла), но не обратную сторону
  (цикл без позиции).

Проверяем: цена/PnL из closed-pnl, pnl_source=closed_pnl, exit_reason по
факту (exchange_sl при StopLoss в исполнениях, иначе external_close),
оставшиеся ордера цикла сняты.

Запуск:  python3 tests/test_flat_cycle_finalize.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


class ZeroPositionClient(FakeClient):
    """Позиции нет: любой reduce-only ордер получает боевой 110017."""

    def create_order(self, **kw):
        if kw.get("reduce_only") and self.pos_qty <= 0:
            raise E.BybitApiError(
                110017, "current position is zero, cannot fix reduce-only order qty")
        return super().create_order(**kw)


def flat_client(stop_hit: bool, pnl: float = -2.5, exit_px: float = 97.5):
    now = int(time.time() * 1000)
    client = ZeroPositionClient()
    client.pos_qty = 0.0
    client.pnl = [{"symbol": "TSTUSDT", "avgExitPrice": str(exit_px),
                   "closedPnl": str(pnl), "openFee": "0.02",
                   "closeFee": "0.03", "updatedTime": str(now)}]
    if stop_hit:
        client.executions = [{"symbol": "TSTUSDT", "execTime": now,
                              "side": "Sell", "execPrice": str(exit_px),
                              "execQty": "2.0", "execType": "Trade",
                              "stopOrderType": "StopLoss",
                              "orderType": "Market"}]
    return client, now


def linked_cycle(now: int, **kw) -> dict:
    c = cyc(qty=2.0, avg_entry=100.0, fee=0.3, stop_level=97.5,
            open_ts=now - 60_000, signal_ts=now - 60_000, **kw)
    c["stop_armed"] = True
    c["stop_price"] = 97.5
    c["links"] = {
        "t1_tp1": {"role": "tp", "side": "Sell", "qty": 2.0,
                   "price": 101.2, "reduce_only": True, "order_id": "o1"},
        "t1_so1": {"role": "dca", "side": "Buy", "qty": 1.0,
                   "price": 98.8, "reduce_only": False, "order_id": "o2"},
    }
    c["fills"] = {"t1_tp1": 0.0, "t1_so1": 0.0}
    return c


def cycle_closed(bot):
    return [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]


def cancels(bot):
    return [c.get("cancel") for c in bot.client.calls if c.get("cancel")]


def api_errors(bot, operation):
    return [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "api_error"
            and kw.get("operation") == operation]


# 1) 110017 на reduce-only маркете, позиции нет, в исполнениях StopLoss
client, now = flat_client(stop_hit=True)
bot = make_bot(client)
c = linked_cycle(now)
c["close_reason"] = "hard_sl"
c["closing"] = {"reason": "hard_sl", "fast": True}
bot.cycles[c["cycle_id"]] = c

bot._monitor_cycles()

closed = cycle_closed(bot)
check("110017 → цикл финализирован", bot.cycles == {}, bot.cycles)
check("cycle_closed записан один раз", len(closed) == 1, closed)
check("exit_reason=exchange_sl (StopLoss в исполнениях)",
      closed and closed[0].get("exit_reason") == "exchange_sl", closed)
check("pnl из closedPnl", closed and abs(closed[0].get("pnl", 0) + 2.5) < 1e-9,
      closed)
check("pnl_source=closed_pnl",
      closed and closed[0].get("pnl_source") == "closed_pnl", closed)
check("ордера цикла сняты", set(cancels(bot)) >= {"t1_tp1", "t1_so1"},
      cancels(bot))
check("110017 не журналируется как ошибка закрытия",
      not api_errors(bot, "market_close"), api_errors(bot, "market_close"))

# 2) тот же 110017, но StopLoss в исполнениях нет → external_close
client2, now2 = flat_client(stop_hit=False, pnl=-1.25, exit_px=98.0)
bot2 = make_bot(client2)
c2 = linked_cycle(now2)
c2["close_reason"] = "hard_sl"
c2["closing"] = {"reason": "hard_sl", "fast": True}
bot2.cycles[c2["cycle_id"]] = c2
bot2._monitor_cycles()
closed2 = cycle_closed(bot2)
check("без StopLoss → exit_reason=external_close",
      closed2 and closed2[0].get("exit_reason") == "external_close", closed2)
check("external_close: pnl из closedPnl",
      closed2 and abs(closed2[0].get("pnl", 0) + 1.25) < 1e-9, closed2)

# 3) reconcile (b): цикл без позиции, без монитора — финализация
client3, now3 = flat_client(stop_hit=True, pnl=-3.0)
bot3 = make_bot(client3)
c3 = linked_cycle(now3)
bot3.cycles[c3["cycle_id"]] = c3
n_pos, n_orph = bot3._reconcile_positions()
closed3 = cycle_closed(bot3)
check("reconcile: цикл без позиции снят", bot3.cycles == {}, bot3.cycles)
check("reconcile: exchange_sl + closedPnl",
      closed3 and closed3[0].get("exit_reason") == "exchange_sl"
      and abs(closed3[0].get("pnl", 0) + 3.0) < 1e-9, closed3)
check("reconcile: это не сирота", n_orph == 0, (n_pos, n_orph))

# 4) reconcile (a): позиция без цикла по-прежнему сирота
client4 = FakeClient()
client4.pos_qty = 5.0
client4.pos_symbol = "TSTUSDT"
client4.pnl = [{"orderId": "f1", "closedPnl": "1.5"}]
bot4 = make_bot(client4)
n_pos4, n_orph4 = bot4._reconcile_positions()
orphans = [kw for n, a, kw in bot4.journal.rows
           if n == "write" and a and a[0] == "orphan_closed"]
check("reconcile: позиция без цикла — сирота", n_orph4 == 1 and orphans,
      (n_pos4, n_orph4, orphans))
check("сирота закрыта на бирже", client4.pos_qty == 0.0, client4.pos_qty)

# 5) позиция ещё есть — цикл не финализируется
client5, now5 = flat_client(stop_hit=True)
client5.pos_qty = 2.0
bot5 = make_bot(client5)
c5 = linked_cycle(now5)
bot5.cycles[c5["cycle_id"]] = c5
bot5._reconcile_positions()
check("позиция есть → цикл жив", c5["cycle_id"] in bot5.cycles, bot5.cycles)

print("FLAT_CYCLE_FINALIZE OK")
