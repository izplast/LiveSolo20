"""Регрессия 110017: не повторять филл, если пост-обработка упала.

Сценарий с прода: биржа закрыла позицию, а в links остался исполненный DCA.
Старый _monitor_cycle обрабатывал филл раньше сверки позиции: _on_fill
пытался выставить reduce-only TP в пустую позицию, Bybit отвечал 110017,
исключение обрывало цикл до `c["fills"][link] = cum`, поэтому один и тот же
филл писался в журнал каждые 3 секунды бесконечно.

Запуск:  python3 tests/test_monitor_fill_loop.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


class ZeroPositionClient(FakeClient):
    """Позиции нет: reduce-only ордер (TP) получает боевой 110017."""

    def create_order(self, **kw):
        if kw.get("reduce_only") and self.pos_qty <= 0:
            raise E.BybitApiError(
                110017, "current position is zero, cannot fix reduce-only order qty")
        return super().create_order(**kw)


class FailTpClient(FakeClient):
    """Позиция есть, но выставление TP падает по любой другой причине."""

    def create_order(self, **kw):
        if kw.get("reduce_only"):
            raise E.BybitApiError(110017, "simulated TP failure")
        return super().create_order(**kw)


def dca_link(link="t1_so2", qty=1.0, price=99.0):
    return {link: {"role": "dca", "side": "Buy", "qty": qty,
                   "price": price, "reduce_only": False, "order_id": "o1"}}


def count_fills(bot, role=None):
    return [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "order_filled"
            and (role is None or kw.get("role") == role)]


def count_errors(bot, operation):
    return [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "api_error"
            and kw.get("operation") == operation]


# 1) биржа закрыла позицию: цикл финализируется фактическим closedPnl,
#    устаревший DCA-филл не применяется и не повторяется
now = int(time.time() * 1000)
client = ZeroPositionClient()
client.pos_qty = 0.0
client.pnl = [{"symbol": "TSTUSDT", "avgExitPrice": "97.1",
               "closedPnl": "-2.0", "openFee": "0.02", "closeFee": "0.03",
               "updatedTime": str(now)}]
client.by_link["t1_so2"] = {"orderStatus": "Filled", "cumExecQty": "1.0",
                            "avgPrice": "99.0", "cumExecFee": "0.01",
                            "price": "99.0"}
bot = make_bot(client)
c = cyc(qty=2.0, avg_entry=100.0, fee=0.0, stop_level=97.5)
c["open_ts"] = now - 60_000
c["signal_ts"] = now - 60_000
c["stop_armed"] = True
c["stop_price"] = 97.5
c["tp_link"] = None
c["links"] = dca_link()
c["fills"] = {"t1_so2": 0.0}
bot.cycles[c["cycle_id"]] = c

bot._monitor_cycles()
bot._monitor_cycles()
bot._monitor_cycles()

check("биржа закрыла позицию → цикл снят", bot.cycles == {}, bot.cycles)
check("cycle_closed ровно один", len(bot.journal.reasons("cycle_closed")) == 1,
      bot.journal.reasons("cycle_closed"))
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("reason=unknown_exchange (следов StopLoss нет)",
      closed and closed[0].get("exit_reason") == "unknown_exchange", closed)
check("pnl из closedPnl", closed and abs(closed[0].get("pnl", 0) + 2.0) < 1e-9,
      closed)
check("устаревший DCA-филл не применён", not count_fills(bot, "dca"),
      count_fills(bot))
check("нет 110017/api_error", not count_errors(bot, "monitor")
      and not count_errors(bot, "on_fill"),
      [kw for n, a, kw in bot.journal.rows
       if n == "write" and a and a[0] == "api_error"])

# 2) сбой пост-обработки филла не должен повторять тот же филл каждый такт
client2 = FailTpClient()
client2.pos_qty = 1.0
client2.by_link["t1_so2"] = {"orderStatus": "Filled", "cumExecQty": "0.5",
                             "avgPrice": "99.0", "cumExecFee": "0.01",
                             "price": "99.0"}
bot2 = make_bot(client2, p=E.DcaParams(entry_usdt=20.0, max_docups=2,
                                       stop_pct=1.0, dca_step_pct=0.8,
                                       multiplier=2.0))
c2 = cyc(qty=1.0, avg_entry=100.0, fee=0.0, stop_level=97.5)
c2["stop_armed"] = True
c2["stop_price"] = 97.5
c2["tp_link"] = None
c2["links"] = dca_link(qty=0.5)
c2["fills"] = {"t1_so2": 0.0}
bot2.cycles[c2["cycle_id"]] = c2

bot2._monitor_cycles()
bot2._monitor_cycles()

check("филл записан один раз", len(count_fills(bot2, "dca")) == 1,
      count_fills(bot2, "dca"))
check("ошибка пост-обработки зажурналирована", len(count_errors(bot2, "on_fill")) == 1,
      count_errors(bot2, "on_fill"))
check("терминальный линк убран", "t1_so2" not in c2["links"], c2["links"])
check("qty учтён ровно один раз", abs(c2["qty"] - 1.5) < 1e-9, c2["qty"])

# 3) рестарт: закрытый на бирже цикл не должен «воскрешать» DCA-ордер
client3 = ZeroPositionClient()
client3.pos_qty = 0.0
client3.pnl = [{"symbol": "TSTUSDT", "avgExitPrice": "97.1",
                "closedPnl": "-2.0", "openFee": "0.02", "closeFee": "0.03",
                "updatedTime": str(now)}]
client3.by_link["t1_so2"] = {"orderStatus": "Filled", "cumExecQty": "1.0",
                             "avgPrice": "99.0", "cumExecFee": "0.01",
                             "price": "99.0"}
bot3 = make_bot(client3)
c3 = cyc(qty=2.0, avg_entry=100.0, fee=0.0, stop_level=97.5)
c3["open_ts"] = now - 60_000
c3["signal_ts"] = now - 60_000
c3["stop_armed"] = True
c3["stop_price"] = 97.5
c3["tp_link"] = None
c3["links"] = dca_link()
c3["fills"] = {"t1_so2": 0.0}
bot3.cycles[c3["cycle_id"]] = c3

bot3._startup_reconcile()

check("startup: цикл закрыт фактическим PnL", c3.get("closed") is True, c3)
closed3 = [kw for n, a, kw in bot3.journal.rows if n == "cycle_closed"]
check("startup: unknown_exchange + closedPnl",
      closed3 and closed3[0].get("exit_reason") == "unknown_exchange"
      and abs(closed3[0].get("pnl", 0) + 2.0) < 1e-9, closed3)
check("startup: DCA не пересоздан",
      not [c for c in client3.calls
           if c.get("order_link_id") == "t1_so2"],
      client3.calls)
print("MONITOR_FILL_LOOP OK")
