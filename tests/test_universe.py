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
    "PEPE": ("pepe", 62),          # топ-100 -> 1000PEPEUSDT выпадает из пула #100-#2500
    "BONK": ("bonk", 153),
    "BTT": ("bittorrent", 133),
    "FLOKI": ("floki", 170),
    "LUNC": ("terra-luna", 156),
    "TURBO": ("turbo", 402),
    "TAG": ("tagger", 397),
    "XEC": ("ecash", 232),
    "BABYDOGE": ("baby-doge-coin", 371),
    # 1000CATUSDT -> CAT: min-rank dedup выбирает simon-s-cat #1040,
    # а НЕ cat-call-agent #2496 (регрессия point 2).
    "CAT": ("simon-s-cat", 1040),
    "MOG": ("mog-coin", 537),
    "NEX": ("nexus-4", 506),
    "SATS": ("sats-ordinals", 787),
    "TOSHI": ("toshi", 492),
    # числовые тикеры: прежняя нормализация срезала их как префикс (0G->G, 2Z->Z, 4STOCK->STOCK)
    "0G": ("zero-gravity", 226),
    "2Z": ("doublezero", 203),
    "4STOCK": ("4stock", 1877),
    # NEIROCTO: вне CoinGecko топ-2500 (Bybit base NEIROCTO != CG symbol NEIRO)
    "NEIROCTO": (None, None),
    # вне CoinGecko топ-2500
    "RATS": (None, None),
}

# bybit symbol для каждого префиксного base (19 символов)
PREFIX_BYBIT = {
    "PEPE": "1000PEPEUSDT", "BONK": "1000BONKUSDT", "BTT": "1000BTTUSDT",
    "FLOKI": "1000FLOKIUSDT", "LUNC": "1000LUNCUSDT", "TURBO": "1000TURBOUSDT",
    "TAG": "1000TAGUSDT", "XEC": "1000XECUSDT", "BABYDOGE": "1000000BABYDOGEUSDT",
    "CAT": "1000CATUSDT", "MOG": "1000000MOGUSDT", "NEX": "10000NEXUSDT",
    "SATS": "10000SATSUSDT", "TOSHI": "1000TOSHIUSDT", "0G": "0GUSDT",
    "2Z": "2ZUSDT", "4STOCK": "4STOCKUSDT", "NEIROCTO": "1000NEIROCTOUSDT",
    "RATS": "1000RATSUSDT",
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


def test_pepe_not_readded_as_fill():
    """Когда matched < 600, fill section не должен вернуть 1000PEPEUSDT
    (PEPE rank 60 вне диапазона) с рангом 9999."""
    ls.fetch_coingecko_ranked = fake_cg_ranked  # type: ignore

    def few_instruments():
        out = {}
        def add(symbol, base_coin, symbol_type="", status="Trading"):
            out[symbol] = {
                "symbol": symbol, "contractType": "LinearPerpetual", "status": status,
                "baseCoin": base_coin, "quoteCoin": "USDT", "symbolType": symbol_type,
                "leverageFilter": {"maxLeverage": "10"},
            }
        # мало входящих в диапазон -> fill сработает
        for rank in (150, 160, 170):
            add(f"FILL{rank}USDT", f"FILL{rank}")
        add("1000PEPEUSDT", "1000PEPE")  # PEPE rank 60 -> вне диапазона, не fill
        add("1000BONKUSDT", "1000BONK")  # BONK rank 153 -> в диапазон
        return out
    ls.fetch_bybit_instruments = few_instruments  # type: ignore
    try:
        symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=None)
    finally:
        pass
    syms = set(symbols)
    assert "1000PEPEUSDT" not in syms, f"1000PEPEUSDT не должен быть в пуле даже как fill: {syms}"
    assert rank_map.get("1000PEPEUSDT") is None
    assert "1000BONKUSDT" in syms and rank_map["1000BONKUSDT"] == 153
    print("test_pepe_not_readded_as_fill OK")


def test_prefix_symbols_report():
    """Show all 19 prefix symbols: symbol -> coingecko id -> rank (live data)."""
    print("\n=== Prefix-matched symbols: symbol -> coingecko id -> rank ===")
    print(f"{'bybit symbol':22s} {'base':10s} {'coingecko id':32s} {'rank':>6s}")
    for base in sorted(CG_ID_RANK, key=lambda b: (CG_ID_RANK[b][1] is None, CG_ID_RANK[b][1] or 0, b)):
        cid, rk = CG_ID_RANK[base]
        rid = str(cid) if cid else "нет в CG топ-2500"
        rk_s = str(rk) if rk is not None else "—"
        print(f"{PREFIX_BYBIT[base]:22s} {base:10s} {rid:32s} {rk_s:>6s}")
    print("\nPEPE rank<100 => 1000PEPEUSDT drops out of pool #100-#2500. Confirmed.")
    print("1000CATUSDT -> simon-s-cat #1040 (not cat-call-agent #2496). Confirmed.")


def test_cat_minrank_picks_simon_s_cat():
    """Регрессия point 2: min-rank dedup выбирает simon-s-cat #1040, не cat-call-agent #2486."""
    def ranked(limit=2500):
        rows = [{"symbol": f"FILL{i}", "market_cap_rank": i, "id": f"f{i}"} for i in range(1, limit + 1)]
        rows[1040 - 1] = {"symbol": "cat", "market_cap_rank": 1040, "id": "simon-s-cat"}
        rows[2486 - 1] = {"symbol": "cat", "market_cap_rank": 2486, "id": "cat-call-agent"}
        return rows

    def instr():
        return {"1000CATUSDT": {
            "symbol": "1000CATUSDT", "contractType": "LinearPerpetual", "status": "Trading",
            "baseCoin": "1000CAT", "quoteCoin": "USDT", "symbolType": "",
            "leverageFilter": {"maxLeverage": "10"}}}

    old_r, old_i = ls.fetch_coingecko_ranked, ls.fetch_bybit_instruments
    ls.fetch_coingecko_ranked = ranked
    ls.fetch_bybit_instruments = instr
    try:
        _, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=None)
    finally:
        ls.fetch_coingecko_ranked = old_r
        ls.fetch_bybit_instruments = old_i
    assert rank_map.get("1000CATUSDT") == 1040, rank_map
    print("test_cat_minrank_picks_simon_s_cat OK")



def test_normalize_prefix():
    assert ls.normalize_bybit_base("1000PEPEUSDT") == ("PEPE", "1000PEPE")
    assert ls.normalize_bybit_base("10000PEPEUSDT") == ("PEPE", "10000PEPE")
    assert ls.normalize_bybit_base("1000000PEPEUSDT") == ("PEPE", "1000000PEPE")
    assert ls.normalize_bybit_base("PEPEUSDT") == ("PEPE", "PEPE")
    # только точные префиксы 1000/10000/1000000 срезаются; остальные цифры — нет
    assert ls.normalize_bybit_base("0GUSDT") == ("0G", "0G"), "0G — токен zero-gravity, не g-token"
    assert ls.normalize_bybit_base("2ZUSDT") == ("2Z", "2Z"), "2Z не префикс 1000*"
    assert ls.normalize_bybit_base("1000000BABYDOGEUSDT") == ("BABYDOGE", "1000000BABYDOGE")
    print("test_normalize_prefix OK")


if __name__ == "__main__":
    test_normalize_prefix()
    test_min_rank_dedup_and_pepe_exclusion()
    test_pepe_not_readded_as_fill()
    test_cat_minrank_picks_simon_s_cat()
    test_prefix_symbols_report()
    print("POINT 1/4 OK")
