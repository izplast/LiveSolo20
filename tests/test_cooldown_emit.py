"""Регрессия кулдауна: _evaluate_symbol не ставит last_signal_ts.

Снапшот символа (timeline) не должен «съедать» окно кулдауна: после него
WS-confirm той же минуты обязан отправить сигнал. Повторный confirm в течение
cooldown_sec (300 с) — уже cooldown, без нового сигнала.
"""
import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls


class FakeCsv:
    def __init__(self):
        self.rows = []

    def write(self, sig):
        self.rows.append(sig)


class FakeJournal:
    def __init__(self):
        self.events = []

    def write(self, kind, **kwargs):
        self.events.append((kind, kwargs))


def _fill_caches(s, sym, last_1m_start_ms):
    dq = deque(maxlen=50)
    for i in range(30):
        start = last_1m_start_ms - (29 - i) * 60_000
        dq.append([start, 1.0, 1.01, 0.99, 1.0, 100.0])
    s.klines_cache[sym] = dq

    last_15m_start = (last_1m_start_ms // 900_000) * 900_000 - 900_000
    dq15 = deque(maxlen=50)
    for i in range(25):
        start = last_15m_start - (24 - i) * 900_000
        dq15.append([start, 1.0, 1.01, 0.99, 1.0, 100.0])
    s.klines_cache_15m[sym] = dq15


def _confirm_msg(sym, start_ms):
    return json.dumps({
        "topic": f"kline.1.{sym}",
        "data": [{
            "start": str(start_ms), "open": "1.0", "high": "1.01",
            "low": "0.99", "close": "1.0", "volume": "100", "confirm": True,
        }],
    })


async def _run_case():
    sym = "AAAUSDT"
    now_ms = int(time.time() * 1000)
    last_1m = (now_ms // 60_000) * 60_000 - 60_000

    s = ls.MidcapScreener(dry_run=True, cooldown_sec=300, vol_mult=1.5,
                          natr_min=0.9, natr_max=1.5)
    s.symbols = [sym]
    s.rank_map = {sym: 100}
    s.csv_logger = FakeCsv()
    s.journal = FakeJournal()
    _fill_caches(s, sym, last_1m)

    # Детерминированный «проходной» сигнал: проверяем именно кулдаун/эмит,
    # а не математику индикаторов.
    saved = {
        "volume_ratio": ls.volume_ratio,
        "compute_natr": ls.compute_natr,
        "compute_uhlo_last3": ls.compute_uhlo_last3,
        "compute_uhlo": ls.compute_uhlo,
        "classify_color": ls.classify_color,
        "fast_uhlo_corner": ls.fast_uhlo_corner,
        "HAS_FACTOR_GATES": ls.HAS_FACTOR_GATES,
    }
    ls.volume_ratio = lambda klines: 2.0
    ls.compute_natr = lambda klines, period=14: 1.2
    ls.compute_uhlo_last3 = lambda klines, length: (
        {"highs": 50, "lows": 50}, {"highs": 50, "lows": 50},
        {"highs": 50, "lows": 50},
    )
    ls.compute_uhlo = lambda klines, length: {"highs": 50, "lows": 50}
    ls.classify_color = lambda uhlo1, uhlo15: "green"
    ls.fast_uhlo_corner = lambda uhlo1: False
    ls.HAS_FACTOR_GATES = False
    try:
        # 1) Снапшот оценивает символ, но не должен ставить кулдаун.
        s._snapshot_timeline_state()
        assert s.last_signal_ts == {}, s.last_signal_ts

        # 2) WS-confirm той же минуты — сигнал отправлен.
        await s._handle_ws_message(_confirm_msg(sym, last_1m))
        assert len(s.signals) == 1, s.signals
        assert s.last_signal_ts.get(sym) == last_1m, s.last_signal_ts
        assert s.csv_logger.rows and s.csv_logger.rows[0].symbol == sym
        assert any(k == "signal_sent" for k, _ in s.journal.events)

        # 3) Повторный confirm в течение 300 с — cooldown, нового сигнала нет.
        await s._handle_ws_message(_confirm_msg(sym, last_1m))
        assert len(s.signals) == 1, s.signals
        sig, _, _, reason = s._evaluate_symbol(sym)
        assert sig is None and reason == "cooldown", (sig, reason)
    finally:
        ls.volume_ratio = saved["volume_ratio"]
        ls.compute_natr = saved["compute_natr"]
        ls.compute_uhlo_last3 = saved["compute_uhlo_last3"]
        ls.compute_uhlo = saved["compute_uhlo"]
        ls.classify_color = saved["classify_color"]
        ls.fast_uhlo_corner = saved["fast_uhlo_corner"]
        ls.HAS_FACTOR_GATES = saved["HAS_FACTOR_GATES"]
    print("test_cooldown_after_snapshot_and_ws_confirm OK")


if __name__ == "__main__":
    asyncio.run(_run_case())
    print("COOLDOWN OK")
