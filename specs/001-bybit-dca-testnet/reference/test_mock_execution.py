"""
Автономные проверки mock-исполнения: чистый матчинг, менеджер состояния,
drain_to_latest и перенос завершённых ордеров в историю.

Проверяемое правило: снапшотный матчинг ордеров честен по трём осям —
(1) по символу (ордера других символов не трогаются), (2) по приоритету
(market впереди лимиток, цена выше — впереди, при равной цене — раньше
созданный), (3) по защите от микрофиллов (уровни < EPSILON не исполняются).

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_mock_execution.py

Контракт модуля mock_execution.py (обязан лежать в этом каталоге):
- EPSILON = 1e-12
- OrderValidationError(Exception)
- validate_order_params(order_type, side, amount, price=None) -> None (бросает)
- match_snapshot_pure(symbol, orderbook, active_orders,
    maker_fee, taker_fee) -> (fills, new_orders_state)
- MockExecutionManager(maker_fee, taker_fee, max_id_cache) с методами
    create_order(symbol, order_type, side, amount, price, client_order_id),
    apply_snapshot(symbol, orderbook), cancel_order(client_order_id);
    полями self.orders, self.client_ids (bounded OrderedDict), self.history.
- async drain_to_latest(queue, timeout) -> dict
- Модуль не импортирует ccxt на верхнем уровне: сетевой стример — вне
  этого файла, иначе проверки без зависимостей упадут на импорте.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

_ref = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> "module":
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def raises(fn) -> bool:
    try:
        fn()
        return False
    except (m.OrderValidationError, ValueError, TypeError):
        return True


def near(actual: float, expected: float) -> bool:
    return abs(actual - expected) < 1e-9


def _order(symbol: str, side: str, otype: str, amount: float,
           price: float | None, created_at: float) -> dict:
    """Ордер в контрактном формате mock_execution.py."""
    return {
        "id": f"{symbol}:{side}:{created_at}",
        "symbol": symbol,
        "type": otype,
        "side": side,
        "amount": float(amount),
        "filled": 0.0,
        "remaining": float(amount),
        "price": float(price) if price is not None else None,
        "avg_price": 0.0,
        "status": "open",
        "created_at": created_at,
        "fills": [],
    }


# ── match_snapshot_pure: приоритет маркет-ордеров ────────────────────────────

print("match_snapshot_pure: маркет-бай первым по лучшей цене")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 0.5], [101.0, 0.5]]}
_orders = {
    "L1": _order("BTCUSDT", "buy", "limit", 0.4, 101.0, 1),
    "M2": _order("BTCUSDT", "buy", "market", 0.4, None, 2),
}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("первый филл — маркет-бай по цене 100.0 (лучшая)",
   _fills[0]["order_id"] == _state["M2"]["id"] and _fills[0]["price"] == 100.0)
ok("маркет-бай закрыт со средней 100.0",
   _state["M2"]["status"] == "closed" and near(_state["M2"]["avg_price"], 100.0),
   _state["M2"]["avg_price"])
ok("лимит-бай добирает остаток уровня (avg 100.75)",
   _state["L1"]["status"] == "closed" and near(_state["L1"]["avg_price"], 100.75),
   _state["L1"]["avg_price"])

print("\nmatch_snapshot_pure: маркет-селл первым по лучшей цене")
_book = {"bids": [[99.0, 0.5], [98.0, 0.5]], "asks": [[100.5, 1.0]]}
_orders = {
    "L1": _order("BTCUSDT", "sell", "limit", 0.4, 98.0, 1),
    "M2": _order("BTCUSDT", "sell", "market", 0.4, None, 2),
}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("первый филл — маркет-селл по цене 99.0 (лучшая)",
   _fills[0]["order_id"] == _state["M2"]["id"] and _fills[0]["price"] == 99.0)
ok("маркет-селл закрыт со средней 99.0",
   _state["M2"]["status"] == "closed" and near(_state["M2"]["avg_price"], 99.0),
   _state["M2"]["avg_price"])
ok("лимит-селл добирает остаток (avg 98.25)",
   _state["L1"]["status"] == "closed" and near(_state["L1"]["avg_price"], 98.25),
   _state["L1"]["avg_price"])

# ── match_snapshot_pure: price-time приоритет ────────────────────────────────

print("\nmatch_snapshot_pure: цена выше — первой")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 0.5]]}
_orders = {
    "A": _order("BTCUSDT", "buy", "limit", 0.4, 100.0, 1),
    "B": _order("BTCUSDT", "buy", "limit", 0.4, 101.0, 2),
}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("лимитка 101.0 исполняется раньше 100.0",
   _fills[0]["order_id"] == "BTCUSDT:buy:2", _fills)
ok("лимитка 101.0 закрыта целиком",
   _state["B"]["status"] == "closed" and _state["B"]["remaining"] == 0.0)
ok("лимитка 100.0 добирает остаток уровня",
   near(_state["A"]["filled"], 0.1) and near(_state["A"]["remaining"], 0.3),
   _state["A"]["filled"])

print("\nmatch_snapshot_pure: при равной цене раньше созданный — первым")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 0.6]]}
_orders = {
    "A": _order("BTCUSDT", "buy", "limit", 0.4, 100.0, 1),
    "B": _order("BTCUSDT", "buy", "limit", 0.4, 100.0, 2),
}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("первым исполняется созданный раньше",
   _fills[0]["order_id"] == "BTCUSDT:buy:1", _fills)
ok("ранний ордер закрыт, поздний — частично",
   _state["A"]["status"] == "closed"
   and _state["B"]["status"] == "open"
   and near(_state["B"]["filled"], 0.2)
   and near(_state["B"]["remaining"], 0.2),
   _state["B"])

# ── match_snapshot_pure: фильтр по символу ───────────────────────────────────

print("\nmatch_snapshot_pure: фильтр по символу")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 1.0]]}
_orders = {
    "BTC": _order("BTCUSDT", "buy", "market", 0.4, None, 1),
    "ETH": _order("ETHUSDT", "buy", "market", 0.5, None, 2),
}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("филлы только по BTCUSDT", all(f["symbol"] == "BTCUSDT" for f in _fills))
ok("BTCUSDT исполнен", _state["BTC"]["status"] == "closed")
ok("ETHUSDT не тронут чужим стаканом",
   _state["ETH"]["status"] == "open" and _state["ETH"]["filled"] == 0.0,
   _state["ETH"])

# ── match_snapshot_pure: защита EPSILON от микрофиллов ───────────────────────

print("\nmatch_snapshot_pure: защита EPSILON")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 1e-13]]}
_orders = {"M": _order("BTCUSDT", "buy", "market", 0.1, None, 1)}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("уровень < EPSILON не даёт микрофилла", _fills == [], _fills)
ok("маркет без исполнимой ликвидности → rejected",
   _state["M"]["status"] == "rejected", _state["M"]["status"])

print("\nmatch_snapshot_pure: обычный уровень исполняется")
_book = {"bids": [[99.5, 1.0]], "asks": [[100.0, 0.5]]}
_orders = {"M": _order("BTCUSDT", "buy", "market", 0.4, None, 1)}
_fills, _state = m.match_snapshot_pure("BTCUSDT", _book, _orders)
ok("уровень 0.5 исполнен целиком",
   _state["M"]["status"] == "closed" and near(_state["M"]["filled"], 0.4),
   _state["M"])

# ── MockExecutionManager: валидация параметров ───────────────────────────────

print("\nMockExecutionManager: валидация параметров")
_mgr = m.MockExecutionManager()
ok("amount = 0 отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "market", "buy", 0)))
ok("amount < 0 отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "market", "buy", -0.1)))
ok("лимит-ордер без цены отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "limit", "buy", 0.1)))
ok("отрицательная цена лимитки отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "limit", "buy", 0.1, price=-5.0)))
ok("неизвестная сторона отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "limit", "hold", 0.1, price=100.0)))
ok("неизвестный тип ордера отклоняется",
   raises(lambda: _mgr.create_order("BTCUSDT", "stop", "buy", 0.1, price=100.0)))
_ok_order = _mgr.create_order("BTCUSDT", "market", "buy", 0.1)
ok("валидный маркет-ордер без цены проходит",
   isinstance(_ok_order, dict) and _ok_order.get("status") == "open", _ok_order)

# ── MockExecutionManager: bounded cache ───────────────────────────────────────

print("\nMockExecutionManager: bounded cache client_ids")
_small = m.MockExecutionManager(max_id_cache=3)
for cid in ("c1", "c2", "c3"):
    _small.create_order("BTCUSDT", "market", "buy", 0.1, client_order_id=cid)
ok("кэш хранит ровно max_id_cache id", len(_small.client_ids) == 3,
   len(_small.client_ids))
_small.create_order("BTCUSDT", "market", "buy", 0.1, client_order_id="c4")
ok("при переполнении вытесняется самый старый (c1)",
   "c1" not in _small.client_ids)
ok("остальные id на месте",
   "c2" in _small.client_ids and "c3" in _small.client_ids
   and "c4" in _small.client_ids)
_reused = _small.create_order("BTCUSDT", "market", "buy", 0.1,
                              client_order_id="c1")
ok("вытесненный id можно переиспользовать",
   isinstance(_reused, dict) and _reused.get("status") == "open", _reused)

_dup_mgr = m.MockExecutionManager()
_dup_mgr.create_order("BTCUSDT", "market", "buy", 0.1,
                      client_order_id="dup1")
_dup = _dup_mgr.create_order("BTCUSDT", "market", "buy", 0.1,
                             client_order_id="dup1")
ok("дубликат cid отклоняется идемпотентно",
   _dup.get("status") == "rejected" and "Duplicate" in _dup.get("reason", ""),
   _dup)

# ── MockExecutionManager: cancel_order ────────────────────────────────────────

print("\nMockExecutionManager: cancel_order")
_cm = m.MockExecutionManager()
ok("несуществующий ордер → OrderNotFound",
   _cm.cancel_order("nope").get("reason") == "OrderNotFound")
_cm.create_order("BTCUSDT", "limit", "buy", 0.1, price=100.0,
                 client_order_id="cancelme")
_cancel = _cm.cancel_order("cancelme")
ok("отмена открытого ордера успешна",
   _cancel.get("status") == "success"
   and _cancel.get("order", {}).get("status") == "canceled", _cancel)
ok("повторная отмена не проходит повторно",
   _cm.cancel_order("cancelme").get("status") != "success")

# ── drain_to_latest: сброс устаревших, task_done, таймаут ────────────────────


async def _drain_stale() -> bool:
    q = asyncio.Queue()
    for book in ("first", "stale", "latest"):
        q.put_nowait(book)
    got = await m.drain_to_latest(q, timeout=1.0)
    return got == "latest" and q.empty()


async def _drain_single() -> bool:
    q = asyncio.Queue()
    q.put_nowait("only")
    got = await m.drain_to_latest(q, timeout=1.0)
    return got == "only" and q.empty()


async def _drain_task_done_balanced() -> bool:
    q = asyncio.Queue()
    for i in range(4):
        q.put_nowait(f"b{i}")
    await m.drain_to_latest(q, timeout=1.0)
    try:
        await asyncio.wait_for(q.join(), timeout=1.0)
        return True
    except asyncio.TimeoutError:
        return False


async def _drain_timeout() -> bool:
    q = asyncio.Queue()
    try:
        await m.drain_to_latest(q, timeout=0.05)
        return False
    except asyncio.TimeoutError:
        return True


print("\ndrain_to_latest")
ok("сбрасывает устаревшие снимки, возвращает свежий",
   asyncio.run(_drain_stale()))
ok("одиночный снимок возвращается как есть", asyncio.run(_drain_single()))
ok("task_done() сбалансирован: join() завершается за 1с",
   asyncio.run(_drain_task_done_balanced()))
ok("пустая очередь → TimeoutError за 0.05с", asyncio.run(_drain_timeout()))

# ── история и очистка завершённых ордеров ────────────────────────────────────

print("\nистория и очистка завершённых ордеров")
_hm = m.MockExecutionManager()
_hm.create_order("BTCUSDT", "market", "buy", 0.4, client_order_id="mk")
_hm.create_order("BTCUSDT", "limit", "buy", 0.4, price=100.0,
                 client_order_id="lim")
_hm.create_order("BTCUSDT", "limit", "buy", 0.4, price=99.0,
                 client_order_id="rest")
_hfills = _hm.apply_snapshot(
    "BTCUSDT", {"bids": [[99.5, 1.0]], "asks": [[100.0, 1.0]]})
_hist_ids = [o["id"] for o in _hm.history]
ok("за тик два филла (маркет + лимитка)", len(_hfills) == 2, _hfills)
ok("закрытый маркет переехал в history", "mk" in _hist_ids)
ok("закрытая лимитка переехала в history", "lim" in _hist_ids)
ok("закрытые убраны из orders",
   "mk" not in _hm.orders and "lim" not in _hm.orders)
ok("открытый ордер остался в orders",
   "rest" in _hm.orders and _hm.orders["rest"]["status"] == "open")
ok("открытый ордер не попал в history", "rest" not in _hist_ids)

print("\nистория: отменённый ордер")
_cm2 = m.MockExecutionManager()
_cm2.create_order("BTCUSDT", "limit", "buy", 0.1, price=90.0,
                  client_order_id="canc")
_cm2.cancel_order("canc")
_cm2.apply_snapshot("BTCUSDT",
                    {"bids": [[99.5, 1.0]], "asks": [[100.0, 1.0]]})
_canc_ids = [o["id"] for o in _cm2.history]
ok("отменённый ордер переехал в history", "canc" in _canc_ids)
ok("отменённый убран из orders", "canc" not in _cm2.orders)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
