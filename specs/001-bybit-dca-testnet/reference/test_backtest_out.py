"""
reference/test_backtest_out.py — автономные проверки экспорта сделок из
бэктеста (reference/backtest.py --out-cycles, T023).

Без сети: CSV-источник, fetch_instrument подменён заглушкой. Проверяются
cycle_to_dict (поля для report_charts.py), write_cycles (.json и .jsonl),
сквозной прогон CLI --out-cycles и приём вывода отчётом report_charts.py.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_backtest_out.py
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time

_ref = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ref)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def _load(name: str):
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load("backtest")
charts = _load("report_charts")

INST = {"symbol": "BTCUSDT", "qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}


def bars(n: int, start: float = 100.0, drift: float = 0.15, rng: float = 1.0,
         ts0: int = 0, step_ms: int = 60_000, amp: float = 0.5, freq: float = 5.0) -> list[list]:
    import math
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100 + amp * math.sin(2 * math.pi * i / freq) / 100)
        out.append([ts0 + i * step_ms, base, base * (1 + rng / 100),
                    base, base * (1 + rng / 200), 1.0])
    return out


def run_backtest(csv_path: str, out_cycles: str, **cli):
    args = ["--csv", csv_path, "--out-cycles", out_cycles,
            "--max-hold-minutes", "30", "--max-concurrent", "1"]
    for k, v in cli.items():
        args += [k, str(v)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bt.main(args)
    return rc, buf.getvalue()


tmp = tempfile.mkdtemp(prefix="bt-out-test-")
now = int(time.time() * 1000)
ts0 = (now // 60_000 - 2200) * 60_000
csv_path = os.path.join(tmp, "uptrend.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ts", "open", "high", "low", "close", "volume"])
    w.writerows(bars(2000, drift=0.15, rng=1.2, ts0=ts0))
bt.fetch_instrument = lambda symbol: dict(INST)

print("cycle_to_dict и write_cycles")
c = bt.Cycle(symbol="BTCUSDT", side="Buy", open_ts=1000,
             qty_step=0.001, min_qty=0.001, tick_size=0.01,
             avg_entry=100.5, qty=0.5, docups=2, exit_ts=61000,
             exit_price=101.5, exit_reason="take_profit", pnl=0.5, fee=0.02)
d = bt.cycle_to_dict(c)
ok("cycle_to_dict содержит поля для equity-графика",
   {"exit_ts", "pnl", "symbol", "open_ts", "exit_reason", "duration_ms",
    "docups", "fee"} <= set(d), sorted(d))
ok("cycle_to_dict: duration_ms = exit - open", d["duration_ms"] == 60_000)
ok("cycle_to_dict: pnl округлён", d["pnl"] == 0.5)
ok("cycle_to_dict: exit_reason перенесён", d["exit_reason"] == "take_profit")

jpath = os.path.join(tmp, "cycles.json")
n = bt.write_cycles([c], jpath, title="BTCUSDT")
ok("write_cycles(.json): число циклов", n == 1)
with open(jpath, encoding="utf-8") as f:
    payload = json.load(f)
ok("write_cycles(.json): обёртка cycles/title",
   payload.get("title") == "BTCUSDT" and isinstance(payload.get("cycles"), list))
ok("write_cycles(.json): цикл имеет exit_ts",
   payload["cycles"][0]["exit_ts"] == 61_000)

jlpath = os.path.join(tmp, "cycles.jsonl")
n = bt.write_cycles([c], jlpath)
ok("write_cycles(.jsonl): число циклов", n == 1)
with open(jlpath, encoding="utf-8") as f:
    rows = [json.loads(line) for line in f if line.strip()]
ok("write_cycles(.jsonl): одна строка на цикл", len(rows) == 1)
ok("write_cycles(.jsonl): поля как в JSON",
   rows[0]["exit_ts"] == 61_000 and rows[0]["pnl"] == 0.5)

# Сортировка по exit_ts при экспорте нескольких циклов
c2 = bt.Cycle(symbol="BTCUSDT", side="Sell", open_ts=2000,
              qty_step=0.001, min_qty=0.001, tick_size=0.01,
              avg_entry=100.0, qty=0.4, docups=0, exit_ts=3000,
              exit_price=99.5, exit_reason="time_exit", pnl=-0.2, fee=0.01)
jpath2 = os.path.join(tmp, "sorted.json")
bt.write_cycles([c, c2], jpath2)
with open(jpath2, encoding="utf-8") as f:
    cycles = json.load(f)["cycles"]
ok("write_cycles: сортировка по exit_ts",
   [x["exit_ts"] for x in cycles] == [3000, 61000],
   [x["exit_ts"] for x in cycles])

print("\nCLI --out-cycles (без сети)")
out_json = os.path.join(tmp, "cli.json")
rc, out = run_backtest(csv_path, out_json)
ok("CLI --out-cycles(.json) rc 0", rc == 0, rc)
with open(out_json, encoding="utf-8") as f:
    payload = json.load(f)
ok("CLI: в JSON есть закрытые циклы", len(payload["cycles"]) >= 1,
   len(payload["cycles"]))
ok("CLI: title = символ", payload["title"] == "BTCUSDT", payload["title"])
cyc0 = payload["cycles"][0]
ok("CLI: поля цикла полные",
   {"exit_ts", "pnl", "symbol", "side", "open_ts", "exit_reason",
    "exit_price", "avg_entry", "duration_ms", "docups", "fee"} <= set(cyc0),
   sorted(cyc0))

out_jsonl = os.path.join(tmp, "cli.jsonl")
rc, _ = run_backtest(csv_path, out_jsonl)
ok("CLI --out-cycles(.jsonl) rc 0", rc == 0, rc)
with open(out_jsonl, encoding="utf-8") as f:
    rows = [json.loads(line) for line in f if line.strip()]
ok("CLI: JSONL строки валидны и отсортированы",
   len(rows) >= 1 and all(rows[i]["exit_ts"] <= rows[i + 1]["exit_ts"]
                          for i in range(len(rows) - 1)))

print("\nсквозной: backtest --out-cycles → report_charts --kind equity")
data, title = charts.load_input(out_json)
ok("report_charts.load_input принимает вывод бэктеста", len(data) >= 1 and title,
   (len(data), title))
svg = charts.render_equity_svg(data, title=title)
ok("render_equity_svg рисует кривую",
   "<svg" in svg and "polyline" in svg)
png_path = os.path.join(tmp, "equity.png")
rc_png = charts.main(["--kind", "equity", "--input", out_json,
                      "--out", png_path, "--title", title])
ok("report_charts CLI equity: rc 0", rc_png == 0, rc_png)
with open(png_path, "rb") as f:
    head = f.read(8)
ok("PNG с сигнатурой", head == b"\x89PNG\r\n\x1a\n")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)