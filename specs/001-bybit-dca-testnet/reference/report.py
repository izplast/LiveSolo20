"""
tools/report.py — сводка по прогону на Testnet.

Закрывает US4 / FR-023 / SC-009: превращает журналы скринера и бота в набор
величин, по которым назначаются финальные пороги задержки и проскальзывания
вместо начальных ориентиров (2 с, 0.3–0.5%).

Запуск:
    python3 tools/report.py logs/screener-events.jsonl logs/bot-events.jsonl
    python3 tools/report.py logs/*.jsonl --json          # машиночитаемый вывод
    python3 tools/report.py logs/*.jsonl --since 2026-08-01T00:00:00

Зависимостей нет: только стандартная библиотека, чтобы сводку можно было
построить прямо на устройстве, где шёл прогон.

Ожидаемые записи (одна строка JSON на событие, поля kind и ts обязательны).

Пишет скринер (reference/screener.py):
  signal_sent      {signal: {signal_id, symbol, side, price, ts, ...}}
  signal_failed    {signal: {...}, error}
  reject           {symbol, reason}
  universe_reject  {symbol, reason}
  stream_down      {cause, shard?}   — начало «слепого» интервала
  stream_up        {cause, duration_ms}

Должен писать бот (контракт из reference/CHANGES.md):
  signal_received  {signal_id, symbol, price_mainnet, price_testnet, ts_signal}
  signal_rejected  {signal_id, reason}        duplicate | limit | exchange_limits | error
  order_filled     {signal_id, cycle_id, symbol, role, expected_price,
                    avg_fill_price, ts_signal, ts_confirmed}
                   role: entry | dca | close
  cycle_opened     {cycle_id, symbol}
  cycle_closed     {cycle_id, symbol, exit_reason, pnl}
                   exit_reason: take_profit | time_exit | manual

Незнакомые kind игнорируются, отсутствующие поля не роняют сводку: журнал
пишется двумя процессами и может быть неполным, а сводка нужна и в этом случае.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# Начальные ориентиры из spec.md. Смысл сводки — заменить их измеренными.
TARGET_LATENCY_MS = 2_000
TARGET_SLIPPAGE_PCT = 0.5
TARGET_SLIPPAGE_MEDIAN_PCT = 0.3
TARGET_BLIND_SHARE = 0.01
TARGET_RECOVERY_MS = 30_000


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------

def read_events(paths: Sequence[str], since_ms: int | None = None,
                until_ms: int | None = None) -> tuple[list[dict], int]:
    """Возвращает события, отсортированные по времени, и число битых строк."""
    events: list[dict] = []
    broken = 0
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        # Обрыв питания посреди записи оставляет неполную строку.
                        broken += 1
                        continue
                    if not isinstance(rec, dict) or "kind" not in rec:
                        broken += 1
                        continue
                    ts = rec.get("ts")
                    if not isinstance(ts, (int, float)):
                        broken += 1
                        continue
                    if since_ms is not None and ts < since_ms:
                        continue
                    if until_ms is not None and ts > until_ms:
                        continue
                    events.append(rec)
        except FileNotFoundError:
            print(f"предупреждение: {path} не найден", file=sys.stderr)
    events.sort(key=lambda r: r["ts"])
    return events, broken


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], q: float) -> float | None:
    """Процентиль методом ближайшего ранга. Без numpy — сводка считается на телефоне."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def describe(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": median(values),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
        "min": min(values) if values else None,
    }


def blind_intervals(events: Iterable[dict]) -> tuple[list[dict], int]:
    """Сводит пары stream_down / stream_up в интервалы недоступности.

    Считается по каждому шарду отдельно: соединения рвутся независимо, и
    недоступность одного шарда не ослепляет остальные. Незакрытый интервал
    (прогон прервался при обрыве) закрывается временем последнего события.
    """
    open_at: dict[Any, int] = {}
    intervals: list[dict] = []
    last_ts = 0
    for e in events:
        last_ts = max(last_ts, int(e["ts"]))
        shard = e.get("shard", "global")
        if e["kind"] == "stream_down":
            open_at.setdefault(shard, int(e["ts"]))
        elif e["kind"] == "stream_up":
            start = open_at.pop(shard, None)
            duration = e.get("duration_ms")
            if start is None and isinstance(duration, (int, float)):
                start = int(e["ts"]) - int(duration)
            if start is not None:
                intervals.append({"shard": shard, "from": start, "to": int(e["ts"]),
                                  "duration_ms": int(e["ts"]) - start,
                                  "cause": e.get("cause", "unknown")})
    unclosed = 0
    for shard, start in open_at.items():
        unclosed += 1
        intervals.append({"shard": shard, "from": start, "to": last_ts,
                          "duration_ms": max(0, last_ts - start),
                          "cause": "unclosed"})
    return intervals, unclosed


