"""П.3: heartbeat показывает, что reconcile крутится, даже без сирот.

Запуск:  python3 tests/test_reconcile_heartbeat.py   (код 0 = ок)
"""
import logging
import time

from exit_fake import FakeClient, make_bot, check
import executor as E


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


def start_capture():
    h = Capture()
    E.logger.addHandler(h)
    return h


def stop_capture(h):
    E.logger.removeHandler(h)


# 1) старт: одна INFO-строка с числом позиций и сирот; heartbeat с метриками
client = FakeClient()
client.pos_qty = 5.0
client.pos_symbol = "JCTUSDT"
client.pos_side = "Buy"
client.pnl = [{"orderId": "f1", "closedPnl": "1.5"}]
bot = make_bot(client)
h = start_capture()
bot._startup_reconcile()
lines = [m for m in h.msgs if m.startswith("startup reconcile:")]
check("startup reconcile — ровно одна строка", len(lines) == 1, h.msgs)
check("startup reconcile: позиций 1, сирот 1",
      lines and "позиций на бирже 1" in lines[0] and "сирот 1" in lines[0],
      lines)

h.msgs.clear()
bot._log_heartbeat(time.monotonic() + 5.0)
hb = [m for m in h.msgs if m.startswith("heartbeat:")][0]
print("HB:", hb)
check("heartbeat: reconcile_last", "reconcile_last=" in hb, hb)
check("heartbeat: positions_on_exchange", "positions_on_exchange=1" in hb, hb)
check("heartbeat: orphans_total", "orphans_total=1" in hb, hb)
stop_capture(h)

# 2) повторная сверка без сирот: heartbeat остаётся информативным
h = start_capture()
n_pos, n_orph = bot._reconcile_positions()
check("после закрытия сирот нет", (n_pos, n_orph) == (0, 0), (n_pos, n_orph))
h.msgs.clear()
bot._log_heartbeat(time.monotonic())
hb2 = [m for m in h.msgs if m.startswith("heartbeat:")][0]
check("heartbeat без сирот: positions_on_exchange=0",
      "positions_on_exchange=0" in hb2, hb2)
check("heartbeat без сирот: orphans_total накопительный",
      "orphans_total=1" in hb2, hb2)
check("reconcile_last = 0 сек назад", "reconcile_last=0" in hb2, hb2)
stop_capture(h)

# 3) до первой сверки reconcile_last=-1
bot3 = make_bot()
h = start_capture()
bot3._log_heartbeat(time.monotonic())
hb3 = [m for m in h.msgs if m.startswith("heartbeat:")][0]
check("reconcile_last=-1 до сверки", "reconcile_last=-1" in hb3, hb3)
stop_capture(h)
print("RECONCILE_HEARTBEAT OK")
