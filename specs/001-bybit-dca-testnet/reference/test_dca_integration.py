"""
Автономные проверки DCA-цикла на mock-исполнении (e2e без сети).

Драйвер reference/dca_cycle.py гоняет стратегию по синтетическим стаканам:
рыночный вход, докупки по лестнице с Мартингейлом, поуровневый TP,
аварийный стоп и выход по времени. Стакан генерируется функцией book() —
обе стороны непустые, ликвидности с запасом, исполнение идёт по лучшему
уровню со спредом.

Сценарии:
  * TP-выход после двух докупок: книга 100 → 98.5 → 97.0 → 100.3;
    роли филлов [entry, dca, dca, close], эскалация TP до Level-2 (2.0%).
  * Стоп-выход: stop_pct 5%, книга 100 → 98.5 → 97.0 → 92.5 — убыток.
  * Выход по времени: max_hold_minutes 10, зависание на 99.9 — закрытие
    рыночным ордером ровно на 10-й минуте, без докупок.
  * TP-выход сразу после входа (без докупок): книга 100 → 102, докупка
    отменяется.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_dca_integration.py
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


dc = _load("dca_cycle")  # подтягивает backtest, mock_execution, pricing, bot

DcaParams = dc.DcaParams
MockExecutionManager = dc.MockExecutionManager
tp_price = dc.tp_price
take_profit_pct_at = dc.take_profit_pct_at

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def near(actual: float, expected: float, tol: float = 1e-9) -> bool:
    return abs(actual - expected) < tol


INST = {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}
SYMBOL = "BTCUSDT"
T0 = 1_700_000_000_000


def book(mid: float, spread: float = 0.01, depth: float = 500.0,
         levels: int = 3, tick: float = 0.01) -> dict:
    """Синтетический стакан: лучшая цена с половиной спреда, уровни с шагом tick.

    Обе стороны непустые и отсортированы (биды по убыванию, аски по
    возрастанию), глубина одного уровня заведомо больше любого ордера цикла.
    """
    half = mid * spread / 200.0
    bids = [[mid - half - i * tick, depth] for i in range(levels)]
    asks = [[mid + half + i * tick, depth] for i in range(levels)]
    return {"bids": bids, "asks": asks}


def base_params(**over) -> DcaParams:
    p = DcaParams(
        entry_usdt=20, dca_step_pct=1.2, max_docups=2, multiplier=2.0,
        tp_pct=1.2, tp_escalation=(1.2, 1.5, 2.0), fee_rate=0.00055,
        max_hold_minutes=240,
    )
    for k, v in over.items():
        setattr(p, k, v)
    p.validate()
    return p


def new_cycle(params: DcaParams):
    return dc.DcaCycle(params, SYMBOL, "Buy", INST["qty_step"],
                       INST["min_qty"], INST["tick_size"])


def no_open_orders(cyc) -> bool:
    return all(o["status"] != "open" for o in cyc.manager.orders.values())


# ── импорт и контракт модуля ──────────────────────────────────────────────────

print("контракт dca_cycle.py")
ok("DcaParams переиспользуется из backtest (та же лестница)",
   dc.DcaParams is dc.backtest.DcaParams)
ok("исполнение — MockExecutionManager из mock_execution",
   dc.MockExecutionManager is dc.mock_execution.MockExecutionManager)
ok("общие функции bot.py: tp_price и take_profit_pct_at",
   dc.tp_price is dc.bot.tp_price and dc.take_profit_pct_at is dc.bot.take_profit_pct_at)
ok("quantize_qty — из pricing", dc.quantize_qty is dc.backtest.quantize_qty)
ok("без ccxt (автономный прогон)", "ccxt" not in sys.modules)

print("\nDcaCycle: валидация и открытие")
try:
    dc.DcaCycle(base_params(), SYMBOL, "Hold", 0.001, 0.001, 0.01)
    ok("неизвестная сторона отклоняется", False)
except ValueError:
    ok("неизвестная сторона отклоняется", True)
_cyc = new_cycle(base_params())
ok("комиссии движка взяты из DcaParams.fee_rate",
   _cyc.manager.taker_fee == 0.00055 and _cyc.manager.maker_fee == 0.00055)
_cyc.open(book(100.0), T0)
ok("вход исполнился по лучшей цене (ask 100.005)",
   near(_cyc.avg_entry, 100.005, 1e-3) and near(_cyc.qty, 0.2, 1e-9),
   (_cyc.avg_entry, _cyc.qty))
ok("после входа докупок нет, цикл не закрыт",
   _cyc.docups == 0 and not _cyc.closed)
ok("уровень TP Level-0 (1.2%) выше входа",
   _cyc.tp_level - _cyc.avg_entry > _cyc.avg_entry * 0.011,
   _cyc.tp_level)
ok("первая докупка выставлена лимиткой на следующем уровне",
   "so_1" in _cyc.manager.orders
   and near(_cyc.manager.orders["so_1"]["price"], _cyc.next_level, 1e-6))
ok("TP-лимитка стоит на встречной стороне",
   _cyc.manager.orders[_cyc._tp_cid]["side"] == "sell")
try:
    _cyc.open(book(100.0), T0 + 1)
    ok("повторный open отклоняется", False)
except RuntimeError:
    ok("повторный open отклоняется", True)

# ── сценарий A: TP-выход после двух докупок (эскалация до Level-2) ───────────

print("\nсценарий A: 100 → 98.5 → 97.0 → 100.3, TP Level-2")
_a = new_cycle(base_params())
_a.open(book(100.0), T0)
ok("A: после входа стоит докупка so_1", "so_1" in _a.manager.orders)
_a.step(book(98.5), T0 + 60_000)
ok("A: первая докупка сработала на просадке",
   _a.docups == 1 and near(_a.qty, 0.605, 1e-9), _a.qty)
ok("A: после доливки TP переставлен выше (Level-1, 1.5%)",
   near(_a.tp_level, tp_price(_a.avg_entry, "Buy", 1.5, 0.01), 1e-9),
   _a.tp_level)
_a.step(book(97.0), T0 + 120_000)
ok("A: вторая докупка с Мартингейлом x4 от входа",
   _a.docups == 2 and near(_a.qty, 1.427, 1e-9), _a.qty)
ok("A: лестница остановилась на max_docups (so_3 не выставлялась)",
   "so_3" not in _a.manager.orders
   and all(o["id"] != "so_3" for o in _a.manager.history))
_a.step(book(100.3), T0 + 180_000)
ok("A: цикл закрыт по TP", _a.closed and _a.exit_reason == "take_profit")
ok("A: роли филлов [entry, dca, dca, close]",
   [f["role"] for f in _a.fills] == ["entry", "dca", "dca", "close"],
   [f["role"] for f in _a.fills])
ok("A: выкуплена вся позиция (entry+dca == close)",
   near(sum(f["qty"] for f in _a.fills if f["role"] != "close"),
        sum(f["qty"] for f in _a.fills if f["role"] == "close"), 1e-9))
ok("A: TP на эскалации Level-2 (2.0%), а не на фиксированном 1.2%",
   near(_a.tp_level, tp_price(_a.avg_entry, "Buy", 2.0, 0.01), 1e-9)
   and _a.tp_level - _a.avg_entry > _a.avg_entry * 0.015,
   _a.tp_level)
ok("A: итоговый PnL положительный", _a.pnl > 0, _a.pnl)
ok("A: все ордера завершены, открытых нет",
   no_open_orders(_a) and len(_a.manager.orders) == 0)
ok("A: закрытый TP-ордер в истории", "tp_3" in [o["id"] for o in _a.manager.history])

# ── сценарий B: аварийный стоп ────────────────────────────────────────────────

print("\nсценарий B: 100 → 98.5 → 97.0 → 92.5, стоп 5%")
_b = new_cycle(base_params(stop_pct=5.0))
_b.open(book(100.0), T0)
_b.step(book(98.5), T0 + 60_000)
_b.step(book(97.0), T0 + 120_000)
ok("B: до стопа обе докупки исполнены", _b.docups == 2 and not _b.closed)
_b.step(book(92.5), T0 + 180_000)
ok("B: цикл закрыт по стопу", _b.closed and _b.exit_reason == "stop")
ok("B: выход рыночным по лучшему биду (ниже 93)",
   _b.exit_price < 93 and _b.exit_price > 92.4, _b.exit_price)
ok("B: итоговый PnL отрицательный", _b.pnl < 0, _b.pnl)
ok("B: открытых ордеров не осталось", no_open_orders(_b))

# ── сценарий C: выход по времени ──────────────────────────────────────────────

print("\nсценарий C: зависание на 99.9, time_exit на 10-й минуте")
_c = new_cycle(base_params(max_hold_minutes=10))
_c.open(book(100.0), T0)
for i in range(1, 10):
    _c.step(book(99.9), T0 + i * 60_000)
ok("C: до истечения удержания цикл открыт, докупок нет",
   not _c.closed and _c.docups == 0)
_c.step(book(99.9), T0 + 10 * 60_000)
ok("C: закрыт по таймеру ровно на 10-й минуте",
   _c.closed and _c.exit_reason == "time_exit"
   and _c.exit_ts - _c.open_ts == 600_000,
   _c.exit_ts - _c.open_ts)
_c.step(book(99.9), T0 + 11 * 60_000)
ok("C: закрытый цикл последующими тактами не трогается",
   _c.closed and _c.exit_ts - _c.open_ts == 600_000)
ok("C: открытых ордеров не осталось (TP и докупка отменены)",
   no_open_orders(_c)
   and any(o["id"] == "so_1" and o["status"] == "canceled"
           for o in _c.manager.history))

# ── сценарий D: TP сразу после входа, докупка отменяется ─────────────────────

print("\nсценарий D: 100 → 102, TP без докупок")
_d = new_cycle(base_params())
_d.open(book(100.0), T0)
ok("D: перед пампом стоит и TP, и докупка",
   _d._tp_cid in _d.manager.orders and "so_1" in _d.manager.orders)
_d.step(book(102.0), T0 + 60_000)
ok("D: цикл закрыт по TP с прибылью без докупок",
   _d.closed and _d.exit_reason == "take_profit"
   and _d.docups == 0 and _d.pnl > 0, _d.pnl)
ok("D: неисполненная докупка отменена при закрытии",
   _d.manager.orders.get("so_1", {}).get("status") == "canceled")
ok("D: открытых ордеров не осталось", no_open_orders(_d))

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
