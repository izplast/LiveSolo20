"""
reference/dca_cycle.py — драйвер DCA-цикла поверх mock-исполнения.

Сводит стратегию «вход → докупки по лестнице с Мартингейлом → поуровневый
TP / стоп / выход по времени» со снапшотным матчингом MockExecutionManager
(reference/mock_execution.py). Параметры цикла — backtest.DcaParams (та же
лестница: entry_usdt, dca_step_pct, max_docups, multiplier, tp_escalation,
stop_pct, max_hold_minutes, fee_rate); уровни TP и квантование — общие
функции bot.tp_price / bot.take_profit_pct_at и pricing.quantize_qty.

Как работает цикл:
  * open(orderbook, now_ms) — рыночный вход (client_order_id="entry") по
    текущему стакану; после филла выставляются TP-лимитка (встречная
    сторона) и первая докупка (лимитка на следующем уровне).
  * step(orderbook, now_ms) — применить снимок к открытым ордерам, разобрать
    филлы (entry/so_*/tp_*/close_*), затем проверить стоп и таймер.
  * Докупка выставляется лимиткой на next_level = последний филл ± step%;
    срабатывает, когда стакан дотягивается до уровня. TP после каждой
    докупки переставляется (cancel+replace) на новый уровень от средней —
    процент выбирает take_profit_pct_at (эскалация по числу докупок).
  * Закрытие — филл TP-лимитки (take_profit) либо рыночный ордер
    close_<reason> (stop / time_exit), сводимый с тем же стаканом.

Цикл не мутирует стакан: apply_snapshot копирует уровни, поэтому повторное
сведение одного снимка (например, при рыночном закрытии) безопасно.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_dca_integration.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    """Загружает модуль из того же каталога, регистрируя его в sys.modules."""
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с dca_cycle.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mock_execution = _load_sibling("mock_execution")
backtest = _load_sibling("backtest")  # подтягивает screener, pricing, bot

MockExecutionManager = mock_execution.MockExecutionManager
EPSILON = mock_execution.EPSILON
DcaParams = backtest.DcaParams
quantize_qty = backtest.quantize_qty
bot = backtest.bot
tp_price = bot.tp_price
take_profit_pct_at = bot.take_profit_pct_at
effective_step_pct = backtest.effective_step_pct
effective_stop_pct = backtest.effective_stop_pct


class DcaCycle:
    """Один DCA-цикл: вход, лестница докупок, поуровневый TP, стоп/таймер.

    Исполнение ведёт MockExecutionManager; цикл лишь выставляет ордера и
    пересчитывает уровни после каждого филла. Стакан передаётся извне
    (синтетический генератор в тестах или drain_to_latest от стримера).
    """

    def __init__(self, params: DcaParams, symbol: str, side: str,
                 qty_step: float, min_qty: float, tick_size: float,
                 natr: float | None = None) -> None:
        if side not in ("Buy", "Sell"):
            raise ValueError(f"side должен быть Buy или Sell, получено {side!r}")
        self.p = params
        self.symbol = symbol
        self.side = side
        self.natr = natr               # NATR-14 сигнала скринера (адаптив шага/стопа)
        self.qty_step = qty_step
        self.min_qty = min_qty
        self.tick_size = tick_size
        self.manager = MockExecutionManager(maker_fee=params.fee_rate,
                                            taker_fee=params.fee_rate)
        self.open_ts = 0
        self.qty = 0.0
        self.avg_entry = 0.0
        self.docups = 0
        self.tp_level = 0.0
        self.stop_level = 0.0
        self.next_level = 0.0
        self.last_fill_price = 0.0
        self.fee = 0.0
        self.fills: list[dict] = []
        self.closed = False
        self.exit_ts = 0
        self.exit_price = 0.0
        self.exit_reason = ""
        self.pnl = 0.0
        self._tp_cid: str | None = None
        self._tp_seq = 0

    @property
    def duration_minutes(self) -> float:
        return (self.exit_ts - self.open_ts) / 60_000

    @staticmethod
    def _api_side(side: str) -> str:
        """Сторона в контракте mock-исполнения (нижний регистр)."""
        return "buy" if side == "Buy" else "sell"

    # ── публичные шаги цикла ──────────────────────────────────────────────────

    def open(self, orderbook: dict, now_ms: int) -> None:
        """Рыночный вход по стакану; после филла выставляются TP и первая докупка."""
        if self.open_ts or self.closed:
            raise RuntimeError("цикл уже открыт: DcaCycle.open вызывается один раз")
        best_bid, best_ask = self._best(orderbook)
        ref = best_ask if self.side == "Buy" else best_bid
        if ref <= 0:
            raise RuntimeError("пустой стакан: не с чем сводить вход")
        qty = quantize_qty(self.p.entry_usdt / ref, self.qty_step)
        if qty < self.min_qty:
            qty = quantize_qty(self.min_qty, self.qty_step)
        if qty <= 0:
            qty = self.min_qty
        self.open_ts = now_ms
        self.manager.create_order(self.symbol, "market", self._api_side(self.side),
                                  qty, client_order_id="entry")
        fills = self.manager.apply_snapshot(self.symbol, orderbook)
        self._on_fills(fills, now_ms)
        if self.qty <= 0:
            raise RuntimeError("вход не исполнился по стакану")

    def step(self, orderbook: dict, now_ms: int) -> None:
        """Такт: свести снимок, разобрать филлы, проверить стоп и таймер."""
        if self.closed:
            return
        fills = self.manager.apply_snapshot(self.symbol, orderbook)
        self._on_fills(fills, now_ms)
        if self.closed:
            return

        best_bid, best_ask = self._best(orderbook)
        if self.side == "Buy":
            stop_hit = bool(self.stop_level and best_bid <= self.stop_level)
        else:
            stop_hit = bool(self.stop_level and best_ask >= self.stop_level)
        if stop_hit:
            self._market_close(orderbook, "stop", now_ms)
            return
        if now_ms - self.open_ts >= self.p.max_hold_minutes * 60_000:
            self._market_close(orderbook, "time_exit", now_ms)

    # ── разбор филлов ─────────────────────────────────────────────────────────

    def _on_fills(self, fills: list[dict], now_ms: int) -> None:
        for fill in fills:
            cid = fill["order_id"]
            if cid == "entry":
                self._accrue(fill, "entry", now_ms)
                self._after_position_change()
            elif cid.startswith("so_"):
                self.docups += 1
                self._accrue(fill, "dca", now_ms)
                self._after_position_change()
            elif cid.startswith("tp_"):
                self._close(fill, "take_profit", now_ms)
            elif cid.startswith("close_"):
                self._close(fill, cid[len("close_"):], now_ms)

    def _accrue(self, fill: dict, role: str, now_ms: int) -> None:
        qty, price = fill["qty"], fill["price"]
        self.fee += fill["fee"]
        new_qty = self.qty + qty
        self.avg_entry = (self.avg_entry * self.qty + price * qty) / new_qty
        self.qty = new_qty
        self.last_fill_price = price
        self.fills.append({"ts": now_ms, "role": role, "side": self.side,
                           "qty": qty, "price": price})

    def _close(self, fill: dict, reason: str, now_ms: int) -> None:
        self.exit_price = fill["price"]
        self.exit_ts = now_ms
        self.exit_reason = reason
        self.fee += fill["fee"]
        if self.side == "Buy":
            self.pnl = (fill["price"] - self.avg_entry) * self.qty
        else:
            self.pnl = (self.avg_entry - fill["price"]) * self.qty
        self.pnl -= self.fee
        self.fills.append({"ts": now_ms, "role": "close", "side": self.side,
                           "qty": self.qty, "price": fill["price"]})
        self.closed = True
        self._cancel_aux_orders()

    # ── пересчёт уровней после филла входа/докупки (FR-011) ───────────────────

    def _after_position_change(self) -> None:
        p = self.p
        step_pct = effective_step_pct(p, self.natr)
        stop_pct = effective_stop_pct(p, self.natr)
        self.tp_level = tp_price(self.avg_entry, self.side,
                                 take_profit_pct_at(p, self.docups),
                                 self.tick_size)
        self._replace_tp()
        if stop_pct:
            factor = 1 - stop_pct / 100 if self.side == "Buy" else 1 + stop_pct / 100
            self.stop_level = self.avg_entry * factor
        self.next_level = self.last_fill_price * (
            1 - step_pct / 100 if self.side == "Buy"
            else 1 + step_pct / 100)
        if self.docups < p.max_docups:
            qty = quantize_qty(
                p.entry_usdt * p.multiplier ** (self.docups + 1) / self.next_level,
                self.qty_step)
            if qty < self.min_qty:
                qty = quantize_qty(self.min_qty, self.qty_step)
            if qty <= 0:
                qty = self.min_qty
            self.manager.create_order(self.symbol, "limit", self._api_side(self.side),
                                      qty, self.next_level,
                                      client_order_id=f"so_{self.docups + 1}")

    def _replace_tp(self) -> None:
        if self._tp_cid is not None:
            self.manager.cancel_order(self._tp_cid)
        self._tp_seq += 1
        cid = f"tp_{self._tp_seq}"
        exit_side = self._api_side("Sell" if self.side == "Buy" else "Buy")
        self.manager.create_order(self.symbol, "limit", exit_side, self.qty,
                                  self.tp_level, client_order_id=cid)
        self._tp_cid = cid

    def _cancel_aux_orders(self) -> None:
        if self._tp_cid is not None:
            self.manager.cancel_order(self._tp_cid)
            self._tp_cid = None
        for cid in [c for c in self.manager.orders if c.startswith("so_")]:
            self.manager.cancel_order(cid)

    def _market_close(self, orderbook: dict, reason: str, now_ms: int) -> None:
        self._cancel_aux_orders()
        exit_side = self._api_side("Sell" if self.side == "Buy" else "Buy")
        self.manager.create_order(self.symbol, "market", exit_side, self.qty,
                                  client_order_id=f"close_{reason}")
        fills = self.manager.apply_snapshot(self.symbol, orderbook)
        self._on_fills(fills, now_ms)

    @staticmethod
    def _best(orderbook: dict) -> tuple[float, float]:
        bids = [b for b in orderbook.get("bids", []) if b[1] > EPSILON]
        asks = [a for a in orderbook.get("asks", []) if a[1] > EPSILON]
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        return best_bid, best_ask
