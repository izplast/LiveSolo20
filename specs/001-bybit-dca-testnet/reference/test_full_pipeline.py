"""
Автономная проверка полной интеграции: скринер → DCA-сетка → mock-исполнение.

Полный конвейер без сети:
  1. скринер (screener.py: SymbolState + evaluate) детектит бычий сигнал на
     синтетических свечах и отдаёт сторону Buy (color_to_side);
  2. на цене открытия сигнальной свечи открывается DcaCycle (рыночный вход)
     и строится сетка: TP-лимитка + первая докупка на следующем уровне;
  3. падающий синтетический стакан дотягивает уровни — докупки исполняются
     лимитками, TP после каждой доливки пересчитывается от средней по
     эскалации take_profit_pct_at (Level 1 → 1.5%, Level 2 → 2.0%);
  4. отскок стакана вверх закрывает позицию филлом TP-лимитки — цикл
     завершён, все ордера завершены, PnL положительный.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_full_pipeline.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

_ref = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> "module":
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dc = _load("dca_cycle")          # подтягивает backtest, mock_execution, bot, pricing
bt = dc.backtest
sc = sys.modules["screener"]

DcaParams = dc.DcaParams
DcaCycle = dc.DcaCycle
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


SYMBOL = "BTCUSDT"
INST = {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}


def book(mid: float, spread: float = 0.01, depth: float = 500.0,
         levels: int = 3, tick: float = 0.01) -> dict:
    """Синтетический стакан: биды по убыванию, аски по возрастанию, оба непустые."""
    half = mid * spread / 200.0
    bids = [[mid - half - i * tick, depth] for i in range(levels)]
    asks = [[mid + half + i * tick, depth] for i in range(levels)]
    return {"bids": bids, "asks": asks}


def bars(n: int, start: float = 100.0, drift: float = 0.15, rng: float = 1.0,
         ts0: int = 0, step_ms: int = 60_000, amp: float = 0.5, freq: float = 5.0) -> list[list]:
    """Свечи [ts, open, high, low, close, volume]; drift — % на бар, rng — размах в %.

    Волнистая база (amp % с периодом freq баров) вместо идеально прямой:
    на прямой UHLO 1м стоит в «углу» (0 и 100 одновременно) и сигнал
    режется фильтром uhlo_corner.
    """
    import math
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100 + amp * math.sin(2 * math.pi * i / freq) / 100)
        out.append([ts0 + i * step_ms, base, base * (1 + rng / 100),
                    base, base * (1 + rng / 200), 1.0])
    return out


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


def detect_signal(rows: list[list], cfg: "sc.Config") -> dict:
    """Первый сигнал скринера: повторяет логику Backtest.run (сигнал → цена открытия).

    Возвращает {side, ts, open} — сторону, момент и цену открытия следующей
    свечи, по которой сигнал исполняется рыночным входом.
    """
    state = sc.SymbolState(
        max(cfg.natr_period + 2, cfg.uhlo_length * 2 + 2),
        cfg.uhlo_length * 2 + 2,
    )
    slow = bt.aggregate_minutes(rows, 15)
    slow_bucket = 15 * 60_000
    slow_ptr = 0
    pending_side: str | None = None
    for row in rows:
        ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        while slow_ptr < len(slow) and slow[slow_ptr][0] + slow_bucket <= ts:
            state.push("slow", slow[slow_ptr][:5])
            slow_ptr += 1
        if pending_side is not None:
            return {"side": pending_side, "ts": ts, "open": o}
        is_new = state.push("fast", row[:5])
        if not is_new:
            continue
        d = sc.evaluate(list(state.fast), list(state.slow), cfg)
        if (d.passed and d.color != state.last_color
                and ts - state.last_signal_ms >= cfg.cooldown_sec * 1000):
            state.last_color = d.color
            state.last_signal_ms = ts
            pending_side = sc.color_to_side(d.color)
    raise AssertionError("скринер не выдал сигнала на синтетических данных")


# ── 1. скринер: сигнал на синтетическом тренде ────────────────────────────────

print("скринер: бычий сигнал на синтетическом тренде")
cfg = sc.Config()
sc.validate_config(cfg)
now = int(time.time() * 1000)
ts0 = (now // 60_000 - 2200) * 60_000
rows = bars(2000, drift=0.15, rng=1.2, ts0=ts0)
ok("сигнал найден, сторона Buy (uptrend → green)",
   detect_signal(rows, cfg)["side"] == "Buy")
_sig = detect_signal(rows, cfg)
_idx = (_sig["ts"] - ts0) // 60_000
ok("сигнал исполняется по цене открытия своей свечи",
   _sig["open"] == rows[_idx][1], (_sig["open"], rows[_idx][1]))
print(f"    сигнал: side={_sig['side']} ts={_sig['ts'] - ts0}мс open={_sig['open']:.4f}")

# ── 2. создание сетки: рыночный вход + TP + первая докупка ───────────────────

print("\nсоздание DCA-сетки по сигналу")
params = base_params()
cycle = DcaCycle(params, SYMBOL, _sig["side"], INST["qty_step"],
                 INST["min_qty"], INST["tick_size"])
cycle.open(book(_sig["open"]), _sig["ts"])
ok("вход исполнен рыночно по ask сигнальной цены",
   cycle.qty > 0 and cycle.fills[0]["ts"] == _sig["ts"]
   and near(cycle.fills[0]["price"], _sig["open"] * (1 + 0.00005), 1e-3),
   (cycle.fills[0]["price"], _sig["open"]))
ok("TP-лимитка на встречной стороне по Level-0 (1.2%)",
   cycle._tp_cid in cycle.manager.orders
   and near(cycle.tp_level, tp_price(cycle.avg_entry, "Buy", 1.2, 0.01), 1e-9),
   cycle.tp_level)
ok("первая докупка сетки выставлена (so_1, объём x2 от входа)",
   "so_1" in cycle.manager.orders
   and near(cycle.manager.orders["so_1"]["amount"],
            20 * 2 ** 1 / cycle.next_level, 1e-3),
   cycle.manager.orders["so_1"]["amount"])
ok("уровень докупки ниже входа на шаг сетки",
   cycle.next_level < cycle.avg_entry * (1 - 0.011), cycle.next_level)

# ── 3. доливки на падающем стакане + пересчёт TP ─────────────────────────────

print("\nдоливки на падающем стакане")
prev_distance = 0.0
for i in range(1, params.max_docups + 1):
    level = cycle.next_level
    cycle.step(book(level * (1 - 0.0002)), _sig["ts"] + i * 60_000)
    ok(f"докупка {i}/{params.max_docups} исполнена на просадке",
       cycle.docups == i, cycle.docups)
    expected_tp = tp_price(cycle.avg_entry, "Buy",
                           take_profit_pct_at(params, cycle.docups), 0.01)
    ok(f"TP пересчитан после доливки {i} (уровень эскалации)",
       near(cycle.tp_level, expected_tp, 1e-9), cycle.tp_level)
    distance = cycle.tp_level - cycle.avg_entry
    ok(f"TP-дистанция выросла после доливки {i} ({distance:.4f})",
       distance > prev_distance, distance)
    prev_distance = distance

ok("сетка остановлена на max_docups (so_3 не выставлялась)",
   "so_3" not in cycle.manager.orders
   and all(o["id"] != "so_3" for o in cycle.manager.history))
ok("позиция собрана из входа и двух доливок",
   near(cycle.qty, cycle.fills[0]["qty"] + sum(
       f["qty"] for f in cycle.fills if f["role"] == "dca"), 1e-9), cycle.qty)

# ── 4. отскок стакана: TP-лимитка закрывает позицию ───────────────────────────

print("\nзакрытие позиции по TP")
recovery = cycle.tp_level * (1 + 0.001)
cycle.step(book(recovery), _sig["ts"] + (params.max_docups + 1) * 60_000)
ok("позиция закрыта филлом TP-лимитки",
   cycle.closed and cycle.exit_reason == "take_profit", cycle.exit_reason)
ok("роли филлов [entry, dca, dca, close]",
   [f["role"] for f in cycle.fills] == ["entry", "dca", "dca", "close"],
   [f["role"] for f in cycle.fills])
ok("TP по эскалации Level-2 (>= 1.5% от средней)",
   cycle.tp_level - cycle.avg_entry > cycle.avg_entry * 0.015,
   cycle.tp_level)
ok("закрытие на отскоке выше TP-уровня",
   cycle.exit_price > cycle.tp_level, cycle.exit_price)
ok("итоговый PnL положительный (прибыль > комиссий)", cycle.pnl > 0, cycle.pnl)
ok("открытых ордеров не осталось",
   all(o["status"] != "open" for o in cycle.manager.orders.values()))

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
