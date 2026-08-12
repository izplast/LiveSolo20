"""
reference/metrics.py — периодический контроль результатов теста.

Снимает снапшот состояния скринера (окна свечей в памяти, активные сигналы)
и симулятора (открытые DCA-циклы, закрытые сделки), пишет одну строку в CSV
(logs/test_metrics.csv по умолчанию) и выводит краткую сводку в консоль.
Интервал по умолчанию — 30 минут, задаётся при создании монитора.

CSV-строка — плоская таблица (CSV_COLUMNS): агрегаты скалярными колонками,
списки активных сигналов и закрытых сделок — JSON-колонками (по сделке хранятся
время открытия/закрытия, длительность, причина выхода и PnL).

Модуль не зависит от сети: снапшоты строятся по duck-typed объектам
(screener.states / DcaCycle), поэтому проверяется оффлайн тестом.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_metrics.py
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с metrics.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sc = _load_sibling("screener")
evaluate = sc.evaluate
color_to_side = sc.color_to_side

CSV_COLUMNS = [
    "ts", "interval_min",
    "universe_size", "processed_coins", "active_signals_json",
    "open_cycles", "open_so_count", "open_avg_entry", "open_unrealized_usdt",
    "open_unrealized_pct",
    "closed_trades", "wins", "win_rate_pct", "avg_duration_min",
    "realized_pnl_usdt", "realized_pnl_pct", "fees_usdt", "closed_trades_json",
]

DEFAULT_CSV = os.path.abspath(os.path.join(_DIR, "..", "..", "..",
                                           "logs", "test_metrics.csv"))


def _uhlo_rows(uhlo: dict | None) -> dict:
    return {"highs": round(uhlo["highs"], 2), "lows": round(uhlo["lows"], 2)} if uhlo else None


def screener_metrics(symbols, states, cfg) -> dict:
    """Снапшот скринера: обработанные монеты и активные сигналы.

    Активный сигнал — символ, цвет которого не сброшен (last_color != none).
    NATR и UHLO берутся из evaluate по текущим окнам в памяти.
    """
    processed = sum(1 for st in states.values() if st.fast)
    active = []
    for sym in symbols:
        st = states.get(sym)
        if st is None or st.last_color == "none":
            continue
        d = evaluate(list(st.fast), list(st.slow), cfg)
        active.append({
            "symbol": sym,
            "side": color_to_side(st.last_color),
            "natr": round(d.natr, 4) if d.natr is not None else None,
            "uhlo_1m": _uhlo_rows(d.uhlo_fast),
            "uhlo_15m": _uhlo_rows(d.uhlo_slow),
        })
    active.sort(key=lambda a: a["symbol"])
    return {"universe_size": len(symbols), "processed_coins": processed,
            "active_signals": active}


def cycle_metrics(cycles, mark_price=None) -> dict:
    """Снапшот симулятора: открытые циклы и закрытые сделки.

    mark_price(c) — текущая цена для нереализованного PnL открытого цикла
    (None — PnL по открытым не считается). PnL % — к сумме вложенного
    (avg_entry * qty).
    """
    open_cycles = []
    closed = []
    for c in cycles:
        invested = c.avg_entry * c.qty if c.qty else 0.0
        if not getattr(c, "closed", False):
            mark = mark_price(c) if mark_price else None
            unreal_usdt = unreal_pct = 0.0
            if mark and invested > 0:
                if c.side == "Buy":
                    unreal_usdt = (mark - c.avg_entry) * c.qty
                else:
                    unreal_usdt = (c.avg_entry - mark) * c.qty
                unreal_pct = unreal_usdt / invested * 100
            open_cycles.append({
                "symbol": c.symbol, "side": c.side, "docups": c.docups,
                "avg_entry": round(c.avg_entry, 6), "open_ts": c.open_ts,
                "tp_level": round(c.tp_level, 6) if c.tp_level else None,
                "unrealized_usdt": round(unreal_usdt, 6),
                "unrealized_pct": round(unreal_pct, 4),
            })
        else:
            closed.append({
                "symbol": c.symbol, "side": c.side, "open_ts": c.open_ts,
                "close_ts": c.exit_ts,
                "duration_minutes": round(c.duration_minutes, 2),
                "exit_reason": c.exit_reason,
                "trend": getattr(c, "trend", ""),
                "entry_natr": getattr(c, "natr", None),
                "pnl_usdt": round(c.pnl, 6),
                "pnl_pct": round(c.pnl / invested * 100, 4) if invested > 0 else 0.0,
                "fees_usdt": round(c.fee, 6),
            })
    closed.sort(key=lambda t: t["close_ts"])
    wins = sum(1 for t in closed if t["pnl_usdt"] > 0)
    realized = sum(t["pnl_usdt"] for t in closed)
    fees = sum(t["fees_usdt"] for t in closed)
    total_invested = sum(c.avg_entry * c.qty for c in cycles if getattr(c, "closed", False))
    return {
        "open_cycles": open_cycles,
        "open_so_count": sum(c["docups"] for c in open_cycles),
        "open_avg_entry": (round(sum(c["avg_entry"] for c in open_cycles)
                                 / len(open_cycles), 6) if open_cycles else 0.0),
        "open_unrealized_usdt": sum(c["unrealized_usdt"] for c in open_cycles),
        "open_unrealized_pct": (sum(c["unrealized_pct"] for c in open_cycles)
                                if open_cycles else 0.0),
        "closed_trades": len(closed),
        "closed_trades_list": closed,
        "wins": wins,
        "win_rate_pct": round(wins / len(closed) * 100, 2) if closed else 0.0,
        "avg_duration_min": (round(sum(t["duration_minutes"] for t in closed)
                                   / len(closed), 2) if closed else 0.0),
        "realized_pnl_usdt": round(realized, 6),
        "realized_pnl_pct": round(realized / total_invested * 100, 4) if total_invested else 0.0,
        "fees_usdt": round(fees, 6),
    }


def metrics_row(sm: dict, cm: dict, ts_ms: int, interval_min: float) -> list:
    """Снапшоты → одна строка CSV (порядок — CSV_COLUMNS)."""
    return [
        ts_ms,
        interval_min,
        sm["universe_size"],
        sm["processed_coins"],
        json.dumps(sm["active_signals"], ensure_ascii=False),
        len(cm["open_cycles"]),
        cm["open_so_count"],
        cm["open_avg_entry"],
        cm["open_unrealized_usdt"],
        cm["open_unrealized_pct"],
        cm["closed_trades"],
        cm["wins"],
        cm["win_rate_pct"],
        cm["avg_duration_min"],
        cm["realized_pnl_usdt"],
        cm["realized_pnl_pct"],
        cm["fees_usdt"],
        json.dumps(cm["closed_trades_list"], ensure_ascii=False),
    ]


def append_csv(path: str, row: list) -> None:
    """Дописывает строку; при отсутствии/пустом файле пишет заголовок."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(CSV_COLUMNS)
        w.writerow(row)


