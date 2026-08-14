"""
reference/journal_sim.py — генератор синтетического JSONL-журнала Testnet.

Строит журнал по контракту contracts/journal.md: события скринера и бота за
N часов (по умолчанию 72). Нужен, чтобы проверять сценарии приёмки SC-001..SC-012
на синтетике ДО того, как накопится настоящий прогон, и чтобы tools/run_tests.py
мог прогнать report.py по детерминированным данным (seed).

Сценарии (детерминированы seed, без сети):

  * stable      — прогон 72 ч: все сигналы исполнены в пределах ориентиров,
                  SC-001..SC-005 должны быть «выполнен»;
  * violations  — 72 ч, каждое 5-е исполнение с задержкой 5 с и проскальзыванием
                  1.2%, один недоставленный сигнал: SC-002/003/004 «НЕ выполнен»;
  * downtime    — 72 ч с разрывом потока 120 с: SC-007 «НЕ выполнен»;
  * short       — прогон 1 ч: SC-001 «НЕ выполнен» (мало времени).

Генератор пишет два файла JSONL (screener-events.jsonl, bot-events.jsonl), как
два живых процесса, и использует reference/report.py для сводки и вердиктов.

Запуск:

    python3 reference/journal_sim.py --out /tmp/sim --scenario stable --seed 42
    python3 reference/journal_sim.py --out /tmp/sim --scenario violations --check
    python3 reference/journal_sim.py --out /tmp/sim --check   # все сценарии

Зависимостей нет: только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с journal_sim.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


report = _load_sibling("report")

HOUR = 3_600_000
MIN = 60_000
T0 = 1_760_000_000_000  # точка отсчёта сценария, мс

# Коды критериев, проверяемые сценариями.
SC_JOURNAL_CODES = ("SC-001", "SC-002", "SC-003", "SC-004", "SC-005", "SC-007")


@dataclass
class CycleSpec:
    """Один цикл-сценарий: сигнал скринера → исполнение ботом → закрытие."""
    ts_signal: int
    latency_ms: int          # задержка сигнал → подтверждение входа
    slippage_pct: float      # невыгодное проскальзывание входа (для Buy — плюс)
    pnl: float
    exit_reason: str = "take_profit"
    side: str = "Buy"
    hold_minutes: int = 60
    failed: bool = False     # вместо доставки — signal_failed


def _price_after(price: float, slippage_pct: float, side: str) -> float:
    if side.lower() in ("sell", "short"):
        return price * (1 - slippage_pct / 100)
    return price * (1 + slippage_pct / 100)


def sim_events(specs: Sequence[CycleSpec],
               symbols: Sequence[str] = ("AAAUSDT", "BBBUSDT", "CCCUSDT"),
               price: float = 100.0,
               downtimes: Sequence[tuple[int, int, str]] = (),
               reject_every: int = 0) -> tuple[list[dict], list[dict]]:
    """Спеки циклов → (screener_events, bot_events) по контракту журнала.

    Каждый успешный цикл: signal_sent → signal_received → cycle_opened →
    order_filled(entry) → order_filled(close) → cycle_closed. Провал вместо
    доставки даёт signal_failed. Простои — пары stream_down/stream_up.
    """
    screener: list[dict] = []
    bot: list[dict] = []

    for i, c in enumerate(specs):
        sym = symbols[i % len(symbols)]
        sid = f"{sym}:{i}:{c.ts_signal}"
        pm = price
        pt = price * (1.002)  # базис Testnet к mainnet ~0.2%

        screener.append({"kind": "signal_sent", "ts": c.ts_signal,
                         "signal": {"signal_id": sid, "symbol": sym,
                                    "side": c.side, "price": pm, "ts": c.ts_signal}})
        if reject_every and i % reject_every == reject_every - 1:
            screener.append({"kind": "reject", "ts": c.ts_signal,
                             "symbol": sym, "reason": "natr_above_max",
                             "turnover24h": 5_000_000})
            continue
        if c.failed:
            screener.append({"kind": "signal_failed", "ts": c.ts_signal,
                             "signal": {"signal_id": sid, "symbol": sym,
                                        "side": c.side, "price": pm,
                                        "ts": c.ts_signal},
                             "error": "connection refused"})
            continue

        recv_ts = c.ts_signal + 50
        open_ts = c.ts_signal + 100
        conf_ts = c.ts_signal + c.latency_ms
        close_ts = open_ts + c.hold_minutes * MIN
        expected = pm
        actual = _price_after(pm, c.slippage_pct, c.side)

        screener.append({"kind": "universe_reject", "ts": c.ts_signal,
                         "symbol": "ZZZUSDT", "reason": "not_on_testnet"})
        bot.append({"kind": "signal_received", "ts": recv_ts, "signal_id": sid,
                    "symbol": sym, "price_mainnet": pm, "price_testnet": pt,
                    "ts_signal": c.ts_signal})
        bot.append({"kind": "cycle_opened", "ts": open_ts, "cycle_id": f"c{i}",
                    "symbol": sym, "open_ts": open_ts})
        bot.append({"kind": "order_filled", "ts": conf_ts, "signal_id": sid,
                    "cycle_id": f"c{i}", "symbol": sym, "role": "entry",
                    "mode": "entry", "side": c.side, "expected_price": expected,
                    "avg_fill_price": actual, "ts_signal": c.ts_signal,
                    "ts_confirmed": conf_ts})
        bot.append({"kind": "order_filled", "ts": close_ts, "signal_id": sid,
                    "cycle_id": f"c{i}", "symbol": sym, "role": "close",
                    "mode": "close", "side": c.side, "expected_price": expected,
                    "avg_fill_price": expected, "ts_signal": c.ts_signal,
                    "ts_confirmed": close_ts})
        bot.append({"kind": "cycle_closed", "ts": close_ts, "cycle_id": f"c{i}",
                    "symbol": sym, "exit_reason": c.exit_reason, "pnl": c.pnl,
                    "open_ts": open_ts, "close_ts": close_ts,
                    "duration_ms": close_ts - open_ts})

    for start_ms, duration_ms, cause in downtimes:
        end = start_ms + duration_ms
        screener.append({"kind": "stream_down", "ts": start_ms, "cause": cause,
                         "shard": 0})
        screener.append({"kind": "stream_up", "ts": end, "cause": cause,
                         "duration_ms": duration_ms, "shard": 0})
    return screener, bot


def effective_duration(scenario: str, duration_h: float = 72) -> float:
    """Действительная длительность сценария: для short — всегда 1 ч,
    иначе используем переданную (по умолчанию 72)."""
    if scenario == "short":
        return 1
    return duration_h


def build_specs(scenario: str, duration_h: float = 72, n_signals: int = 100,
                seed: int = 0) -> list[CycleSpec]:
    """Спеки циклов по сценарию; детерминированы seed."""
    duration_h = effective_duration(scenario, duration_h)
    rng = random.Random(seed)
    specs: list[CycleSpec] = []
    for i in range(n_signals):
        ts = T0 + int(i * duration_h * HOUR / n_signals)
        if scenario == "stable":
            latency = rng.randint(400, 800)
            slip = round(rng.uniform(0.05, 0.15), 3)
            pnl = round(rng.uniform(0.3, 0.8), 2)
        elif scenario == "violations":
            slow = i % 5 == 0
            latency = 5000 if slow else rng.randint(400, 800)
            slip = 1.2 if slow else round(rng.uniform(0.05, 0.15), 3)
            pnl = round(rng.uniform(0.3, 0.8), 2)
        elif scenario == "downtime":
            latency = rng.randint(400, 800)
            slip = round(rng.uniform(0.05, 0.15), 3)
            pnl = round(rng.uniform(0.3, 0.8), 2)
        elif scenario == "short":
            latency = rng.randint(400, 800)
            slip = round(rng.uniform(0.05, 0.15), 3)
            pnl = round(rng.uniform(0.3, 0.8), 2)
        else:
            raise ValueError(f"неизвестный сценарий: {scenario!r}")
        specs.append(CycleSpec(ts_signal=ts, latency_ms=latency, slippage_pct=slip,
                               pnl=pnl))
    if scenario == "violations":
        specs[0].failed = True
    return specs


def scenario_downtimes(scenario: str, duration_h: float = 72) -> list[tuple[int, int, str]]:
    """Простои потока для сценария. В stable — только стартовый (исключается
    из доли «слепого» времени, report.py считает cause=startup отдельно)."""
    if scenario == "downtime":
        duration_h = effective_duration(scenario, duration_h)
        mid = T0 + int(duration_h * HOUR / 2)
        return [(mid, 120_000, "disconnect")]
    if scenario == "stable":
        return [(T0, 1_000, "startup")]
    return []


def generate(out_dir: str, scenario: str = "stable", duration_h: float = 72,
             n_signals: int = 100, seed: int = 0,
             reject_every: int = 0) -> tuple[str, str]:
    """Пишет два файла JSONL; возвращает (screener_path, bot_path)."""
    os.makedirs(out_dir, exist_ok=True)
    duration_h = effective_duration(scenario, duration_h)
    specs = build_specs(scenario, duration_h, n_signals, seed)
    downtimes = scenario_downtimes(scenario, duration_h)
    screener, bot = sim_events(specs, downtimes=downtimes,
                               reject_every=reject_every)
    # heartbeat в конце, чтобы прогон дотянул до duration_h (как в test_report)
    if specs:
        end = T0 + int(duration_h * HOUR)
        bot.append({"kind": "heartbeat", "ts": end})

    screener_path = os.path.join(out_dir, "screener-events.jsonl")
    bot_path = os.path.join(out_dir, "bot-events.jsonl")

    def write(path: str, events: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    write(screener_path, screener)
    write(bot_path, bot)
    return screener_path, bot_path


def summary_for(scenario: str, duration_h: float = 72, n_signals: int = 100,
                seed: int = 0, reject_every: int = 0) -> dict[str, Any]:
    """Сводка report.build_summary по синтетическому журналу сценария."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="journal-sim-")
    sp, bp = generate(tmp, scenario, duration_h, n_signals, seed, reject_every)
    events, broken = report.read_events([sp, bp])
    return report.build_summary(events)


