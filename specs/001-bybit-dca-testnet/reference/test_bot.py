"""
Автономные проверки логики ордеров DCA-бота (reference/bot.py).

Режимы ордеров ENTRY/ADJUST/CLOSE, уровень тейк-профита от средней цены
входа с квантованием к шагу, одноразовая установка плеча. Сети нет:
обращения к API передаются инъекцией-заглушкой.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_bot.py
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
    sys.modules[name] = mod  # до exec_module: bot.py импортирует pricing
    spec.loader.exec_module(mod)
    return mod


pricing = _load("pricing")
bot = _load("bot")

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
    except (ValueError, TypeError):
        return True


# ── режимы ордеров ────────────────────────────────────────────────────────────

print("режимы ордеров")
ok("ENTRY/ADJUST/CLOSE заданы",
   {m.value for m in bot.OrderMode} == {"entry", "adjust", "close"})
ok("ENTRY не reduce-only", bot.REDUCE_ONLY[bot.OrderMode.ENTRY] is False)
ok("ADJUST и CLOSE reduce-only",
   bot.REDUCE_ONLY[bot.OrderMode.ADJUST] is True
   and bot.REDUCE_ONLY[bot.OrderMode.CLOSE] is True)
ok("сторона закрытия лонга — Sell", bot.exit_side("Buy") == "Sell")
ok("сторона закрытия шорта — Buy", bot.exit_side("Sell") == "Buy")
ok("неизвестная сторона отвергается", raises(lambda: bot.exit_side("Long")))

# ── первый вход (US1) ─────────────────────────────────────────────────────────

print("\nпервый вход")
e = bot.entry_request("BTCUSDT", "Buy", 100.55, "sig:1", 0.1)
ok("режим ENTRY", e.mode == bot.OrderMode.ENTRY)
ok("reduce_only=False", e.reduce_only is False)
ok("размер приведён к шагу количества", e.qty == 100.6, e.qty)
ok("размер — float", isinstance(e.qty, float))
ok("order_link_id пробрасывается", e.order_link_id == "sig:1")
ok("рыночный вход без лимитной цены", e.price is None)

# ── уровень тейк-профита (US2) ───────────────────────────────────────────────

print("\ntp_price")
ok("лонг: +1% от 100 при шаге 0.01 → 101.0", bot.tp_price(100.0, "Buy", 1.0, 0.01) == 101.0)
ok("шорт: −1% от 100 при шаге 0.01 → 99.0", bot.tp_price(100.0, "Sell", 1.0, 0.01) == 99.0)
ok("TP квантован к шагу (1.235 % → 101.24)",
   bot.tp_price(100.0, "Buy", 1.235, 0.01) == 101.24)
ok("TP квантован у шорта", bot.tp_price(100.0, "Sell", 1.235, 0.01) == 98.77)
ok("неположительная средняя цена отвергается",
   raises(lambda: bot.tp_price(0.0, "Buy", 1.0, 0.01)))
ok("неположительный tp_pct отвергается",
   raises(lambda: bot.tp_price(100.0, "Buy", 0.0, 0.01)))

# ── set_take_profit: режимы CLOSE и ADJUST (US2) ─────────────────────────────

print("\nset_take_profit")
tp1 = bot.set_take_profit("BTCUSDT", "Buy", 100.0, 1.0, 0.01, "c1:tp", has_existing=False)
ok("первичная установка → режим CLOSE", tp1.mode == bot.OrderMode.CLOSE)
ok("CLOSE — встречная сторона (Sell)", tp1.side == "Sell")
ok("CLOSE — reduce_only", tp1.reduce_only is True)
ok("цена TP = 101.0", tp1.price == 101.0, tp1.price)

tp2 = bot.set_take_profit("BTCUSDT", "Buy", 100.5, 1.0, 0.01, "c1:tp", has_existing=True)
ok("перенос после докупки → режим ADJUST", tp2.mode == bot.OrderMode.ADJUST)
ok("ADJUST — reduce_only", tp2.reduce_only is True)
ok("ADJUST — уровень от новой средней (100.5 → 101.51)",
   tp2.price == 101.51, tp2.price)
ok("ADJUST — встречная сторона", tp2.side == "Sell")
ok("order_link_id пробрасывается в обоих режимах",
   tp1.order_link_id == tp2.order_link_id == "c1:tp")

# ── set_leverage_once (US1, FR-018) ───────────────────────────────────────────

print("\nset_leverage_once")
calls: list[tuple] = []


def apply_leverage(symbol: str, leverage: float) -> None:
    calls.append((symbol, leverage))


ls = bot.LeverageSetter()
ok("первый вызов применяет плечо", ls.set_leverage_once("BTCUSDT", 3.0, apply_leverage) is True)
ok("вызвано ровно один раз", calls == [("BTCUSDT", 3.0)], calls)
ok("повторный вызов не дёргает API",
   ls.set_leverage_once("BTCUSDT", 3.0, apply_leverage) is False)
ok("API вызван по-прежнему один раз", len(calls) == 1, calls)
ok("другой символ применяется отдельно",
   ls.set_leverage_once("ETHUSDT", 3.0, apply_leverage) is True)
ok("разные символы не путаются", len(calls) == 2 and calls[1] == ("ETHUSDT", 3.0), calls)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
