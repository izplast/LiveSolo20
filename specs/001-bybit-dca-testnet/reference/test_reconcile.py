"""
Автономные проверки сверки состояния (reference/reconcile.py).

Синтетический локальный стейт бота и снимок биржи → расхождения, действия,
рендер. Без сети: модуль сам не обращается к API, только сравнивает данные.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_reconcile.py
"""

from __future__ import annotations

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


rc = _load("reconcile")

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


LP = rc.LocalPosition
LO = rc.LocalOrder
EP = rc.ExchangePosition
EO = rc.ExchangeOrder


# ── парсинг ответов REST ────────────────────────────────────────────────────

print("parse_positions")
rows = [
    {"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "65000.5",
     "positionStatus": "Normal"},
    {"symbol": "ETHUSDT", "side": "Sell", "size": "0", "avgPrice": "3000.0"},
]
ps = rc.parse_positions(rows)
ok("разобрано две строки, нулевой размер отброшен", len(ps) == 1, ps)
ok("строковые размеры/цены → float",
   ps[0].qty == 0.01 and abs(ps[0].avg_price - 65000.5) < 1e-9, ps[0])
ok("сторона сохранена", ps[0].side == "Buy", ps[0])

print("\nparse_orders")
orows = [
    {"symbol": "BTCUSDT", "side": "Sell", "qty": "0.01", "price": "66000",
     "reduceOnly": True, "orderId": "O1", "orderLinkId": "L1",
     "orderStatus": "New"},
    {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.01", "price": "0",
     "reduceOnly": False, "orderId": "O2", "orderLinkId": "L2",
     "orderStatus": "PartiallyFilled"},
]
os_ = rc.parse_orders(orows)
ok("два ордера разобраны", len(os_) == 2, os_)
ok("reduce_only и статус строковые → bool/str",
   os_[0].reduce_only is True and os_[0].status == "New", os_[0])
ok("цена 0 → None (маркет)", os_[1].price is None, os_[1])
ok("order_link_id сохранён", os_[1].order_link_id == "L2", os_[1])

# ── сверка: согласованное состояние ─────────────────────────────────────────

print("\nreconcile: согласованные стейты")
rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5, "C1")],
    [LO("BTCUSDT", "Sell", 0.01, 66000, True, "L1", "tp", "C1")],
    [EP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [EO("BTCUSDT", "Sell", 0.01, 66000, True, "O1", "L1", "New", "tp")],
)
ok("без расхождений → ok", rep.ok, rep.all)
ok("пустые списки действий", rep.actions() == {}, rep.actions())

# ── сверка: расхождения по позициям ─────────────────────────────────────────

print("\nreconcile: позиции")
rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5, "C1"),
     LP("SOLUSDT", "Sell", 5.0, 150.0, "C2")],
    [],
    [EP("BTCUSDT", "Buy", 0.005, 64900.0),   # qty + avg расходятся
     EP("ETHUSDT", "Sell", 2.0, 3000.0)],    # есть на бирже, нет локально
    [],
)
kinds = {d.symbol: d.kind for d in rep.position_discrepancies}
btc_kinds = [d.kind for d in rep.position_discrepancies if d.symbol == "BTCUSDT"]
ok("BTC: qty_mismatch и avg_price_mismatch",
   "qty_mismatch" in btc_kinds and "avg_price_mismatch" in btc_kinds,
   btc_kinds)
ok("SOL: есть локально, на бирже нет → missing_on_exchange",
   any(d.symbol == "SOLUSDT" and d.kind == "missing_on_exchange"
       for d in rep.position_discrepancies), kinds)
ok("ETH: на бирже, локально нет → unknown_local",
   any(d.symbol == "ETHUSDT" and d.kind == "unknown_local"
       for d in rep.position_discrepancies), kinds)
ok("действия: close_local_cycle и adopt_position",
   rep.actions().get("close_local_cycle") == 1 and rep.actions().get("adopt_position") == 1,
   rep.actions())

rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [],
    [EP("BTCUSDT", "Sell", 0.01, 65000.5)],
    [],
)
ok("сторона разошлась → side_mismatch",
   any(d.kind == "side_mismatch" for d in rep.position_discrepancies),
   [d.kind for d in rep.position_discrepancies])

# ── сверка: расхождения по ордерам ──────────────────────────────────────────

print("\nreconcile: ордера")
rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [LO("BTCUSDT", "Sell", 0.01, 66000, True, "L1", "tp"),
     LO("BTCUSDT", "Buy", 0.02, 64000, False, "SO1", "so"),
     LO("BTCUSDT", "Sell", 0.01, 66000, False, "L9", "tp")],
    [EP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [EO("BTCUSDT", "Sell", 0.01, 66100, True, "O1", "L1", "New", "tp"),
     EO("BTCUSDT", "Buy", 0.03, 64000, False, "O2", "SO1", "PartiallyFilled", "so"),
     EO("BTCUSDT", "Sell", 1.0, 50000, True, "O9", "X1", "New", "tp")],
)
ok("L1: цена разошлась → price_mismatch",
   any(d.ref_id == "L1" and d.kind == "price_mismatch" for d in rep.order_discrepancies),
   [(d.ref_id, d.kind) for d in rep.order_discrepancies])
ok("SO1: размер разошёлся → qty_mismatch",
   any(d.ref_id == "SO1" and d.kind == "qty_mismatch" for d in rep.order_discrepancies),
   [(d.ref_id, d.kind) for d in rep.order_discrepancies])
ok("L9: локально, на бирже нет → missing_on_exchange",
   any(d.ref_id == "L9" and d.kind == "missing_on_exchange" for d in rep.order_discrepancies),
   [(d.ref_id, d.kind) for d in rep.order_discrepancies])
ok("X1: на бирже, локально нет → unknown_local + cancel_exchange_order",
   any(d.ref_id == "X1" and d.kind == "unknown_local" for d in rep.order_discrepancies)
   and rep.actions().get("cancel_exchange_order") == 1,
   [(d.ref_id, d.kind) for d in rep.order_discrepancies])

rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [LO("BTCUSDT", "Sell", 0.01, 66000, True, "L1", "tp")],
    [EP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [EO("BTCUSDT", "Sell", 0.01, 66000, True, "O1", "L1", "Filled", "tp")],
)
ok("биржевой статус Filled при локальном open → not_open_exchange",
   any(d.kind == "not_open_exchange" for d in rep.order_discrepancies),
   [(d.kind, d.ref_id) for d in rep.order_discrepancies])

rep = rc.reconcile(
    [LP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [LO("BTCUSDT", "Buy", 0.01, 66000, False, "L1", "tp")],
    [EP("BTCUSDT", "Buy", 0.01, 65000.5)],
    [EO("BTCUSDT", "Sell", 0.01, 66000, True, "O1", "L1", "New", "tp")],
)
ok("reduce_only разошёлся → reduce_only_mismatch",
   any(d.kind == "reduce_only_mismatch" for d in rep.order_discrepancies),
   [d.kind for d in rep.order_discrepancies])

# ── вывод ───────────────────────────────────────────────────────────────────

print("\nrender / to_dict")
rend = rc.render(rep)
ok("в рендере есть reduce_only_mismatch и Действия",
   "reduce_only_mismatch" in rend and "Действия" in rend, rend)
ok("OK-рендер без расхождений",
   "OK: расхождений нет" in rc.render(
       rc.reconcile([], [], [], [])), "")

d = rc.to_dict(rep)
ok("to_dict: ok=False, ключи positions/orders/actions",
   d["ok"] is False and "positions" in d and "orders" in d and "actions" in d, d)
ok("to_dict: расхождение с полями",
   d["orders"] and all(k in d["orders"][0] for k in ("kind", "symbol", "action", "message")),
   d["orders"])

print(f"\nитог: {PASS} ok, {FAIL} fail")
raise SystemExit(1 if FAIL else 0)