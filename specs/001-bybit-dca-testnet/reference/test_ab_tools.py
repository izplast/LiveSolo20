"""
Автономные проверки A/B-инструментов (reference/ab_common.py, ab_sl.py,
ab_grid.py).

Без сети: метрики и сводка (aggregate), типизация значений (coerce),
разбор --params/--grid (parse_params/parse_grid) и применение переопределений
(apply_overrides), подписи (label), ранжирование (rank_results/take_top),
таблица (render_table), выбор комбинаций (resolve_combos), окно периода
(resolve_window), CLI-парсеры обоих скриптов (build_parser): --params/--grid/
--metric/--top-k/--out и совместимость флагов.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_ab_tools.py
"""

from __future__ import annotations

import importlib.util
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


common = _load("ab_common")
ab_sl = _load("ab_sl")
ab_grid = _load("ab_grid")
bt = common.bt
sc = common.sc

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
    except SystemExit:
        return True


# ── 1. Импорт и API ──────────────────────────────────────────────────────

print("импорт и API")
for fn in ("coerce", "parse_params", "parse_grid", "apply_overrides", "label",
           "aggregate", "render_table", "rank_results", "take_top",
           "add_common_args", "resolve_combos", "resolve_window", "load_data",
           "run_combos", "print_summary", "dump_out", "load_screener_cfg",
           "build_params"):
    ok(f"ab_common.{fn} доступен", hasattr(common, fn))
ok("ab_sl.CONFIGS — 4 гипотезы по умолчанию", len(ab_sl.CONFIGS) == 4)
ok("ab_sl.build_parser доступен", hasattr(ab_sl, "build_parser"))
ok("ab_grid.build_parser доступен", hasattr(ab_grid, "build_parser"))

# ── 2. Метрики и сводка ───────────────────────────────────────────────────

print("\naggregate (сводка по закрытым циклам)")
# Синтетические циклы: 4 закрытых (2 TP, 1 stop, 1 hard_loss_limit).
class _C:
    def __init__(self, pnl, reason, exit_ts):
        self.pnl = pnl
        self.exit_reason = reason
        self.exit_ts = exit_ts


closed = [
    _C(0.5, "take_profit", 3000),
    _C(0.7, "take_profit", 1000),
    _C(-0.4, "stop", 2000),
    _C(-0.2, "hard_loss_limit", 4000),
]
m = common.aggregate(closed)
ok("сводка строится", m is not None)
ok("WR = 50% (2 из 4)", m and abs(m["wr"] - 50.0) < 1e-9, m)
ok("сделок 4", m and m["total"] == 4)
ok("SL: stop + hard_loss_limit = 2", m and m["n_sl"] == 2, m)
ok("ср. TP = +0.6", m and abs(m["avg_tp"] - 0.6) < 1e-9, m)
ok("ср. SL = -0.3", m and abs(m["avg_sl"] - (-0.3)) < 1e-9, m)
ok("суммарный PnL = +0.6", m and abs(m["total_pnl"] - 0.6) < 1e-9, m)
ok("пустой список → None", common.aggregate([]) is None)
ok("METRICS содержит total_pnl/wr/mdd/avg_sl",
   {"total_pnl", "wr", "mdd", "avg_sl"} <= set(common.METRICS))
ok("avg_sl — лучше-меньше", common.METRICS["avg_sl"][1] is True)

# ── 3. coerce ─────────────────────────────────────────────────────────────

print("\ncoerce (типизация значений)")
ok("bool из 'true'", common.coerce("true", True) is True)
ok("bool из '0'", common.coerce("0", True) is False)
ok("кортеж из '1.2:1.5:2.0'",
   common.coerce("1.2:1.5:2.0", (1.0,)) == (1.2, 1.5, 2.0))
ok("список из 'AUSDT,BUSDT'",
   common.coerce("AUSDT,BUSDT", []) == ["AUSDT", "BUSDT"])
ok("int из '3'", common.coerce("3", 0) == 3)
ok("float из '1.5'", common.coerce("1.5", 0.0) == 1.5)
ok("незнакомый тип — строка", common.coerce("abc", object()) == "abc")

# ── 4. parse_params / parse_grid ──────────────────────────────────────────

print("\nparse_params / parse_grid")
base_params = bt.DcaParams()
base_cfg = sc.Config()
pp = common.parse_params(["bot.sl_atr_mult=1.8",
                          "bot.max_docups=1;bot.sl_atr_mult=2.0"], base_params, base_cfg)
