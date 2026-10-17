"""Point 2: бэкофф реконнекта WS — max 60с между попытками; после
WS_RECONNECT_MAX_ATTEMPTS (5) неудач подряд — exit(1) + ws_reconnect_exhausted.
"""
import sys
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.screener_common import ws_backoff_delay as _real_backoff

import live_screener_midcap as ls
from tools.screener_common import (
    WS_RECONNECT_MAX_ATTEMPTS, WS_RECONNECT_MAX_DELAY, ws_backoff_delay,
)

TMP = Path("/tmp/opencode/screener-test-reconnect")
TMP.mkdir(parents=True, exist_ok=True)


def _fail_connect(*args, **kwargs):
    raise RuntimeError("no network")


def test_backoff_bounds():
    d1 = ws_backoff_delay(1)
    d2 = ws_backoff_delay(2)
    dmax = ws_backoff_delay(100)
    assert 0.7 <= d1 <= 1.3, d1
    assert d1 < d2, (d1, d2)
    assert dmax <= WS_RECONNECT_MAX_DELAY, dmax
    print("test_backoff_bounds OK")


async def _run_exhausted():
    s = ls.MidcapScreener(dry_run=True)
    s.symbols = []
    s.rank_map = {}
    s.ws_url = ls.BYBIT_WS_PUBLIC
    s._exit_code = 0
    s._fatal_reason = None
    jpath = TMP / "reconnect-events.jsonl"
    s.journal = ls.Journal(str(jpath))

    ls.websockets.connect = _fail_connect  # type: ignore
    ls.ws_backoff_delay = lambda attempt: 0.005  # type: ignore
    try:
        await s._ws_loop()
    finally:
        ls.ws_backoff_delay = _real_backoff  # type: ignore
    return s, jpath


def test_reconnect_exhausted():
    s, jpath = asyncio.run(_run_exhausted())
    assert s._exit_code == 1, s._exit_code
    assert s._fatal_reason == "ws_reconnect_exhausted", s._fatal_reason
    assert s._reconnect_attempts == WS_RECONNECT_MAX_ATTEMPTS, s._reconnect_attempts
    assert s._stop.is_set()
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(l).get("kind") for l in lines]
    assert "ws_reconnect_exhausted" in kinds, kinds
    print("test_reconnect_exhausted OK")


if __name__ == "__main__":
    test_backoff_bounds()
    test_reconnect_exhausted()
    print("POINT 2 OK")
