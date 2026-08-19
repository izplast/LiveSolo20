"""
reference/scan_universe.py — накопление сделок: скан всего топа из конфига.

Скринер (NATR-14 + UHLO-15 на 1м/15м) прогоняется по реальным klines всех монет
топа по обороту из config/config.yml (top_n_turnover, 600) за --days дней; каждый
сигнал проходит полный DCA-цикл (backtest.Backtest) с параметрами бота из
config.yml (вход 20 USDT, шаг 1.2%, 2 докупки, TP 1.2%, стоп 5%, hold 90 мин).
Скан останавливается, как только накоплено --target закрытых сделок (30).

Загрузка klines по вселенной идёт параллельно (--workers) с кэшем, чтобы
повторные прогоны не качали данные заново. Сделки пишутся в --out (JSONL).

Пример:
    python3 reference/scan_universe.py --days 30 --target 30 \
        --cache-dir /tmp/scan-cache --out /tmp/trades.jsonl

Примечание: backtest.Backtest исполняет докупки фиксированным объёмом и TP
tp_pct (эскалация/Мартингейл — в dca_cycle.DcaCycle); скан собирает статистику
сигналов и закрытых циклов на реальной истории, а не точную реплику исполнения.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sys
import time


_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с scan_universe.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


run_sim = _load_sibling("run_sim")     # подтягивает backtest, screener, bot_config
bt = run_sim.bt
sc = run_sim.sc
bc = run_sim.bc
load_screener_cfg = run_sim.load_screener_cfg
DEFAULT_CONFIG = run_sim.DEFAULT_CONFIG
Backtest = bt.Backtest

DEFAULTS = {
    "days": 30,
    "target": 30,
    "workers": 8,
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULTS["days"],
                    help="глубина истории в днях")
    ap.add_argument("--target", type=int, default=DEFAULTS["target"],
                    help="сколько закрытых сделок накопить (стоп скана)")
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"],
                    help="параллельная загрузка klines по вселенной")
    ap.add_argument("--universe", type=int, default=None,
                    help="вместо топа из конфига (top_n_turnover, 600)")
    ap.add_argument("--config", default=None,
                    help="config.yml бота; по умолчанию config/config.yml корня репо")
    ap.add_argument("--cache-dir", default=None,
                    help="каталог для кэша загруженных klines")
    ap.add_argument("--out", default=None,
                    help="JSONL-файл со сделками (по умолчанию /tmp/trades.jsonl)")
    return ap


def _process_one(symbol: str, start_ms: int, end_ms: int, cache_dir: str | None,
                 cfg, params):
    """Загрузка klines символа + полный прогон Backtest → (сигналы, циклы)."""
    rows = bt.fetch_klines(symbol, start_ms, end_ms, "1", cache_dir=cache_dir)
    if len(rows) < 100:
        return symbol, None, "мало данных"
    b = Backtest(cfg, params, symbol, None)
    result = b.run(rows, slow_tf_minutes=15)
    b.remove_closed()
    result.closed.sort(key=lambda c: c.exit_ts)
    return symbol, result, None


def _cycle_line(cyc) -> dict:
    return {
        "symbol": cyc.symbol,
        "side": cyc.side,
        "open_ts": cyc.open_ts,
        "exit_ts": cyc.exit_ts,
        "duration_minutes": round(cyc.duration_minutes, 2),
        "docups": cyc.docups,
        "avg_entry": round(cyc.avg_entry, 6),
        "exit_price": round(cyc.exit_price, 6),
        "exit_reason": cyc.exit_reason,
        "qty": round(cyc.qty, 6),
        "fee": round(cyc.fee, 6),
        "pnl": round(cyc.pnl, 6),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_screener_cfg(args.config)
    dca = bc.read_dca_section(args.config or DEFAULT_CONFIG)
    params = bc.dca_params_from_config(dca)
    top_n = args.universe if args.universe is not None else cfg.top_n_turnover

    end_ms = (int(time.time()) // 86_400) * 86_400_000  # якорь: начало UTC-суток
    start_ms = end_ms - args.days * 86_400_000
    out_path = args.out or "/tmp/trades.jsonl"

    sys.stderr.write(f"[scan] вселенная: топ-{top_n} по обороту, {args.days} дней, "
                     f"target {args.target} сделок, NATR {cfg.natr_min}..{cfg.natr_max}%\n")
    symbols = bt.fetch_universe(top_n, cfg.required_leverage)
    sys.stderr.write(f"[scan] символов: {len(symbols)} "
                     f"(первые: {', '.join(symbols[:5])}…)\n")

    closed: list = []
    signals = errors = 0
    checked = 0
    start_wall = time.monotonic()

    def _worker(symbol: str):
        try:
            return _process_one(symbol, start_ms, end_ms, args.cache_dir, cfg, params)
        except Exception as e:  # noqa: BLE001 — одиночный символ не валит скан
            return symbol, None, f"{type(e).__name__}: {e}"

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    futures = {ex.submit(_worker, s): s for s in symbols}
    for fut in concurrent.futures.as_completed(futures):
        symbol = futures[fut]
        _, result, err = fut.result()
        checked += 1
        if err:
            errors += 1
            sys.stderr.write(f"\r[scan] {symbol}: {err}        \n")
            continue
        signals += result.signals
        closed.extend(result.closed)
        sys.stderr.write(f"\r[scan] проверено {checked}/{len(symbols)}, сигналов "
                         f"{signals}, закрыто сделок {len(closed)} "
                         f"({time.monotonic() - start_wall:.0f}с)    ")
        sys.stderr.flush()
        if len(closed) >= args.target:
            ex.shutdown(wait=False, cancel_futures=True)
            break
    sys.stderr.write("\n")

    wins = sum(1 for c in closed if c.pnl > 0)
    total_pnl = sum(c.pnl for c in closed)
    fees = sum(c.fee for c in closed)
    reasons: dict[str, int] = {}
    for c in closed:
        reasons[c.exit_reason] = reasons.get(c.exit_reason, 0) + 1

    with open(out_path, "w", encoding="utf-8") as f:
        for c in closed:
            f.write(json.dumps(_cycle_line(c), ensure_ascii=False) + "\n")

    print(f"=== Скан топ-{top_n}, {args.days} дней, "
          f"{time.monotonic() - start_wall:.0f}с, ошибок {errors} ===")
    print(f"Закрыто сделок: {len(closed)}, win-rate: "
          f"{wins / len(closed) * 100:.1f}%" if closed else "Закрытых сделок нет")
    print(f"Суммарный PnL: {total_pnl:+.2f} USDT (комиссии {fees:.2f}), "
          f"сигналов {signals}")
    print(f"По причинам выхода: {reasons}")
    for c in closed:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(c.exit_ts / 1000))
        print(f"  {ts} {c.symbol:12s} {c.side:4s} "
              f"docups={c.docups} {c.exit_reason:9s} PnL={c.pnl:+.2f}")
    print(f"Сделки → {out_path}")
    return 0 if len(closed) >= args.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
