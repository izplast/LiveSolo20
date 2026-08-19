"""
Автономные проверки WS-цикла скринера (reference/screener.py).

Проверяется цикл потока, а не индикаторы: подписка (2 топика на символ,
пакеты ≤ 10), приём закрытых свечей (confirm=true) с дедупликацией и маршрут
в состояние, сторож тишины, переподключение с парой stream_down/stream_up.
Сеть не нужна: requests и websockets заменены заглушками до импорта модуля.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_ws.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types

# ── заглушки внешних зависимостей (до импорта модуля) ────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _RequestException(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class _RequestException(Exception):
    pass


class _FakeRequests(types.ModuleType):
    RequestException = _RequestException

    def __init__(self):
        super().__init__("requests")
        self.posted: list[dict] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.posted.append({"url": url, "payload": json})
        return _FakeResponse()


fake_requests = _FakeRequests()
sys.modules["requests"] = fake_requests

ws_client = types.ModuleType("websockets.asyncio.client")
ws_client.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("подмена ws_connect"))
ws_asyncio = types.ModuleType("websockets.asyncio")
ws_asyncio.client = ws_client
ws_root = types.ModuleType("websockets")
ws_root.asyncio = ws_asyncio
sys.modules["websockets"] = ws_root
sys.modules["websockets.asyncio"] = ws_asyncio
sys.modules["websockets.asyncio.client"] = ws_client

_tmp = tempfile.mkdtemp(prefix="ws-test-")
os.chdir(_tmp)

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener.py")
_spec = importlib.util.spec_from_file_location("screener_under_test", _path)
assert _spec and _spec.loader
sc = importlib.util.module_from_spec(_spec)
sys.modules["screener_under_test"] = sc
_spec.loader.exec_module(sc)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def bars(n: int, step_ms: int = 60_000, drift: float = 0.15, rng: float = 1.2,
         amp: float = 0.5, freq: float = 5.0) -> list[list]:
    import math
    out = []
    for i in range(n):
        base = 100.0 * (1 + drift * i / 100 + amp * math.sin(2 * math.pi * i / freq) / 100)
        out.append([i * step_ms, base, base * (1 + rng / 100), base, base * (1 + rng / 200)])
    return out


def fresh(cfg_over: dict | None = None) -> "sc.Screener":
    cfg = sc.Config(journal_path=os.path.join(_tmp, "ws-events.jsonl"), **(cfg_over or {}))
    return sc.Screener(cfg)


# ── подписка ──────────────────────────────────────────────────────────────────

print("подписка")

class RecorderWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, msg):  # noqa: D102
        self.sent.append(json.loads(msg))


s = fresh({"ws_subscribe_batch": 3})
rec = RecorderWs()
asyncio.run(s._subscribe(rec, ["AAAUSDT", "BBBUSDT"]))
topics = [t for m in rec.sent for t in m["args"]]
ok("два топика на символ", len(topics) == 4, topics)
ok("топики kline.{tf}.{symbol} для fast и slow",
   sorted(topics) == ["kline.1.AAAUSDT", "kline.1.BBBUSDT",
                      "kline.15.AAAUSDT", "kline.15.BBBUSDT"], topics)
ok("пакеты не больше лимита батча",
   all(len(m["args"]) <= 3 for m in rec.sent), rec.sent)
ok("все топики отправлены ровно один раз", len(rec.sent) == 2)

# ── приём сообщений ───────────────────────────────────────────────────────────

print("\nприём сообщений (_on_message)")


def state_with_history():
    s = fresh()
    st = sc.SymbolState(s._fast_cap, s._slow_cap)
    for row in bars(41):
        st.push("fast", row)
    for row in bars(60, step_ms=15 * 60_000):
        st.push("slow", row)
    s.states["TESTUSDT"] = st
    return s, st


# чужой топик и битый json не роняют приём
s, st = state_with_history()
before = len(st.fast)
s._on_message(json.dumps({"topic": "ticker.TESTUSDT", "data": [{"lastPrice": "1"}]}))
s._on_message(json.dumps({"topic": "kline.1.TESTUSDT", "data": []}))
s._on_message("{битый json")
s._on_message(json.dumps({"op": "subscribe", "success": False, "ret_msg": "too many args"}))
ok("не-kline сообщения игнорируются", len(st.fast) == before)

# незакрытая свеча в окно не попадает
s, st = state_with_history()
before = len(st.fast)
item = {"start": 60 * 60_000, "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0,
        "confirm": False}
s._on_message(json.dumps({"topic": "kline.1.TESTUSDT", "data": [item]}))
ok("confirm != true не добавляет свечу", len(st.fast) == before)

# закрытая свеча fast → состояние растёт и решение запускается
s, st = state_with_history()
fake_requests.posted.clear()
# свеча-продолжение роста (не вертикальный пробой): вертикальный пробой даёт
# UHLO 1м в «углу» (highs 100 / lows 0) и режется фильтром uhlo_corner
trigger = {"start": 60 * 60_000, "open": 106.5, "high": 107.2, "low": 105.5,
           "close": 107.0, "confirm": True}


async def pump_once():
    s._on_message(json.dumps({"topic": "kline.1.TESTUSDT", "data": [trigger]}))
    await asyncio.sleep(0.05)  # дождаться фоновой задачи решения


asyncio.run(pump_once())
ok("закрытая свеча fast добавляется в окно", len(st.fast) == before + 1)
ok("повторный снапшот той же свечи не дублируется",
   (lambda: (s._on_message(json.dumps({"topic": "kline.1.TESTUSDT", "data": [trigger]})),
             len(st.fast) == before + 1)[1])())
ok("сигнал по закрытой свече уходит в бота", len(fake_requests.posted) == 1,
   fake_requests.posted)

# ── сторож тишины (_pump) ─────────────────────────────────────────────────────

print("\nсторож тишины")


class SilentWs:
    async def send(self, msg):  # noqa: D102
        pass

    async def recv(self):  # noqa: D102
        await asyncio.sleep(30)


async def stale_pump() -> str:
    s = fresh({"ws_stale_sec": 0.05})
    try:
        await s._pump(SilentWs(), 0)
        return "не упал"
    except RuntimeError as e:
        return str(e)


res = asyncio.run(stale_pump())
ok("молчание дольше лимита поднимает RuntimeError", "мёртвое" in res, res)

# ── переподключение (_shard_loop) ─────────────────────────────────────────────

print("\nпереподключение")


class FakeWs:
    def __init__(self, s):
        self.s = s

    async def send(self, msg):  # noqa: D102
        pass

    async def recv(self):
        while not self.s._stop.is_set():
            await asyncio.sleep(0.01)
        raise ConnectionError("остановлено тестом")


class FakeConn:
    def __init__(self, s, connected: asyncio.Event):
        self.s = s
        self.connected = connected

    async def __aenter__(self):
        self.connected.set()
        return FakeWs(self.s)

    async def __aexit__(self, *exc):
        return False


class FakeConnector:
    def __init__(self, s, connected: asyncio.Event):
        self.s = s
        self.connected = connected
        self.calls = 0

    def __call__(self, url, **kw):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("первое соединение обрывается")
        return FakeConn(self.s, self.connected)


async def run_reconnect() -> tuple[int, list[dict]]:
    s = fresh()
    s.symbols = ["TESTUSDT"]
    connected = asyncio.Event()
    connector = FakeConnector(s, connected)
    sc.ws_connect = connector
    task = asyncio.create_task(s._shard_loop(0, ["TESTUSDT"]))
    await asyncio.wait_for(connected.wait(), timeout=5)
    s.stop()
    await asyncio.wait_for(task, timeout=5)
    s.journal.close()
    lines = []
    with open(s.cfg.journal_path, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    return connector.calls, lines


calls, events = asyncio.run(run_reconnect())
kinds = [e["kind"] for e in events]
ok("после обрыва было повторное соединение", calls == 2, calls)
ok("обрыв зафиксирован как stream_down", "stream_down" in kinds, kinds)
ok("восстановление зафиксировано как stream_up", "stream_up" in kinds, kinds)
ok("причина обрыва — disconnect",
   any(e.get("cause") == "disconnect" for e in events if e["kind"] == "stream_down"), events)
ok("stream_down раньше stream_up",
   kinds.index("stream_down") < kinds.index("stream_up"), kinds)
down = next(e for e in events if e["kind"] == "stream_down")
up = next(e for e in events if e["kind"] == "stream_up")
ok("длительность «слепого» интервала записана", up.get("duration_ms", 0) > 0, up)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
