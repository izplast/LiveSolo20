"""
tools/stats_summary.py — сводка по закрытым DCA-циклам из журнала бота.

Читает `logs/bot-events.jsonl` (ключ `bot.journal_path` в config/config.yml)
и по событиям `cycle_opened` / `cycle_closed` считает итоги прогона:

- сколько циклов закрыто и по какой причине (TP / SL / time_exit / manual);
- совокупный PnL в USDT по закрытым циклам;
- win-rate (доля прибыльных циклов);
- средняя/медианная длительность удержания позиции;
- опционально разбивку по символам (--by-symbol);
- в --json --by-symbol дополнительно equity-ряды по символам
  (по_символам_эквити) — вход для reference/report_charts.py --kind equity-by-symbol.

Запуск (зависимостей нет, только стандартная библиотека):

    python3 tools/stats_summary.py                          # всё время
    python3 tools/stats_summary.py --days 7                 # последние 7 суток
    python3 tools/stats_summary.py --days 7 --by-symbol     # + разбивка по парам
    python3 tools/stats_summary.py logs/bot-events.jsonl --since 2026-08-01
    python3 tools/stats_summary.py --json                   # машиночитаемый вывод

Период фильтруется по моменту ЗАКРЫТИЯ цикла: цикл, закрытый вчера, попадает
в `--days 7`, открытый вчера и незакрытый — нет.

Формат событий (см. specs/001-bybit-dca-testnet/contracts/journal.md и
reference/cycle_journal.py):

    {"kind": "cycle_opened", "cycle_id": "C1", "symbol": "BTCUSDT",
     "open_ts": 1753960860123, "ts": 1753960860123}
    {"kind": "cycle_closed", "cycle_id": "C1", "symbol": "BTCUSDT",
     "exit_reason": "take_profit", "pnl": 0.64,
     "open_ts": 1753960860123, "close_ts": 1753961400000,
     "duration_ms": 539877, "ts": 1753961400000}

Старые записи без полей open_ts/close_ts/duration_ms тоже считаются:
моменты берутся из `ts` соответствующих событий, а циклы связываются по
`cycle_id`. Незнакомые kind и битые строки игнорируются.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_JOURNAL = Path("logs/bot-events.jsonl")


# ---------------------------------------------------------------------------
# Чтение журнала
# ---------------------------------------------------------------------------

def iter_records(path: Path) -> Iterable[dict]:
    """Построчно читает JSONL, отбрасывая пустые строки и битый JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "kind" in rec:
                    yield rec
    except FileNotFoundError:
        pass