def console_summary(sm: dict, cm: dict, ts_ms: int) -> str:
    """Краткая сводка для консоли (одна строка-шапка + сигналы + сделки)."""
    t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts_ms / 1000))
    lines = [f"[metrics] {t} — скринер: {sm['processed_coins']}/{sm['universe_size']} монет, "
             f"активных сигналов {len(sm['active_signals'])}; "
             f"симулятор: открыто {len(cm['open_cycles'])}, закрыто {cm['closed_trades']} "
             f"(WR {cm['win_rate_pct']}%, avg {cm['avg_duration_min']} мин, "
             f"PnL {cm['realized_pnl_usdt']:+.4f} USDT / {cm['realized_pnl_pct']:+.2f}%)"]
    for a in sm["active_signals"]:
        lines.append(f"  сигнал {a['symbol']:12s} {a['side']:4s} "
                     f"NATR={a['natr']} UHLO1m={a['uhlo_1m']} UHLO15m={a['uhlo_15m']}")
    for c in cm["open_cycles"]:
        lines.append(f"  открыт {c['symbol']:12s} {c['side']:4s} SO={c['docups']} "
                     f"avg={c['avg_entry']} unreal={c['unrealized_usdt']:+.4f} USDT")
    for t in cm["closed_trades_list"]:
        o = time.strftime("%m-%d %H:%M", time.gmtime(t["open_ts"] / 1000))
        cl = time.strftime("%m-%d %H:%M", time.gmtime(t["close_ts"] / 1000))
        trend = f" ({t.get('trend', '')})" if t.get("trend") else ""
        lines.append(f"  закрыта {t['symbol']:12s} {t['side']:4s}{trend} {o}→{cl} "
                     f"({t['duration_minutes']} мин, {t['exit_reason']}) "
                     f"PnL={t['pnl_usdt']:+.4f} USDT")
    return "\n".join(lines)


class MetricsMonitor:
    """Периодический контроль: снапшот → CSV + сводка каждые interval_min."""

    def __init__(self, screener_getter, cycles_getter, csv_path: str = DEFAULT_CSV,
                 interval_min: float = 30.0, mark_price=None):
        self.screener_getter = screener_getter   # callable → (symbols, states, cfg) | None
        self.cycles_getter = cycles_getter        # callable → список циклов
        self.csv_path = csv_path
        self.interval_min = interval_min
        self.mark_price = mark_price
        self.last_row_ts = 0

    def snapshot(self) -> tuple[dict, dict]:
        src = self.screener_getter()
        sm = screener_metrics(src[0], src[1], src[2]) if src else {
            "universe_size": 0, "processed_coins": 0, "active_signals": []}
        cm = cycle_metrics(self.cycles_getter(), self.mark_price)
        return sm, cm

    def snapshot_and_report(self) -> dict:
        sm, cm = self.snapshot()
        row = metrics_row(sm, cm, int(time.time() * 1000), self.interval_min)
        append_csv(self.csv_path, row)
        print(console_summary(sm, cm, row[0]), flush=True)
        return {"screener": sm, "cycles": cm}

    async def run(self, stop: asyncio.Event | None = None) -> None:
        while True:
            await asyncio.sleep(self.interval_min * 60)
            self.snapshot_and_report()
            if stop is not None and stop.is_set():
                return
