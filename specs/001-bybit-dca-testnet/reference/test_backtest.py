"""
Автономные проверки бэктеста стратегии DCA (reference/backtest.py).

Без сети: загрузка данных (CSV, пагинация/кэш klines через заглушку
_http_get, ограничения инструмента, вселенная), симуляция цикла
(сигнал → вход → докупки → TP/время/стоп, шорт-симметрия), лимит
одновременных циклов, метрики и сводка, CLI-прогон по синтетическому CSV.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_backtest.py
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
import urllib.parse

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


bt = _load("backtest")      # подтягивает screener, pricing, bot
sc = sys.modules["screener"]
pricing = sys.modules["pricing"]
bot = sys.modules["bot"]

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


INST = {"symbol": "BTCUSDT", "qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}


def bars(n: int, start: float = 100.0, drift: float = 0.0, rng: float = 1.0,
         ts0: int = 0, step_ms: int = 60_000) -> list[list]:
    """Свечи [ts, open, high, low, close, volume]; drift — % на бар, rng — размах в %."""
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100)
        out.append([ts0 + i * step_ms, base, base * (1 + rng / 100),
                    base, base * (1 + rng / 200), 1.0])
    return out


# ── 1. Импорт и доступность API ───────────────────────────────────────────────

print("импорт и API модуля")
for name in ("DcaParams", "Backtest", "RunResult", "read_csv", "aggregate_minutes",
             "fetch_klines", "fetch_instrument", "fetch_universe",
             "max_drawdown", "summarize", "render", "build_parser", "main"):
    ok(f"backtest.{name} доступен", hasattr(bt, name))
ok("tp_price взят из bot.py (тот же объект)", bt.tp_price is bot.tp_price)
ok("quantize_qty взят из pricing.py", bt.quantize_qty is pricing.quantize_qty)

# ── 2. DcaParams.validate ─────────────────────────────────────────────────────

print("\nDcaParams.validate")
ok("значения по умолчанию валидны", not raises(lambda: bt.DcaParams().validate()))
ok("entry_usdt <= 0 отвергается",
   raises(lambda: bt.DcaParams(entry_usdt=0).validate()))
ok("dca_step_pct <= 0 отвергается",
   raises(lambda: bt.DcaParams(dca_step_pct=0).validate()))
ok("max_docups < 0 отвергается",
   raises(lambda: bt.DcaParams(max_docups=-1).validate()))
ok("tp_pct <= 0 отвергается",
   raises(lambda: bt.DcaParams(tp_pct=0).validate()))
ok("max_hold_minutes <= 0 отвергается",
   raises(lambda: bt.DcaParams(max_hold_minutes=0).validate()))
ok("stop_pct вне [0, 50) отвергается",
   raises(lambda: bt.DcaParams(stop_pct=50).validate()))
ok("fee_rate < 0 отвергается",
   raises(lambda: bt.DcaParams(fee_rate=-0.001).validate()))
ok("slippage_pct > 5% отвергается",
   raises(lambda: bt.DcaParams(slippage_pct=0.051).validate()))
ok("max_concurrent < 1 отвергается",
   raises(lambda: bt.DcaParams(max_concurrent=0).validate()))

# ── 3. read_csv ───────────────────────────────────────────────────────────────

print("\nread_csv")
_tmp = tempfile.mkdtemp(prefix="bt-test-csv-")


def write_csv(rows: list[list], cols: tuple[str, ...]) -> str:
    path = os.path.join(_tmp, f"{cols[0]}-{len(rows)}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return path


rows_ms = [[1750000000000, 100.0, 101.0, 99.0, 100.5, 7.0],
           [1750000006000, 100.5, 102.0, 100.0, 101.0, 3.0],
           [1750000003000, 100.2, 100.9, 99.8, 100.6, 2.0]]
p1 = write_csv(rows_ms, ("ts", "open", "high", "low", "close", "volume"))
r = bt.read_csv(p1, None, None)
ok("строки разобраны и отсортированы по ts", [x[0] for x in r]
   == [1750000000000, 1750000003000, 1750000006000], [x[0] for x in r])
ok("ohlc и объём корректны", r[1] == [1750000003000, 100.2, 100.9, 99.8, 100.6, 2.0], r[1])

rows_sec = [[1750000000, 100.0, 101.0, 99.0, 100.5],
            [1750000060, 100.5, 102.0, 100.0, 101.0]]
p2 = write_csv(rows_sec, ("ts", "open", "high", "low", "close"))
r2 = bt.read_csv(p2, None, None)
ok("epoch-секунды автоматически переводятся в мс",
   [x[0] for x in r2] == [1750000000 * 1000, 1750000060 * 1000], r2)
ok("объём опционален (0.0)", all(x[5] == 0.0 for x in r2), r2)

ok("фильтр по start/end",
   [x[0] for x in bt.read_csv(p1, 1750000003000, 1750000004000)]
   == [1750000003000])
ok("колонка времени называется start",
   len(bt.read_csv(p2.replace("ts,", "start,"), None, None)) == 2)
p_bad = os.path.join(_tmp, "bad-header.csv")
with open(p_bad, "w", encoding="utf-8") as f:
    f.write("tick,open,high,low,close\n1750000000000,100,101,99,100.5\n")
ok("отсутствие колонки времени → ValueError",
   raises(lambda: bt.read_csv(p_bad, None, None)))

# ── 4. aggregate_minutes ──────────────────────────────────────────────────────

print("\naggregate_minutes")
agg = bt.aggregate_minutes([
    [0, 100.0, 101.0, 99.0, 100.5, 2.0],
    [60_000, 100.5, 102.0, 100.0, 101.0, 3.0],
    [120_000, 101.0, 101.5, 99.5, 99.8, 5.0],
], 3)
ok("3 свечи внутри одного бакета схлопываются в одну",
   len(agg) == 1 and agg[0][0] == 0, agg)
ok("open первого, high=max, low=min, close последнего, объём суммируется",
   agg[0][1:] == [100.0, 102.0, 99.0, 99.8, 10.0], agg)
agg2 = bt.aggregate_minutes([
    [0, 100.0, 101.0, 99.0, 100.5, 1.0],
    [180_000, 100.5, 102.0, 100.0, 101.0, 2.0],
], 3)
ok("свечи на границе бакета разделяются",
   len(agg2) == 2 and agg2[1][0] == 180_000, agg2)
ok("старший ТФ 15м строится из минутных", len(bt.aggregate_minutes(
    [[i * 60_000, 100.0, 101.0, 99.0, 100.5, 1.0] for i in range(45)], 15)) == 3)

# ── 5. fetch_klines: пагинация, дедупликация, кэш ────────────────────────────

print("\nfetch_klines")
_all = [[i * 60_000, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(2500)]
_calls: list[str] = []


def fake_kline_http(url: str) -> dict:
    _calls.append(url)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    start, end, limit = int(q["start"][0]), int(q["end"][0]), int(q["limit"][0])
    return {"list": [x for x in _all if start <= x[0] <= end][-limit:]}


_orig_http = bt._http_get
bt._http_get = fake_kline_http
rows = bt.fetch_klines("BTCUSDT", 0, 2500 * 60_000 - 1, interval="1", pause_sec=0)
ok("2500 свечей подтягиваются пагинацией (> 1 запрос)",
   len(rows) == 2500 and len(_calls) > 1, (len(rows), len(_calls)))
ok("результат отсортирован от старых к новым без дубликатов",
   all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1)))
ok("запросы пагинации уходят назад от конца окна",
   _calls[0].split("end=")[1].split("&")[0] == str(2500 * 60_000 - 1))
_cache = tempfile.mkdtemp(prefix="bt-test-cache-")
_calls.clear()
bt.fetch_klines("BTCUSDT", 0, 2500 * 60_000 - 1, pause_sec=0, cache_dir=_cache)
first_calls = len(_calls)
_calls.clear()
rows2 = bt.fetch_klines("BTCUSDT", 0, 2500 * 60_000 - 1, pause_sec=0, cache_dir=_cache)
ok("первый запуск пишет кэш (сетевых запросов > 0)", first_calls > 0)
ok("второй запуск читается из кэша без сети", len(_calls) == 0 and len(rows2) == 2500,
   len(_calls))
bt._http_get = _orig_http

# ── 6. fetch_instrument ───────────────────────────────────────────────────────

print("\nfetch_instrument")
def fake_inst_http(url: str) -> dict:
    if "NOEXIST" in url:
        return {"list": []}
    return {"list": [{"symbol": "BTCUSDT", "contractType": "LinearPerpetual",
                      "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                      "priceFilter": {"tickSize": "0.01"}}]}


bt._http_get = fake_inst_http
ok("ограничения инструмента разбираются",
   bt.fetch_instrument("BTCUSDT") == {"symbol": "BTCUSDT", "qty_step": 0.001,
                                      "min_qty": 0.001, "tick_size": 0.01})
ok("нет инструмента → безопасные значения по умолчанию",
   bt.fetch_instrument("NOEXIST")["tick_size"] == 0.01)
bt._http_get = _orig_http

# ── 7. fetch_universe ─────────────────────────────────────────────────────────

print("\nfetch_universe")


def mk(sym: str, lev: int = 10, status: str = "Trading") -> dict:
    return {"symbol": sym, "contractType": "LinearPerpetual", "quoteCoin": "USDT",
            "status": status, "leverageFilter": {"maxLeverage": str(lev)}}


_univ_main = [mk("BTCUSDT", 100), mk("ETHUSDT", 100), mk("XUSDT", 100),
              mk("YUSDT", 2), mk("ZUSDT", 100), mk("WUSDT", 100, "Suspended")]
_univ_test = [mk("BTCUSDT", 100), mk("ETHUSDT", 100), mk("XUSDT", 100), mk("YUSDT", 2)]
_univ_turn = {"BTCUSDT": "9000", "ETHUSDT": "8000", "XUSDT": "5000",
              "YUSDT": "3000", "ZUSDT": "7000", "WUSDT": "6000"}


def fake_univ_http(url: str) -> dict:
    if "tickers" in url:
        return {"list": [{"symbol": s, "turnover24h": v} for s, v in _univ_turn.items()]}
    if "api-testnet" in url:
        return {"list": _univ_test}
    return {"list": _univ_main}


bt._http_get = fake_univ_http
ok("топ-N по turnover24h", bt.fetch_universe(2, 3.0) == ["BTCUSDT", "ETHUSDT"])
u5 = bt.fetch_universe(5, 3.0)
ok("отсекаются Suspended, отсутствующие на Testnet и с малым плечом",
   u5 == ["BTCUSDT", "ETHUSDT", "XUSDT"], u5)


def fake_empty(url: str) -> dict:
    return {"list": []}


bt._http_get = fake_empty
empty_ok = False
try:
    bt.fetch_universe(5, 3.0)
except RuntimeError:
    empty_ok = True
except Exception:
    pass
ok("пустая вселенная → RuntimeError", empty_ok)
bt._http_get = _orig_http

# ── 8. max_drawdown ───────────────────────────────────────────────────────────

print("\nmax_drawdown")
ok("пустая кривая → (0, 0)", bt.max_drawdown([]) == (0.0, 0.0))
ok("монотонный рост → просадки нет", bt.max_drawdown([1.0, 2.0, 3.0]) == (0.0, 0.0))
ok("пик → падение → восстановление: абс. и % от пика",
   bt.max_drawdown([5.0, -3.0, 2.0]) == (3.0, 60.0), bt.max_drawdown([5.0, -3.0, 2.0]))

# ── 9. Полный run(): сигнал → вход → TP на синтетике ─────────────────────────

print("\nrun() на синтетическом росте")
cfg = sc.Config()
params = bt.DcaParams(entry_usdt=50, max_concurrent=1)
b = bt.Backtest(cfg, params, "BTCUSDT", INST)
res = b.run(bars(2000, drift=0.15, rng=1.2))
b.remove_closed()
res.open = b.open
ok("есть сигналы и закрытые циклы",
   res.signals >= 1 and len(res.closed) >= 1, (res.signals, len(res.closed)))
ok("после remove_closed в open нет закрытых циклов",
   all(not c.closed for c in res.open), [c.closed for c in res.open])
ok("закрытие — только по тейку",
   all(c.exit_reason == "take_profit" for c in res.closed),
   [c.exit_reason for c in res.closed])
ok("в каждом закрытом цикле вход и закрытие",
   all([f["role"] for f in c.fills] == ["entry", "close"] for c in res.closed))
ok("комиссии учитываются на каждом филле",
   all(c.fee > 0 for c in res.closed))
ok("в лонге на росте PnL положительный",
   all(c.pnl > 0 for c in res.closed), [round(c.pnl, 4) for c in res.closed])
m9 = bt.summarize(res)
ok("сводка сходится: n_cycles = closed + open",
   m9["n_cycles"] == m9["closed"] + m9["open_at_end"],
   (m9["n_cycles"], m9["closed"], m9["open_at_end"]))
ok("гросс = нетто + комиссии",
   abs(m9["gross_pnl"] - (m9["total_pnl"] + m9["total_fees"])) < 1e-9)

# ── 10. Полный DCA-цикл лонга: entry → докупка → TP ──────────────────────────

print("\nполный DCA-цикл (лонг)")
p = bt.DcaParams(entry_usdt=50, dca_step_pct=0.8, max_docups=2, tp_pct=1.0,
                 slippage_pct=0.0005, fee_rate=0.00055)
b10 = bt.Backtest(sc.Config(), p, "BTCUSDT", INST)
T = 10 ** 12
b10._open_cycle("Buy", T, 100.0)
c = b10.open[0]
entry = c.fills[0]
ok("вход по цене открытия с проскальзыванием",
   entry["price"] == 100.0 * 1.0005, entry)
ok("размер входа приведён к шагу количества",
   entry["qty"] == bt.quantize_qty(50 / entry["price"], 0.001), entry)
tp_after_entry = c.tp_level
next_after_entry = c.next_level
b10._update_cycles([T + 60_000, 100.0, 100.05, 99.2, 99.3, 1.0], T + 60_000)
ok("пробитие уровня → одна докупка",
   c.docups == 1 and [f["role"] for f in c.fills] == ["entry", "dca"], c.docups)
ok("средняя пересчитана к новой цене (между филлами)",
   min(entry["price"], c.fills[1]["price"]) < c.avg_entry < max(entry["price"], c.fills[1]["price"]),
   (c.avg_entry, entry["price"], c.fills[1]["price"]))
ok("TP пересчитан от новой средней (усреднение вниз → уровень ниже)",
   c.tp_level == bt.tp_price(c.avg_entry, "Buy", p.tp_pct, INST["tick_size"])
   and c.tp_level < tp_after_entry, (tp_after_entry, c.tp_level))
b10._update_cycles([T + 120_000, 99.5, 102.0, 99.4, 101.5, 1.0], T + 120_000)
ok("пробой TP → закрытие по тейку",
   c.closed and c.exit_reason == "take_profit", c.exit_reason)
ok("выход на TP с проскальзыванием",
   abs(c.exit_price - c.tp_level * (1 - p.slippage_pct)) < 1e-9, c.exit_price)
ok("филлы цикла: вход, докупка, закрытие",
   [f["role"] for f in c.fills] == ["entry", "dca", "close"])
ok("нетто PnL меньше гросс на комиссии",
   c.pnl + c.fee > c.pnl)

# ── 11. Шорт-симметрия ───────────────────────────────────────────────────────

print("\nшорт-симметрия")
b11 = bt.Backtest(sc.Config(), p, "BTCUSDT", INST)
T2 = 2 * 10 ** 12
b11._open_cycle("Sell", T2, 100.0)
s = b11.open[0]
ok("вход шорта по цене ниже открытия (проскальзывание вниз)",
   s.fills[0]["price"] == 100.0 * 0.9995, s.fills[0])
ok("уровень докупки шорта — выше последнего филла",
   s.next_level > s.fills[0]["price"], (s.next_level, s.fills[0]["price"]))
b11._update_cycles([T2 + 60_000, 100.8, 100.8, 100.6, 100.7, 1.0], T2 + 60_000)
ok("рост цены → докупка шорта", s.docups == 1, s.docups)
b11._update_cycles([T2 + 120_000, 100.0, 100.1, 98.5, 99.0, 1.0], T2 + 120_000)
ok("падение ниже TP → закрытие шорта по тейку",
   s.closed and s.exit_reason == "take_profit"
   and [f["role"] for f in s.fills] == ["entry", "dca", "close"],
   (s.exit_reason, [f["role"] for f in s.fills]))

# ── 12. Выход по времени ─────────────────────────────────────────────────────

print("\nвыход по времени")
b12 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, max_hold_minutes=1),
                  "BTCUSDT", INST)
T3 = 3 * 10 ** 12
b12._open_cycle("Buy", T3, 100.0)
t = b12.open[0]
b12._update_cycles([T3, 100.0, 100.05, 99.95, 100.0, 1.0], T3)
ok("до истечения времени цикл не закрывается",
   not t.closed and t.exit_reason == "")
b12._update_cycles([T3 + 60_000, 100.0, 100.05, 99.95, 100.0, 1.0], T3 + 60_000)
ok("после max_hold_minutes → time_exit по цене закрытия",
   t.closed and t.exit_reason == "time_exit"
   and [f["role"] for f in t.fills] == ["entry", "close"],
   (t.exit_reason, [f["role"] for f in t.fills]))

# ── 13. Стоп ─────────────────────────────────────────────────────────────────

print("\nстоп")
b13 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50, stop_pct=2.0,
                                            max_docups=0), "BTCUSDT", INST)
T4 = 4 * 10 ** 12
b13._open_cycle("Buy", T4, 100.0)
st = b13.open[0]
b13._update_cycles([T4 + 60_000, 100.0, 100.05, 97.0, 98.0, 1.0], T4 + 60_000)
ok("пробой стопа → закрытие по стопу", st.exit_reason == "stop", st.exit_reason)
ok("стоп — убыток с учётом комиссий",
   st.pnl < 0 and [f["role"] for f in st.fills] == ["entry", "close"])

# ── 14. Подавление повтора и лимит циклов (FR-015) ───────────────────────────

print("\nподавление повтора и лимит циклов")
b14 = bt.Backtest(sc.Config(cooldown_sec=0), bt.DcaParams(max_concurrent=3),
                  "BTCUSDT", INST)
b14._on_decision(sc.Decision(True, color="green"), 1_000, None)
b14._on_decision(sc.Decision(True, color="green"), 2_000, None)
ok("повтор того же цвета сигнал не даёт",
   b14.signals == 1 and b14._pending_side == "Buy", b14.signals)

b15 = bt.Backtest(sc.Config(cooldown_sec=0), bt.DcaParams(max_concurrent=1),
                  "BTCUSDT", INST)
b15._on_decision(sc.Decision(True, color="green"), 1_000, None)
b15._open_cycle("Buy", 1_000, 100.0)
b15._pending_side = None  # run() тратит pending на свече открытия цикла
b15._on_decision(sc.Decision(True, color="red"), 2_000, None)
ok("при занятом слоте сигнал отклонён по лимиту (FR-015)",
   b15.signals == 2 and b15.rejected_limit == 1 and b15._pending_side is None,
   (b15.signals, b15.rejected_limit, b15._pending_side))
b15.open.pop()
b15._on_decision(sc.Decision(True, color="green"), 3_000, None)
ok("после освобождения слота сигнал принимается",
   b15.signals == 3 and b15._pending_side == "Buy",
   (b15.signals, b15._pending_side))

b16 = bt.Backtest(sc.Config(cooldown_sec=300), bt.DcaParams(max_concurrent=3),
                  "BTCUSDT", INST)
b16._on_decision(sc.Decision(True, color="green"), 300_001, None)
b16._on_decision(sc.Decision(True, color="red"), 300_002, None)
ok("кулдаун между сигналами соблюдается",
   b16.signals == 1, b16.signals)

# ── 15. summarize ─────────────────────────────────────────────────────────────

print("\nsummarize")
b17 = bt.Backtest(sc.Config(), bt.DcaParams(entry_usdt=50), "BTCUSDT", INST)
b17._open_cycle("Buy", 5 * 10 ** 12, 100.0)
w1 = b17.open[-1]
b17._close(w1, 105.0, "take_profit", 5 * 10 ** 12 + 60_000)
b17._open_cycle("Sell", 6 * 10 ** 12, 100.0)
w2 = b17.open[-1]
b17._close(w2, 95.0, "take_profit", 6 * 10 ** 12 + 120_000)
b17._open_cycle("Buy", 7 * 10 ** 12, 100.0)
l1 = b17.open[-1]
b17._close(l1, 90.0, "time_exit", 7 * 10 ** 12 + 3 * 60_000)
b17.remove_closed()
res17 = bt.RunResult("BTCUSDT", INST, 5 * 10 ** 12, 7 * 10 ** 12 + 180_000, 2000,
                     3, 0, [], b17.closed)
m17 = bt.summarize(res17)
ok("счётчики закрытых/выигранных/проигранных",
   m17["closed"] == 3 and m17["wins"] == 2 and m17["losses"] == 1, m17)
ok("win-rate 2 из 3", abs(m17["win_rate"] - 200 / 3) < 1e-9, m17["win_rate"])
ok("причины выхода: 2 тейка и 1 по времени",
   m17["exit_reasons"] == {"take_profit": 2, "time_exit": 1}, m17["exit_reasons"])
ok("гросс = нетто + комиссии",
   abs(m17["gross_pnl"] - (m17["total_pnl"] + m17["total_fees"])) < 1e-9)
ok("комиссии входят и в PnL (нетто < гросс)",
   m17["total_fees"] > 0)
ok("среднее число филлов = 2 (вход + закрытие)",
   abs(m17["avg_fill_count"] - 2.0) < 1e-9, m17["avg_fill_count"])
ok("средняя длительность = (1 + 2 + 3) / 3 мин",
   abs(m17["avg_duration_min"] - 2.0) < 1e-9, m17["avg_duration_min"])

# ── 16. render ───────────────────────────────────────────────────────────────

print("\nrender")
m17["_cycles"] = res17.closed
text = bt.render(m17, bt.DcaParams(entry_usdt=50))
ok("в отчёте есть символ и раздел с сигналами",
   "Backtest: BTCUSDT" in text and "Сигналы:" in text)
ok("в отчёте есть итоговый PnL и причины выхода",
   "Итоговый PnL" in text and "take_profit" in text and "time_exit" in text)

# ── 17. CLI --csv / --json (без сети) ────────────────────────────────────────

print("\nCLI --csv / --json")
now = int(time.time() * 1000)
ts0 = (now // 60_000 - 2200) * 60_000
csv_path = os.path.join(_tmp, "uptrend.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ts", "open", "high", "low", "close", "volume"])
    w.writerows(bars(2000, drift=0.15, rng=1.2, ts0=ts0))

bt.fetch_instrument = lambda symbol: dict(INST)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = bt.main(["--csv", csv_path, "--max-hold-minutes", "30",
                  "--max-concurrent", "1"])
ok("CLI --csv завершается кодом 0", rc == 0, rc)
out = buf.getvalue()
ok("в текстовом отчёте есть сигнал и закрытый цикл",
   "Сигналы: 1" in out and "закрыто 1 из 1" in out, out)
ok("в сводке нет «открыто к концу: 1» из-за закрытого цикла",
   "открыто к концу: 0" in out, out)

buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    rc2 = bt.main(["--csv", csv_path, "--max-hold-minutes", "30",
                   "--max-concurrent", "1", "--json"])
ok("CLI --json завершается кодом 0", rc2 == 0, rc2)
data = json.loads(buf2.getvalue())
ok("JSON содержит метрики сводки",
   {"symbol", "candles", "signals", "closed", "win_rate", "total_pnl",
    "exit_reasons"} <= set(data), sorted(data))
ok("JSON: данные из CSV без сети, candles = 2000",
   data["candles"] == 2000 and data["closed"] >= 1, data)
shutil.rmtree(_tmp, ignore_errors=True)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
