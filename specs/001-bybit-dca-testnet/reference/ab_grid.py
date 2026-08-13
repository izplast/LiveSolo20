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
(убирается --no-baseline). Отдельные конфигурации задаются --params
(повторяемый; пары 'поле=значение' через ';'), с --grid несовместим.
Сравнение — как в ab_sl: WR, число сделок/SL, средние TP/SL, максимальная
просадка, суммарный PnL по всем символам. Ранжирование — по --metric,
таблица — только топ --top-k строк, полные результаты — в --out.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 30 \
        --universe 40 --grid "bot.tp_pct=1.2,1.5,2.0"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 14 \
        --symbols BTCUSDT,ETHUSDT \
        --grid "screener.natr_min=0.9,1.2,1.5" \
        --grid "bot.dca_step_pct=1.0,1.2,1.5"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py --days 90 \
        --universe 60 --metric wr --grid "bot.sl_atr_mult=1.0,1.5,2.0"
    python3 specs/001-bybit-dca-testnet/reference/ab_grid.py ... \
        --top-k 5 --out /tmp/ab_grid.json   # топ-5 и полные результаты
"""

from __future__ import annotations

import argparse
import os
import sys

import ab_common

DEFAULT_CONFIG = os.path.abspath(
    os.path.join(os.path.dirname(ab_common.__file__), "..", "..", "..",
                 "config", "config.yml"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="TUTUSDT,APRUSDT,SKYAI1USDT",
                    help="пары через запятую (по умолчанию — монеты live-прогона)")
    ap.add_argument("--universe", type=int, default=None,
                    help="вместо --symbols: топ-N по turnover24h как у скринера")
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
                                      base_params, base_cfg,
                                      [("Baseline (config.yml)", {})])

    if args.universe is not None:
        symbols = ab_common.bt.fetch_universe(
            args.universe, base_cfg.required_leverage, base_cfg.skip_top_volume,
            base_cfg.base_coin_blacklist)
        if base_cfg.blacklist:
            symbols = [s for s in symbols if s not in base_cfg.blacklist]
        sys.stderr.write(f"[ab_grid] топ-{args.universe} по обороту: "
                         f"{len(symbols)} символов\n")
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    sys.stderr.write(
        f"[ab_grid] вселенная: {symbols}, период "
        f"{ab_common.datetime.fromtimestamp(start_ms / 1000, tz=ab_common.UTC):%Y-%m-%d}"
        f" — {ab_common.datetime.fromtimestamp(end_ms / 1000, tz=ab_common.UTC):%Y-%m-%d}, "
        f"комбинаций: {len(combos)}\n")

    data, instruments = ab_common.load_data(
        symbols, start_ms, end_ms, args.tf, args.cache_dir, "ab_grid")
    if not data:
        sys.stderr.write("[ab_grid] данных нет ни по одному символу\n")
        return 1

    results = ab_common.run_combos(combos, data, instruments, args.config)
    if not results:
        sys.stderr.write("[ab_grid] ни одна комбинация не дала закрытых сделок\n")
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