ok("два --params → две комбинации", len(pp) == 2)
ok("одно поле в первой", pp[0] == {"bot.sl_atr_mult": "1.8"}, pp)
ok("два поля через ';' во второй",
   pp[1] == {"bot.max_docups": "1", "bot.sl_atr_mult": "2.0"}, pp)
ok("--params без '=' отвергается",
   raises(lambda: common.parse_params(["sl_atr_mult"], base_params, base_cfg)))
ok("--params без области отвергается",
   raises(lambda: common.parse_params(["sl_atr_mult=1.5"], base_params, base_cfg)))
ok("--params с неизвестным полем bot отвергается",
   raises(lambda: common.parse_params(["bot.unknown=1"], base_params, base_cfg)))
ok("--params с неизвестной областью отвергается",
   raises(lambda: common.parse_params(["bogus.tp_pct=1.0"], base_params, base_cfg)))
ok("--params screener.natr_min проходит",
   common.parse_params(["screener.natr_min=1.0"], base_params, base_cfg)
   == [{"screener.natr_min": "1.0"}])

pg = common.parse_grid(["bot.tp_pct=1.2,1.5", "screener.natr_min=0.9,1.2"],
                       base_params, base_cfg)
ok("грид: 2×2 = 4 комбинации", len(pg) == 4, pg)
ok("первая комбинация грида",
   pg[0] == {"bot.tp_pct": "1.2", "screener.natr_min": "0.9"}, pg)
ok("грид без '=' отвергается",
   raises(lambda: common.parse_grid(["tp_pct"], base_params, base_cfg)))
ok("грид с неизвестным полем screener отвергается",
   raises(lambda: common.parse_grid(["screener.unknown=1"], base_params, base_cfg)))
ok("пустой грид → одна пустая комбинация",
   common.parse_grid([], base_params, base_cfg) == [{}])

# ── 5. apply_overrides ────────────────────────────────────────────────────

print("\napply_overrides")
p = bt.DcaParams()
c = sc.Config()
common.apply_overrides(p, c, {"bot.sl_atr_mult": "1.5",
                              "screener.natr_min": "1.2"})
ok("bot.sl_atr_mult применён", abs(p.sl_atr_mult - 1.5) < 1e-9, p.sl_atr_mult)
ok("screener.natr_min применён", abs(c.natr_min - 1.2) < 1e-9, c.natr_min)
ok("незатронутые поля не тронуты", p.tp_pct == base_params.tp_pct)
p2 = bt.DcaParams()
common.apply_overrides(p2, sc.Config(), {"bot.tp_escalation": "1.2:1.5:2.0"})
ok("кортеж применён", p2.tp_escalation == (1.2, 1.5, 2.0), p2.tp_escalation)

# ── 6. label ──────────────────────────────────────────────────────────────

print("\nlabel")
ok("подпись комбинации",
   common.label({"bot.tp_pct": "1.5", "screener.natr_min": "1.2"})
   == "tp_pct=1.5, natr_min=1.2")
ok("пустая подпись пустой комбинации", common.label({}) == "")

# ── 7. rank_results / take_top ────────────────────────────────────────────

print("\nrank_results / take_top")
r1 = {"label": "A", "metrics": {"total_pnl": 1.0, "wr": 50.0}}
r2 = {"label": "B", "metrics": {"total_pnl": 3.0, "wr": 80.0}}
r3 = {"label": "C", "metrics": {"total_pnl": 2.0, "wr": 60.0}}
ranked = common.rank_results([r1, r2, r3], "total_pnl")
ok("по total_pnl лучший первый (B, C, A)",
   [r["label"] for r in ranked] == ["B", "C", "A"], ranked)
ranked_wr = common.rank_results([r1, r2, r3], "wr")
ok("по wr лучший первый (B, C, A)",
   [r["label"] for r in ranked_wr] == ["B", "C", "A"])
ok("top-k ограничивает вывод", [r["label"] for r in common.take_top(ranked, 2)]
   == ["B", "C"])
ok("top-k=0 — без ограничения", len(common.take_top(ranked, 0)) == 3)

# ── 8. render_table ───────────────────────────────────────────────────────

