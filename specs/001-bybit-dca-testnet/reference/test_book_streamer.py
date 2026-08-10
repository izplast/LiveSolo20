"""
Автономные проверки стримера стакана (reference/book_streamer.py).

Без сети: разбор и сортировка уровней из result /v5/market/orderbook,
сетевые вызовы и ретраи — через поддельный urllib.request.urlopen,
асинхронный опрос — на подменённом fetch_orderbook с короткими паузами.
Проверяется совместимость выхода с drain_to_latest (mock_execution).

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_book_streamer.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse

_ref = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> "module":
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bs = _load("book_streamer")
m = _load("mock_execution")

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def raises(fn, exc: type = bs.BookStreamerError) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


class _FakeResp:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> bool:
        return False


def fake_urlopen(payloads: list[str], fail_first: int = 0):
    """Поддельный urlopen: fail_first вызовов падает, дальше циклится по payloads."""
    calls: list[str] = []

    def _fake(url: str, timeout: float = 15.0):
        calls.append(url)
        if len(calls) <= fail_first:
            raise urllib.error.URLError("boom")
        return _FakeResp(payloads[(len(calls) - 1 - fail_first) % len(payloads)])

    return _fake, calls


def payload(b: list, a: list, ts: int = 1786371763202,
            retcode: int = 0, retmsg: str = "OK") -> str:
    return json.dumps({"retCode": retcode, "retMsg": retmsg,
                       "result": {"s": "BTCUSDT", "ts": ts, "b": b, "a": a}})


# ── контракт модуля ───────────────────────────────────────────────────────────

print("контракт book_streamer.py")
ok("модуль не импортирует внешние зависимости (ws/ccxt)",
   "websockets" not in sys.modules and "ccxt" not in sys.modules)
for name in ("parse_orderbook", "fetch_orderbook", "poll_orderbook",
             "BookStreamerError", "BYBIT_REST", "DEFAULT_LIMIT"):
    ok(f"book_streamer.{name} доступен", hasattr(bs, name))

# ── parse_orderbook ───────────────────────────────────────────────────────────

print("\nparse_orderbook")
parsed = bs.parse_orderbook(
    {"ts": "1786371763202",
     "b": [["99.5", "1.0"], ["100.0", "2.5"]],
     "a": [["101.0", "0.5"], ["100.5", "3.0"]]})
ok("цены и объёмы приведены к float",
   all(isinstance(p, float) and isinstance(q, float)
       for side in ("bids", "asks") for p, q in parsed[side]), parsed)
ok("биды отсортированы по убыванию, аски по возрастанию",
   parsed["bids"] == [[100.0, 2.5], [99.5, 1.0]]
   and parsed["asks"] == [[100.5, 3.0], [101.0, 0.5]], parsed)
ok("ts приводится к int", parsed["ts"] == 1786371763202, parsed["ts"])

# ── fetch_orderbook: разбор + валидация ───────────────────────────────────────

print("\nfetch_orderbook")
_resp = payload([["100.0", "2.5"]], [["100.5", "3.0"]])
_http, _calls = fake_urlopen([_resp])
_orig_urlopen, _orig_sleep = bs.urllib.request.urlopen, bs.time.sleep
bs.urllib.request.urlopen = _http
bs.time.sleep = lambda s: None
try:
    _book = bs.fetch_orderbook("https://api.bybit.com", "BTCUSDT", limit=1)
    ok("возвращается стакан в контракте mock-исполнения",
       _book["bids"] == [[100.0, 2.5]] and _book["asks"] == [[100.5, 3.0]],
       _book)
    ok("запрос уходит с symbol и limit",
       "symbol=BTCUSDT" in _calls[0] and "limit=1" in _calls[0], _calls[0])

    _http2, _calls2 = fake_urlopen([payload([], [], retcode=1, retmsg="err")])
    bs.urllib.request.urlopen = _http2
    ok("retCode != 0 → BookStreamerError",
       raises(lambda: bs.fetch_orderbook("https://api.bybit.com", "BTCUSDT")),
       _calls2)

    _http3, _calls3 = fake_urlopen([payload([], [])])
    bs.urllib.request.urlopen = _http3
    ok("пустой стакан → BookStreamerError",
       raises(lambda: bs.fetch_orderbook("https://api.bybit.com", "BTCUSDT")),
       _calls3)

    bs.urllib.request.urlopen = _orig_urlopen
finally:
    bs.time.sleep = _orig_sleep

print("\nfetch_orderbook: ретраи")
_http4, _calls4 = fake_urlopen([_resp], fail_first=2)
bs.urllib.request.urlopen = _http4
bs.time.sleep = lambda s: None
try:
    ok("после двух сбоев третий запрос удачен",
       bs.fetch_orderbook("https://api.bybit.com", "BTCUSDT")["bids"]
       == [[100.0, 2.5]] and len(_calls4) == 3, len(_calls4))

    _http5, _calls5 = fake_urlopen([_resp], fail_first=99)
    bs.urllib.request.urlopen = _http5
    ok("все попытки сорвались → BookStreamerError (retries+1 вызовов)",
       raises(lambda: bs.fetch_orderbook("https://api.bybit.com", "BTCUSDT"))
       and len(_calls5) == 4, len(_calls5))
finally:
    bs.urllib.request.urlopen = _orig_urlopen
    bs.time.sleep = _orig_sleep

# ── poll_orderbook: поток снапшотов + интеграция с drain_to_latest ───────────

print("\npoll_orderbook")
_books_seen: list[int] = []


def _fake_fetch(*a, **k):
    _books_seen.append(len(_books_seen))
    return {"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]],
            "ts": 1786371763202}


async def _run_poll() -> bool:
    q = asyncio.Queue()
    orig = bs.fetch_orderbook
    bs.fetch_orderbook = _fake_fetch
    try:
        task = asyncio.create_task(
            bs.poll_orderbook(q, "https://api.bybit.com", "BTCUSDT",
                              interval_sec=0.01, retries=0))
        await asyncio.sleep(0.045)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        latest = await m.drain_to_latest(q, timeout=0.2)
        return q.empty() and latest["bids"] == [[100.0, 1.0]]
    finally:
        bs.fetch_orderbook = orig


ok("опрос кладёт свежие снапшоты, drain_to_latest берёт последний",
   asyncio.run(_run_poll()) and len(_books_seen) >= 3, len(_books_seen))


async def _run_fail() -> bool:
    q = asyncio.Queue()

    def _bad(*a, **k):
        raise bs.BookStreamerError("down")

    orig = bs.fetch_orderbook
    bs.fetch_orderbook = _bad
    try:
        try:
            await bs.poll_orderbook(q, "https://api.bybit.com", "BTCUSDT",
                                    interval_sec=0.001, max_failures=2)
            return False
        except bs.BookStreamerError:
            return True
    finally:
        bs.fetch_orderbook = orig


ok("после max_failures ошибок подряд цикл останавливается",
   asyncio.run(_run_fail()))

# ── запись/чтение записи стакана (JSONL) ──────────────────────────────────────

print("\nwrite_record / read_records")
_tmp = tempfile.mkdtemp(prefix="bs-test-")
_path = os.path.join(_tmp, "book-test.jsonl")
_book1 = {"bids": [[100.0, 2.5]], "asks": [[100.5, 3.0]], "ts": 1000}
_book2 = {"bids": [[99.5, 2.5]], "asks": [[100.0, 3.0]], "ts": 2000}
with open(_path, "w", encoding="utf-8"):
    pass
bs.write_record(_path, "BTCUSDT", _book1)
bs.write_record(_path, "BTCUSDT", _book2)
_back = bs.read_records(_path)
ok("запись/чтение круговая — стакан сохраняет порядок и ts",
   _back == [_book1 | {"symbol": "BTCUSDT"},
            _book2 | {"symbol": "BTCUSDT"}], _back)
ok("формат совместим с контрактом mock-исполнения (bids/asks)",
   _back[0]["bids"] == [[100.0, 2.5]] and _back[0]["asks"] == [[100.5, 3.0]])
ok("из записи достаётся символ для replay-скринера",
   _back[0]["symbol"] == "BTCUSDT", _back[0].get("symbol"))
os.remove(_path)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
