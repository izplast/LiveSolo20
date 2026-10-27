#!/usr/bin/env python3
"""Fade shadow: evaluate the reverse of every delegated screener signal.

Research only. The script never places, cancels, or modifies orders. It reads
``logs/screener-events.jsonl`` from 2026-10-28 00:00 Europe/Kyiv, takes the
first closed 1m mainnet bar after each ``signal_sent`` event, enters opposite
to the signal, and simulates TP/SL/timeout on Bybit public 1m klines.

Selected pair from the train grid: TP = 2.0 x NATR, SL = 3.0 x NATR,
timeout = 60 minutes. Fees are 0.055% per side; stops add 0.3% slippage.
When TP and SL are both touched inside one 1m bar, the stop wins.

Results are appended to ``logs/fade_shadow.jsonl`` as ``fade_result`` records,
followed by a cumulative ``fade_summary`` record. The last stdout line is the
summary: N, PF, and the sum of net percentage returns (1x notional).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo

    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV = timezone(timedelta(hours=2))

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "logs" / "screener-events.jsonl"
OUTPUT = ROOT / "logs" / "fade_shadow.jsonl"
API = "https://api.bybit.com"
CATEGORY = "linear"
INTERVAL = "1"

START_KYIV = datetime(2026, 10, 28, 0, 0, tzinfo=KYIV)
TP_MULT = 2.0
SL_MULT = 3.0
HOLD_MIN = 60
FEE_RATE = 0.00055
STOP_SLIP = 0.003
READY_BUFFER_MS = 5_000
NO_DATA_GRACE_MS = 6 * 3600 * 1000


def _bar(ts: int, open_: float, high: float, low: float, close: float) -> list[Any]:
    return [ts, open_, high, low, close, 0.0]


def _natr(signal: dict[str, Any]) -> float | None:
    raw = signal.get("atr")
    if raw is None:
        raw = (signal.get("diagnostics") or {}).get("natr")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _entry_ts(event_ts: int) -> int:
    return ((int(event_ts) + 59_999) // 60_000) * 60_000


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except FileNotFoundError:
        return


def iter_signals(path: Path) -> Iterable[dict[str, Any]]:
    for rec in _jsonl(path):
        if rec.get("kind") != "signal_sent":
            continue
        signal = rec.get("signal")
        if not isinstance(signal, dict) or not signal.get("signal_id"):
            continue
        event_ts = rec.get("ts") or signal.get("ts")
        if event_ts is None:
            continue
        yield {
            "signal_id": signal["signal_id"],
            "symbol": signal.get("symbol"),
            "side": signal.get("side"),
            "price": signal.get("price"),
            "natr_pct": _natr(signal),
            "signal_ts": signal.get("ts"),
            "event_ts": int(event_ts),
        }


def load_done(path: Path) -> set[str]:
    done: set[str] = set()
    for rec in _jsonl(path):
        if rec.get("kind") in ("fade_result", "fade_skipped") and rec.get("signal_id"):
            done.add(str(rec["signal_id"]))
    return done


def load_results(path: Path) -> list[dict[str, Any]]:
    return [rec for rec in _jsonl(path) if rec.get("kind") == "fade_result"]


def append_jsonl(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def fetch_klines(symbol: str, start_ms: int, end_ms: int, tries: int = 4) -> list[list[Any]]:
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": INTERVAL,
        "start": int(start_ms),
        "end": int(end_ms),
        "limit": 1000,
    }
    url = f"{API}/v5/market/kline?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                payload = json.load(resp)
            if payload.get("retCode") != 0:
                raise RuntimeError(
                    f"{symbol}: retCode={payload.get('retCode')} {payload.get('retMsg')}"
                )
            rows = payload["result"]["list"]
            bars = [
                [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in rows
            ]
            bars.sort(key=lambda b: b[0])
            return bars
        except Exception as exc:
            last = exc
            if attempt < tries:
                time.sleep(1.0 * attempt)
    raise RuntimeError(f"{symbol}: kline fetch failed: {last}")


def simulate_fade(
    bars: list[list[Any]],
    entry_ts: int,
    fade_side: str,
    natr_pct: float,
    tp_mult: float = TP_MULT,
    sl_mult: float = SL_MULT,
    hold_min: int = HOLD_MIN,
    fee_rate: float = FEE_RATE,
    stop_slip: float = STOP_SLIP,
) -> dict[str, Any] | None:
    index = next((i for i, bar in enumerate(bars) if int(bar[0]) == int(entry_ts)), None)
    if index is None:
        index = next((i for i, bar in enumerate(bars) if int(bar[0]) > int(entry_ts)), None)
    if index is None:
        return None
    segment = bars[index:index + hold_min]
    if len(segment) < hold_min:
        return None

    entry = float(segment[0][1])
    if entry <= 0:
        return None

    direction = 1 if fade_side == "Buy" else -1
    tp_pct = tp_mult * natr_pct
    sl_pct = sl_mult * natr_pct
    tp_price = entry * (1 + direction * tp_pct / 100)
    sl_price = entry * (1 - direction * sl_pct / 100)

    exit_reason = "timeout"
    exit_price = float(segment[-1][4])
    exit_ts = int(segment[-1][0])
    held_min = hold_min

    for idx, bar in enumerate(segment):
        high = float(bar[2])
        low = float(bar[3])
        hit_sl = low <= sl_price if direction == 1 else high >= sl_price
        hit_tp = high >= tp_price if direction == 1 else low <= tp_price
        if hit_sl:
            exit_reason = "sl"
            exit_price = sl_price * (1 - direction * stop_slip)
            exit_ts = int(bar[0])
            held_min = idx + 1
            break
        if hit_tp:
            exit_reason = "tp"
            exit_price = tp_price
            exit_ts = int(bar[0])
            held_min = idx + 1
            break

    gross_pct = direction * (exit_price / entry - 1) * 100
    net_pct = gross_pct - 2 * fee_rate * 100
    return {
        "entry_ts": int(entry_ts),
        "entry_price": entry,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "exit_ts": exit_ts,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "held_min": held_min,
        "gross_pct": gross_pct,
        "net_pct": net_pct,
        "fee_pct": 2 * fee_rate * 100,
    }


def pf_and_sum(records: Iterable[dict[str, Any]]) -> tuple[int, float | None, float]:
    values = [float(r.get("net_pct", 0.0)) for r in records]
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if not values:
        pf: float | None = None
    elif losses > 0:
        pf = gains / losses
    elif gains > 0:
        pf = None
    else:
        pf = 0.0
    return len(values), pf, sum(values)


def _fmt_pf(pf: float | None) -> str:
    return "n/a" if pf is None else f"{pf:.3f}"


def _summary_line(n: int, pf: float | None, total: float, pending: int, skipped: int, errors: int) -> str:
    return (
        f"fade_shadow: N={n} PF={_fmt_pf(pf)} sum={total:+.2f}% "
        f"| TP={TP_MULT:g}xNATR SL={SL_MULT:g}xNATR hold={HOLD_MIN}m "
        f"| pending={pending} skipped={skipped} errors={errors}"
    )


def self_test() -> None:
    def bars_with(first: list[Any], close: float = 100.0) -> list[list[Any]]:
        rows = [_bar(i * 60_000, close, close, close, close) for i in range(HOLD_MIN)]
        rows[0] = first
        return rows

    tp_long = simulate_fade(bars_with(_bar(0, 100, 103, 99, 100)), 0, "Buy", 1.0)
    assert tp_long and tp_long["exit_reason"] == "tp"
    assert math.isclose(tp_long["net_pct"], 1.89, abs_tol=1e-9)

    sl_long = simulate_fade(bars_with(_bar(0, 100, 101, 96, 100)), 0, "Buy", 1.0)
    assert sl_long and sl_long["exit_reason"] == "sl"
    assert math.isclose(sl_long["net_pct"], -3.401, abs_tol=1e-9)

    tp_short = simulate_fade(bars_with(_bar(0, 100, 101, 97, 100)), 0, "Sell", 1.0)
    assert tp_short and tp_short["exit_reason"] == "tp"
    assert math.isclose(tp_short["net_pct"], 1.89, abs_tol=1e-9)

    timeout = simulate_fade(bars_with(_bar(0, 100, 100.5, 100, 100), close=100.5), 0, "Buy", 1.0)
    assert timeout and timeout["exit_reason"] == "timeout"
    assert math.isclose(timeout["net_pct"], 0.39, abs_tol=1e-9)

    n, pf, total = pf_and_sum([{"net_pct": 1.0}, {"net_pct": -1.0}, {"net_pct": 2.0}])
    assert n == 3 and pf is not None and math.isclose(pf, 3.0)
    assert math.isclose(total, 2.0)
    print("fade_shadow self-test: ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fade shadow evaluator (research only)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    now_ms = int(time.time() * 1000)
    start_ms = int(START_KYIV.timestamp() * 1000)
    done = load_done(OUTPUT)
    pending = 0
    skipped = 0
    errors = 0
    added = 0

    for sig in sorted(iter_signals(JOURNAL), key=lambda s: s["event_ts"]):
        if sig["event_ts"] < start_ms or sig["signal_id"] in done:
            continue
        symbol = sig.get("symbol")
        side = sig.get("side")
        natr = sig.get("natr_pct")
        if not symbol or side not in ("Buy", "Sell") or natr is None:
            append_jsonl(OUTPUT, {
                "kind": "fade_skipped",
                "signal_id": sig["signal_id"],
                "symbol": symbol,
                "event_ts": sig["event_ts"],
                "reason": "bad_signal",
                "ts": now_ms,
            })
            done.add(sig["signal_id"])
            skipped += 1
            continue

        entry_ts = _entry_ts(sig["event_ts"])
        ready_at = entry_ts + HOLD_MIN * 60_000 + READY_BUFFER_MS
        if now_ms < ready_at:
            pending += 1
            continue

        try:
            bars = fetch_klines(symbol, entry_ts, entry_ts + (HOLD_MIN - 1) * 60_000)
        except Exception as exc:
            print(f"warning: {exc}", file=sys.stderr)
            errors += 1
            continue

        result = simulate_fade(bars, entry_ts, "Sell" if side == "Buy" else "Buy", natr)
        if result is None:
            if now_ms - sig["event_ts"] > NO_DATA_GRACE_MS:
                append_jsonl(OUTPUT, {
                    "kind": "fade_skipped",
                    "signal_id": sig["signal_id"],
                    "symbol": symbol,
                    "event_ts": sig["event_ts"],
                    "reason": "no_1m_data",
                    "ts": now_ms,
                })
                done.add(sig["signal_id"])
                skipped += 1
            else:
                pending += 1
            continue

        record = {
            "kind": "fade_result",
            "signal_id": sig["signal_id"],
            "symbol": symbol,
            "signal_side": side,
            "fade_side": "Sell" if side == "Buy" else "Buy",
            "natr_pct": natr,
            "signal_ts": sig["signal_ts"],
            "event_ts": sig["event_ts"],
            "source": "bybit_mainnet",
            "interval": "1m",
            "tp_natr": TP_MULT,
            "sl_natr": SL_MULT,
            "hold_min": HOLD_MIN,
            "fee_pct": 2 * FEE_RATE * 100,
            "stop_slip_pct": STOP_SLIP * 100,
            "ts": now_ms,
            **result,
        }
        append_jsonl(OUTPUT, record)
        done.add(sig["signal_id"])
        added += 1
        time.sleep(0.1)

    results = load_results(OUTPUT)
    n, pf, total = pf_and_sum(results)
    summary = {
        "kind": "fade_summary",
        "ts": now_ms,
        "n": n,
        "pf": pf,
        "sum_net_pct": total,
        "tp_natr": TP_MULT,
        "sl_natr": SL_MULT,
        "hold_min": HOLD_MIN,
        "fee_pct": 2 * FEE_RATE * 100,
        "stop_slip_pct": STOP_SLIP * 100,
        "start_kyiv": START_KYIV.isoformat(),
        "added": added,
        "pending": pending,
        "skipped": skipped,
        "errors": errors,
    }
    append_jsonl(OUTPUT, summary)
    print(_summary_line(n, pf, total, pending, skipped, errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