def build_summary(events: Sequence[dict]) -> dict[str, Any]:
    if not events:
        return {"error": "журнал пуст"}

    first_ts, last_ts = int(events[0]["ts"]), int(events[-1]["ts"])
    span_ms = last_ts - first_ts

    kinds = Counter(e["kind"] for e in events)
    reject_reasons = Counter(e.get("reason", "?") for e in events if e["kind"] == "reject")
    universe_reasons = Counter(e.get("reason", "?") for e in events if e["kind"] == "universe_reject")
    bot_reject_reasons = Counter(e.get("reason", "?") for e in events if e["kind"] == "signal_rejected")

    # ── задержка и проскальзывание по исполнениям ─────────────────────────
    latency: dict[str, list[float]] = defaultdict(list)
    slippage: dict[str, list[float]] = defaultdict(list)
    slippage_signed: dict[str, list[float]] = defaultdict(list)
    missing_metrics = 0

    for e in events:
        if e["kind"] != "order_filled":
            continue
        role = e.get("role", "unknown")
        ts_signal, ts_conf = e.get("ts_signal"), e.get("ts_confirmed")
        if isinstance(ts_signal, (int, float)) and isinstance(ts_conf, (int, float)):
            latency[role].append(float(ts_conf) - float(ts_signal))
        else:
            missing_metrics += 1

        expected, actual = e.get("expected_price"), e.get("avg_fill_price")
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)) and expected:
            pct = (float(actual) - float(expected)) / float(expected) * 100
            # Знак приводим к «невыгодно = плюс»: для лонга плата за вход выше
            # ожидания, для шорта — ниже. Иначе медиана по смешанным сторонам
            # схлопывается около нуля и скрывает реальные потери.
            side = (e.get("side") or "").lower()
            signed = -pct if side in ("sell", "short") else pct
            slippage_signed[role].append(signed)
            slippage[role].append(abs(pct))
        else:
            missing_metrics += 1

    # ── базис между контурами ────────────────────────────────────────────
    basis: list[float] = []
    for e in events:
        if e["kind"] != "signal_received":
            continue
        pm, pt = e.get("price_mainnet"), e.get("price_testnet")
        if isinstance(pm, (int, float)) and isinstance(pt, (int, float)) and pm:
            basis.append((float(pt) - float(pm)) / float(pm) * 100)

    # ── циклы ────────────────────────────────────────────────────────────
    opened = {e.get("cycle_id") for e in events if e["kind"] == "cycle_opened"}
    closed_by: Counter = Counter()
    pnl: list[float] = []
    closed_ids = set()
    for e in events:
        if e["kind"] != "cycle_closed":
            continue
        closed_ids.add(e.get("cycle_id"))
        closed_by[e.get("exit_reason", "unknown")] += 1
        if isinstance(e.get("pnl"), (int, float)):
            pnl.append(float(e["pnl"]))

    intervals, unclosed = blind_intervals(events)
    blind_ms = sum(i["duration_ms"] for i in intervals if i["cause"] != "startup")
    blind_share = blind_ms / span_ms if span_ms else 0.0

    entry_latency = latency.get("entry", [])
    entry_slippage = slippage.get("entry", [])

    summary: dict[str, Any] = {
        "период": {
            "начало": iso(first_ts),
            "конец": iso(last_ts),
            "длительность_ч": round(span_ms / 3_600_000, 2),
        },
        "события": dict(kinds),
        "сигналы": {
            "отправлено_скринером": kinds.get("signal_sent", 0),
            "не_доставлено": kinds.get("signal_failed", 0),
            "получено_ботом": kinds.get("signal_received", 0),
            "отклонено_ботом": dict(bot_reject_reasons),
            "исполнено_входов": len(entry_latency),
        },
        "отсечения_скринера": dict(reject_reasons),
        "отсечения_вселенной": dict(universe_reasons),
        "задержка_сигнал_исполнение_мс": {role: describe(v) for role, v in sorted(latency.items())},
        "проскальзывание_абс_pct": {role: describe(v) for role, v in sorted(slippage.items())},
        "проскальзывание_со_знаком_pct": {
            role: describe(v) for role, v in sorted(slippage_signed.items())
        },
        "базис_testnet_к_mainnet_pct": describe(basis),
        "циклы": {
            "открыто": len(opened),
            "закрыто": len(closed_ids),
            "не_закрыто_к_концу": len(opened - closed_ids),
            "по_причине_выхода": dict(closed_by),
            "pnl": describe(pnl),
        },
        "недоступность_потока": {
            "интервалов": len(intervals),
            "незакрытых": unclosed,
            "суммарно_мс": blind_ms,
            "доля_прогона": round(blind_share, 5),
            "худший_мс": max((i["duration_ms"] for i in intervals), default=0),
        },
        "записей_без_метрик": missing_metrics,
    }

    summary["превышения_ориентиров"] = {
        "задержка_входа_свыше_2с": share(entry_latency, lambda v: v > TARGET_LATENCY_MS),
        "проскальзывание_входа_свыше_0.5pct": share(
            entry_slippage, lambda v: v > TARGET_SLIPPAGE_PCT),
    }
    summary["критерии"] = verdicts(summary, entry_latency, entry_slippage, intervals, span_ms)
    return summary


