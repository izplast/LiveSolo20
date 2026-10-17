"""Point 1: stale-watchdog — выход код 1 при absence confirm=kline.1 > STALE_EXIT_SEC.
Проверка в Timeline-таймере, не в WS-цикле.
"""
import sys
import json
import time
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls
from tools.screener_common import STALE_EXIT_SEC, is_stale

TMP = Path("/tmp/opencode/screener-test-stale")
TMP.mkdir(parents=True, exist_ok=True)


def test_is_stale_bounds():
    now = 1_000_000_000_000
    assert is_stale(None, now, STALE_EXIT_SEC) is True
    assert is_stale(now - STALE_EXIT_SEC * 1000, now, STALE_EXIT_SEC) is False
    assert is_stale(now - (STALE_EXIT_SEC + 1) * 1000, now, STALE_EXIT_SEC) is True
    print("test_is_stale_bounds OK")


def test_timeline_stale_exit():
    s = ls.MidcapScreener(dry_run=True)
    s.symbols = []
    s.rank_map = {}
    s.interval_sec = 1
    jpath = TMP / "stale-events.jsonl"
    tpath = TMP / "stale-timeline.csv"
    s.journal = ls.Journal(str(jpath))
    s.timeline_logger = ls.TimelineLogger(str(tpath))
    # последняя confirm=kline.1 старше STALE_EXIT_SEC
    s._last_kline1_confirm_ms = int(time.time() * 1000) - (STALE_EXIT_SEC + 60) * 1000
    s._exit_code = 0
    s._stop.clear()

    asyncio.run(s._timeline_loop(end_ts=time.time() + 30))

    assert s._exit_code == 1, s._exit_code
    assert s._fatal_reason == "ws_stale_exit", s._fatal_reason
    assert s._stop.is_set()
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(l).get("kind") for l in lines]
    assert "ws_stale_exit" in kinds, kinds
    print("test_timeline_stale_exit OK")


if __name__ == "__main__":
    test_is_stale_bounds()
    test_timeline_stale_exit()
    print("POINT 1 OK")
