"""
reference/reconcile.py — сверка локального стейта бота с состоянием Bybit.

Автономный слой без сетевых вызовов: по локальному стейту (открытые DCA-циклы
и их ордера) и снимку биржи (Bybit REST `/v5/position/list` и
`/v5/order/realtime`) находит расхождения и предлагает действие для каждого.

Сверка запускается при старте бота и после сбоя: локальный журнал может
отстать (процесс убит до записи `cycle_closed`) или разойтись (позиция
открыта, но филл не доехал; ордер закрыт, но монитор не успел это заметить).

Что сравнивается:

* позиции — по символу: нет на бирже (`missing_on_exchange`), есть на бирже,
  но не у нас (`unknown_local`), размер/сторона/средняя цена не совпадают
  (`qty_mismatch`, `side_mismatch`, `avg_price_mismatch`);
* ордера — по orderLinkId/order_link_id: нет на бирже (`missing_on_exchange`),
  есть на бирже, но не у нас (`unknown_local`), параметры разошлись
  (`qty_mismatch`, `price_mismatch`, `side_mismatch`, `reduce_only_mismatch`),
  статус на бирже уже не open (`not_open_exchange`).

Каждое расхождение несёт `action` — что бот должен сделать, чтобы вернуть
состояние в согласие (закрыть локальный цикл, принять позицию биржи,
отменить/перевыставить ордер, скорректировать размер). Сами вызовы API —
инъекция: модуль их не делает, поэтому проверяется без сети.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_reconcile.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

QTY_EPS = 1e-9
PRICE_EPS_RATIO = 1e-6


def _f(value: Any) -> float:
    return float(value) if value is not None else 0.0


@dataclass(frozen=True)
class LocalPosition:
    """Открытая позиция в локальном стейте бота (из DCA-цикла)."""

    symbol: str
    side: str                 # Buy / Sell
    qty: float
    avg_entry: float
    cycle_id: str | None = None


@dataclass(frozen=True)
class LocalOrder:
    """Открытый ордер в локальном стейте бота."""

    symbol: str
    side: str                 # Buy / Sell
    qty: float
    price: float | None
    reduce_only: bool
    order_link_id: str
    kind: str = "unknown"     # entry / tp / so_* / close_*
    cycle_id: str | None = None


@dataclass(frozen=True)
class ExchangePosition:
    """Позиция с биржи (Bybit /v5/position/list)."""

    symbol: str
    side: str                 # Buy / Sell
    qty: float
    avg_price: float
    status: str = "Normal"


@dataclass(frozen=True)
class ExchangeOrder:
    """Ордер с биржи (Bybit /v5/order/realtime)."""

    symbol: str
    side: str
    qty: float
    price: float | None
    reduce_only: bool
    order_id: str
    order_link_id: str
    status: str = "New"       # New / PartiallyFilled / ...
    kind: str = "unknown"


@dataclass(frozen=True)
class Discrepancy:
    """Одно расхождение локального стейта и биржи + предлагаемое действие."""

    kind: str
    symbol: str
    message: str
    action: str
    ref_id: str | None = None
    local: object | None = None
    exchange: object | None = None


@dataclass
class ReconciliationReport:
    """Результат сверки: расхождения и сгруппированная сводка действий."""

    position_discrepancies: list[Discrepancy] = field(default_factory=list)
    order_discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def all(self) -> list[Discrepancy]:
        return self.position_discrepancies + self.order_discrepancies

    @property
    def ok(self) -> bool:
        return not self.all

    def actions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.all:
            out[d.action] = out.get(d.action, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Парсинг ответов Bybit REST
# ---------------------------------------------------------------------------

def parse_positions(rows: Iterable[dict]) -> list[ExchangePosition]:
    """rows из result.list /v5/position/list → ExchangePosition[]. Строковые
    size/avgPrice приводятся к float; позиции нулевого размера пропускаются."""
    out: list[ExchangePosition] = []
    for r in rows:
        size = _f(r.get("size"))
        if size <= QTY_EPS:
            continue
        out.append(ExchangePosition(
            symbol=str(r.get("symbol", "?")),
            side=str(r.get("side") or "None"),
            qty=size,
            avg_price=_f(r.get("avgPrice")),
            status=str(r.get("positionStatus") or "Normal"),
        ))
    return out


def parse_orders(rows: Iterable[dict]) -> list[ExchangeOrder]:
    """rows из result.list /v5/order/realtime → ExchangeOrder[]. Открытые —
    статус New/PartiallyFilled; параметры reduceOnly/qty/price строковые."""
    out: list[ExchangeOrder] = []
    for r in rows:
        out.append(ExchangeOrder(
            symbol=str(r.get("symbol", "?")),
            side=str(r.get("side") or "None"),
            qty=_f(r.get("qty")),
            price=_f(r.get("price")) if r.get("price") not in (None, "0") else None,
            reduce_only=bool(r.get("reduceOnly")),
            order_id=str(r.get("orderId", "")),
            order_link_id=str(r.get("orderLinkId", "")),
            status=str(r.get("orderStatus") or "New"),
        ))
    return out


def _same_side(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _same_price(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    if b <= 0:
        return a is None or a == 0.0
    return abs(a - b) / max(b, 1e-12) <= PRICE_EPS_RATIO


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------

def reconcile(local_positions: Sequence[LocalPosition],
              local_orders: Sequence[LocalOrder],
              exchange_positions: Sequence[ExchangePosition],
              exchange_orders: Sequence[ExchangeOrder]) -> ReconciliationReport:
    """Сравнивает локальный стейт с биржей и возвращает отчёт о расхождениях.

    Позиции сводятся по символу, ордера — по order_link_id. Согласованность
    цикла и его ордеров проверяется только косвенно (kind/reduce_only):
    привязка ордера к циклу — задача цикла, а не сверки.
    """
    report = ReconciliationReport()

    exch_pos = {p.symbol: p for p in exchange_positions}
    for p in local_positions:
        ep = exch_pos.get(p.symbol)
        if ep is None:
            report.position_discrepancies.append(Discrepancy(
                kind="missing_on_exchange",
                symbol=p.symbol,
                message="позиция есть локально, но отсутствует на бирже",
                action="close_local_cycle",
                ref_id=p.cycle_id, local=p,
            ))
            continue
        if not _same_side(p.side, ep.side):
            report.position_discrepancies.append(Discrepancy(
                kind="side_mismatch",
                symbol=p.symbol,
                message=f"сторона: локально {p.side}, биржа {ep.side}",
                action="sync_position",
                ref_id=p.cycle_id, local=p, exchange=ep,
            ))
        if abs(p.qty - ep.qty) > QTY_EPS:
            report.position_discrepancies.append(Discrepancy(
                kind="qty_mismatch",
                symbol=p.symbol,
                message=f"размер: локально {p.qty:.6g}, биржа {ep.qty:.6g}",
                action="sync_position_qty",
                ref_id=p.cycle_id, local=p, exchange=ep,
            ))
        if not _same_price(p.avg_entry, ep.avg_price):
            report.position_discrepancies.append(Discrepancy(
                kind="avg_price_mismatch",
                symbol=p.symbol,
                message=f"средняя: локально {p.avg_entry:.6g}, биржа {ep.avg_price:.6g}",
                action="sync_position_avg",
                ref_id=p.cycle_id, local=p, exchange=ep,
            ))
    for ep in exchange_positions:
        if ep.symbol not in {p.symbol for p in local_positions}:
            report.position_discrepancies.append(Discrepancy(
                kind="unknown_local",
                symbol=ep.symbol,
                message="позиция есть на бирже, но неизвестна боту",
                action="adopt_position",
                local=None, exchange=ep,
            ))

    exch_orders = {o.order_link_id: o for o in exchange_orders}
    for o in local_orders:
        eo = exch_orders.get(o.order_link_id)
        if eo is None:
            report.order_discrepancies.append(Discrepancy(
                kind="missing_on_exchange",
                symbol=o.symbol,
                message="ордер есть локально, но отсутствует на бирже",
                action="recreate_order",
                ref_id=o.order_link_id, local=o,
            ))
            continue
        if eo.status not in ("New", "PartiallyFilled"):
            report.order_discrepancies.append(Discrepancy(
                kind="not_open_exchange",
                symbol=o.symbol,
                message=f"локально ордер open, на бирже статус {eo.status}",
                action="mark_closed",
                ref_id=o.order_link_id, local=o, exchange=eo,
            ))
        if not _same_side(o.side, eo.side):
            report.order_discrepancies.append(Discrepancy(
                kind="side_mismatch",
                symbol=o.symbol,
                message=f"сторона: локально {o.side}, биржа {eo.side}",
                action="recreate_order",
                ref_id=o.order_link_id, local=o, exchange=eo,
            ))
        if abs(o.qty - eo.qty) > QTY_EPS:
            report.order_discrepancies.append(Discrepancy(
                kind="qty_mismatch",
                symbol=o.symbol,
                message=f"размер: локально {o.qty:.6g}, биржа {eo.qty:.6g}",
                action="sync_order_qty",
                ref_id=o.order_link_id, local=o, exchange=eo,
            ))
        if not _same_price(o.price, eo.price):
            report.order_discrepancies.append(Discrepancy(
                kind="price_mismatch",
                symbol=o.symbol,
                message=f"цена: локально {o.price}, биржа {eo.price}",
                action="sync_order_price",
                ref_id=o.order_link_id, local=o, exchange=eo,
            ))
        if o.reduce_only != eo.reduce_only:
            report.order_discrepancies.append(Discrepancy(
                kind="reduce_only_mismatch",
                symbol=o.symbol,
                message=f"reduce_only: локально {o.reduce_only}, биржа {eo.reduce_only}",
                action="recreate_order",
                ref_id=o.order_link_id, local=o, exchange=eo,
            ))
    for eo in exchange_orders:
        if eo.order_link_id not in {o.order_link_id for o in local_orders}:
            report.order_discrepancies.append(Discrepancy(
                kind="unknown_local",
                symbol=eo.symbol,
                message="ордер есть на бирже, но неизвестен боту",
                action="cancel_exchange_order",
                ref_id=eo.order_link_id, local=None, exchange=eo,
            ))
    return report


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def render(report: ReconciliationReport) -> str:
    """Человекочитаемый отчёт о сверке."""
    lines = ["Сверка локального стейта с Bybit"]
    if report.ok:
        lines.append("  OK: расхождений нет")
    else:
        lines.append(f"  Расхождений: {len(report.all)} "
                     f"(позиции {len(report.position_discrepancies)}, "
                     f"ордера {len(report.order_discrepancies)})")
        for d in report.position_discrepancies:
            lines.append(f"  [pos] {d.symbol} {d.kind}: {d.message} → {d.action}")
        for d in report.order_discrepancies:
            lines.append(f"  [ord] {d.symbol} {d.kind}: {d.message} → {d.action}")
        acts = report.actions()
        lines.append("  Действия: " + ", ".join(f"{k}×{v}" for k, v in sorted(acts.items())))
    return "\n".join(lines)


def to_dict(report: ReconciliationReport) -> dict[str, Any]:
    """Машиночитаемое представление для журнала/JSON."""
    def _disc(d: Discrepancy) -> dict[str, Any]:
        return {
            "kind": d.kind, "symbol": d.symbol, "message": d.message,
            "action": d.action, "ref_id": d.ref_id,
        }

    return {
        "ok": report.ok,
        "positions": [_disc(d) for d in report.position_discrepancies],
        "orders": [_disc(d) for d in report.order_discrepancies],
        "actions": report.actions(),
    }