def share(values: Sequence[float], predicate) -> dict[str, Any]:
    if not values:
        return {"n": 0, "доля": None}
    hits = sum(1 for v in values if predicate(v))
    return {"n": len(values), "превысило": hits, "доля": round(hits / len(values), 4)}


def verdicts(s: dict, entry_latency: Sequence[float], entry_slippage: Sequence[float],
             intervals: Sequence[dict], span_ms: int) -> list[dict]:
    """Вердикт по каждому измеримому критерию spec.md.

    Критерии, которые из журнала не проверяются (например «без ручного
    вмешательства»), помечаются как требующие ручной проверки — молча
    опускать их нельзя, иначе сводка выглядит полнее, чем есть.
    """
    out: list[dict] = []

    def add(code: str, target: str, value: Any, ok: bool | None) -> None:
        out.append({"код": code, "порог": target, "измерено": value,
                    "вердикт": "нет данных" if ok is None else ("выполнен" if ok else "НЕ выполнен")})

    hours = span_ms / 3_600_000
    add("SC-001", ">= 72 ч непрерывно", f"{hours:.1f} ч", hours >= 72 if span_ms else None)

    failed = s["сигналы"]["не_доставлено"] + s["сигналы"]["отклонено_ботом"].get("error", 0)
    add("SC-002", "0 сигналов с ошибкой", failed, failed == 0)

    p95_lat = percentile(entry_latency, 95)
    add("SC-003", f"p95 задержки входа <= {TARGET_LATENCY_MS} мс",
        None if p95_lat is None else round(p95_lat), None if p95_lat is None else p95_lat <= TARGET_LATENCY_MS)

    p95_slip, med_slip = percentile(entry_slippage, 95), median(entry_slippage)
    add("SC-004", f"p95 <= {TARGET_SLIPPAGE_PCT}% и медиана <= {TARGET_SLIPPAGE_MEDIAN_PCT}%",
        None if p95_slip is None else {"p95": round(p95_slip, 4), "медиана": round(med_slip or 0, 4)},
        None if p95_slip is None else (p95_slip <= TARGET_SLIPPAGE_PCT
                                      and (med_slip or 0) <= TARGET_SLIPPAGE_MEDIAN_PCT))

    filled = len(entry_latency)
    add("SC-005", "у 100% исполнений обе метрики", s["записей_без_метрик"],
        s["записей_без_метрик"] == 0 if filled else None)

    above = s["отсечения_скринера"].get("natr_above_max", 0)
    add("SC-006", "нет сделок по слишком волатильным", f"отсечено {above}", None)

    worst = max((i["duration_ms"] for i in intervals if i["cause"] != "startup"), default=0)
    blind_ok = (s["недоступность_потока"]["доля_прогона"] <= TARGET_BLIND_SHARE
                and worst <= TARGET_RECOVERY_MS)
    add("SC-007", f"восстановление <= {TARGET_RECOVERY_MS} мс, слепых <= {TARGET_BLIND_SHARE:.0%}",
        {"худший_мс": worst, "доля": s["недоступность_потока"]["доля_прогона"]},
        blind_ok if intervals else None)

    add("SC-008", "0 ордеров вне Testnet", "проверяется по истории счёта", None)

    open_left = s["циклы"]["не_закрыто_к_концу"]
    add("SC-011", "нет циклов дольше максимума удержания",
        {"не_закрыто": open_left, "по_таймеру": s["циклы"]["по_причине_выхода"].get("time_exit", 0)}, None)

    not_on_testnet = s["сигналы"]["отклонено_ботом"].get("exchange_limits", 0)
    add("SC-012", "0 ордеров по символам вне Testnet", not_on_testnet, not_on_testnet == 0)
    return out


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def render(s: dict) -> str:
    if "error" in s:
        return f"Сводка не построена: {s['error']}"
    lines: list[str] = []
    add = lines.append

    p = s["период"]
    add(f"Прогон: {p['начало']} → {p['конец']}  ({p['длительность_ч']} ч)")
    add("")

    sig = s["сигналы"]
    add("Сигналы")
    add(f"  отправлено скринером : {sig['отправлено_скринером']}")
    add(f"  не доставлено        : {sig['не_доставлено']}")
    add(f"  получено ботом       : {sig['получено_ботом']}")
    add(f"  исполнено входов     : {sig['исполнено_входов']}")
    if sig["отклонено_ботом"]:
        for reason, n in sorted(sig["отклонено_ботом"].items(), key=lambda kv: -kv[1]):
            add(f"  отклонено ботом      : {reason} — {n}")
    add("")

    for title, block in (("Задержка сигнал→исполнение, мс", s["задержка_сигнал_исполнение_мс"]),
                         ("Проскальзывание (абс.), %", s["проскальзывание_абс_pct"]),
                         ("Проскальзывание (знак: + невыгодно), %", s["проскальзывание_со_знаком_pct"])):
        if not block:
            continue
        add(title)
        add(f"  {'роль':<8} {'n':>5} {'медиана':>12} {'p95':>12} {'макс':>12}")
        for role, d in block.items():
            add(f"  {role:<8} {d['n']:>5} {fmt(d['median']):>12} {fmt(d['p95']):>12} {fmt(d['max']):>12}")
        add("")

    b = s["базис_testnet_к_mainnet_pct"]
    if b["n"]:
        add("Базис Testnet к mainnet, %")
        add(f"  n={b['n']}  медиана={fmt(b['median'])}  p95={fmt(b['p95'])}  "
            f"мин={fmt(b['min'])}  макс={fmt(b['max'])}")
        add("  (эта величина НЕ входит в порог проскальзывания — она про разницу контуров)")
        add("")

    c = s["циклы"]
    add("Циклы DCA")
    add(f"  открыто {c['открыто']}, закрыто {c['закрыто']}, не закрыто к концу {c['не_закрыто_к_концу']}")
    for reason, n in sorted(c["по_причине_выхода"].items(), key=lambda kv: -kv[1]):
        add(f"  выход: {reason} — {n}")
    if c["pnl"]["n"]:
        add(f"  pnl: медиана {fmt(c['pnl']['median'])}, мин {fmt(c['pnl']['min'])}, макс {fmt(c['pnl']['max'])}")
    add("")

    d = s["недоступность_потока"]
    add("Недоступность потока")
    add(f"  интервалов {d['интервалов']} (незакрытых {d['незакрытых']}), суммарно {d['суммарно_мс']} мс "
        f"= {d['доля_прогона'] * 100:.3f}% прогона, худший {d['худший_мс']} мс")
    add("")

    if s["отсечения_скринера"]:
        add("Отсечения скринера")
        for reason, n in sorted(s["отсечения_скринера"].items(), key=lambda kv: -kv[1]):
            add(f"  {reason:<24} {n}")
        add("")

    add("Критерии приёмки")
    for v in s["критерии"]:
        mark = {"выполнен": "+", "НЕ выполнен": "-", "нет данных": "?"}[v["вердикт"]]
        add(f"  [{mark}] {v['код']:<7} {v['порог']}")
        add(f"          измерено: {v['измерено']}")
    add("")
    add("Пороги 2 с и 0.3–0.5% — начальные ориентиры. Назначайте финальные по")
    add("p95 из этой сводки, а не по ним.")
    return "\n".join(lines)


def fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def parse_since(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Сводка по прогону DCA-бота на Testnet")
    ap.add_argument("paths", nargs="+", help="файлы журналов JSONL (допустимы шаблоны)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ap.add_argument("--since", help="нижняя граница периода, ISO-8601")
    ap.add_argument("--until", help="верхняя граница периода, ISO-8601")
    args = ap.parse_args(argv)

    paths: list[str] = []
    for pattern in args.paths:
        expanded = sorted(glob.glob(pattern))
        paths.extend(expanded or [pattern])

    events, broken = read_events(
        paths,
        parse_since(args.since) if args.since else None,
        parse_since(args.until) if args.until else None,
    )
    if broken:
        print(f"предупреждение: пропущено битых строк: {broken}", file=sys.stderr)

    summary = build_summary(events)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0 if "error" not in summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
