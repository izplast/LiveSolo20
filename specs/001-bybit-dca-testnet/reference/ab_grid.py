"""
reference/ab_grid.py — грид-свип параметров бота и скринера до тестнета.

Гоняет комбинации параметров (декартово произведение) на ОДНОМ наборе
минутных свечей — честное сравнение, как в ab_sl.py. Данные: публичный Bybit
/v5/market/kline с локальным кэшем (--cache-dir) для повторных прогонов.
Параметр задаётся с префиксом области:

  --grid "bot.<поле DcaParams>=v1,v2,..."      — параметры сетки (бота)
  --grid "screener.<поле Config>=v1,v2,..."    — параметры скринера

Несколько --grid дают произведение. Спец-форматы значений:
  bool          — true/false/1/0
  кортеж        — tp_escalation через ':' (1.2:1.5:2.0)
  список        — blacklist/base_coin_blacklist через ','
  остальное     — int/float по типу поля.

Базовые значения — config/config.yml (секции dca и screener); сверху —
переопределения грида. Строка Baseline — незаданные дефолты конфига
(убирается --no-baseline). Сравнение — как в ab_sl: WR, число сделок/SL,
средние TP/SL, максимальная просадка, суммарный PnL по всем символам.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 30 \
        --universe 40 --grid "bot.tp_pct=1.2,1.5,2.0"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 14 \
        --symbols BTCUSDT,ETHUSDT \
        --grid "screener.natr_min=0.9,1.2,1.5" \
        --grid "bot.dca_step_pct=1.0,1.2,1.5"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 90 \
        --universe 60 --sort wr --grid "bot.sl_atr_mult=1.0,1.5,2.0"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py ... \
        --json-out /tmp/ab_grid.json   # полные результаты для анализа
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

_DIR = os.path.dirname(os.path.abspath(__file__))

# Поля DcaParams, которые осмысленно перебирать гридом (исключены служебные:
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

SL_REASONS = {"stop", "hard_loss_limit"}

SORTS = {
    # метрика: (ключ в сводке, лучше-меньше)
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
    assert spec and spec.loader, f"{name}.py не найден рядом с ab_grid.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# screener при импорте создаёт logs/ — уводим его во временный каталог.
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="ab-grid-import-"))
try:
    sc = _load_sibling("screener")
finally:
    os.chdir(_cwd)
bt = _load_sibling("backtest")
bc = _load_sibling("bot_config")

DEFAULT_CONFIG = os.path.abspath(
    os.path.join(_DIR, "..", "..", "..", "config", "config.yml"))


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
    """Строка из грида → тип целевого поля (по текущему значению)."""
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


def parse_grid(specs: list[str], bot: bt.DcaParams, cfg: "sc.Config"):
    """Спеки --grid → список комбинаций: [ {поле: str}, ... ] (декартово)."""
    groups: list[list[tuple[str, str]]] = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"--grid: ожидается имя=значения, получено {spec!r}")
        full, vals = spec.split("=", 1)
        full = full.strip()
        if "." not in full:
            raise SystemExit(
                f"--grid {spec!r}: укажите область (bot. или screener.), "
                f"например bot.tp_pct=1.2,1.5,2.0")
        ns, name = full.split(".", 1)
        if ns == "bot":
            if name not in BOT_TUNABLE:
                raise SystemExit(
                    f"bot.{name} не в списке перебираемых: {sorted(BOT_TUNABLE)}")
            cur = getattr(bot, name)
        elif ns == "screener":
            if name not in SCREENER_TUNABLE:
                raise SystemExit(
                    f"screener.{name} не влияет на бэктест; доступно: "
                    f"{sorted(SCREENER_TUNABLE)}")
            cur = getattr(cfg, name)
        else:
            raise SystemExit(f"неизвестная область {ns!r}: bot. или screener.")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grid", action="append", default=[],
                    metavar="bot.ПОЛЕ=v1,v2 или screener.ПОЛЕ=v1,v2",
                    help="перебираемый параметр; несколько --grid — произведение")
    ap.add_argument("--symbols", default="TUTUSDT,APRUSDT,SKYAI1USDT",
                    help="пары через запятую (по умолчанию — монеты live-прогона)")
    ap.add_argument("--universe", type=int, default=None,
                    help="вместо --symbols: топ-N по turnover24h как у скринера")
    ap.add_argument("--days", type=int, default=7, help="глубина истории в днях")
    ap.add_argument("--start", default=None, help="начало, YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="конец, YYYY-MM-DD")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="путь к config.yml")
    ap.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(_DIR), "..", "..", "data", "cache"),
        help="каталог кэша klines (по умолчанию data/cache)")
    ap.add_argument("--tf", default="1", help="разрешение данных в минутах")
    ap.add_argument("--sort", choices=sorted(SORTS), default="total_pnl",
                    help="метрика рейтинга (по умолчанию total_pnl)")
    ap.add_argument("--no-baseline", action="store_true",
                    help="не включать строку с дефолтами config.yml")
    ap.add_argument("--json-out", default=None,
                    help="файл JSON с полными результатами для анализа")
    args = ap.parse_args(argv)

    from datetime import datetime, timezone

    def _ms(date: str | None, default_ms: int) -> int:
        if date is None:
            return default_ms
        return int(datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() * 1000)

    end_ms = _ms(args.end, int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000))
    start_ms = _ms(args.start, end_ms - args.days * 86_400_000)

    base_cfg = load_screener_cfg(args.config)
    base_cfg.reject_log = "none"  # журнал скринеру не нужен
    base_params = build_params(args.config)
    combos = parse_grid(args.grid, base_params, base_cfg)
    if not args.no_baseline:
        combos = [{}] + combos

    if args.universe is not None:
        symbols = bt.fetch_universe(args.universe, base_cfg.required_leverage,
                                    base_cfg.skip_top_volume,
                                    base_cfg.base_coin_blacklist)
        if base_cfg.blacklist:
            symbols = [s for s in symbols if s not in base_cfg.blacklist]
        sys.stderr.write(f"[ab_grid] топ-{args.universe} по обороту: "
                         f"{len(symbols)} символов\n")
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    sys.stderr.write(
        f"[ab_grid] вселенная: {symbols}, период "
        f"{datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc):%Y-%m-%d}"
        f" — {datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc):%Y-%m-%d}, "
        f"комбинаций: {len(combos)}\n")

    data: dict[str, list[list]] = {}
    instruments: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        def _load(symbol: str):
            rows = bt.fetch_klines(symbol, start_ms, end_ms, args.tf,
                                   cache_dir=args.cache_dir)
            return symbol, rows, bt.fetch_instrument(symbol)

        futures = [ex.submit(_load, s) for s in symbols]
        for fut in concurrent.futures.as_completed(futures):
            symbol, rows, inst = fut.result()
            if not rows:
                sys.stderr.write(f"[ab_grid] {symbol}: нет данных в периоде — пропуск\n")
                continue
            data[symbol] = rows
            instruments[symbol] = inst
            sys.stderr.write(f"[ab_grid] {symbol}: {len(rows)} свечей\n")
    if not data:
        sys.stderr.write("[ab_grid] данных нет ни по одному символу\n")
        return 1

    results: list[dict] = []
    for combo in combos:
        params = build_params(args.config)
        cfg = load_screener_cfg(args.config)
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
            sys.stderr.write(f"[ab_grid] {label(combo) or 'Baseline'}: сделок нет\n")
            continue
        results.append({"label": label(combo) or "Baseline (config.yml)",
                        "params": {k: v for k, v in combo.items()},
                        "metrics": m})
        sys.stderr.write(f"[ab_grid] {label(combo) or 'Baseline'}: "
                         f"сделок {m['total']}, SL {m['n_sl']}, "
                         f"PnL {m['total_pnl']:+.2f}\n")

    if not results:
        sys.stderr.write("[ab_grid] ни одна комбинация не дала закрытых сделок\n")
        return 1

    key, asc = SORTS[args.sort]
    results.sort(key=lambda r: (r["metrics"][key] is None, r["metrics"][key]),
                 reverse=not asc)

    rows = [(r["label"], r["metrics"]) for r in results]
    print(render_table(rows))
    print()
    period_days = round((end_ms - start_ms) / 86_400_000, 1)
    print(f"(свечи: {', '.join(sorted(data))}; период {period_days} д; "
          f"сортировка — {args.sort}; SL = stop + hard_loss_limit; "
          f"klines из {args.cache_dir})")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"(результаты: {args.json_out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
