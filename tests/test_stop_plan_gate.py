"""Стоп-план: отказ до входа и неподвижность стопа после DCA.

- Если стоп не дальше последнего уровня лестницы, сигнал отклоняется
  до создания entry-ордера.
- После докупки stop_level не пересчитывается и на биржу не перевыставляется.

Запуск:  python3 tests/test_stop_plan_gate.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


now = int(time.time() * 1000)
client = FakeClient()
p = E.DcaParams(entry_usdt=20.0, max_docups=2, multiplier=2.0,
                dca_step_pct=1.2, step_atr_mult=0.0,
                stop_pct=0.1, sl_atr_mult=0.0, tp_pct=1.2,
                max_hold_minutes=90, leverage=4.0)
bot = make_bot(client, p=p)
sig = {"signal_id": "SIG1", "symbol": "TSTUSDT", "side": "Buy",
       "price": 100.0, "ts": now, "natr": 1.0}
bot._open_cycle(sig, now)

rejected = [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "signal_rejected"]
check("слабый стоп: сигнал отклонён", len(rejected) == 1
      and rejected[0].get("reason") == "risk_limit", rejected)
check("слабый стоп: entry не создавался",
      not [c for c in client.calls if c.get("order_link_id") == "SIG1"],
      client.calls)
plan_rej = [kw for n, a, kw in bot.journal.rows
            if n == "write" and a and a[0] == "stop_plan_rejected"]
check("слабый стоп: stop_plan_rejected в журнале", len(plan_rej) == 1,
      plan_rej)
check("слабый стоп: стоп не дальше последнего уровня",
      plan_rej and plan_rej[0]["stop_level"] >= plan_rej[0]["last_level"],
      plan_rej)

# --- DCA не двигает уже выставленный стоп ---
bot2 = make_bot(FakeClient(), p=E.DcaParams(
    entry_usdt=20.0, max_docups=2, multiplier=2.0, dca_step_pct=1.2,
    step_atr_mult=0.0, stop_pct=2.5, sl_atr_mult=0.0, tp_pct=1.2,
    max_hold_minutes=90, leverage=4.0))
c = cyc(side="Buy", qty=1.0, avg_entry=100.0, natr=1.0,
        stop_level=97.5, stop_avg_entry=98.0, docups=0)
c["links"] = {}
c["fills"] = {}
c["tp_link"] = None
c["so_link"] = None
c["so_seq"] = 0
bot2.cycles["t1"] = c
before = c["stop_level"]
bot2._on_fill(c, "t1_so1", "dca", 1.0, 98.8, 0.05, now, {"price": "98.8"})
check("DCA: средняя изменилась", abs(c["avg_entry"] - 99.4) < 1e-12,
      c["avg_entry"])
check("DCA: stop_level не пересчитан", c["stop_level"] == before,
      (before, c["stop_level"]))
check("DCA: trading_stop не перевыставлялся",
      not [x for x in bot2.client.calls if "trading_stop" in x],
      bot2.client.calls)

print("STOP_PLAN_GATE OK")
