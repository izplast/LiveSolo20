"""Point 3: seed 1m×60 / 15m×40 и минимумы истории (пункт ревью 12):
1m >= max(natr_period+1, 20, uhlo_length+2), 15m >= uhlo_length_15m+2.
Символы с недостаточной историей не оцениваются; счётчик логгируется.
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls
from tools.screener_common import SEED_LIMIT_1M, SEED_LIMIT_15M, min_history_1m, min_history_15m


def test_min_history_formulas():
    assert min_history_1m(14, 15) == max(15, 20, 17) == 20
    assert min_history_1m(14, 50) == 52
    assert min_history_1m(2, 15) == 20
    assert min_history_15m(20) == 22
    assert min_history_15m(15) == 17
    print("test_min_history_formulas OK")


async def _run_seed(monkeypatch_limit):
    calls = []

    def fake(base, symbol, interval, limit=30):
        calls.append((symbol, interval, limit))
        rows = [[i * 1000, 1.0, 2.0, 0.5, 1.0, 100.0] for i in range(monkeypatch_limit.get(symbol, 10))]
        return rows

    ls.fetch_klines_rest = fake  # type: ignore
    s = ls.MidcapScreener(dry_run=False)
    s.symbols = ["AAAUSDT", "BBBUSDT"]
    s.rank_map = {"AAAUSDT": 100, "BBBUSDT": 101}
    await s.seed_history()
    return s, calls


def test_seed_limits_and_insufficient_counter():
    s, calls = asyncio.run(_run_seed({"AAAUSDT": 10, "BBBUSDT": 60}))
    # сид тянет 1m×60 и 15m×40
    assert ("AAAUSDT", "1", SEED_LIMIT_1M) in calls, calls
    assert ("AAAUSDT", "15", SEED_LIMIT_15M) in calls, calls
    assert ("BBBUSDT", "1", SEED_LIMIT_1M) in calls, calls
    assert ("BBBUSDT", "15", SEED_LIMIT_15M) in calls, calls
    # 10 баров < минимума -> символ не оценивается, счётчик++
    sig, _, _, reason = s._evaluate_symbol("AAAUSDT")
    assert sig is None and reason == "no_history", (sig, reason)
    assert s._insufficient_history["AAAUSDT"] == 1
    # 60 баров достаточно для проверки истории (отказ будет по другим причинам, а не no_history)
    sig2, _, _, reason2 = s._evaluate_symbol("BBBUSDT")
    assert reason2 != "no_history", reason2
    assert s._insufficient_history["BBBUSDT"] == 0
    print("test_seed_limits_and_insufficient_counter OK")


if __name__ == "__main__":
    test_min_history_formulas()
    test_seed_limits_and_insufficient_counter()
    print("POINT 3 OK")
