"""
Автономные проверки reference/metrics.py: снапшоты скринера и симулятора,
CSV-строка, консольная сводка. Сеть не нужна: скринер и циклы — фикстуры.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_metrics.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

_ref = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load("metrics")
sc = m.sc

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def bars(n: int, step_ms: int = 60_000, drift: float = 0.15, rng: float = 1.2) -> list[list]:
    out = []
    for i in range(n):
        base = 100.0 * (1 + drift * i / 100)
        out.append([i * step_ms, base, base * (1 + rng / 100), base, base * (1 + rng / 200), 1.0])
    return out


class FakeState:
    def __init__(self, fast, slow, color="none"):
        self.fast = list(fast)
        self.slow = list(slow)
        self.last_color = color


class FakeCycle:
    def __init__(self, symbol="BTCUSDT", side="Buy", closed=False, pnl=0.42,
                 avg_entry=100.0, qty=0.2, docups=1, open_ts=0, exit_ts=60_000,
                 exit_reason="take_profit", fee=0.03, tp_level=101.2,
                 trend="green", natr=1.6):
        self.symbol = symbol
        self.side = side
        self.closed = closed
        self.pnl = pnl
        self.avg_entry = avg_entry
        self.qty = qty
        self.docups = docups
        self.open_ts = open_ts
        self.exit_ts = exit_ts
        self.exit_reason = exit_reason
        self.fee = fee
        self.tp_level = tp_level
        self.trend = trend
        self.natr = natr

    @property
    def duration_minutes(self):
        return (self.exit_ts - self.open_ts) / 60_000


cfg = sc.Config(natr_min=0.9, natr_max=2.5)

# ── снапшот скринера ─────────────────────────────────────────────────────────

print("снапшот скринера")
fast = bars(44)
slow = bars(44, step_ms=15 * 60_000)
states = {
    "AAAUSDT": FakeState(fast, slow, color="green"),
    "BBBUSDT": FakeState([], [], color="none"),
}
sm = m.screener_metrics(["AAAUSDT", "BBBUSDT", "CCCUSDT"], states, cfg)
ok("обработанные монеты = с окнами в памяти", sm["processed_coins"] == 1, sm)
ok("вселенная = все символы списка", sm["universe_size"] == 3, sm)
ok("активный сигнал один (green → Buy)",
   len(sm["active_signals"]) == 1 and sm["active_signals"][0]["side"] == "Buy",
   sm["active_signals"])
ok("у сигнала есть NATR и UHLO на обоих ТФ",
   sm["active_signals"][0]["natr"] is not None
   and sm["active_signals"][0]["uhlo_1m"] is not None
   and sm["active_signals"][0]["uhlo_15m"] is not None,
   sm["active_signals"][0])
ok("none-цвет не считается сигналом", all(a["symbol"] != "BBBUSDT" for a in sm["active_signals"]))

# ── снапшот симулятора ───────────────────────────────────────────────────────

print("\nснапшот симулятора")
open_cyc = FakeCycle(symbol="BTCUSDT", side="Buy", closed=False, avg_entry=100.0,
                     qty=0.2, docups=1, open_ts=0, tp_level=101.5)
win_cyc = FakeCycle(symbol="ETHUSDT", side="Buy", closed=True, pnl=0.42,
                    avg_entry=100.0, qty=0.2, exit_ts=120_000, exit_reason="take_profit")
loss_cyc = FakeCycle(symbol="SOLUSDT", side="Sell", closed=True, pnl=-0.10,
                     avg_entry=50.0, qty=0.4, open_ts=0, exit_ts=60_000,
                     exit_reason="stop", fee=0.05)

cm = m.cycle_metrics([open_cyc, win_cyc, loss_cyc], mark_price=lambda c: 102.0)
ok("открытый цикл: 1, SO=1", cm["open_cycles"] and cm["open_so_count"] == 1, cm)
ok("нереализованный PnL по mark=102 (Buy, avg=100, qty=0.2 → +0.4)",
   cm["open_unrealized_usdt"] == 0.4, cm["open_unrealized_usdt"])
ok("PnL % открытого = +2.0", abs(cm["open_unrealized_pct"] - 2.0) < 1e-9,
   cm["open_unrealized_pct"])
ok("закрытых сделок 2, WR 50%", cm["closed_trades"] == 2 and cm["win_rate_pct"] == 50.0, cm)
ok("реализованный PnL = 0.42-0.10 = 0.32",
   abs(cm["realized_pnl_usdt"] - 0.32) < 1e-9, cm["realized_pnl_usdt"])
ok("комиссии = 0.03+0.05 = 0.08",
   abs(cm["fees_usdt"] - 0.08) < 1e-9, cm["fees_usdt"])
ok("среднее время в позиции = (2+1)/2 = 1.5 мин",
   abs(cm["avg_duration_min"] - 1.5) < 1e-9, cm["avg_duration_min"])
ok("в сделках есть open_ts/close_ts",
   all("open_ts" in t and "close_ts" in t for t in cm["closed_trades_list"]), cm["closed_trades_list"])
ok("в сделках фиксируется тренд и NATR на входе",
   all("trend" in t and "entry_natr" in t for t in cm["closed_trades_list"])
   and cm["closed_trades_list"][0]["trend"] == "green"
   and cm["closed_trades_list"][1]["entry_natr"] == 1.6,
   cm["closed_trades_list"])
inv = win_cyc.avg_entry * win_cyc.qty + loss_cyc.avg_entry * loss_cyc.qty
ok("реализованный PnL % = 0.32 / вложенное",
   abs(cm["realized_pnl_pct"] - 0.32 / inv * 100) < 1e-9, cm["realized_pnl_pct"])

# ── CSV-строка и файл ────────────────────────────────────────────────────────

print("\nCSV")
row = m.metrics_row(sm, cm, 1_700_000_000_000, 30.0)
ok("число колонок совпадает с CSV_COLUMNS", len(row) == len(m.CSV_COLUMNS), len(row))
ok("первая колонка — ts", row[0] == 1_700_000_000_000)
ok("JSON-колонки валидные",
   json.loads(row[4]) == sm["active_signals"] and json.loads(row[17]) == cm["closed_trades_list"])

tmp = tempfile.mkdtemp(prefix="metrics-test-")
path = os.path.join(tmp, "test_metrics.csv")
m.append_csv(path, row)
m.append_csv(path, row)
with open(path, encoding="utf-8") as f:
    lines = f.read().strip().splitlines()
ok("заголовок пишется один раз, строк две",
   lines[0].startswith("ts,interval_min") and len(lines) == 3, len(lines))

# ── консольная сводка ────────────────────────────────────────────────────────

print("\nконсольная сводка")
summary = m.console_summary(sm, cm, 1_700_000_000_000)
ok("сводка содержит счётчики скринера", "активных сигналов 1" in summary, summary)
ok("сводка содержит WR и PnL симулятора",
   "WR 50.0%" in summary and "PnL +0.3200" in summary, summary)
ok("сводка содержит каждую закрытую сделку с open→close",
   "ETHUSDT" in summary and "SOLUSDT" in summary and "→" in summary, summary)
print(summary)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
