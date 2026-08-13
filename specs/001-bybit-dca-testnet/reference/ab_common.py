"""
reference/ab_common.py — общий каркас A/B-инструментов (ab_sl.py, ab_grid.py).

Оба скрипта сравнивают конфигурации бота/скринера на ОДНОМ наборе минутных
свечей (одинаковые кандлы — честное сравнение). Чтобы CLI и вывод были
единообразны, общая логика собрана здесь:

  * метрики и причины SL: METRICS, SL_REASONS;
  * разбор комбинаций: parse_params (одна конкретная конфигурация, повторяемый
    --params) и parse_grid (декартово произведение значений, повторяемый
    --grid), типизация значений по целевому полю (coerce);
  * применение переопределений к свежим DcaParams/Config (apply_overrides),
    подпись комбинации (label);
  * сводка по закрытым циклам (aggregate), таблица (render_table),
    ранжирование по --metric и ограничение топа (rank_results/take_top);
  * общие флаги CLI (add_common_args), выбор комбинаций (resolve_combos),
    загрузка данных (load_data), окно периода (resolve_window).

Запуск A/B-прогонов: ссылки на модули reference-каталога выставляются в
атрибутах bt/sc/bc (backtest, screener, bot_config), чтобы скрипты-надстройки
использовали один загрузчик и один способ подмены сети на заглушки.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import itertools
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

UTC = timezone.utc

_DIR = os.path.dirname(os.path.abspath(__file__))

# Поля DcaParams, которые осмысленно перебирать (служебные исключены:
# fee_rate/slippage/max_concurrent — их трогать не стоит).
BOT_TUNABLE = {
    "entry_usdt", "dca_step_pct", "max_docups", "tp_pct", "max_hold_minutes",
    "stop_pct", "multiplier", "tp_escalation", "step_atr_mult",
    "step_min_pct", "step_max_pct", "sl_atr_mult", "sl_min_pct",
    "sl_max_pct", "max_cycle_loss_usdt",
}
# Поля Config, влияющие на бэктест-скринер (reference/screener.py).
SCREENER_TUNABLE = {
    "natr_period", "natr_min", "natr_max", "uhlo_length", "tf_slow",
    "cooldown_sec",
}

SL_REASONS = {"stop", "hard_loss_limit", "dynamic_sl"}

# метрика: (ключ в сводке, лучше-меньше)
METRICS = {
    "total_pnl": ("total_pnl", False),
    "wr": ("wr", False),
    "total": ("total", False),
    "avg_tp": ("avg_tp", False),
    "avg_sl": ("avg_sl", True),
    "mdd": ("mdd", True),
}


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с ab_common.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# screener при импорте создаёт logs/ — уводим его во временный каталог.
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="ab-common-import-"))
try:
    sc = _load_sibling("screener")
finally:
    os.chdir(_cwd)
bt = _load_sibling("backtest")
bc = _load_sibling("bot_config")


def load_screener_cfg(config_path: str) -> "sc.Config":
    """Конфиг скринера из config.yml (секция screener) или значения по умолчанию."""
    raw = bc.read_section(config_path, "screener")
    cfg = sc.Config()
    known = {f.name for f in sc.Config.__dataclass_fields__.values()}
    for k, v in raw.items():
        if k in known:
            setattr(cfg, k, v)
    sc.validate_config(cfg)
    return cfg


def build_params(config_path: str) -> bt.DcaParams:
    """Параметры сетки из config.yml (секция dca), без переопределений."""
    return bc.dca_params_from_config(bc.read_dca_section(config_path))


def coerce(value: str, current: object) -> object:
    """Строка из --params/--grid → тип целевого поля (по текущему значению)."""
    if isinstance(current, bool):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(current, tuple):  # tp_escalation: '1.2:1.5:2.0'
        return tuple(float(x) for x in value.split(":") if x.strip())
    if isinstance(current, list):   # blacklist: 'XUSDT,YUSDT'
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def _check_tunable(full: str, bot: bt.DcaParams, cfg: "sc.Config") -> tuple[str, object]:
    """Полное имя ('bot.поле' | 'screener.поле') → (область, целевое значение)."""
    if "." not in full:
        raise SystemExit(
            f"{full!r}: укажите область (bot. или screener.), "
            f"например bot.tp_pct=1.2,1.5,2.0")
    ns, name = full.split(".", 1)
    if ns == "bot":
        if name not in BOT_TUNABLE:
            raise SystemExit(
                f"bot.{name} не в списке перебираемых: {sorted(BOT_TUNABLE)}")
        return ns, getattr(bot, name)
    if ns == "screener":
        if name not in SCREENER_TUNABLE:
            raise SystemExit(
                f"screener.{name} не влияет на бэктест; доступно: "
                f"{sorted(SCREENER_TUNABLE)}")
        return ns, getattr(cfg, name)
    raise SystemExit(f"неизвестная область {ns!r}: bot. или screener.")


def parse_params(specs: list[str], bot: bt.DcaParams,
                 cfg: "sc.Config") -> list[dict]:
    """Спеки --params → список конкретных комбинаций: [{поле: значение}].

    Каждый --params — одна конфигурация; пары 'поле=значение' разделены ';'
    (запятая занята значениями списков/кортежей).
    """
    combos: list[dict] = []
    for spec in specs:
        combo: dict[str, str] = {}
        for part in spec.split(";"):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise SystemExit(
                    f"--params {spec!r}: ожидается имя=значение, получено {part!r}")
            full, value = part.split("=", 1)
            full = full.strip()
            _check_tunable(full, bot, cfg)
            combo[full] = value.strip()
        combos.append(combo)
    return combos


def parse_grid(specs: list[str], bot: bt.DcaParams,
               cfg: "sc.Config") -> list[dict]:
    """Спеки --grid → список комбинаций: [ {поле: значение}, ... ] (декартово)."""
    groups: list[list[tuple[str, str]]] = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"--grid: ожидается имя=значения, получено {spec!r}")
        full, vals = spec.split("=", 1)
        full = full.strip()
        _check_tunable(full, bot, cfg)
        groups.append([(full, v) for v in vals.split(",") if v.strip() != ""])
    if not groups:
        return [{}]
    return [dict(combo) for combo in itertools.product(*groups)]


def apply_overrides(bot: bt.DcaParams, cfg: "sc.Config",
                    combo: dict[str, str]) -> None:
    """Применяет переопределения combo (полное имя → строка) к свежим объектам."""
    for full, value in combo.items():
        ns, name = full.split(".", 1)
        target = bot if ns == "bot" else cfg
        setattr(target, name, coerce(value, getattr(target, name)))


def label(combo: dict[str, str]) -> str:
    """Короткая подпись комбинации, например 'tp_pct=1.5, natr_min=1.2'."""
    return ", ".join(f"{full.split('.', 1)[1]}={v}" for full, v in combo.items())


def aggregate(closed: list) -> dict | None:
    """Сводка по закрытым циклам: WR, число сделок/SL, средние TP/SL, MDD, PnL."""
    if not closed:
        return None
    sl = [c for c in closed if c.exit_reason in SL_REASONS]
    tp = [c for c in closed if c.exit_reason == "take_profit"]
    pnls = [c.pnl for c in closed]
    wins = sum(1 for x in pnls if x > 0)
    mdd_abs, _ = bt.max_drawdown(
        [c.pnl for c in sorted(closed, key=lambda c: c.exit_ts)])
    return {
        "wr": wins / len(pnls) * 100 if pnls else 0.0,
        "total": len(closed),
        "n_sl": len(sl),
        "avg_tp": sum(c.pnl for c in tp) / len(tp) if tp else 0.0,
        "avg_sl": sum(c.pnl for c in sl) / len(sl) if sl else 0.0,
        "mdd": mdd_abs,
        "total_pnl": sum(pnls),
    }


def render_table(rows: list[tuple[str, dict]]) -> str:
    hdr = ("Конфигурация", "WinRate %", "Сделок / SL", "Ср. TP $",
           "Ср. SL $", "Max DD $", "Total PnL $")
    widths = [len(h) for h in hdr]
    cells: list[list[str]] = []
    for label_name, m in rows:
        vals = [
            label_name,
            f"{m['wr']:.1f}",
            f"{m['total']} / {m['n_sl']}",
            f"{m['avg_tp']:+.2f}",
            f"{m['avg_sl']:+.2f}",
            f"{m['mdd']:.2f}",
            f"{m['total_pnl']:+.2f}",
        ]
        cells.append(vals)
        for i, v in enumerate(vals):
            widths[i] = max(widths[i], len(v))
    lines = ["  " + "  ".join(h.ljust(w) for h, w in zip(hdr, widths))]
    lines.append("  " + "  ".join("-" * w for w in widths))
    for vals in cells:
        lines.append("  " + "  ".join(
            v.ljust(w) if i in (0,) else v.rjust(w)
            for i, (v, w) in enumerate(zip(vals, widths))))
    return "\n".join(lines)


def rank_results(results: list[dict], metric: str) -> list[dict]:
    """Сортирует результаты по метрике (лучший первый); метрика из METRICS."""
    key, asc = METRICS[metric]
    return sorted(results, key=lambda r: (r["metrics"][key] is None,
                                          r["metrics"][key]),
                  reverse=not asc)


def take_top(results: list[dict], k: int) -> list[dict]:
    """Первые k строк рейтинга; 0/None — без ограничения."""
    if k and k > 0:
        return results[:k]
    return results


def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Флаги, общие для ab_sl.py и ab_grid.py."""
    ap.add_argument("--params", action="append", default=[],
                    metavar="bot.ПОЛЕ=з;bot.ПОЛЕ=з",
                    help="одна конкретная конфигурация; несколько --params — "
                         "несколько строк (с --grid несовместим)")
    ap.add_argument("--grid", action="append", default=[],
                    metavar="bot.ПОЛЕ=з1,з2 или screener.ПОЛЕ=з1,з2",
                    help="перебираемый параметр; несколько --grid — произведение")
    ap.add_argument("--metric", "--sort", dest="metric",
                    choices=sorted(METRICS), default="total_pnl",
                    help="метрика рейтинга (по умолчанию total_pnl)")
    ap.add_argument("--top-k", type=int, default=0,
                    help="показать только топ-N строк рейтинга (0 — все)")
    ap.add_argument("--out", "--json-out", dest="out", default=None,
                    help="файл JSON с полными результатами для анализа")


