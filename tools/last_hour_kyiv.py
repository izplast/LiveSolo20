#!/usr/bin/env python3
"""last_hour_kyiv.py — отчёт «что по сделкам за N часов» в киевском времени.

Дефолт: последний 1 час от текущего Kyiv-времени, даты не спрашиваем.
Читает только stdlib, битые строки пропускает.

Usage:
    python3 tools/last_hour_kyiv.py [--hours 1]
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

KYIV = timezone(timedelta(hours=3))  # EEST (лето); зимой будет +2 — см. заметку ниже
# Точнее через zoneinfo, если доступна:
try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    pass

UTC = timezone.utc

EVENT_FILES = [
    "logs/screener-events.jsonl",
    "logs/bot-events.jsonl",
]
HEARTBEAT_LOGS = ["logs/bot.log"]
TIMELINE = "live_100_700_timeline.csv"


def fmt(ts_ms, tz=KYIV):
    return datetime.fromtimestamp(ts_ms / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=1.0)
    return p.parse_args()


def main():
    args = parse_args()
    now_utc = datetime.now(UTC)
    now_kyiv = now_utc.astimezone(KYIV)
    cut_utc = now_utc - timedelta(hours=args.hours)
    cut_ms = int(cut_utc.timestamp() * 1000)

    print(f"Сейчас: {now_kyiv.strftime('%Y-%m-%d %H:%M:%S %Z')} (Kyiv) = "
          f"{now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Период: последние {args.hours:g} ч. "
          f"(с {cut_utc.astimezone(KYIV).strftime('%H:%M')} Kyiv)")

    # --- heartbeat / свежесть логов ---
    print("\n--- Пульс процессов ---")
    for path in HEARTBEAT_LOGS + [TIMELINE, "logs/screener-events.jsonl"]:
        if not os.path.exists(path):
            print(f"{path}: нет файла")
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
        age_min = (now_utc - mtime).total_seconds() / 60
        mkyiv = mtime.astimezone(KYIV).strftime("%H:%M:%S")
        flag = "OK" if age_min <= 5 else "СТОИТ"
        print(f"{path}: mtime {mkyiv} Kyiv ({age_min:.0f} мин назад) [{flag}]")

    # --- события из jsonl ---
    print(f"\n--- События за {args.hours:g} ч ---")
    total_pnl = 0.0
    n_closed = 0
    for path in EVENT_FILES:
        if not os.path.exists(path):
            print(f"{path}: нет файла")
            continue
        kinds = Counter()
        n_hour = 0
        closes = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ts = e.get("ts", 0) or 0
                    try:
                        ts = int(ts)
                    except Exception:
                        continue
                    # ts в мс; если в секундах — привести
                    if 0 < ts < 10**11:
                        ts = ts * 1000
                    if ts >= cut_ms:
                        n_hour += 1
                        kinds[e.get("kind", "?")] += 1
                        if e.get("kind") == "cycle_closed":
                            closes.append(e)
        except Exception as ex:
            print(f"{path}: ошибка чтения: {ex}")
            continue
        print(f"{path}: {n_hour} событий за период | {dict(kinds) if kinds else '-'}")
        for c in closes[-10:]:
            pnl = c.get("pnl", 0)
            try:
                total_pnl += float(pnl)
            except Exception:
                pass
            n_closed += 1
            print(f"    {fmt(c.get('ts', 0))} {c.get('symbol')} "
                  f"{c.get('exit_reason')} pnl={pnl}")

    # --- сигналы скринера отдельно (signal_*) ---
    print("\n--- Сигналы скринера за период ---")
    sig_path = "logs/screener-events.jsonl"
    if os.path.exists(sig_path):
        n_sig = 0
        with open(sig_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                k = e.get("kind", "")
                if k.startswith("signal") and int(e.get("ts", 0) or 0) >= cut_ms:
                    n_sig += 1
                    sig = e.get("signal", e)
                    print(f"    {fmt(e.get('ts', 0))} {k} "
                          f"{sig.get('symbol', e.get('symbol', '?'))}")
        if n_sig == 0:
            print("    сигналов не было (только rejected_snapshot)")

    # --- пульс timeline ---
    if os.path.exists(TIMELINE):
        try:
            with open(TIMELINE, newline="") as f:
                rows = list(csv.DictReader(f))
            print(f"\nTimeline {TIMELINE}: строк {len(rows)}, "
                  f"последняя: {rows[-1].get('datetime')} "
                  f"n_signals={rows[-1].get('n_signals')}")
        except Exception as ex:
            print(f"\nTimeline: ошибка {ex}")

    if n_closed:
        print(f"\nИтог: закрыто циклов {n_closed}, суммарный PnL {total_pnl:+.4f}")
    else:
        print("\nИтог: закрытых сделок за период — 0")


if __name__ == "__main__":
    sys.exit(main())