def verdicts_for(scenario: str, duration_h: float = 72, n_signals: int = 100,
                 seed: int = 0) -> list[dict]:
    """Вердикты SC-критериев по синтетическому журналу сценария."""
    s = summary_for(scenario, duration_h, n_signals, seed)
    if "error" in s:
        return []
    return [v for v in s["критерии"] if v["код"] in SC_JOURNAL_CODES]


# Ожидаемые вердикты по сценариям: код → вердикт (проверяются только «сильные»
# случаи; «нет данных» не жёстко).
EXPECTED = {
    "stable": {"SC-001": "выполнен", "SC-002": "выполнен", "SC-003": "выполнен",
               "SC-004": "выполнен", "SC-005": "выполнен"},
    "violations": {"SC-002": "НЕ выполнен", "SC-003": "НЕ выполнен",
                   "SC-004": "НЕ выполнен"},
    "downtime": {"SC-007": "НЕ выполнен"},
    "short": {"SC-001": "НЕ выполнен"},
}


def check_scenario(scenario: str, duration_h: float = 72, n_signals: int = 100,
                   seed: int = 0) -> tuple[bool, list[dict]]:
    """Сверяет вердикты сценария с EXPECTED. Возвращает (ok, детали)."""
    verdicts = verdicts_for(scenario, duration_h, n_signals, seed)
    by_code = {v["код"]: v["вердикт"] for v in verdicts}
    expected = EXPECTED.get(scenario, {})
    details: list[dict] = []
    ok = True
    for code, want in expected.items():
        got = by_code.get(code)
        good = got == want
        ok = ok and good
        details.append({"код": code, "ожидалось": want, "получено": got,
                        "ok": good})
    return ok, details


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render_check(scenario: str, ok: bool, details: list[dict]) -> str:
    lines = [f"Сценарий {scenario}: " + ("OK" if ok else "FAIL")]
    for d in details:
        mark = "ok" if d["ok"] else "FAIL"
        lines.append(f"  [{mark}] {d['код']}: ожидалось {d['ожидалось']}, "
                     f"получено {d['получено']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Генератор синтетического JSONL-журнала Testnet")
    ap.add_argument("--out", default="/tmp/journal-sim",
                    help="каталог для журналов (по умолчанию %(default)s)")
    ap.add_argument("--scenario", default=None,
                    help="stable|violations|downtime|short (без флага — все)")
    ap.add_argument("--duration-h", type=float, default=72)
    ap.add_argument("--n-signals", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--check", action="store_true",
                    help="прогнать report.py и сверить вердикты SC")
    args = ap.parse_args(argv)

    scenarios = [args.scenario] if args.scenario else list(EXPECTED)
    rc = 0
    for name in scenarios:
        if args.check:
            ok, details = check_scenario(name, args.duration_h, args.n_signals,
                                         args.seed)
            print(render_check(name, ok, details))
            if not ok:
                rc = 1
        else:
            sp, bp = generate(args.out, name, args.duration_h, args.n_signals,
                              args.seed)
            print(f"[journal_sim] {name}: {sp}, {bp}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())