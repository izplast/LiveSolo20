"""
Проверки сводки по закрытым циклам (tools/stats_summary.py).

Синтетический журнал бота → ожидаемые счётчики, PnL, win-rate и длительности.
Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_stats_summary.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_repo = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_path = os.path.join(_repo, "tools", "stats_summary.py")
_spec = importlib.util.spec_from_file_location("stats_summary_under_test", _path)
assert _spec and _spec.loader
ss = importlib.util.module_from_spec(_spec)
sys.modules["stats_summary_under_test"] = ss
_spec.loader.exec_module(ss)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def write_journal(events: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


T0 = 1_760_000_000_000  # 2026-08-07 ... UTC, мс
HOUR = 3_600_000
MIN = 60_000


def opened(cid: str, sym: str, open_ts: int) -> dict:
    return {"kind": "cycle_opened", "cycle_id": cid, "symbol": sym,
            "open_ts": open_ts, "ts": open_ts}


def closed(cid: str, sym: str, reason: str, pnl: float, open_ts: int,
           close_ts: int, ts: int | None = None) -> dict:
    return {"kind": "cycle_closed", "cycle_id": cid, "symbol": sym,
            "exit_reason": reason, "pnl": pnl,
            "open_ts": open_ts, "close_ts": close_ts,
            "duration_ms": close_ts - open_ts,
            "ts": ts if ts is not None else close_ts}


print("collect_cycles: новый формат с полями open_ts/close_ts/duration_ms")

path = write_journal([
    opened("C1", "BTCUSDT", T0),
    opened("C2", "ETHUSDT", T0),
    closed("C1", "BTCUSDT", "take_profit", 0.64, T0, T0 + 5 * MIN),
    closed("C2", "ETHUSDT", "hard_sl", -2.50, T0, T0 + 10 * MIN),
    {"kind": "signal_received", "cycle_id": "C9", "ts": T0},   # посторонний kind
    "not-json-at-all\n",
])
cycles = ss.collect_cycles([__import__("pathlib").Path(path)])
ok("закрыто два цикла", len(cycles) == 2, cycles)
by_id = {c["cycle_id"]: c for c in cycles}
ok("причины выхода сохранены",
   by_id["C1"]["exit_reason"] == "take_profit" and by_id["C2"]["exit_reason"] == "hard_sl")
ok("duration_ms считан из поля",
   by_id["C1"]["duration_ms"] == 5 * MIN and by_id["C2"]["duration_ms"] == 10 * MIN)
ok("PnL сохранён со знаком", by_id["C2"]["pnl"] == -2.50)
os.unlink(path)

print("\ncollect_cycles: старый формат без полей — связывание по cycle_id")

path = write_journal([
    {"kind": "cycle_opened", "cycle_id": "OLD1", "symbol": "BTCUSDT", "ts": T0},
    {"kind": "cycle_closed", "cycle_id": "OLD1", "symbol": "BTCUSDT",
     "exit_reason": "time_exit", "pnl": 0.0, "ts": T0 + 3 * MIN},
])
cycles = ss.collect_cycles([__import__("pathlib").Path(path)])
ok("старый формат разобран", len(cycles) == 1, cycles)
c = cycles[0]
ok("open_ts взят из cycle_opened.ts", c["open_ts"] == T0, c)
ok("close_ts взят из cycle_closed.ts", c["close_ts"] == T0 + 3 * MIN, c)
ok("duration_ms выведен из разницы", c["duration_ms"] == 3 * MIN, c)
os.unlink(path)

print("\nsummarize: агрегаты")

path = write_journal([
    opened("C1", "BTCUSDT", T0),
    opened("C2", "ETHUSDT", T0),
    opened("C3", "SOLUSDT", T0),
    opened("C4", "BTCUSDT", T0),
    opened("C5", "BTCUSDT", T0),          # не закрыт — в сводку не входит
    closed("C1", "BTCUSDT", "take_profit", 0.64, T0, T0 + 5 * MIN),
    closed("C2", "ETHUSDT", "hard_sl", -2.50, T0, T0 + 10 * MIN),
    closed("C3", "SOLUSDT", "time_exit", 0.10, T0, T0 + 30 * MIN),
    closed("C4", "BTCUSDT", "take_profit", 3.20, T0, T0 + 15 * MIN),
])
cycles = ss.collect_cycles([__import__("pathlib").Path(path)])
s = ss.summarize(cycles)
ok("закрыто 4 цикла (незакрытый не считается)", s["закрыто_циклов"] == 4, s)
ok("разбивка по причинам",
   s["по_причине"]["take_profit"] == 2
   and s["по_причине"]["hard_sl"] == 1
   and s["по_причине"]["time_exit"] == 1, s["по_причине"])
ok("совокупный PnL = 0.64 - 2.50 + 0.10 + 3.20 = 1.44", s["pnl_usdt"] == 1.44, s)
ok("win-rate: прибыль только pnl > 0 → 3 из 4 = 75%", s["win_rate_pct"] == 75.0, s)
durs = sorted([5 * MIN, 10 * MIN, 30 * MIN, 15 * MIN])
ok("среднее удержание",
   s["удержание_мс"]["среднее"] == round(sum(durs) / 4), s["удержание_мс"])
ok("медиана удержания (ближайший ранг, n=4 → 2-й элемент 10м)",
   s["удержание_мс"]["медиана"] == 10 * MIN, s["удержание_мс"])
ok("min/max удержания",
   s["удержание_мс"]["min"] == 5 * MIN and s["удержание_мс"]["max"] == 30 * MIN)

s = ss.summarize(cycles, by_symbol=True)
ok("разбивка по символам есть", "по_символам" in s, s)
btc = s["по_символам"]["BTCUSDT"]
ok("по символу: 2 цикла, PnL 3.84, WR 100%",
   btc["закрыто_циклов"] == 2 and btc["pnl_usdt"] == 3.84 and btc["win_rate_pct"] == 100.0,
   btc)
os.unlink(path)

print("\nокно по датам (--days / --since / --until)")

# Окно считается от реального «сейчас», поэтому события строим от него,
# а не от произвольного T0 (который может оказаться вне --days 7).
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
DAY = 86_400_000
path = write_journal([
    opened("W1", "BTCUSDT", now_ms - 10 * DAY),
    closed("W1", "BTCUSDT", "take_profit", 1.0, now_ms - 10 * DAY,
           now_ms - 10 * DAY + 5 * MIN),
    opened("W2", "ETHUSDT", now_ms - 1 * HOUR),
    closed("W2", "ETHUSDT", "time_exit", 0.5, now_ms - 1 * HOUR,
           now_ms - 1 * HOUR + 30 * MIN),
    opened("W3", "SOLUSDT", now_ms - 2 * HOUR),
    closed("W3", "SOLUSDT", "hard_sl", -1.0, now_ms - 2 * HOUR,
           now_ms - 2 * HOUR + 10 * MIN),
])
class FakeArgs:
    days = 7
    since = None
    until = None

cycles = ss.collect_cycles([__import__("pathlib").Path(path)])
since_ms, until_ms = ss.parse_windows(FakeArgs())
win = [c for c in cycles if (since_ms is None or (c["close_ts"] or 0) >= since_ms)
       and (until_ms is None or (c["close_ts"] or 0) <= until_ms)]
ok("--days 7 отсекает цикл, закрытый 10 суток назад",
   sorted(c["cycle_id"] for c in win) == ["W2", "W3"], [c["cycle_id"] for c in win])
os.unlink(path)

print("\nCLI: рендер и JSON")

lines = ss.render(s, by_symbol=True).splitlines()
ok("в рендере есть строка win-rate", any("Win-rate" in l for l in lines), lines)
ok("в рендере есть блок по символам", any("По символам" in l for l in lines), lines)

print(f"\nИтог: {PASS} ok, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