def resolve_combos(params_specs: list[str], grid_specs: list[str],
                   no_baseline: bool, base_params: bt.DcaParams,
                   base_cfg: "sc.Config",
                   defaults: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Комбинации (подпись, переопределения) по флагам --params/--grid.

    --params и --grid взаимоисключающие. Baseline (конфиг по умолчанию)
    добавляется первой строкой, если не --no-baseline. Без флагов —
    defaults (конфигурации скрипта по умолчанию).
    """
    if params_specs and grid_specs:
        raise SystemExit("--params и --grid одновременно не поддерживаются")
    if params_specs:
        combos = parse_params(params_specs, base_params, base_cfg)
    elif grid_specs:
        combos = parse_grid(grid_specs, base_params, base_cfg)
    else:
        return defaults
    if not no_baseline:
        combos = [{}] + combos
    return [(label(c) or "Baseline (config.yml)", c) for c in combos]


def resolve_window(start: str | None, end: str | None,
                   days: int) -> tuple[int, int]:
    """(start_ms, end_ms). Конец округляется до начала UTC-суток: кэш klines
    привязан к (start, end), и плавающий now ломал бы повторные прогоны."""
    def _ms(date: str | None, default_ms: int) -> int:
        if date is None:
            return default_ms
        return int(datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() * 1000)

    end_ms = _ms(end, int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000))
    return _ms(start, end_ms - days * 86_400_000), end_ms


def load_data(symbols: list[str], start_ms: int, end_ms: int, tf: str,
              cache_dir: str, tag: str) -> tuple[dict, dict]:
    """Загружает klines один раз на символ; возвращает (data, instruments)."""
    data: dict[str, list[list]] = {}
    instruments: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        def _load(symbol: str):
            rows = bt.fetch_klines(symbol, start_ms, end_ms, tf,
                                   cache_dir=cache_dir)
            return symbol, rows, bt.fetch_instrument(symbol)

        futures = [ex.submit(_load, s) for s in symbols]
        for fut in concurrent.futures.as_completed(futures):
            symbol, rows, inst = fut.result()
            if not rows:
                sys.stderr.write(f"[{tag}] {symbol}: нет данных в периоде — пропуск\n")
                continue
            data[symbol] = rows
            instruments[symbol] = inst
            sys.stderr.write(f"[{tag}] {symbol}: {len(rows)} свечей\n")
    return data, instruments


def run_combos(combos: list[tuple[str, dict]], data: dict, instruments: dict,
               config_path: str) -> list[dict]:
    """Прогон всех комбинаций по общим свечам → список результатов.

    Результат: {"label", "params", "metrics"} — metrics из aggregate по
    закрытым циклам всех символов.
    """
    results: list[dict] = []
    for combo_label, combo in combos:
        params = build_params(config_path)
        cfg = load_screener_cfg(config_path)
        cfg.reject_log = "none"
        apply_overrides(params, cfg, combo)
        params.validate()
        sc.validate_config(cfg)
        all_closed: list = []
        for symbol in data:
            b = bt.Backtest(cfg, params, symbol, instruments[symbol])
            b.run(data[symbol], slow_tf_minutes=int(cfg.tf_slow))
            b.remove_closed()
            all_closed.extend(b.closed)
        m = aggregate(all_closed)
        if m is None:
            sys.stderr.write(f"[ab] {combo_label or 'Baseline'}: сделок нет\n")
            continue
        results.append({"label": combo_label,
                        "params": {k: v for k, v in combo.items()},
                        "metrics": m})
        sys.stderr.write(f"[ab] {combo_label or 'Baseline'}: "
                         f"сделок {m['total']}, SL {m['n_sl']}, "
                         f"PnL {m['total_pnl']:+.2f}\n")
    return results


def print_summary(results: list[dict], metric: str, symbols: list[str],
                  period_days: float, cache_dir: str) -> None:
    """Таблица итоговых метрик по рейтингу + строка контекста прогона."""
    rows = [(r["label"], r["metrics"]) for r in results]
    print(render_table(rows))
    print()
    print(f"(свечи: {', '.join(sorted(symbols))}; период {period_days} д; "
          f"сортировка — {metric}; SL = stop + hard_loss_limit + dynamic_sl; "
          f"klines из {cache_dir})")


def dump_out(results: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"(результаты: {path})")
