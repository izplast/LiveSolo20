"""
reference/bot.py — логика ордеров DCA-бота (FR-008…FR-018).

Автономный слой без сетевых вызовов: режимы ордеров (как ордер влияет на
позицию), уровень тейк-профита от средней цены входа, одноразовая установка
плеча, приведение размера и цены к шагам инструмента. Настоящий бот живёт
в ~/bybit-dca-bot; здесь — проверяемые правила, которые он обязан соблюдать
перед обращением к бирже, и контракт запросов.

Режимы ордеров (mode):
  ENTRY  — первый вход и докупки: увеличивает позицию, reduce_only=False.
  ADJUST — перенос уровня тейк-профита после докупки (FR-011):
           cancel+replace reduce-only TP-ордера.
  CLOSE  — закрытие позиции целиком по тейку или выходу по времени
           (FR-012, FR-014): reduce-only на встречной стороне.

Поуровневый тейк-профит (эскалация): процент TP выбирается по числу
выполненных докупок (уровень = cycle.dca_done) через take_profit_pct_at —
Level 0 → 1.2%, Level 1 → 1.5%, Level 2 → 2.0%; выше последнего уровня
удерживается максимум, пустая эскалация — фиксированный take_profit_pct.

Сетевые вызовы передаются инъекцией (callable apply), поэтому поведение
проверяется заглушками без сети (см. test_bot.py).

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_bot.py
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pricing import quantize_price, quantize_qty


class OrderMode(str, Enum):
    """Как ордер влияет на позицию."""

    ENTRY = "entry"
    ADJUST = "adjust"
    CLOSE = "close"


# Reduce-only для каждого режима: вход увеличивает позицию, перенос уровня
# TP и закрытие её только уменьшают.
REDUCE_ONLY = {
    OrderMode.ENTRY: False,
    OrderMode.ADJUST: True,
    OrderMode.CLOSE: True,
}


@dataclass(frozen=True)
class OrderRequest:
    """Параметры ордера для Bybit v5, собранные из логики цикла."""

    mode: OrderMode
    symbol: str
    side: str
    qty: float
    reduce_only: bool
    order_link_id: str
    price: float | None = None  # лимитная цена (TP-ордер); рыночные входы — None


def exit_side(side: str) -> str:
    """Сторона закрытия — встречная стороне входа."""
    if side == "Buy":
        return "Sell"
    if side == "Sell":
        return "Buy"
    raise ValueError(f"side должен быть Buy или Sell, получено {side!r}")


def entry_request(symbol: str, side: str, qty: float, order_link_id: str,
                  qty_step: float) -> OrderRequest:
    """Первый вход/докупка: режим ENTRY, размер приведён к шагу количества."""
    return OrderRequest(
        mode=OrderMode.ENTRY,
        symbol=symbol,
        side=side,
        qty=quantize_qty(qty, qty_step),
        reduce_only=REDUCE_ONLY[OrderMode.ENTRY],
        order_link_id=order_link_id,
    )


def tp_price(avg_entry: float, side: str, tp_pct: float, tick_size: float) -> float:
    """Уровень тейк-профита от средней цены входа (FR-011), квантован к шагу.

    Для лонга TP выше средней, для шорта — ниже; расстояние — tp_pct процентов.
    """
    if avg_entry <= 0:
        raise ValueError(f"avg_entry должен быть положительным, получено {avg_entry!r}")
    if tp_pct <= 0:
        raise ValueError(f"tp_pct должен быть положительным, получено {tp_pct!r}")
    factor = 1 + tp_pct / 100 if side == "Buy" else 1 - tp_pct / 100
    return quantize_price(avg_entry * factor, tick_size)


def set_take_profit(symbol: str, side: str, avg_entry: float, tp_pct: float,
                    tick_size: float, order_link_id: str,
                    has_existing: bool) -> OrderRequest:
    """Задать/перенести уровень тейк-профита (FR-011, FR-012).

    Первая установка — режим CLOSE (reduce-only лимит на встречной стороне
    по уровню TP). Повторный вызов после докупки (has_existing=True) — режим
    ADJUST: старый TP-ордер отменяется, выставляется новый на пересчитанном
    уровне от новой средней цены входа. Процент выбирает вызывающая сторона
    по уровню лестницы через take_profit_pct_at (см. поуровневый TP).
    """
    mode = OrderMode.ADJUST if has_existing else OrderMode.CLOSE
    return OrderRequest(
        mode=mode,
        symbol=symbol,
        side=exit_side(side),
        qty=0.0,  # TP-ордер закрывает остаток позиции; qty задаёт монитор цикла
        reduce_only=REDUCE_ONLY[mode],
        order_link_id=order_link_id,
        price=tp_price(avg_entry, side, tp_pct, tick_size),
    )


def take_profit_pct_at(params, level: int) -> float:
    """Процент TP для уровня лестницы (эскалация от бэктеста).

    Уровень = число выполненных докупок (`cycle.dca_done`); вход без доливок —
    Level 0. Уровень N возвращает элемент эскалации (params.tp_escalation),
    выше последнего удерживается максимум; пустой список — фиксированный
    params.take_profit_pct (прежнее поведение). Параметр принимается как
    объект с полями tp_escalation и take_profit_pct (фолбэк tp_pct
    совместим с backtest.DcaParams).
    """
    escalation = getattr(params, "tp_escalation", ())
    fallback = getattr(params, "take_profit_pct", None)
    if fallback is None:
        fallback = getattr(params, "tp_pct", 1.0)
    if not escalation:
        return fallback
    if level < 0:
        level = 0
    if level >= len(escalation):
        level = len(escalation) - 1
    return escalation[level]


class LeverageSetter:
    """Установка фиксированного плеча ровно один раз на символ (FR-018).

    Повторный сигнал по тому же символу не должен дёргать API: повторная
    установка того же значения может вернуть ошибку и превратиться в лишний
    отказ в журнале. Реальная установка передаётся callable apply.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def set_leverage_once(self, symbol: str, leverage: float, apply) -> bool:
        """Применить плечо, если ещё не применено. True — API вызывался."""
        if symbol in self._applied:
            return False
        apply(symbol, leverage)
        self._applied.add(symbol)
        return True
