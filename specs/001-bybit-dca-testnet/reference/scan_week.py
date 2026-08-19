"""scan_week.py — сколько монет дали сигнал за N дней на текущей вселенной.

Точная реплика логики скринера (screener.py): вселенная строится build_universe
с фильтрами T033 (min_turnover_usdt, cg_max_rank, blacklists), история качается
постранично из mainnet REST (backtest.fetch_klines), решение принимается
evaluate() на закрытии каждой 1м-свечи. Считаем: сколько монет дали хотя бы
один сигнал, сколько сигналов всего, по каким причинам остальные отсеяны.

Пример:
    python3 reference/scan_week.py --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import importlib.util
import os
import sys
import time


_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с scan_week.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sc = _load_sibling("screener")
bt = _load_sibling("backtest")

DEFAULTS = {"days": 7, "workers": 8}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULTS["days"],
                    help="глубина истории в днях")
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"],
                    help="параллельная загрузка klines")
    ap.add_argument("--config", default=None,
                    help="config.yml бота; по умолчанию config/config.yml корня репо")
    return ap


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(_DIR))), "config", "config.yml")
    cfg = sc.load_config(config_path)
    print(f"вселенная: top-{cfg.top_n_turnover}, skip {cfg.skip_top_volume}, "
          f"min_turnover {cfg.min_turnover_usdt:g} USDT, cg_top {cfg.cg_max_rank}, "
          f"NATR {cfg.natr_min}..{cfg.natr_max}%, UHLO {cfg.uhlo_length}, "
          f"ТФ {cfg.tf_fast}/{cfg.tf_slow}")

    screener = sc.Screener(cfg)
    symbols = await screener.build_universe()
    print(f"символов во вселенной: {len(symbols)}")

    end_ms = (int(time.time()) // 86_400) * 86_400_000
    start_ms = end_ms - args.days * 86_400_000

    fast_cap = max(cfg.natr_period + 2, cfg.uhlo_length * 2 + 2)
    slow_cap = cfg.uhlo_length * 2 + 2

    sem = asyncio.Semaphore(args.workers)

    async def one(symbol: str):
        async with sem:
            fast, slow = await asyncio.gather(
                asyncio.to_thread(bt.fetch_klines, symbol, start_ms, end_ms, "1"),
                asyncio.to_thread(bt.fetch_klines, symbol, start_ms, end_ms, "15"),
            )
        return symbol, fast, slow

    results = await asyncio.gather(*(one(s) for s in symbols))

    signal_syms: dict[str, int] = {}
    rejects: collections.Counter = collections.Counter()
    insufficient = 0

    for symbol, fast, slow in results:
        if len(fast) < fast_cap + 5 or len(slow) < slow_cap + 2:
            insufficient += 1
            continue
        # slow-окно скользит вместе с fast: находим последнюю 15м-свечу,
        # закрытую до текущей 1м-свечи, и держим окно slow_cap.
        sig = 0
        slow_idx = 0
        for i in range(fast_cap, len(fast)):
            cur = fast[i][0]
            while slow_idx + 1 < len(slow) and slow[slow_idx + 1][0] <= cur:
                slow_idx += 1
            slow_win = slow[max(0, slow_idx - slow_cap + 1):slow_idx + 1]
            fast_win = fast[i - fast_cap + 1:i + 1]
            d = sc.evaluate(fast_win, slow_win, cfg)
            if d.passed:
                sig += 1
            elif d.reason:
                rejects[d.reason] += 1
        if sig:
            signal_syms[symbol] = sig

    print(f"\n=== Неделя ({args.days} д), вселенная {len(symbols)} монет ===")
    print(f"монет, давших хотя бы один сигнал: {len(signal_syms)} из {len(symbols)}")
    print(f"всего сигналов по ним: {sum(signal_syms.values())}")
    if insufficient:
        print(f"монет без достаточной истории: {insufficient}")
    print("режекты (по причинам):")
    for reason, n in rejects.most_common():
        print(f"  {reason}: {n}")
    print("\nмонеты-сигналы:")
    for sym, n in sorted(signal_syms.items(), key=lambda kv: -kv[1]):
        print(f"  {sym}: {n} сигналов")
    if not signal_syms:
        print("  (нет)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))