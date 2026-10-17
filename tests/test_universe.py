"""Point 4: вселенная — префиксы 1000/10000/1000000 -> базовый токен
(1000PEPEUSDT матчится с rank PEPE), исключение токенизированных акций
(symbolType == "stock" по Bybit instruments-info), дельта пула.
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls

NAMED_RANKS = {"PEPE": 2224, "BONK": 153, "STRK": 100, "PEAQ": 900, "BTC": 1}


def fake_cg_ranked(limit: int = 600):
    rows = [{"symbol": f"FILL{i}", "market_cap_rank": i} for i in range(1, 2501)]
    for sym, rank in NAMED_RANKS.items():
        rows[rank - 1] = {"symbol": sym, "market_cap_rank": rank}
    return rows[:limit]


def fake_instruments():
    out = {}
    def add(symbol, base_coin, symbol_type="", status="Trading"):
        out[symbol] = {
            "symbol": symbol, "contractType": "LinearPerpetual", "status": status,
            "baseCoin": base_coin, "quoteCoin": "USDT", "symbolType": symbol_type,
            "leverageFilter": {"maxLeverage": "10"},
        }
    add("1000PEPEUSDT", "1000PEPE")
    add("1000BONKUSDT", "1000BONK")
    add("PEPEUSDT", "PEPE")  # спот-тикер на linear не существует, но на случай
    add("PLTRUSDT", "PLTR", "stock")
    add("MSFTUSDT", "MSFT", "stock")
    add("VUSDT", "V", "stock")
    add("SNDKUSDT", "SNDK", "stock")
    add("STRKUSDT", "STRK")
    add("PEAQUSDT", "PEAQ")
    add("BTCUSDT", "BTC")  # ранг 1 — вне среза 100-2500
    return out


def test_normalize_prefix():
    assert ls.normalize_bybit_base("1000PEPEUSDT") == ("PEPE", "1000PEPE")
    assert ls.normalize_bybit_base("10000PEPEUSDT") == ("PEPE", "10000PEPE")
    assert ls.normalize_bybit_base("1000000PEPEUSDT") == ("PEPE", "1000000PEPE")
    assert ls.normalize_bybit_base("PEPEUSDT") == ("PEPE", "PEPE")
    print("test_normalize_prefix OK")


def test_universe_prefix_and_stocks(monkeypatch_log=None):
    ls.fetch_coingecko_ranked = fake_cg_ranked  # type: ignore
    ls.fetch_bybit_instruments = fake_instruments  # type: ignore
    messages = []
    ls.log = lambda m: messages.append(m)  # type: ignore
    symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=None)

    syms = set(symbols)
    assert "1000PEPEUSDT" in syms, syms
    assert rank_map["1000PEPEUSDT"] == 2224, rank_map.get("1000PEPEUSDT")
    assert "1000BONKUSDT" in syms and rank_map["1000BONKUSDT"] == 153
    for stock in ("PLTRUSDT", "MSFTUSDT", "VUSDT", "SNDKUSDT"):
        assert stock not in syms, stock
    print("test_universe_prefix_and_stocks OK:", len(symbols), "символов")


def test_universe_delta_logged(tmp_path="/tmp/opencode/universe-test-cache.json"):
    ls.fetch_coingecko_ranked = fake_cg_ranked  # type: ignore
    ls.fetch_bybit_instruments = fake_instruments  # type: ignore
    Path(tmp_path).write_text(
        json.dumps({"symbols": ["PLTRUSDT", "MSFTUSDT", "VUSDT", "STRKUSDT", "OLDCOINUSDT"], "rank_map": {}}),
        encoding="utf-8",
    )
    messages = []
    ls.log = lambda m: messages.append(m)  # type: ignore
    ls.build_midcap_universe(100, 2500, save_cache=tmp_path)
    text = "\n".join(messages)
    delta_lines = [m for m in messages if "Вселенная пула:" in m]
    assert delta_lines, messages
    assert "1000PEPEUSDT" in "\n".join(messages) or any("добавлено" in m for m in messages)
    print("test_universe_delta_logged OK:")
    for m in delta_lines:
        print("   ", m)


if __name__ == "__main__":
    test_normalize_prefix()
    test_universe_prefix_and_stocks()
    test_universe_delta_logged()
    print("POINT 4 OK")
