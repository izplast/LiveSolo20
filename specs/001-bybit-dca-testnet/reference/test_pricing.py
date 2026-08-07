"""
Автономные проверки квантования цены к шагу биржи (FR-017).

Проверяемое правило: округление к ближайшему кратному шага, а при цене
ровно на половине шага от обоих соседних кратных — вверх. Стандартный
round() тут не подходит: он округляет половину к чётному, и на границе
дал бы несимметричный результат.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_pricing.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.py")
_spec = importlib.util.spec_from_file_location("pricing_under_test", _path)
assert _spec and _spec.loader
pr = importlib.util.module_from_spec(_spec)
sys.modules["pricing_under_test"] = pr
_spec.loader.exec_module(pr)

q = pr.quantize_price

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def near(actual: object, expected: object) -> bool:
    """Проверка точного равенства: результат должен быть представим как есть."""
    return actual == expected and isinstance(actual, float)


def _raises(price: float, step: float) -> bool:
    try:
        q(price, step)
    except ValueError:
        return True
    return False


def _raises_via(fn) -> bool:
    try:
        fn(0.1, 0)
    except ValueError:
        return True
    return False


# ── базовое округление к ближайшему кратному ─────────────────────────────────

print("quantize_price: ближайшее кратное шага")
ok("1.234 при шаге 0.01 → 1.23", near(q(1.234, 0.01), 1.23))
ok("1.236 при шаге 0.01 → 1.24", near(q(1.236, 0.01), 1.24))
ok("0.04 при шаге 0.1 → 0.0", near(q(0.04, 0.1), 0.0))
ok("0.07 при шаге 0.1 → 0.1", near(q(0.07, 0.1), 0.1))
ok("68142.42 при шаге 0.01 → 68142.42 (кратное не трогаем)",
   near(q(68142.42, 0.01), 68142.42))

# ── половина шага — вверх (ключевое правило) ─────────────────────────────────

print("\nполовина шага — вверх")
ok("1.235 при шаге 0.01 → 1.24 (tie)", near(q(1.235, 0.01), 1.24))
ok("1.25 при шаге 0.1 → 1.3 (tie)", near(q(1.25, 0.1), 1.3))
ok("0.05 при шаге 0.1 → 0.1 (tie)", near(q(0.05, 0.1), 0.1))
ok("0.3 при шаге 0.2 → 0.4 (tie)", near(q(0.3, 0.2), 0.4))
ok("5.5 при шаге 1 → 6.0 (tie)", near(q(5.5, 1), 6.0))
ok("68142.1235 при шаге 0.001 → 68142.124 (tie)",
   near(q(68142.1235, 0.001), 68142.124))

# ── мелкие шаги крипто-фьючерсов без дрейфа float ────────────────────────────

print("\nмелкие шаги, отсутствие дрейфа float")
ok("0.000014 при шаге 0.00001 → 0.00001", near(q(0.000014, 0.00001), 0.00001))
ok("0.000015 при шаге 0.00001 → 0.00002 (tie)", near(q(0.000015, 0.00001), 0.00002))
ok("0.000016 при шаге 0.00001 → 0.00002", near(q(0.000016, 0.00001), 0.00002))
ok("0.0000025 при шаге 0.00001 → 0.0", near(q(0.0000025, 0.00001), 0.0))
ok("0.125 при шаге 0.5 → 0.0", near(q(0.125, 0.5), 0.0))
ok("0.25 при шаге 0.5 → 0.5 (tie)", near(q(0.25, 0.5), 0.5))

# ── границы: шаг больше цены, цена ровно на кратном ──────────────────────────

print("\nграницы")
ok("цена ниже шага округляется к нулю", near(q(0.03, 0.1), 0.0))
ok("цена ровно на кратном не меняется", near(q(1.2, 0.1), 1.2))
ok("цена ровно на кратном ровно в tie", near(q(0.1, 0.1), 0.1))
ok("большая цена, шаг 1 → целое", near(q(99999.6, 1), 100000.0))
ok("шаг 0 → ValueError", _raises(0.1, 0))
ok("шаг отрицательный → ValueError", _raises(0.1, -0.01))


# ── тип результата ────────────────────────────────────────────────────────────

print("\nтип результата")
ok("результат — float", isinstance(q(1.235, 0.01), float))
ok("результат на tie — не int", isinstance(q(5.5, 1), float))

# ── размер ордера к шагу количества ───────────────────────────────────────────

print("\nquantize_qty: размер к шагу количества")
qq = pr.quantize_qty
ok("100.5 при шаге 0.1 → 100.5 (кратное)", near(qq(100.5, 0.1), 100.5))
ok("100.55 при шаге 0.1 → 100.6 (tie)", near(qq(100.55, 0.1), 100.6))
ok("0.012 при шаге 0.001 → 0.012", near(qq(0.012, 0.001), 0.012))
ok("0.0125 при шаге 0.001 → 0.013 (tie)", near(qq(0.0125, 0.001), 0.013))
ok("шаг 0 → ValueError", _raises_via(qq))

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
