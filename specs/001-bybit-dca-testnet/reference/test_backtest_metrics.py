"""
Автономные проверки расширенных метрик бэктеста (T026).

Sharpe/Sortino по закрытым циклам, гистограмма длительностей удержания,
интеграция в сводку/отчёт. Без сети, без pytest.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_backtest_metrics.py
"""

from __future__ import annotations

import importlib.util
import math
import os
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


def near(a, b, tol=1e-9):
    return a is not None and abs(a - b) < tol


# ── sharpe_ratio ─────────────────────────────────────────────────────────────

print("sharpe_ratio")
ok("меньше двух сделок → None", bt.sharpe_ratio([]) is None)
ok("одна сделка → None", bt.sharpe_ratio([1.0]) is None)
ok("одинаковые PnL → None (std=0)", bt.sharpe_ratio([1.0, 1.0, 1.0]) is None)
# pnls = [-1, 1]: mean=0, std=1, sharpe = 0/1*sqrt(2)=0
ok("симметричные сделки → 0", near(bt.sharpe_ratio([-1.0, 1.0]), 0.0),
   bt.sharpe_ratio([-1.0, 1.0]))
# pnls = [1, 1, -2]: mean=0, std=sqrt((1+1+4)/3)=sqrt(2), sharpe=0*sqrt(3)/sqrt(2)=0
# вместо него возьмём [2, 2, -1]: mean=1, std=sqrt((1+1+4)/3)=sqrt(2)
# sharpe = 1/sqrt(2)*sqrt(3) = sqrt(3/2)
ok("положительная серия → sharpe = sqrt(3/2)",
   near(bt.sharpe_ratio([2.0, 2.0, -1.0]), math.sqrt(1.5)),
   bt.sharpe_ratio([2.0, 2.0, -1.0]))
# безрисковая rf: mean-rf; pnls=[2,2,-1] с rf=1 → mean=1, (mean-rf)=0 → 0
ok("безрисковая вычитается", near(bt.sharpe_ratio([2.0, 2.0, -1.0], rf=1.0), 0.0),
   bt.sharpe_ratio([2.0, 2.0, -1.0], rf=1.0))

print("\nsortino_ratio")
ok("меньше двух сделок → None", bt.sortino_ratio([1.0]) is None)
ok("нет отрицательных отклонений → None",
   bt.sortino_ratio([1.0, 2.0, 3.0]) is None)
# pnls=[-2, 2]: mean=0, downside=[-2] → d_std=2, sortino=0*sqrt(2)/2=0
ok("симметричные → 0", near(bt.sortino_ratio([-2.0, 2.0]), 0.0),
   bt.sortino_ratio([-2.0, 2.0]))
# pnls=[2, 2, -1]: mean=1, downside=[-1] → d_std=1,
# sortino = 1/1*sqrt(3)=sqrt(3)
ok("с одним проигрышем → sqrt(3)",
   near(bt.sortino_ratio([2.0, 2.0, -1.0]), math.sqrt(3.0)),
   bt.sortino_ratio([2.0, 2.0, -1.0]))

print("\nduration_histogram")
# 3 цикла: 10, 35, 90 мин; bins=4 → шаг 22.5
fake = [
    type("C", (), {"duration_minutes": 10.0})(),
    type("C", (), {"duration_minutes": 35.0})(),
    type("C", (), {"duration_minutes": 90.0})(),
]
h = bt.duration_histogram(fake, bins=4)
ok("4 бина, сумма = 3", len(h) == 4 and sum(b["count"] for b in h) == 3, h)
ok("первый бин ловит 10 мин", h[0]["count"] == 1, h)
ok("максимум попадает в последний бин", h[-1]["count"] == 1, h)
ok("границы монотонно растут",
   all(h[i]["lo"] < h[i + 1]["lo"] for i in range(len(h) - 1)), h)
ok("пустой список → []", bt.duration_histogram([], 4) == [])
ok("bins < 1 → []", bt.duration_histogram(fake, 0) == [])
h1 = bt.duration_histogram(fake, bins=1)
ok("bins=1 → один бин со всеми", len(h1) == 1 and h1[0]["count"] == 3, h1)
h0 = bt.duration_histogram([type("C", (), {"duration_minutes": 0.0})()], 4)
ok("нулевые длительности → один бин", len(h0) == 1 and h0[0]["count"] == 1, h0)

