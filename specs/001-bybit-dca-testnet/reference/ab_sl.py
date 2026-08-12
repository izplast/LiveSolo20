"""
reference/ab_sl.py — изолированное A/B-сравнение гипотез снижения убытка по SL.

Все конфигурации гоняются на ОДНОМ наборе минутных свечей (одинаковые
кандлы — честное сравнение). Данные: публичный Bybit /v5/market/kline с
локальным кэшем (--cache-dir) для повторных прогонов.

Сравниваемые конфигурации (steps = число ступеней: вход + докупки):
  Baseline      steps=3 (20/40/80), sl_atr_mult=2.0
  Test A        steps=3,             sl_atr_mult=1.5   (поджатый SL)
  Test B        steps=2 (20/40),     sl_atr_mult=2.0   (короткая лестница)
  Test C        steps=3,             sl_atr_mult=2.0,
                max_cycle_loss_usdt=3.0                (жёсткий лимит в USDT)

Параметры сетки берутся из config/config.yml (секция dca), поверх применяются
A/B-переопределения; скринер — из секции screener того же конфига.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py \
        --symbols TUTUSDT,APRUSDT --days 14 --cache-dir data/cache
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py \
        --universe 60 --days 14     # топ-60 по обороту (как вселенная скринера)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import os
import sys
import tempfile

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с ab_sl.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# screener при импорте создаёт logs/ — уводим его во временный каталог.
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="ab-sl-import-"))
try:
    sc = _load_sibling("screener")
finally:
    os.chdir(_cwd)
bt = _load_sibling("backtest")
bc = _load_sibling("bot_config")

DEFAULT_CONFIG = os.path.abspath(
    os.path.join(_DIR, "..", "..", "..", "config", "config.yml"))

# (название, steps, sl_atr_mult, max_cycle_loss_usdt)
CONFIGS = [
    ("Baseline: steps=3 (20/40/80), sl*2.0", 3, 2.0, 0.0),
    ("Test A:   steps=3,             sl*1.5", 3, 1.5, 0.0),
    ("Test B:   steps=2 (20/40),     sl*2.0", 2, 2.0, 0.0),
    ("Test C:   steps=3, sl*2.0, cap 3 USDT", 3, 2.0, 3.0),
]

SL_REASONS = {"stop", "hard_loss_limit"}


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


def build_params(config_path: str, steps: int, sl_atr_mult: float,
                 max_cycle_loss_usdt: float = 0.0) -> bt.DcaParams:
    """Параметры сетки из config.yml + A/B-переопределения.

    steps считаются ступенями (вход + докупки), поэтому в DcaParams
    max_docups = steps - 1: steps=3 → 20/40/80, steps=2 → 20/40.
    """
    dca = bc.read_dca_section(config_path)
    p = bc.dca_params_from_config(dca)
    p.max_docups = steps - 1
    p.sl_atr_mult = sl_atr_mult
    p.max_cycle_loss_usdt = max_cycle_loss_usdt
    p.validate()
    return p


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
    for label, m in rows:
        vals = [
            label,
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
    ap.add_argument("--symbols", default="TUTUSDT,APRUSDT,SKYAI1USDT",
                    help="пары через запятую (по умолчанию — монеты live-прогона, "
                         "без INXUSDT)")
    ap.add_argument("--universe", type=int, default=None,
                    help="вместо --symbols: топ-N по turnover24h как у скринера "
                         "(skip_top_volume, чёрные списки и плечо учитываются)")
    ap.add_argument("--days", type=int, default=7, help="глубина истории в днях")
    ap.add_argument("--start", default=None, help="начало, YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="конец, YYYY-MM-DD")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="путь к config.yml")
    ap.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(_DIR), "..", "..", "data", "cache"),
        help="каталог кэша klines (по умолчанию data/cache)")
    ap.add_argument("--tf", default="1", help="разрешение данных в минутах")
    args = ap.parse_args(argv)

    from datetime import datetime, timezone

    def _ms(date: str | None, default_ms: int) -> int:
        if date is None:
            return default_ms
        return int(datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() * 1000)

    # Конец окна округляется до начала UTC-суток: кэш klines привязан к
    # (start, end), и плавающий now ломал бы повторные прогоны. Даты зажаты
    # так, чтобы период был инвариантен между запусками.
    end_ms = _ms(args.end, int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000))
    start_ms = _ms(args.start, end_ms - args.days * 86_400_000)

    cfg = load_screener_cfg(args.config)
    cfg.reject_log = "none"  # журнал скринеру не нужен
    if args.universe is not None:
        symbols = bt.fetch_universe(args.universe, cfg.required_leverage,
                                    cfg.skip_top_volume, cfg.base_coin_blacklist)
        if cfg.blacklist:
            symbols = [s for s in symbols if s not in cfg.blacklist]
        sys.stderr.write(f"[ab_sl] топ-{args.universe} по обороту: "
                         f"{len(symbols)} символов\n")
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    sys.stderr.write(f"[ab_sl] вселенная: {symbols}, период "
                     f"{datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc):%Y-%m-%d}"
                     f" — {datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc):%Y-%m-%d}\n")

    # Данные грузим один раз на символ и гоняем все конфиги на них же.
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
                sys.stderr.write(f"[ab_sl] {symbol}: нет данных в периоде — пропуск\n")
                continue
            data[symbol] = rows
            instruments[symbol] = inst
            sys.stderr.write(f"[ab_sl] {symbol}: {len(rows)} свечей\n")
    if not data:
        sys.stderr.write("[ab_sl] данных нет ни по одному символу\n")
        return 1

    rows_out: list[tuple[str, dict]] = []
    for label, steps, sl_mult, cap in CONFIGS:
        params = build_params(args.config, steps, sl_mult, cap)
        all_closed: list = []
        for symbol in data:
            b = bt.Backtest(cfg, params, symbol, instruments[symbol])
            b.run(data[symbol], slow_tf_minutes=15)
            b.remove_closed()
            all_closed.extend(b.closed)
        m = aggregate(all_closed)
        if m is None:
            sys.stderr.write(f"[ab_sl] {label}: сделок нет\n")
            continue
        rows_out.append((label, m))
        sys.stderr.write(f"[ab_sl] {label}: сделок {m['total']}, "
                         f"SL {m['n_sl']}, PnL {m['total_pnl']:+.2f}\n")

    print(render_table(rows_out))
    print()
    print(f"(свечи: {', '.join(sorted(data))}; период {args.days} д; "
          f"SL = stop + hard_loss_limit; klines из {args.cache_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
