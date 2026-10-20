"""П.3: сирота (позиция без цикла) закрывается маркетом, orphan_closed.

Запуск:  python3 tests/test_orphan_reconcile.py   (код 0 = ок)
"""
from exit_fake import FakeClient, make_bot, cyc, check
import executor as E


def make_orphan_bot(qty=5.0, symbol="JCTUSDT"):
    client = FakeClient()
    client.mark = 0.0024 if symbol == "JCTUSDT" else 100.0
    client.pos_qty = qty
    client.pos_symbol = symbol
    client.pos_side = "Buy"
    client.pnl = [{"orderId": "f1", "closedPnl": "1.5"}]
    p = E.DcaParams(entry_usdt=20.0, max_docups=2, stop_pct=1.0,
                    dca_step_pct=0.8, multiplier=2.0, leverage=4.0, tp_pct=1.2)
    return client, make_bot(client, p=p)


# 1) периодический reconcile закрывает сироту и пишет событие
client, bot = make_orphan_bot()
bot._reconcile_positions()
closed = [kw for n, a, kw in bot.journal.rows
          if n == "write" and a and a[0] == "orphan_closed"]
check("orphan_closed записан", len(closed) == 1, closed)
ev = closed[0] if closed else {}
check("orphan_closed symbol", ev.get("symbol") == "JCTUSDT", ev)
check("orphan_closed qty", abs(float(ev.get("qty") or 0) - 5.0) < 1e-9, ev)
check("orphan_closed side", ev.get("side") == "Buy", ev)
check("orphan_closed pnl", abs(float(ev.get("pnl") or 0) - 1.5) < 1e-9, ev)
check("позиция закрыта на бирже", client.pos_qty == 0.0, client.pos_qty)

# 2) стартовая сверка тоже ловит сироту (cycles пуст)
client2, bot2 = make_orphan_bot()
bot2._startup_reconcile()
closed2 = [kw for n, a, kw in bot2.journal.rows
           if n == "write" and a and a[0] == "orphan_closed"]
check("startup reconcile закрывает сироту", len(closed2) == 1, closed2)
check("позиция закрыта на старте", client2.pos_qty == 0.0, client2.pos_qty)

# 3) позиция под циклом бота не трогается
client3, bot3 = make_orphan_bot()
c = cyc(symbol="JCTUSDT")
c["qty"] = 5.0
bot3.cycles = {c["cycle_id"]: c}
bot3._reconcile_positions()
closed3 = [kw for n, a, kw in bot3.journal.rows
           if n == "write" and a and a[0] == "orphan_closed"]
check("под циклом не закрывается", not closed3, closed3)
check("позиция осталась", client3.pos_qty == 5.0, client3.pos_qty)
print("ORPHAN OK")