print("\nсводка: sharpe/sortino/гистограмма")
b = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50), "BTCUSDT",
                {"symbol": "BTCUSDT", "qty_step": 0.001, "min_qty": 0.001,
                 "tick_size": 0.01})
for i, pnl in enumerate((0.3, -0.2, 0.4)):
    c = bt.Cycle(symbol="BTCUSDT", side="Buy", open_ts=1000 + i * 60_000,
                 qty_step=0.001, min_qty=0.001, tick_size=0.01)
    c.pnl = pnl
    c.exit_ts = 1000 + i * 60_000 + 60_000
    c.fee = 0.0
    c.exit_reason = "take_profit" if pnl >= 0 else "stop"
    b.closed.append(c)
res = bt.RunResult("BTCUSDT", {}, 1000, 1000 + 3 * 60_000, 10, 3, 0, [], b.closed)
m = bt.summarize(res, bins=5)
ok("в сводке есть sharpe и sortino",
   "sharpe" in m and "sortino" in m, m)
# pnls=[0.3,-0.2,0.4]: mean=1/6, std=sqrt(((0.3-1/6)^2+(-0.2-1/6)^2+(0.4-1/6)^2)/3)
vals = [0.3, -0.2, 0.4]
mean = sum(vals) / 3
std = math.sqrt(sum((v - mean) ** 2 for v in vals) / 3)
ok("sharpe совпадает с формулой",
   near(m["sharpe"], mean / std * math.sqrt(3)), (m["sharpe"], mean / std * math.sqrt(3)))
# sortino: downside = [-0.2-0] → d_std = 0.2
ok("sortino: downside только отрицательные",
   near(m["sortino"], mean / 0.2 * math.sqrt(3)), m["sortino"])
ok("гистограмма в сводке (5 бинов)",
   len(m["_duration_hist"]) == 5 and sum(bn["count"] for bn in m["_duration_hist"]) == 3,
   m["_duration_hist"])
ok("длительности 1 мин → гистограмма корректна",
   m["_duration_hist"][-1]["count"] == 3, m["_duration_hist"])

print("\nrender: строка Sharpe и гистограмма")
m["_cycles"] = b.closed
render = bt.render(m, bt.DcaParams(entry_usdt=50), verbose=True)
ok("в отчёте есть Sharpe и Sortino",
   "Sharpe:" in render and "Sortino:" in render, render)
ok("в verbose-отчёте есть гистограмма",
   "Гистограмма длительностей" in render, render)
ok("в non-verbose нет гистограммы",
   "Гистограмма длительностей" not in bt.render(m, bt.DcaParams(entry_usdt=50)),
   "")

print("\nCLI --hist-bins")
import contextlib
import csv
import io
import tempfile
import time as _time
now = int(_time.time() * 1000)
ts0 = (now // 60_000 - 2200) * 60_000
_tmp = tempfile.mkdtemp(prefix="bt-metrics-test-")
csv_path = os.path.join(_tmp, "uptrend.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ts", "open", "high", "low", "close", "volume"])
    for i in range(2000):
        base = 100.0 + 0.15 * i
        w.writerow([ts0 + i * 60_000, round(base, 4), round(base + 1.2, 4),
                    round(base - 1.2, 4), round(base, 4), 1000.0])
bt.fetch_instrument = lambda symbol: {"symbol": "BTCUSDT", "qty_step": 0.001,
                                      "min_qty": 0.001, "tick_size": 0.01,
                                      "max_leverage": 100.0}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = bt.main(["--csv", csv_path, "--max-concurrent", "1",
                    "--hist-bins", "3", "--json"])
ok("CLI --hist-bins парсится, rc=0", code == 0, code)
import json as _json
data = _json.loads(buf.getvalue())
ok("JSON содержит sharpe/sortino",
   "sharpe" in data and "sortino" in data, sorted(data))
import shutil
shutil.rmtree(_tmp, ignore_errors=True)

print(f"\nитог: {PASS} ok, {FAIL} fail")
raise SystemExit(1 if FAIL else 0)