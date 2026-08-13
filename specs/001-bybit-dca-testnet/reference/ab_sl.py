"""
reference/ab_sl.py — изолированное A/B-сравнение гипотез снижения убытка по SL.

Все конфигурации гоняются на ОДНОМ наборе минутных свечей (одинаковые
кандлы — честное сравнение). Данные: публичный Bybit /v5/market/kline с
локальным кэшем (--cache-dir) для повторных прогонов.

Конфигурации по умолчанию (шаги = число ступеней: вход + докупки):
  Baseline      steps=3 (20/40/80), sl_atr_mult=2.0
  Test A        steps=3,             sl_atr_mult=1.5   (поджатый SL)
  Test B        steps=2 (20/40),     sl_atr_mult=2.0   (короткая лестница)
  Test C        steps=3,             sl_atr_mult=2.0,
                max_cycle_loss_usdt=3.0                (жёсткий лимит в USDT)

Параметры сетки берутся из config/config.yml (секция dca), поверх применяются
A/B-переопределения; скринер — из секции screener того же конфига. Свои
конфигурации задаются --params (одна конфигурация) или --grid (декартово
произведение); Baseline (значения конфига) добавляется первой строкой, если
не --no-baseline. Метрики и ранжирование — как в ab_grid.py (--metric,
--top-k, --out).

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py \
        --symbols TUTUSDT,APRUSDT --days 14 --cache-dir data/cache
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py \
        --universe 60 --days 14     # топ-60 по обороту (как вселенная скринера)
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py --days 14 \
        --params "bot.sl_atr_mult=1.8" \
        --params "bot.max_docups=1;bot.sl_atr_mult=2.0"
    python3 specs/001-bybit-dca-testnet/reference/ab_sl.py --days 14 \
        --grid "bot.sl_atr_mult=1.0,1.5,2.0" --top-k 3 --metric wr --out /tmp/ab.json
"""

from __future__ import annotations

import argparse
import os
import sys

import ab_common

DEFAULT_CONFIG = os.path.abspath(
    os.path.join(os.path.dirname(ab_common.__file__), "..", "..", "..",
                 "config", "config.yml"))

# (подпись, переопределения) — гипотезы SL по умолчанию. steps → max_docups
# (max_docups = steps - 1), sl_atr_mult — адаптивный стоп, max_cycle_loss_usdt —
# жёсткий лимит убытка цикла.
CONFIGS = [
    ("Baseline: steps=3 (20/40/80), sl*2.0",
     {"bot.max_docups": "2", "bot.sl_atr_mult": "2.0"}),
    ("Test A:   steps=3,             sl*1.5",
     {"bot.max_docups": "2", "bot.sl_atr_mult": "1.5"}),
    ("Test B:   steps=2 (20/40),     sl*2.0",
     {"bot.max_docups": "1", "bot.sl_atr_mult": "2.0"}),
    ("Test C:   steps=3, sl*2.0, cap 3 USDT",
     {"bot.max_docups": "2", "bot.sl_atr_mult": "2.0",
      "bot.max_cycle_loss_usdt": "3.0"}),
]


def build_parser() -> argparse.ArgumentParser:
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
        os.path.dirname(os.path.dirname(ab_common.__file__)), "..", "..",
        "data", "cache"),
        help="каталог кэша klines (по умолчанию data/cache)")
    ap.add_argument("--tf", default="1", help="разрешение данных в минутах")
    ap.add_argument("--no-baseline", action="store_true",
                    help="не включать строку с дефолтами config.yml")
    ab_common.add_common_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    start_ms, end_ms = ab_common.resolve_window(args.start, args.end, args.days)
    base_cfg = ab_common.load_screener_cfg(args.config)
    base_cfg.reject_log = "none"
    base_params = ab_common.build_params(args.config)
    combos = ab_common.resolve_combos(args.params, args.grid, args.no_baseline,
                                      base_params, base_cfg, CONFIGS)

    if args.universe is not None:
        symbols = ab_common.bt.fetch_universe(
            args.universe, base_cfg.required_leverage, base_cfg.skip_top_volume,
            base_cfg.base_coin_blacklist)
        if base_cfg.blacklist:
            symbols = [s for s in symbols if s not in base_cfg.blacklist]
        sys.stderr.write(f"[ab_sl] топ-{args.universe} по обороту: "
                         f"{len(symbols)} символов\n")
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    sys.stderr.write(
        f"[ab_sl] вселенная: {symbols}, период "
        f"{ab_common.datetime.fromtimestamp(start_ms / 1000, tz=ab_common.UTC):%Y-%m-%d}"
        f" — {ab_common.datetime.fromtimestamp(end_ms / 1000, tz=ab_common.UTC):%Y-%m-%d}, "
        f"конфигураций: {len(combos)}\n")

    data, instruments = ab_common.load_data(
        symbols, start_ms, end_ms, args.tf, args.cache_dir, "ab_sl")
    if not data:
        sys.stderr.write("[ab_sl] данных нет ни по одному символу\n")
        return 1

    results = ab_common.run_combos(combos, data, instruments, args.config)
    if not results:
        sys.stderr.write("[ab_sl] ни одна конфигурация не дала закрытых сделок\n")
        return 1

    results = ab_common.rank_results(results, args.metric)
    ab_common.print_summary(ab_common.take_top(results, args.top_k),
                            args.metric, symbols,
                            round((end_ms - start_ms) / 86_400_000, 1),
                            args.cache_dir)
    if args.out:
        ab_common.dump_out(results, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
