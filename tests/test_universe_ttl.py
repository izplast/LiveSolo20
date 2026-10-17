"""Point 3: TTL кэша вселенной (UNIVERSE_TTL_SEC) + повторы CoinGecko при 429.

- Если data/midcap_universe.json моложе UNIVERSE_TTL_SEC — используется без запроса к CoinGecko.
- fetch_coingecko_ranked повторяет страницу при 429 (30–60 с, до CG_FETCH_RETRIES), не пропуская страницу.
- При полной неудаче — fallback на старый кэш с предупреждением.
"""
import sys
import json
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls
from tools import screener_common as sc

TMP = Path("/tmp/opencode/screener-test-ttl")
TMP.mkdir(parents=True, exist_ok=True)


def _write_cache(path: Path, symbols: list, rank_map: dict, age_sec: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"symbols": symbols, "rank_map": rank_map, "cg_slice": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age_sec is not None:
        now = time.time()
        os.utime(path, (now - age_sec, now - age_sec))


def _patch(mod, name, new):
    old = getattr(mod, name)
    setattr(mod, name, new)
    return old


class _fast_retry:
    """Убирает реальные паузы 30–60 с в cg_fetch_with_retry на время теста."""

    def __enter__(self):
        self.min, self.max = sc.CG_RETRY_MIN_SEC, sc.CG_RETRY_MAX_SEC
        sc.CG_RETRY_MIN_SEC = sc.CG_RETRY_MAX_SEC = 0
        return self

    def __exit__(self, *exc):
        sc.CG_RETRY_MIN_SEC, sc.CG_RETRY_MAX_SEC = self.min, self.max
        return False


def test_cache_fresh_used_without_coingecko():
    """Кэш моложе TTL -> возвращается из кэша, CoinGecko не вызывается."""
    cache = TMP / "fresh.json"
    _write_cache(cache, ["AAAUSDT", "BBBUSDT"], {"AAAUSDT": 100, "BBBUSDT": 101}, age_sec=100)
    calls = {"n": 0}
    old_ranked = ls.fetch_coingecko_ranked
    old_instr = ls.fetch_bybit_instruments
    ls.fetch_coingecko_ranked = lambda limit=600: (calls.__setitem__("n", calls["n"] + 1) or [])
    ls.fetch_bybit_instruments = lambda: {}
    try:
        symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=str(cache))
    finally:
        ls.fetch_coingecko_ranked = old_ranked
        ls.fetch_bybit_instruments = old_instr
    assert calls["n"] == 0, "CoinGecko не должен вызываться для свежего кэша"
    assert symbols == ["AAAUSDT", "BBBUSDT"], symbols
    assert rank_map == {"AAAUSDT": 100, "BBBUSDT": 101}
    print("test_cache_fresh_used_without_coingecko OK")


def test_cache_stale_rebuilds():
    """Кэш старше TTL -> перестраивается через CoinGecko."""
    cache = TMP / "stale.json"
    _write_cache(cache, ["OLDUSDT"], {"OLDUSDT": 1}, age_sec=sc.UNIVERSE_TTL_SEC + 100)
    fetched = {"n": 0}
    old_ranked = ls.fetch_coingecko_ranked
    old_instr = ls.fetch_bybit_instruments
    ls.fetch_coingecko_ranked = lambda limit=600: (fetched.__setitem__("n", fetched["n"] + 1) or
                                                   [{"symbol": f"FILL{i}", "market_cap_rank": i} for i in range(1, 2501)] +
                                                   [{"symbol": "NEW", "market_cap_rank": 150, "id": "new"}])
    ls.fetch_bybit_instruments = lambda: {"NEWUSDT": {
        "symbol": "NEWUSDT", "contractType": "LinearPerpetual", "status": "Trading",
        "baseCoin": "NEW", "quoteCoin": "USDT", "symbolType": "", "leverageFilter": {"maxLeverage": "10"}}}
    try:
        symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=str(cache))
    finally:
        ls.fetch_coingecko_ranked = old_ranked
        ls.fetch_bybit_instruments = old_instr
    assert fetched["n"] >= 1, "CoinGecko должен быть вызван для старого кэша"
    assert "OLDUSDT" not in symbols, symbols
    print("test_cache_stale_rebuilds OK")


