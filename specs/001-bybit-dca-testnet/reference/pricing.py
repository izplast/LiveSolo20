"""
Квантование цен и размеров к шагам биржи (tick size / qty step).

Назначение: перед выставлением ордера расчётные цена и размер приводятся
к шагам инструмента — биржевые ограничения, которые бот обязан проверить
(FR-017). Величина, не кратная шагу, была бы отклонена биржей.

Правило округления: к ближайшему кратному шага, а при значении ровно на
половине шага от обоих соседних кратных — вверх (round-half-up).
Стандартный round() округляет половину к чётному (banker's rounding),
поэтому на границе даёт несимметричный результат; здесь ties уходят вверх.

Расчёт ведётся в Decimal через строковое представление входа, чтобы
бинарное представление float (0.1 + 0.2 != 0.3) не влияло на выбор
кратного. Возвращается float — наименьшее кратное шага имеет точное
двоичное представление для десятичных шагов вида 10^-k.

Цены и шаги биржи неотрицательны; отрицательный шаг бессмыслен.
Отрицательное значение допускается, ties уходят от нуля.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_pricing.py
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def quantize_step(value: float, step: float) -> float:
    """
    Привести значение к ближайшему кратному шага; на половине шага — вверх.

    >>> quantize_step(1.234, 0.01)
    1.23
    >>> quantize_step(1.235, 0.01)   # ровно половина шага — вверх
    1.24
    >>> quantize_step(1.2, 0.1)      # точное кратное не трогаем
    1.2
    """
    if step <= 0:
        raise ValueError(f"step должен быть положительным, получено {step!r}")

    s = Decimal(str(step))
    v = Decimal(str(value))
    n = (v / s).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * s)


def quantize_price(price: float, step: float) -> float:
    """Округлить цену к шагу цены инструмента (tick size)."""
    return quantize_step(price, step)


def quantize_qty(qty: float, step: float) -> float:
    """Округлить размер ордера к шагу количества (qty step)."""
    return quantize_step(qty, step)
