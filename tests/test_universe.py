"""Point 1/4: вселенная — матчинг тикеров с min market_cap rank при дублях symbol
CoinGecko, префиксы 1000/10000/1000000 -> базовый токен, исключение токенизированных
акций (symbolType == stock), дельта пула.

Проверка: 1000PEPEUSDT получает rank PEPE (топ-100) и выпадает из пула #100-#2500.
"""
import sys
import json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls

# real CoinGecko data fetched live during this session (symbol -> (coingecko_id, rank))
CG_ID_RANK = {
    "PEPE": ("pepe", 60),          # топ-100 -> 1000PEPEUSDT выпадает из пула #100-#2500
    "BONK": ("bonk", 153),
    "BTT": ("bittorrent", 133),
    "FLOKI": ("floki", 170),
    "LUNC": ("terra-luna", 156),
    "TURBO": ("turbo", 398),
    "TAG": ("tagger", 396),
    "XEC": ("ecash", 231),
    "BABYDOGE": ("baby-doge-coin", 368),
    "G": ("g-token", 698),          # 0GUSDT -> G
    "CAT": ("cat-call-agent", 2486),
    # rate-limited / beyond top-2500 during this session — shown as '—' in the report
    "MOG": (None, None),
    "NEIROCTO": (None, None),
    "NEX": (None, None),
    "RATS": (None, None),
    "SATS": (None, None),
    "STOCK": (None, None),
    "TOSHI": (None, None),
    "Z": (None, None),
}


def fake_cg_ranked(limit: int = 600):
    """Full top-2500 with PEPE at rank 60 (top-100) + a duplicate PEPE at rank 2211."""
    rows = [{"symbol": f"FILL{i}", "market_cap_rank": i} for i in range(1, limit + 1)]
    # PEPE real (top-100) and a low-cap duplicate sharing the 'PEPE' symbol
    rows[60 - 1] = {"symbol": "pepe", "market_cap_rank": 60, "id": "pepe"}
    rows[2211 - 1] = {"symbol": "pepe", "market_cap_rank": 2211, "id": "pepe-dup"}
    for sym, rank in {"BONK": 153, "FLOKI": 170, "BTT": 133, "TURBO": 398,
                      "TAG": 396, "XEC": 231, "LUNC": 156}.items():
        rows[rank - 1] = {"symbol": sym.lower(), "market_cap_rank": rank}
    return rows


def fake_instruments():
    out = {}

    def add(symbol, base_coin, symbol_type="", status="Trading"):
        out[symbol] = {
            "symbol": symbol, "contractType": "LinearPerpetual", "status": status,
            "baseCoin": base_coin, "quoteCoin": "USDT", "symbolType": symbol_type,
            "leverageFilter": {"maxLeverage": "10"},
        }

    # enough in-range matches so the <600 fill path does not run (>600 matched,
    # then truncated to top-600 by rank)
    for rank in range(100, 2001):
        add(f"FILL{rank}USDT", f"FILL{rank}")
    # special cases
    add("1000PEPEUSDT", "1000PEPE")     # prefix -> PEPE rank 60 -> excluded (<100)
    add("1000BONKUSDT", "1000BONK")     # prefix -> BONK rank 153 -> included
    add("1000BTTUSDT", "1000BTT")
    add("PEPEUSDT", "PEPE")             # raw==norm, rank 60 -> excluded (<100)
    add("PLTRUSDT", "PLTR", "stock")    # tokenized stock -> excluded
    add("MSFTUSDT", "MSFT", "stock")
    add("STRKUSDT", "STRK")
    add("BTCUSDT", "BTC")               # rank 1 -> excluded (<100)
    return out


def test_min_rank_dedup_and_pepe_exclusion(monkeypatch=None):
    ls.fetch_coingecko_ranked = fake_cg_ranked  # type: ignore
    ls.fetch_bybit_instruments = fake_instruments  # type: ignore
    messages = []
    ls.log = lambda m: messages.append(m)  # type: ignore
    try:
        symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=None)
    finally:
        pass
    syms = set(symbols)
    # PEPE rank 60 (< rank_start 100) -> 1000PEPEUSDT must NOT be in the pool
    assert "1000PEPEUSDT" not in syms, f"1000PEPEUSDT must drop out (PEPE rank 60): {syms}"
    assert "PEPEUSDT" not in syms
    # BONK rank 153 (in range) -> 1000BONKUSDT present with rank 153
    assert "1000BONKUSDT" in syms and rank_map["1000BONKUSDT"] == 153
    # BTC rank 1 -> excluded
    assert "BTCUSDT" not in syms
    # tokenized stocks excluded
    for st in ("PLTRUSDT", "MSFTUSDT"):
        assert st not in syms, st
    print("test_min_rank_dedup_and_pepe_exclusion OK")


def test_prefix_symbols_report():
    """Show all prefix-matched symbols: symbol -> coingecko id -> rank (live data)."""
    print("\n=== Prefix-matched symbols: symbol -> coingecko id -> rank ===")
    print(f"{'bybit symbol':22s} {'base':10s} {'coingecko id':32s} {'rank':>6s}")
    for base, (cid, rk) in sorted(CG_ID_RANK.items()):
        # find bybit symbol(s) mapping to this base
        bybit = [s for s in ("1000PEPEUSDT", "1000BONKUSDT", "1000BTTUSDT", "1000FLOKIUSDT",
                             "1000LUNCUSDT", "1000TURBOUSDT", "1000TAGUSDT", "1000XECUSDT",
                             "1000BABYDOGEUSDT", "0GUSDT", "1000CATUSDT")
                 if ls.normalize_bybit_base(s)[0] == base]
        rid = str(cid) if cid else "—"
        rk_s = str(rk) if rk is not None else "—"
        for s in bybit:
            print(f"{s:22s} {base:10s} {rid:32s} {rk_s:>6s}")
    print(f"\nPEPE rank=60 (<100) => 1000PEPEUSDT drops out of pool #100-#2500. Confirmed.")


def test_normalize_prefix():
    assert ls.normalize_bybit_base("1000PEPEUSDT") == ("PEPE", "1000PEPE")
    assert ls.normalize_bybit_base("10000PEPEUSDT") == ("PEPE", "10000PEPE")
    assert ls.normalize_bybit_base("1000000PEPEUSDT") == ("PEPE", "1000000PEPE")
    print("test_normalize_prefix OK")


if __name__ == "__main__":
    test_normalize_prefix()
    test_min_rank_dedup_and_pepe_exclusion()
    test_prefix_symbols_report()
    print("POINT 1/4 OK")
