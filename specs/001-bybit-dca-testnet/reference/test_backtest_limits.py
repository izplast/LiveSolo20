"""
Автономные проверки лимитов и ограничений инструмента в бэктесте (T024).

Покрывает FR-017/FR-018 как в живом боте: лимит номинала лестницы
(max_notional_usdt), максимальное плечо инструмента (max_leverage) в расчёте
маржи и при отклонении сигнала, трекинг номинала/маржи в цикле, сводка и CLI.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_backtest_limits.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import sys

_ref = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, _ref)


def _load(name: str):
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load("backtest")
sc = sys.modules["screener"]

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


# INST без max_leverage — фолбэк на 100x
INST = {"symbol": "BTCUSDT", "qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}
# Инструмент с низким плечом
LOWLEV = dict(INST, max_leverage=2.0)

print("валидация параметров")
ok("max_notional_usdt отрицательный → ValueError",
   raises(lambda: bt.DcaParams(max_notional_usdt=-1).validate()))
ok("max_notional_usdt = 0 (выключен) валиден",
   not raises(lambda: bt.DcaParams(max_notional_usdt=0).validate()))
ok("leverage = 0 → ValueError (деление на ноль в марже)",
   raises(lambda: bt.DcaParams(leverage=0).validate()))
ok("leverage по умолчанию 3x валиден",
   not raises(lambda: bt.DcaParams().validate()))

print("\nplanned_ladder_notional")
ok("лестница 20 + 40 + 80 = 140 USDT (multiplier 2, 2 докупки)",
   abs(bt.planned_ladder_notional(bt.DcaParams(entry_usdt=20, multiplier=2.0,
                                               max_docups=2)) - 140.0) < 1e-9)
ok("multiplier 1 → entry * (docups + 1)",
   abs(bt.planned_ladder_notional(bt.DcaParams(entry_usdt=20, multiplier=1.0,
                                               max_docups=3)) - 80.0) < 1e-9)
ok("без докупок (max_docups=0) → только вход",
   abs(bt.planned_ladder_notional(bt.DcaParams(entry_usdt=20, multiplier=2.0,
                                               max_docups=0)) - 20.0) < 1e-9)

print("\nэффективное плечо и маржа")
b0 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, leverage=4.0),
                 "BTCUSDT", INST)
ok("инструмент без max_leverage → фолбэк 100x, плечо 4x",
   b0.effective_leverage == 4.0 and b0.max_leverage == 100.0)
b0._open_cycle("Buy", 0, 100.0)
c0 = b0.open[-1]
ok("маржа цикла = номинал / плечо (4x)",
   abs(c0.margin - c0.notional / 4.0) < 1e-9, (c0.notional, c0.margin))
ok("номинал цикла после входа ≈ 50 USDT (qty*price)",
   abs(c0.notional - c0.qty * c0.avg_entry) < 1e-9)

print("\nмаксимальное плечо инструмента (FR-017/018)")
bl = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, leverage=4.0),
                 "BTCUSDT", LOWLEV)
bl._open_cycle("Buy", 0, 100.0)
ok("плечо выше максимума инструмента → сигнал отклонён",
   bl.rejected_leverage == 1 and len(bl.open) == 0)
ok("эффективное плечо не выше максимума инструмента",
   bl.effective_leverage == 2.0 and bl.max_leverage == 2.0)

bl2 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, leverage=2.0),
                  "BTCUSDT", LOWLEV)
bl2._open_cycle("Buy", 0, 100.0)
ok("плечо в пределах максимума → цикл открывается",
   bl2.rejected_leverage == 0 and len(bl2.open) == 1)
ok("маржа с плечом 2x = номинал / 2",
   abs(bl2.open[0].margin - bl2.open[0].notional / 2.0) < 1e-9)

print("\nлимит номинала лестницы (max_notional_usdt)")
bn = bt.Backtest(sc.Config(),
                 bt.DcaParams(entry_usdt=20, multiplier=2.0, max_docups=2,
                              max_notional_usdt=100.0),
                 "BTCUSDT", INST)
bn._open_cycle("Buy", 0, 100.0)
ok("план 140 USDT > лимит 100 → сигнал отклонён",
   bn.rejected_notional == 1 and len(bn.open) == 0)

bn2 = bt.Backtest(sc.Config(),
                  bt.DcaParams(entry_usdt=20, multiplier=2.0, max_docups=2,
                               max_notional_usdt=200.0),
                  "BTCUSDT", INST)
bn2._open_cycle("Buy", 0, 100.0)
ok("план 140 USDT <= лимит 200 → цикл открывается",
   bn2.rejected_notional == 0 and len(bn2.open) == 1)
ok("лимит 0 (выключен) → цикл не отклоняется",
   bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=20, multiplier=2.0,
                                         max_docups=2, max_notional_usdt=0.0),
               "BTCUSDT", INST).rejected_notional == 0)

print("\nсводка и экспорт")
b17 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, leverage=4.0),
                  "BTCUSDT", INST)
b17._open_cycle("Buy", 5 * 10 ** 12, 100.0)
w1 = b17.open[-1]
b17._close(w1, 105.0, "take_profit", 5 * 10 ** 12 + 60_000)
b17.remove_closed()
res = bt.RunResult("BTCUSDT", INST, 5 * 10 ** 12, 5 * 10 ** 12 + 60_000, 2000,
                   1, 0, [], b17.closed)
m = bt.summarize(res)
ok("сводка содержит отклонения по номиналу и плечу",
   m["rejected_notional"] == 0 and m["rejected_leverage"] == 0, m)
ok("сводка содержит максимальный номинал цикла",
   abs(m["max_notional_usdt"] - w1.notional) < 1e-9, m["max_notional_usdt"])
ok("сводка содержит максимальную маржу цикла",
   abs(m["max_margin_usdt"] - w1.margin) < 1e-9, m["max_margin_usdt"])
d = bt.cycle_to_dict(w1)
ok("экспорт цикла содержит номинал и маржу",
   "notional" in d and "margin" in d, d)

print("\nCLI --max-notional-usdt")
import csv as _csv
import contextlib
import io
import tempfile
import time as _time
now = int(_time.time() * 1000)
ts0 = (now // 60_000 - 2200) * 60_000
_tmp = tempfile.mkdtemp(prefix="bt-limits-test-")
csv_path = os.path.join(_tmp, "uptrend.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = _csv.writer(f)
    w.writerow(["ts", "open", "high", "low", "close", "volume"])
    for i in range(2000):
        base = 100.0 + 0.15 * i + 0.5 * math.sin(2 * math.pi * i / 5)
        w.writerow([ts0 + i * 60_000, round(base, 4), round(base + 1.2, 4),
                    round(base - 1.2, 4), round(base, 4), 1000.0])

bt.fetch_instrument = lambda symbol: dict(INST)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = bt.main(["--csv", csv_path, "--max-notional-usdt", "200",
                    "--max-concurrent", "1", "--json"])
ok("CLI --max-notional-usdt парсится и завершается кодом 0", code == 0, code)
try:
    data = json.loads(buf.getvalue())
    parsed = True
except json.JSONDecodeError:
    data, parsed = {}, False
ok("CLI --json выдаёт валидный JSON", parsed, buf.getvalue()[:200])
ok("JSON: сводка содержит лимиты и маржу",
   parsed and {"rejected_notional", "max_notional_usdt", "max_margin_usdt"} <= set(data),
   sorted(data))
shutil.rmtree(_tmp, ignore_errors=True)

print(f"\nитог: {PASS} ok, {FAIL} fail")
raise SystemExit(1 if FAIL else 0)