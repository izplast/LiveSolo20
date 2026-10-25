"""Закрытия: только closing fills, closed-pnl, funding и order history.

Проверяем:
- входные филлы/funding не попадают в цену выхода;
- qty/avgEntry берутся из closed-pnl, а не из локального состояния;
- funding пишется отдельно и не вычитается из closedPnl повторно;
- triggerPrice/stopOrderType берутся из order history закрывающего orderId;
- TP-филл финализируется по closed-pnl своего orderId.

Запуск:  python3 tests/test_closing_fills.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


class ExecClient(FakeClient):
    def __init__(self, rows, exec_rows):
        super().__init__()
        self.pnl = rows
        self._exec_rows = exec_rows
        # FakeClient хранит executions списком; здесь нужен метод.
        del self.executions

    def executions(self, symbol, start_time_ms=None, limit=50):
        return [e for e in self._exec_rows
                if e.get("symbol") == symbol
                and (not start_time_ms
                     or e.get("execTime", 0) >= start_time_ms)]


now = int(time.time() * 1000)
open_ts = now - 60_000

# 1) агрегация: вход, funding и чужая сторона игнорируются
execs = [
    {"symbol": "TSTUSDT", "execTime": now - 50_000, "side": "Buy",
     "closedSize": "0", "execQty": "2.0", "execPrice": "100",
     "execFee": "0.11", "execType": "Trade"},
    {"symbol": "TSTUSDT", "execTime": now - 40_000, "side": "Sell",
     "closedSize": "2.0", "execQty": "2.0", "execPrice": "97",
     "execFee": "0.08", "execType": "Trade", "orderId": "sl1"},
    {"symbol": "TSTUSDT", "execTime": now - 30_000, "side": "Sell",
     "closedSize": "0", "execQty": "0", "execPrice": "0",
     "execFee": "0.12", "execType": "Funding"},
]
agg = E.DcaBot._aggregate_executions(execs, "TSTUSDT", open_ts,
                                     exit_side="Sell")
check("qty только закрывающая", abs(agg["qty"] - 2.0) < 1e-12, agg)
check("цена только закрывающая (97)", abs(agg["exit_price"] - 97) < 1e-12,
      agg)
check("fee только closing (0.08)", abs(agg["fee"] - 0.08) < 1e-12, agg)
check("orderId закрытия", agg["order_id"] == "sl1", agg)

# 2) flat-цикл: qty/avgEntry/PnL из closed-pnl, funding отдельно
pnl = [{"symbol": "TSTUSDT", "orderId": "sl1", "qty": "1.5",
        "avgEntryPrice": "99.0", "avgExitPrice": "96.0",
        "closedPnl": "-4.6", "openFee": "0.03", "closeFee": "0.04",
        "stopOrderType": "StopLoss", "updatedTime": str(now)}]
funding = [{"symbol": "TSTUSDT", "execTime": now - 20_000,
            "side": "Sell", "closedSize": "0", "execQty": "0",
            "execPrice": "0", "execFee": "0.10", "execType": "Funding"}]
client = ExecClient(pnl, funding)
client.pos_qty = 0.0
client.by_link["sl1"] = {"orderId": "sl1", "orderStatus": "Filled",
                         "stopOrderType": "StopLoss",
                         "triggerPrice": "95.5"}
bot = make_bot(client)
c = cyc(qty=1.0, avg_entry=100.0, fee=0.5, stop_level=95.0,
        open_ts=open_ts, signal_ts=open_ts)
bot.cycles["t1"] = c

bot._finalize_flat_cycle(c, position_checked=True)
closed = [kw for n, a, kw in bot.journal.rows if n == "cycle_closed"]
check("flat: cycle_closed один", len(closed) == 1, closed)
kw = closed[0]
check("flat: qty из closed-pnl (1.5)", abs(kw["qty"] - 1.5) < 1e-12, kw)
check("flat: avg_entry из closed-pnl (99)", abs(kw["avg_price"] - 99) < 1e-12,
      kw)
check("flat: exit_price фактический (96)", abs(kw["exit_price"] - 96) < 1e-12,
      kw)
check("flat: trigger_price из order history (95.5)",
      abs(kw["trigger_price"] - 95.5) < 1e-12, kw)
check("flat: trigger_type=StopLoss", kw["trigger_type"] == "StopLoss", kw)
check("flat: reason=exchange_sl", kw["exit_reason"] == "exchange_sl", kw)
check("flat: pnl=closedPnl без повторного fee", abs(kw["pnl"] + 4.6) < 1e-12,
      kw)
check("flat: funding отдельно (-0.10)", abs(kw["funding"] + 0.10) < 1e-12,
      kw)
check("flat: pnl_source=closed_pnl", kw["pnl_source"] == "closed_pnl", kw)
check("flat: closing_order_id=sl1", kw["closing_order_id"] == "sl1", kw)

# 3) TP: closed-pnl по orderId, локальный fee входа не вычитается снова
client2 = ExecClient(
    [{"symbol": "TSTUSDT", "orderId": "tp1", "qty": "1.0",
      "avgEntryPrice": "100.0", "avgExitPrice": "101.0",
      "closedPnl": "0.8", "openFee": "0.05", "closeFee": "0.05",
      "updatedTime": str(now)}], [])
client2.pos_qty = 0.0
client2.by_link["tp1"] = {"orderId": "tp1", "orderStatus": "Filled",
                          "orderLinkId": "t1_tp1"}
bot2 = make_bot(client2)
c2 = cyc(qty=1.0, avg_entry=100.0, fee=0.1, stop_level=97.5,
         open_ts=open_ts, signal_ts=open_ts)
c2["tp_link"] = "t1_tp1"
c2["links"] = {"t1_tp1": {"role": "tp", "side": "Sell", "qty": 1.0,
                          "price": 101.0, "reduce_only": True,
                          "order_id": "tp1"}}
bot2.cycles["t1"] = c2
bot2._on_fill(c2, "t1_tp1", "tp", 1.0, 101.0, 0.05, now, client2.by_link["tp1"])
closed2 = [kw for n, a, kw in bot2.journal.rows if n == "cycle_closed"]
check("TP: cycle_closed один", len(closed2) == 1, closed2)
kw2 = closed2[0]
check("TP: pnl=closedPnl (0.8)", abs(kw2["pnl"] - 0.8) < 1e-12, kw2)
check("TP: pnl_source=closed_pnl", kw2["pnl_source"] == "closed_pnl", kw2)
check("TP: reason=take_profit", kw2["exit_reason"] == "take_profit", kw2)
check("TP: exit_price=101", abs(kw2["exit_price"] - 101) < 1e-12, kw2)

print("CLOSING_FILLS OK")
