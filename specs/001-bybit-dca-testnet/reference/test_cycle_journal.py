"""
Проверки журнала циклов (reference/cycle_journal.py): метки открытия/закрытия,
длительность удержания, восстановление после перезапуска процесса.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_cycle_journal.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

_ref = os.path.dirname(os.path.abspath(__file__))
_path = os.path.join(_ref, "cycle_journal.py")
_spec = importlib.util.spec_from_file_location("cycle_journal_under_test", _path)
assert _spec and _spec.loader
cj = importlib.util.module_from_spec(_spec)
sys.modules["cycle_journal_under_test"] = cj
_spec.loader.exec_module(cj)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def read_lines(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


print("cycle_journal: открытие/закрытие цикла")

fd, path = tempfile.mkstemp(suffix=".jsonl")
os.close(fd)
os.unlink(path)
clock = Clock()
j = cj.CycleJournal(path, now=clock.now)

clock.advance(60_000)
j.cycle_opened("C1", "BTCUSDT")
clock.advance(60_000)
j.cycle_closed("C1", "BTCUSDT", "take_profit", pnl=0.64)

rows = read_lines(path)
ok("открытие и закрытие записаны", [r["kind"] for r in rows] == ["cycle_opened", "cycle_closed"], rows)
opened = rows[0]
ok("cycle_opened содержит open_ts", isinstance(opened.get("open_ts"), int), opened)
ok("open_ts равен моменту записи", opened["open_ts"] == opened["ts"], opened)
closed = rows[1]
ok("cycle_closed содержит close_ts", isinstance(closed.get("close_ts"), int), closed)
ok("cycle_closed дублирует open_ts", closed["open_ts"] == opened["open_ts"], closed)
ok("duration_ms = close_ts - open_ts", closed["duration_ms"] == closed["close_ts"] - closed["open_ts"], closed)
ok("duration_ms положителен", closed["duration_ms"] > 0, closed)
ok("pnl сохранён", closed.get("pnl") == 0.64, closed)
ok("kind и ts не перезаписываются payload", "kind" in closed and "ts" in closed)
j.close()
os.unlink(path)

print("\ncycle_closed: exit_reason валидируется")

fd, path = tempfile.mkstemp(suffix=".jsonl")
os.close(fd)
os.unlink(path)
j = cj.CycleJournal(path, now=Clock().now)
j.cycle_opened("C2", "ETHUSDT")
raised = False
try:
    j.cycle_closed("C2", "ETHUSDT", "magic_exit")
except ValueError:
    raised = True
ok("неизвестная причина выхода отклоняется", raised)
j.cycle_closed("C2", "ETHUSDT", "hard_sl", pnl=-2.5)
rows = read_lines(path)
ok("закрытие по SL записано", rows[-1]["exit_reason"] == "hard_sl", rows[-1])
ok("отрицательный PnL сохранён", rows[-1]["pnl"] == -2.5, rows[-1])
j.cycle_closed("C2", "ETHUSDT", "trailing", pnl=0.9)
rows = read_lines(path)
ok("закрытие по трейлингу записано", rows[-1]["exit_reason"] == "trailing", rows[-1])
j.close()
os.unlink(path)

print("\nвосстановление после перезапуска процесса")

fd, path = tempfile.mkstemp(suffix=".jsonl")
os.close(fd)
os.unlink(path)
clock = Clock()
j1 = cj.CycleJournal(path, now=clock.now)
j1.cycle_opened("C3", "SOLUSDT", open_ts=1_000_000)
j1.cycle_opened("C4", "XRPUSDT", open_ts=1_060_000)
j1.cycle_closed("C4", "XRPUSDT", "time_exit", pnl=0.0, close_ts=1_120_000)
j1.close()

clock2 = Clock()
clock2.t = 1_500_000
close3 = 1_500_000
j2 = cj.CycleJournal(path, now=clock2.now)  # «процесс перезапустился»
j2.cycle_closed("C3", "SOLUSDT", "take_profit", pnl=1.1, close_ts=close3)

rows = read_lines(path)
last = rows[-1]
ok("длительность пережила перезапуск", last["duration_ms"] == close3 - 1_000_000, last)
ok("закрытый до рестарта цикл не засчитан открытым", last["cycle_id"] == "C3")
j2.close()

# двойное закрытие: второй раз open_ts неизвестен, событие всё равно пишется
j2 = cj.CycleJournal(path, now=clock2.now)
j2.cycle_closed("C3", "SOLUSDT", "take_profit", pnl=1.1, close_ts=clock2.t)
rows = read_lines(path)
ok("повторное закрытие без open_ts не падает", rows[-1]["kind"] == "cycle_closed")
ok("при повторном закрытии open_ts отсутствует", rows[-1].get("open_ts") is None, rows[-1])
ok("duration_ms отсутствует при неизвестном open_ts", rows[-1].get("duration_ms") is None, rows[-1])
j2.close()
os.unlink(path)

print(f"\nИтог: {PASS} ok, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