def test_fallback_to_old_cache_on_failure():
    """При полном отказе CoinGecko — работа по старому кэшу с предупреждением."""
    cache = TMP / "fail.json"
    _write_cache(cache, ["OLDUSDT"], {"OLDUSDT": 1}, age_sec=sc.UNIVERSE_TTL_SEC + 100)
    old_ranked = ls.fetch_coingecko_ranked
    old_log = ls.log
    warnings = []
    ls.log = lambda m: warnings.append(m)
    ls.fetch_coingecko_ranked = lambda limit=600: (_ for _ in ()).throw(RuntimeError("429"))
    try:
        symbols, rank_map, _ = ls.build_midcap_universe(100, 2500, save_cache=str(cache))
    finally:
        ls.fetch_coingecko_ranked = old_ranked
        ls.log = old_log
    assert symbols == ["OLDUSDT"], symbols
    assert rank_map == {"OLDUSDT": 1}
    assert any("кэш" in w.lower() for w in warnings), warnings
    print("test_fallback_to_old_cache_on_failure OK")


def test_cg_fetch_with_retry_then_success():
    """cg_fetch_with_retry повторяет при неудаче и возвращает первый успех."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 Too Many Requests")
        return {"ok": True}

    with _fast_retry():
        res = sc.cg_fetch_with_retry(flaky, label="test")
    assert res == {"ok": True}, res
    assert calls["n"] == 3, calls["n"]
    print("test_cg_fetch_with_retry_then_success OK")

def test_cg_fetch_with_retry_exhausted():
    """cg_fetch_with_retry возвращает None после исчерпания попыток."""
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise RuntimeError("429 Too Many Requests")

    with _fast_retry():
        res = sc.cg_fetch_with_retry(always_fail, label="test")
    assert res is None
    assert calls["n"] == sc.CG_FETCH_RETRIES, calls["n"]
    print("test_cg_fetch_with_retry_exhausted OK")


def test_page_failure_raises_not_partial():
    """Сбой страницы после всех повторов -> RuntimeError, а не частичная вселенная.

    Регрессия point 2: молчаливый break на недоступной странице строил обрезанную
    вселенную (сдвиг рангов), из-за чего 1000CATUSDT матчился в cat-call-agent
    вместо simon-s-cat. Теперь fetch_coingecko_ranked обязан поднять исключение,
    чтобы build_midcap_universe ушёл в fallback на старый кэш.
    """
    calls = {"n": 0}

    def fake_get(path, params=None):
        params = params or {}
        calls["n"] += 1
        if int(params.get("page", 1)) == 2:
            raise RuntimeError("429 Too Many Requests")
        return [{"symbol": f"P{params['page']}_{i}", "market_cap_rank": (params["page"] - 1) * 250 + i + 1,
                 "id": f"coin-{params['page']}-{i}"} for i in range(250)]

    old_get = ls._cg_get
    ls._cg_get = fake_get
    try:
        with _fast_retry():
            try:
                ls.fetch_coingecko_ranked(limit=500)
            except RuntimeError as e:
                assert "страница 2" in str(e), e
            else:
                raise AssertionError("fetch_coingecko_ranked должен поднять RuntimeError, "
                                     "а не вернуть обрезанную вселенную")
    finally:
        ls._cg_get = old_get
    print("test_page_failure_raises_not_partial OK")


if __name__ == "__main__":
    test_cache_fresh_used_without_coingecko()
    test_cache_stale_rebuilds()
    test_fallback_to_old_cache_on_failure()
    test_cg_fetch_with_retry_then_success()
    test_cg_fetch_with_retry_exhausted()
    test_page_failure_raises_not_partial()
    print("POINT 3 OK")