def collect_cycles(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Сводит события cycle_opened/cycle_closed в список закрытых циклов.

    Каждый закрытый цикл — словарь с полями cycle_id, symbol, exit_reason,
    pnl (float | None), open_ts, close_ts, duration_ms (int | None).
    Двухпроходный: сначала собираются все open_ts, потом строятся закрытия,
    чтобы порядок строк в файле не влиял на результат.
    """
    open_ts: dict[str, int] = {}
    for path in paths:
        for rec in iter_records(path):
            cid = rec.get("cycle_id")
            if not isinstance(cid, str):
                continue
            if rec.get("kind") == "cycle_opened":
                ts = rec.get("open_ts", rec.get("ts"))
                if isinstance(ts, (int, float)):
                    open_ts[cid] = int(ts)

    cycles: list[dict[str, Any]] = []
    for path in paths:
        for rec in iter_records(path):
            if rec.get("kind") != "cycle_closed":
                continue
            cid = rec.get("cycle_id")
            if not isinstance(cid, str):
                continue
            close_ts = rec.get("close_ts", rec.get("ts"))
            close_ts = int(close_ts) if isinstance(close_ts, (int, float)) else None
            start = rec.get("open_ts", open_ts.get(cid))
            start = int(start) if isinstance(start, (int, float)) else None
            duration = rec.get("duration_ms")
            if not isinstance(duration, (int, float)) and start is not None and close_ts is not None:
                duration = max(0, close_ts - start)
            pnl = rec.get("pnl")
            cycles.append({
                "cycle_id": cid,
                "symbol": rec.get("symbol", "?"),
                "exit_reason": rec.get("exit_reason", "unknown"),
                "pnl": float(pnl) if isinstance(pnl, (int, float)) else None,
                "open_ts": start,
                "close_ts": close_ts,
                "duration_ms": int(duration) if duration is not None else None,
            })
    return cycles


# ---------------------------------------------------------------------------
# Агрегаты
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), math.ceil(q / 100 * len(ordered))))
    return ordered[rank - 1]


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def equity_by_symbol(cycles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Кумулятивный PnL по каждому символу для report_charts --kind equity-by-symbol.

    Возвращает [{"symbol": "BTCUSDT", "points": [[close_ts, equity], ...]}, ...],
    отсортированные по символу и внутри — по времени закрытия. Циклы без
    close_ts отбрасываются, pnl=None трактуется как 0 (как equity_points).
    """
    per: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for c in cycles:
        ts = c.get("close_ts")
        if ts is None:
            continue
        pnl = c.get("pnl")
        per[c.get("symbol", "?")].append((int(ts), float(pnl) if isinstance(pnl, (int, float)) else 0.0))

    out: list[dict[str, Any]] = []
    for sym in sorted(per):
        rows = sorted(per[sym], key=lambda t: t[0])
        points: list[list[Any]] = []
        run = 0.0
        for ts, pnl in rows:
            run += pnl
            points.append([ts, round(run, 6)])
        out.append({"symbol": sym, "points": points})
    return out


ANOMALY_PNL_THRESHOLD = -100.0
ANOMALY_SYMBOLS = {"OPGUSDT"}


def is_anomalous(
    cycle: dict[str, Any],
    *,
    pnl_threshold: float | None = ANOMALY_PNL_THRESHOLD,
    symbols: set[str] | None = None,
) -> bool:
    """Проверка на аномальный цикл.

    Аномалией считается:
    * pnl < pnl_threshold (по умолчанию -100 USDT) — как OPGUSDT -628;
    * symbol в списке аномальных (по умолчанию {"OPGUSDT"}).
    """
    sym = str(cycle.get("symbol") or "")
    if symbols is not None:
        if sym in symbols:
            return True
    else:
        if sym in ANOMALY_SYMBOLS:
            return True
    if pnl_threshold is not None:
        pnl = cycle.get("pnl")
        if isinstance(pnl, (int, float)) and float(pnl) < pnl_threshold:
            return True
    return False


def filter_anomalies(
    cycles: Sequence[dict[str, Any]],
    *,
    pnl_threshold: float | None,
    symbols: set[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Делит циклы на (чистые, аномальные) по критериям is_anomalous."""
    clean: list[dict[str, Any]] = []
    anomalous: list[dict[str, Any]] = []
    for c in cycles:
        if is_anomalous(c, pnl_threshold=pnl_threshold, symbols=symbols):
            anomalous.append(c)
        else:
            clean.append(c)
    return clean, anomalous


def summarize(cycles: Sequence[dict[str, Any]],
              by_symbol: bool = False) -> dict[str, Any]:
    """Считает сводку по закрытым циклам. pnl=None (неизвестен) из
    денежных и win-rate расчётов исключается, но в общем счётчике остаётся."""
    closed = len(cycles)
    reasons = Counter(c["exit_reason"] for c in cycles)

    pnls = [c["pnl"] for c in cycles if c["pnl"] is not None]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    durations = [c["duration_ms"] for c in cycles if c["duration_ms"] is not None]

    open_tss = [c["open_ts"] for c in cycles if c["open_ts"] is not None]
    close_tss = [c["close_ts"] for c in cycles if c["close_ts"] is not None]

    out: dict[str, Any] = {
        "период_торговли": {
            "начало": min(open_tss) if open_tss else None,
            "конец": max(close_tss) if close_tss else None,
        },
        "закрыто_циклов": closed,
        "по_причине": {r: reasons.get(r, 0) for r in
                       ("take_profit", "hard_sl", "trailing", "time_exit", "manual", "unknown")},
        "pnl_usdt": round(total_pnl, 4) if pnls else 0.0,
        "циклов_с_pnl": len(pnls),
        "win_rate_pct": round(wins / len(pnls) * 100, 1) if pnls else 0.0,
        "удержание_мс": {
            "среднее": round(sum(durations) / len(durations)) if durations else None,
            "медиана": round(median(durations)) if durations else None,
            "min": min(durations) if durations else None,
            "max": max(durations) if durations else None,
        },
    }

    if by_symbol:
        per: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in cycles:
            per[c["symbol"]].append(c)
        out["по_символам"] = {
            sym: summarize(group, by_symbol=False) for sym, group in sorted(per.items())
        }
        out["по_символам_эквити"] = equity_by_symbol(cycles)
    return out


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def fmt_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    seconds = int(ms / 1000)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def fmt_iso(ms: int | None) -> str:
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def render_trades(cycles: Sequence[dict[str, Any]]) -> str:
    """Построчный список сделок: начало, конец, длительность, причина выхода, PnL."""
    lines = ["Сделки (начало → конец):"]
    for c in sorted(cycles, key=lambda x: (x["open_ts"] or 0, x["close_ts"] or 0)):
        pnl = c["pnl"]
        lines.append(
            f"  {fmt_iso(c['open_ts'])} → {fmt_iso(c['close_ts'])}   "
            f"{fmt_duration(c['duration_ms']):>10}   "
            f"{c.get('symbol', '?'):<12} {c.get('exit_reason', '?'):<12} "
            f"{pnl:+.4f} USDT" if pnl is not None else
            f"  {fmt_iso(c['open_ts'])} → {fmt_iso(c['close_ts'])}   "
            f"{fmt_duration(c['duration_ms']):>10}   "
            f"{c.get('symbol', '?'):<12} {c.get('exit_reason', '?'):<12} PnL —")
    return "\n".join(lines)


def render(s: dict[str, Any], by_symbol: bool) -> str:
    lines = ["Сводка по закрытым DCA-циклам"]
    hold = s["удержание_мс"]
    per = s["период_торговли"]
    lines.append(f"  Торговля бота:     {fmt_iso(per['начало'])} → {fmt_iso(per['конец'])}")
    lines.append(f"  Закрыто циклов:   {s['закрыто_циклов']}")
    lines.append(f"    по TP:          {s['по_причине']['take_profit']}")
    lines.append(f"    по SL:          {s['по_причине']['hard_sl']}")
    lines.append(f"    по трейлингу:   {s['по_причине']['trailing']}")
    lines.append(f"    по времени:     {s['по_причине']['time_exit']}")
    lines.append(f"    вручную/иное:   {s['по_причине']['manual'] + s['по_причине']['unknown']}")
    lines.append(f"  Совокупный PnL:   {s['pnl_usdt']:+.2f} USDT  "
                 f"(по {s['циклов_с_pnl']} циклам с известным PnL)")
    lines.append(f"  Win-rate:         {s['win_rate_pct']:.1f}%")
    lines.append(f"  Удержание (ср.):  {fmt_duration(hold['среднее'])}   "
                 f"(медиана {fmt_duration(hold['медиана'])}, "
                 f"min {fmt_duration(hold['min'])}, max {fmt_duration(hold['max'])})")
    if s.get("исключено_аномалий"):
        excl = s["исключено_аномалий"]
        lines.append(f"  Исключено аномалий: {excl['count']}  "
                     f"(PnL {excl['pnl']:+.2f}, фильтр: {excl['filter']})")
    if by_symbol:
        lines.append("\nПо символам:")
        for sym, g in s["по_символам"].items():
            gh = g["удержание_мс"]
            lines.append(
                f"  {sym:<12} {g['закрыто_циклов']:>4} циклов   "
                f"PnL {g['pnl_usdt']:+8.2f}   WR {g['win_rate_pct']:>5.1f}%   "
                f"ср. удерж. {fmt_duration(gh['среднее'])}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_windows(args: argparse.Namespace) -> tuple[int | None, int | None]:
    """Возвращает (since_ms, until_ms). Приоритет: --since/--until явные,
    затем --days (окно закрытий за последние N суток)."""
    now = datetime.now(timezone.utc)
    until_ms = None
    if args.until:
        until_ms = int(datetime.fromisoformat(args.until.replace("Z", "+00:00"))
                       .astimezone(timezone.utc).timestamp() * 1000)
    if args.days:
        since = now - timedelta(days=args.days)
        if until_ms is None:
            until_ms = int(now.timestamp() * 1000)
        return int(since.timestamp() * 1000), until_ms
    since_ms = None
    if args.since:
        since_ms = int(datetime.fromisoformat(args.since.replace("Z", "+00:00"))
                       .astimezone(timezone.utc).timestamp() * 1000)
    return since_ms, until_ms


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Сводка по закрытым DCA-циклам из logs/bot-events.jsonl")
    ap.add_argument("journal", nargs="?", type=Path, default=DEFAULT_JOURNAL,
                    help="путь к журналу бота (по умолчанию %(default)s)")
    ap.add_argument("--days", type=int, default=None,
                    help="считать только циклы, закрытые за последние N суток")
    ap.add_argument("--since", default=None,
                    help="включить циклы, закрытые не раньше ISO-момента (например 2026-08-01)")
    ap.add_argument("--until", default=None,
                    help="включить циклы, закрытые не позже ISO-момента")
    ap.add_argument("--by-symbol", action="store_true",
                    help="добавить разбивку по символам/парам "
                         "(в --json — также equity-ряды для report_charts)")
    ap.add_argument("--json", action="store_true",
                    help="вывести машиночитаемый JSON")
    # Фильтрация аномалий (OPGUSDT -628): по символу и по размеру убытка
    ap.add_argument("--exclude-anomalies", action="store_true",
                    help="исключить аномальные циклы: symbol==OPGUSDT или pnl < -100 "
                         "(включает --exclude-symbol OPGUSDT и --exclude-pnl-below -100)")
    ap.add_argument("--exclude-symbol", action="append", dest="exclude_symbols",
                    default=None,
                    help="исключить циклы по символу (можно указывать несколько раз: "
                         "--exclude-symbol OPGUSDT --exclude-symbol XYZUSDT)")
    ap.add_argument("--exclude-pnl-below", type=float, default=None,
                    dest="exclude_pnl_below",
                    help="исключить циклы с pnl < порога (например --exclude-pnl-below -100)")
    ap.add_argument("--include-anomalies", action="store_true",
                    help="включить аномальные циклы обратно (отменяет --exclude-anomalies)")
    args = ap.parse_args(argv)

    since_ms, until_ms = parse_windows(args)
    cycles = collect_cycles([args.journal])
    if since_ms is not None or until_ms is not None:
        def in_window(c: dict[str, Any]) -> bool:
            ts = c["close_ts"] if c["close_ts"] is not None else sys.maxsize
            if since_ms is not None and ts < since_ms:
                return False
            if until_ms is not None and ts > until_ms:
                return False
            return True
        cycles = [c for c in cycles if in_window(c)]

    # --- фильтрация аномалий ---
    exclude_symbols: set[str] | None = None
    exclude_pnl: float | None = None
    active_filter_desc: str | None = None
    if args.exclude_anomalies and not args.include_anomalies:
        exclude_symbols = set(ANOMALY_SYMBOLS)
        exclude_pnl = ANOMALY_PNL_THRESHOLD
        active_filter_desc = f"symbol in {sorted(ANOMALY_SYMBOLS)} или pnl < {ANOMALY_PNL_THRESHOLD}"
    if args.exclude_symbols:
        exclude_symbols = (exclude_symbols or set()) | set(args.exclude_symbols)
        active_filter_desc = f"symbol in {sorted(exclude_symbols)}" + (
            f" или pnl < {exclude_pnl}" if exclude_pnl is not None else "")
    if args.exclude_pnl_below is not None:
        exclude_pnl = args.exclude_pnl_below
        if active_filter_desc:
            active_filter_desc = f"{active_filter_desc} / pnl < {exclude_pnl}"
        else:
            active_filter_desc = f"pnl < {exclude_pnl}"
    # Явные фильтры имеют приоритет над дефолтом
    if exclude_symbols is not None or exclude_pnl is not None:
        clean, anomalous = filter_anomalies(
            cycles, pnl_threshold=exclude_pnl, symbols=exclude_symbols)
        s = summarize(clean, by_symbol=args.by_symbol)
        # Добавляем метаданные об исключённых для render/json
        excl_pnl_sum = sum(c["pnl"] for c in anomalous if isinstance(c.get("pnl"), (int, float)))
        s["исключено_аномалий"] = {
            "count": len(anomalous),
            "pnl": round(excl_pnl_sum, 4),
            "filter": active_filter_desc or "",
            "symbols": sorted(exclude_symbols) if exclude_symbols else [],
            "pnl_threshold": exclude_pnl,
        }
        if args.json:
            s["_anomalous_cycles"] = anomalous
        cycles = clean
    else:
        s = summarize(cycles, by_symbol=args.by_symbol)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(s, args.by_symbol))
        if cycles:
            print()
            print(render_trades(cycles))
        if since_ms is not None or until_ms is not None:
            period = []
            if since_ms is not None:
                period.append(f"с {fmt_iso(since_ms)}")
            if until_ms is not None:
                period.append(f"до {fmt_iso(until_ms)}")
            print(f"\n(окно: {' '.join(period)})")
        if s.get("исключено_аномалий") and s["исключено_аномалий"]["count"]:
            print(f"\n(исключено аномалий: {s['исключено_аномалий']['count']} "
                  f"PnL {s['исключено_аномалий']['pnl']:+.2f} — {active_filter_desc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