print("\nrender_table")
table = common.render_table([("Baseline", m), ("Test A", m)])
ok("шапка таблицы присутствует", "WinRate %" in table and "Total PnL $" in table)
ok("обе строки присутствуют", "Baseline" in table and "Test A" in table)
ok("числа отформатированы", "50.0" in table and "+0.60" in table, table)

# ── 9. resolve_combos ─────────────────────────────────────────────────────

print("\nresolve_combos")
combos = common.resolve_combos(["bot.sl_atr_mult=1.5"], [], False,
                               base_params, base_cfg, ab_sl.CONFIGS)
ok("--params + baseline → 2 строки", len(combos) == 2, combos)
ok("первая строка — Baseline",
   combos[0][0] == "Baseline (config.yml)", combos[0])
combos_nb = common.resolve_combos([], ["bot.tp_pct=1.2,1.5"], True,
                                  base_params, base_cfg, ab_sl.CONFIGS)
ok("--grid без baseline → только комбинации", len(combos_nb) == 2, combos_nb)
ok("подпись грида",
   combos_nb[0] == ("tp_pct=1.2", {"bot.tp_pct": "1.2"}), combos_nb[0])
combos_def = common.resolve_combos([], [], False, base_params, base_cfg,
                                   ab_sl.CONFIGS)
ok("без флагов — конфигурации по умолчанию", len(combos_def) == 4)
ok("--params и --grid несовместимы",
   raises(lambda: common.resolve_combos(["a=1"], ["b=2"], False,
                                        base_params, base_cfg, [])))

# ── 10. resolve_window ────────────────────────────────────────────────────

print("\nresolve_window")
start, end = common.resolve_window("2026-01-10", "2026-01-20", 7)
ok("явный период сохраняется",
   abs((end - start) / 86_400_000 - 10.0) < 1e-9, (start, end))
start2, end2 = common.resolve_window(None, None, 14)
ok("без дат: длина периода = дни", (end2 - start2) == 14 * 86_400_000)
ok("без end: конец — начало UTC-суток",
   end2 % 86_400_000 == 0, end2)
ok("без end: start = end - days",
   start2 == end2 - 14 * 86_400_000)

# ── 11. CLI обоих скриптов ────────────────────────────────────────────────

print("\nCLI ab_sl / ab_grid")
sl_ap = ab_sl.build_parser()
a = sl_ap.parse_args(["--days", "14", "--params", "bot.sl_atr_mult=1.8",
                      "--metric", "wr", "--top-k", "3", "--out", "/tmp/x.json"])
ok("ab_sl: --params разобран", a.params == ["bot.sl_atr_mult=1.8"])
ok("ab_sl: --metric разобран", a.metric == "wr")
ok("ab_sl: --top-k разобран", a.top_k == 3)
ok("ab_sl: --out разобран", a.out == "/tmp/x.json")
ok("ab_sl: неверная метрика отвергается",
   raises(lambda: sl_ap.parse_args(["--metric", "bogus"])))
ok("ab_sl: --sort — синоним --metric",
   sl_ap.parse_args(["--sort", "mdd"]).metric == "mdd")
ok("ab_sl: --json-out — синоним --out",
   sl_ap.parse_args(["--json-out", "/tmp/y.json"]).out == "/tmp/y.json")

g_ap = ab_grid.build_parser()
b = g_ap.parse_args(["--grid", "bot.tp_pct=1.2,1.5", "--metric", "avg_sl",
                     "--top-k", "5", "--out", "/tmp/g.json"])
ok("ab_grid: --grid разобран", b.grid == ["bot.tp_pct=1.2,1.5"])
ok("ab_grid: --metric разобран", b.metric == "avg_sl")
ok("ab_grid: --top-k разобран", b.top_k == 5)
ok("ab_grid: --out разобран", b.out == "/tmp/g.json")
ok("ab_grid: --no-baseline разобран",
   g_ap.parse_args(["--no-baseline"]).no_baseline is True)
ok("ab_grid: --sort — синоним --metric",
   g_ap.parse_args(["--sort", "mdd"]).metric == "mdd")
ok("ab_grid: --json-out — синоним --out",
   g_ap.parse_args(["--json-out", "/tmp/h.json"]).out == "/tmp/h.json")
ok("ab_grid: совместимость старого --sort и нового --metric",
   g_ap.parse_args(["--sort", "wr", "--metric", "mdd"]).metric == "mdd")


print(f"\nитог: {PASS} ok, {FAIL} fail")
if FAIL:
    raise SystemExit(1)
