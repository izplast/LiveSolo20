"""
reference/mock_execution.py — снапшотный матчинг ордеров (mock-исполнение).

Автономный слой без сетевых вызовов: чистая функция match_snapshot_pure
сводит активные ордера с копией стакана и возвращает филлы и новое состояние
(без мутации входов), менеджер MockExecutionManager хранит состояние и
переносит завершённые ордера в историю, drain_to_latest отдаёт самый свежий
снимок из очереди. Сетевой стример здесь отсутствует намеренно: этот модуль
не импортирует ccxt, чтобы проверки шли без внешних зависимостей.

Правила матчинга:
  - по символу: ордера других символов не исполняются этим стаканом;
  - price-time priority: маркет-ордера впереди лимиток, выше цена — раньше,
    при равной цене раньше созданный;
  - уровень стакана < EPSILON не исполняется (защита от микрофиллов float);
  - все филлы помечаются TAKER и тарифицируются taker_fee (дизайн-решение:
    снапшотный матчинг не моделирует resting-ордера, maker-филл невозможен);
  - маркет-ордер не закрылся по стакану за тик — IOC: expired (частично
    исполнен) или rejected (не исполнен вовсе).

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_mock_execution.py
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections import OrderedDict

EPSILON = 1e-12

# Завершённые статусы, которые очистка переносит в manager.history.
DONE_STATUSES = ("closed", "rejected", "expired", "canceled")


class OrderValidationError(Exception):
    pass


def validate_order_params(order_type: str, side: str, amount: float,
                          price: float | None = None) -> None:
    """Отклонить ордер с невалидными параметрами (бросает OrderValidationError)."""
    if amount is None or float(amount) <= 0:
        raise OrderValidationError(f"Invalid amount: {amount}. Must be > 0.")
    if side not in ("buy", "sell"):
        raise OrderValidationError(f"Invalid side: {side}. Must be 'buy' or 'sell'.")
    if order_type == "limit":
        if price is None or float(price) <= 0:
            raise OrderValidationError(
                f"Limit order requires price > 0, got: {price}")
    elif order_type != "market":
        raise OrderValidationError(f"Unsupported order type: {order_type}")


def match_snapshot_pure(
    symbol: str,
    orderbook: dict,
    active_orders: dict,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.00055,
) -> tuple[list[dict], dict]:
    """Свести ордера с копией стакана: (fills, новое состояние ордеров).

    Входы не мутируются: стакан копируется поуровнево, состояние ордеров —
    глубокой копией. maker_fee сохранён в сигнатуре контракта, но не
    применяется: все филлы снапшотного матчинга — TAKER (см. докстринг).
    """
    bids = [list(level) for level in orderbook.get("bids", [])]
    asks = [list(level) for level in orderbook.get("asks", [])]

    if not bids or not asks:
        return [], active_orders

    orders_state = copy.deepcopy(active_orders)
    fills: list[dict] = []

    open_symbol = [
        o for o in orders_state.values()
        if o["status"] == "open" and o["symbol"] == symbol
    ]
    buy_orders = sorted(
        [o for o in open_symbol if o["side"] == "buy"],
        key=lambda x: (-x["price"] if x["price"] is not None else -float("inf"),
                       x["created_at"]),
    )
    sell_orders = sorted(
        [o for o in open_symbol if o["side"] == "sell"],
        key=lambda x: (x["price"] if x["price"] is not None else -float("inf"),
                       x["created_at"]),
    )

    for order in buy_orders + sell_orders:
        levels = asks if order["side"] == "buy" else bids

        for level in levels:
            level_price, level_qty = level[0], level[1]
            if level_qty < EPSILON:
                continue

            if order["type"] == "limit":
                if order["side"] == "buy" and order["price"] < level_price:
                    break
                if order["side"] == "sell" and order["price"] > level_price:
                    break

            match_qty = min(order["remaining"], level_qty)
            level[1] = max(0.0, level_qty - match_qty)
            order["filled"] += match_qty
            order["remaining"] -= match_qty

            fill_info = {
                "order_id": order["id"],
                "symbol": order["symbol"],
                "price": level_price,
                "qty": match_qty,
                "role": "TAKER",
                "fee": (match_qty * level_price) * taker_fee,
                "timestamp": time.time(),
            }
            order["fills"].append(fill_info)
            fills.append(fill_info)

            if order["remaining"] <= 1e-8:
                order["status"] = "closed"
                break

        if order["filled"] > 0:
            total_cost = sum(f["price"] * f["qty"] for f in order["fills"])
            order["avg_price"] = total_cost / order["filled"]

        # Маркет-ордер, не закрывшийся по стакану за тик, гасится по IOC.
        if order["type"] == "market" and order["status"] == "open":
            order["status"] = "expired" if order["filled"] > 0 else "rejected"

    return fills, orders_state


class MockExecutionManager:
    """Хранит состояние ордеров и применяет к нему результаты чистой функции."""

    def __init__(self, maker_fee: float = 0.0002,
                 taker_fee: float = 0.00055,
                 max_id_cache: int = 10000) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.orders: dict[str, dict] = {}
        self.client_ids: OrderedDict[str, bool] = OrderedDict()
        self.max_id_cache = max_id_cache
        self.history: list[dict] = []

    def create_order(self, symbol: str, order_type: str, side: str,
                     amount: float, price: float | None = None,
                     client_order_id: str | None = None) -> dict:
        validate_order_params(order_type, side, amount, price)

        cid = client_order_id or f"mock_{uuid.uuid4().hex[:8]}"
        if cid in self.client_ids:
            return {"status": "rejected", "reason": "Duplicate clientOrderId",
                    "id": cid}

        # Bounded cache: старый id вытесняется, чтобы сет не рос бесконечно.
        self.client_ids[cid] = True
        if len(self.client_ids) > self.max_id_cache:
            self.client_ids.popitem(last=False)

        order = {
            "id": cid,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": float(amount),
            "filled": 0.0,
            "remaining": float(amount),
            "price": float(price) if price is not None else None,
            "avg_price": 0.0,
            "status": "open",
            "created_at": time.time(),
            "fills": [],
        }
        self.orders[cid] = order
        return order

    def apply_snapshot(self, symbol: str, orderbook: dict) -> list[dict]:
        """Применить чистый матчинг и перенести завершённые ордера в историю."""
        fills, new_orders_state = match_snapshot_pure(
            symbol,
            orderbook=orderbook,
            active_orders=self.orders,
            maker_fee=self.maker_fee,
            taker_fee=self.taker_fee,
        )
        self.orders = new_orders_state

        # Итерация по копии — удаление из dict по месту не даёт RuntimeError.
        for cid in [cid for cid, o in self.orders.items()
                    if o["status"] in DONE_STATUSES]:
            self.history.append(self.orders.pop(cid))
        return fills

    def cancel_order(self, client_order_id: str) -> dict:
        if client_order_id not in self.orders:
            return {"status": "error", "reason": "OrderNotFound",
                    "id": client_order_id}

        order = self.orders[client_order_id]
        if order["status"] != "open":
            return {"status": "rejected",
                    "reason": f"Order is already {order['status']}",
                    "id": client_order_id}

        order["status"] = "canceled"
        order["canceled_at"] = time.time()
        return {"status": "success", "order": order}


async def drain_to_latest(queue: asyncio.Queue, timeout: float = 5.0) -> dict:
    """Самый свежий стакан из очереди, выкидывая устаревшие снимки.

    Таймаут защищает от вечного зависания, если WS упал без разрыва
    соединения; task_done() подтверждает каждый get(), чтобы queue.join()
    не завис.
    """
    orderbook = await asyncio.wait_for(queue.get(), timeout=timeout)
    queue.task_done()

    while not queue.empty():
        try:
            orderbook = queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            break

    return orderbook
