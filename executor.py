"""
executor.py — исполнительный модуль DCA-бота на Bybit Testnet.

Вычитывает новые сигналы из журнала скринера (`logs/screener-events.jsonl`,
ключ `screener.journal_path` в config/config.yml), дедуплицирует их по
`signal_id`, проверяет лимиты (максимум одновременных циклов, суммарный
номинал лестницы, биржевые шаги инструмента) и выставляет реальные DCA-ордера
на Bybit Testnet, а затем сопровождает открытые циклы: докупки по лестнице
Мартингейла, поуровневый тейк-профит, аварийный стоп и выход по времени.

Логика ордеров берётся из reference-слоя (specs/001-bybit-dca-testnet/reference):
  * bot.py        — режимы ENTRY/ADJUST/CLOSE, tp_price, take_profit_pct_at,
                     set_leverage_once;
  * pricing.py    — квантование цены/размера к шагам инструмента (FR-017);
  * backtest.py   — DcaParams, effective_step_pct/effective_stop_pct (адаптив
                     от NATR), planned_ladder_notional;
  * bot_config.py — маппинг config/config.yml в DcaParams/BotParams;
  * resilience.py — ретраи с экспоненциальной задержкой + circuit breaker;
  * cycle_journal.py — журнал циклов (logs/bot-events.jsonl);
  * reconcile.py  — сверка локального стейта с биржей на старте (T029).

Сетевой слой — собственный тонкий клиент Bybit v5 (REST, только stdlib +
requests), без ccxt. В live-режиме ордера идут на Testnet (demo-контур);
в paper-режиме исполнение симулируется по тестовому тикеру, ордера на биржу
не уходят.

Два режима:
  * paper (по умолчанию) — проверка конвейера «журнал → решение → журнал»,
    ордера не выставляются; нужен только публичный API Testnet;
  * --live — реальные ордера на Bybit Testnet (нужны API-ключи).

Запуск:
    python3 executor.py --check-config                 # валидация config.yml
    python3 executor.py --self-test                    # самопроверка чистой логики
    python3 executor.py --once                         # один такт (paper)
    python3 executor.py                                # непрерывный цикл (paper)
    python3 executor.py --live --once                  # одна итерация, реальные ордера
    python3 executor.py --live                         # реальные ордера Testnet

Пошаговая инструкция по запуску — в конце этого docstring и в README.md.

---
Пошаговый запуск:

1. Установить зависимости:      pip install requests
   (websockets/pyyaml нужны скринеру, не боту; бот использует только requests.)

2. Настроить config/config.yml:
   - секция dca/bot/screener — уже заполнены (параметры v3);
   - добавить секцию bybit с ключами Testnet:
         bybit:
           testnet: true
           api_key: ""            # либо переменная окружения BYBIT_API_KEY
           api_secret: ""         # либо переменная окружения BYBIT_API_SECRET
           recv_window_ms: 5000
     Ключи берутся с https://testnet.bybit.com → API → Create New Key
     (разрешения: Spot/Contract trade — Read-Write).

3. Проверить конфиг и чистую логику:
         python3 executor.py --check-config
         python3 executor.py --self-test

4. Скринер должен работать и писать signal_sent/signal_dry_run в
   logs/screener-events.jsonl (ключ screener.journal_path). Бот догоняет
   журнал с конца (offset хранится в logs/bot-state.json) и пропускает
   сигналы, старее --max-signal-age (по умолчанию 300 с = кулдаун скринера).

5. Paper-прогон без ордеров (проверка конвейера):
         python3 executor.py --once
         python3 executor.py                 # непрерывный режим, Ctrl+C — остановка

6. Боевой прогон на Testnet (реальные ордера):
         python3 executor.py --live           # непрерывный режим
   В терминале, для фоновой работы:
         nohup python3 executor.py --live >> logs/executor.out 2>&1 &

7. Смотреть события: logs/bot-events.jsonl (cycle_opened / cycle_closed /
   order_filled / signal_rejected), логи — logs/bot.log.
   Сводка по прогону: python3 tools/report.py logs/screener-events.jsonl logs/bot-events.jsonl

Примечания:
  * Бот торгует ТОЛЬКО на Testnet (api-testnet.bybit.com). Для mainnet нужно
    выставить bybit.testnet: false — не делайте этого без причины.
  * Стейт (обработанные сигналы, открытые циклы, offset) хранится в
    logs/bot-state.json; при первом запуске после перерыва сверяет стейт с
    биржей и закрывает циклы, которых нет на бирже (exchange_take_profit).
  * При старте проверяется расхождение часов с Testnet (FR-032); при
    превышении bot.max_clock_skew_ms старт запрещается (обход — --force).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

# ── reference-слой ───────────────────────────────────────────────────────────

_REF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "specs", "001-bybit-dca-testnet", "reference")
if _REF not in sys.path:
    sys.path.insert(0, _REF)

# Логирование настраиваем ДО импорта reference/backtest: иначе screener.py
# успеет привязать root-логирование к logs/screener.log, и наш bot.log не
# подключится (basicConfig игнорируется, если у root уже есть обработчики).
_logger = logging.getLogger("executor")


def _setup_logging() -> None:
    if _logger.handlers:
        return
    os.makedirs("logs", exist_ok=True)
    _logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        fh = logging.FileHandler("logs/bot.log", encoding="utf-8")
        fh.setFormatter(fmt)
        _logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    _logger.addHandler(sh)
    _logger.propagate = False


_setup_logging()

from pricing import quantize_qty, quantize_price  # noqa: E402
import bot as rbot  # noqa: E402
import bot_config  # noqa: E402
from resilience import (  # noqa: E402
    CircuitBreaker, ExponentialBackoff, ResilientCaller, ResponseError,
    CircuitOpenError, MaxRetriesError,
)
from cycle_journal import CycleJournal  # noqa: E402
import reconcile as rcon  # noqa: E402

backtest = bot_config.backtest
DcaParams = backtest.DcaParams
effective_step_pct = backtest.effective_step_pct
effective_stop_pct = backtest.effective_stop_pct
planned_ladder_notional = backtest.planned_ladder_notional

logger = _logger

# ── вспомогательное ──────────────────────────────────────────────────────────


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _diag_float(sig: dict, key: str) -> float | None:
    d = sig.get("diagnostics") or {}
    v = d.get(key)
    return _f(v) if v is not None else None


def _link(sig_id: str, suffix: str = "") -> str:
    """orderLinkId из signal_id: допустимы только [0-9A-Za-z_-], лимит 36."""
    base = re.sub(r"[^0-9A-Za-z_-]", "_", str(sig_id))
    return (base + suffix)[:36]


class BybitOrderError(Exception):
    """Ордер не исполнился (rejected/expired/нулевой филл)."""


# ── конфиг Bybit ─────────────────────────────────────────────────────────────


@dataclass
class BybitConfig:
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    recv_window_ms: int = 5000

    @property
    def base_url(self) -> str:
        return ("https://api-testnet.bybit.com" if self.testnet
                else "https://api.bybit.com")

    @classmethod
    def from_dict(cls, d: dict, env: dict | None = None) -> "BybitConfig":
        env = env if env is not None else {}
        testnet = str(env.get("BYBIT_TESTNET", d.get("testnet", "true"))).lower() in (
            "1", "true", "yes")
        return cls(
            api_key=str(env.get("BYBIT_API_KEY") or d.get("api_key") or ""),
            api_secret=str(env.get("BYBIT_API_SECRET") or d.get("api_secret") or ""),
            testnet=testnet,
            recv_window_ms=int(d.get("recv_window_ms") or 5000),
        )


class BybitApiError(Exception):
    """Ненулевой retCode из ответа Bybit v5."""

    def __init__(self, ret_code: int, message: str = "", retryable: bool = False):
        super().__init__(f"Bybit retCode {ret_code}: {message}".strip())
        self.ret_code = ret_code
        self.retryable = retryable


# Коды, которые стоит ретраить: rate limit / временная недоступность.
_RETRYABLE_CODES = {10002, 10004, 10016, 10024}


def _bybit_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BybitApiError):
        return exc.retryable
    return isinstance(exc, (ConnectionError, TimeoutError, OSError)) or (
        isinstance(exc, ResponseError) and exc.retryable)


# ── клиент Bybit v5 ──────────────────────────────────────────────────────────


class BybitClient:
    """Тонкий REST-клиент Bybit v5 (Testnet). Публичные и подписанные вызовы.

    Подпись запроса (HMAC SHA256): timestamp + api_key + recv_window + query/body.
    Ретраи и предохранитель — через reference/resilience.py (ResilientCaller).
    """

    def __init__(self, cfg: BybitConfig):
        self.cfg = cfg
        self.base = cfg.base_url
        self.caller = ResilientCaller(
            backoff=ExponentialBackoff(base=0.4, factor=2.0, max_delay=8.0,
                                       jitter=0.2),
            breaker=CircuitBreaker(failure_threshold=5, cooldown=20.0),
            max_attempts=4,
        )

    def _sign(self, ts: int, payload: str) -> str:
        msg = f"{ts}{self.cfg.api_key}{self.cfg.recv_window_ms}{payload}"
        return hmac.new(self.cfg.api_secret.encode("utf-8"), msg.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def _headers(self, ts: int, sig: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.cfg.api_key,
            "X-BAPI-TIMESTAMP": str(ts),
            "X-BAPI-RECV-WINDOW": str(self.cfg.recv_window_ms),
            "X-BAPI-SIGN": sig,
            "Content-Type": "application/json",
        }

    def _check(self, r: requests.Response) -> dict:
        if not 200 <= r.status_code < 300:
            raise ResponseError(r.status_code, r.text[:200])
        try:
            data = r.json()
        except ValueError:
            raise BybitApiError(0, f"битый ответ: {r.text[:200]}") from None
        rc = data.get("retCode", -1)
        if rc == 0:
            return data.get("result") or {}
        raise BybitApiError(rc, str(data.get("retMsg", "")),
                            retryable=rc in _RETRYABLE_CODES)

    def _get(self, path: str, params: dict | None = None,
             signed: bool = False) -> dict:
        params = dict(params or {})
        if not signed:
            def fn():
                r = requests.get(self.base + path, params=params, timeout=10)
                return self._check(r)
            return self.caller.call(fn, retryable=_bybit_retryable)
        qs = urllib.parse.urlencode(sorted(params.items()))
        ts = int(time.time() * 1000)

        def fn():
            r = requests.get(self.base + path + "?" + qs, timeout=10,
                             headers=self._headers(ts, self._sign(ts, qs)))
            return self._check(r)
        return self.caller.call(fn, retryable=_bybit_retryable)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body, separators=(",", ":"))
        ts = int(time.time() * 1000)

        def fn():
            r = requests.post(self.base + path, data=data, timeout=10,
                              headers=self._headers(ts, self._sign(ts, data)))
            return self._check(r)
        return self.caller.call(fn, retryable=_bybit_retryable)

    # публичные
    def server_time(self) -> int:
        # поле time лежит в корне ответа, а не в result — читаем сырой JSON.
        def fn():
            r = requests.get(self.base + "/v5/market/time", timeout=10)
            if not 200 <= r.status_code < 300:
                raise ResponseError(r.status_code, r.text[:200])
            return int(r.json().get("time", 0))
        return self.caller.call(fn, retryable=_bybit_retryable)

    def instruments(self, symbol: str | None = None) -> list[dict]:
        p = {"category": "linear"}
        if symbol:
            p["symbol"] = symbol
        return self._get("/v5/market/instruments-info", p).get("list", [])

    def ticker(self, symbol: str) -> dict | None:
        lst = self._get("/v5/market/tickers",
                        {"category": "linear", "symbol": symbol}).get("list", [])
        return lst[0] if lst else None

    # приватные
    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return self._post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage)})

    def create_order(self, *, symbol: str, side: str, order_type: str, qty: float,
                     price: float | None = None, reduce_only: bool = False,
                     time_in_force: str = "GTC", order_link_id: str = "") -> dict:
        body: dict[str, Any] = {
            "category": "linear", "symbol": symbol, "side": side,
            "orderType": order_type, "qty": str(qty),
            "timeInForce": time_in_force, "reduceOnly": reduce_only,
            "orderLinkId": order_link_id, "positionIdx": 0,
        }
        if price is not None:
            body["price"] = str(price)
        return self._post("/v5/order/create", body)

    def cancel_order(self, symbol: str, order_link_id: str) -> dict:
        return self._post("/v5/order/cancel",
                          {"category": "linear", "symbol": symbol,
                           "orderLinkId": order_link_id})

    def get_order(self, symbol: str, order_link_id: str) -> dict | None:
        lst = self._get("/v5/order/realtime",
                        {"category": "linear", "symbol": symbol,
                         "orderLinkId": order_link_id}, signed=True).get("list", [])
        return lst[0] if lst else None

    def open_orders(self, symbol: str) -> list[dict]:
        lst = self._get("/v5/order/realtime",
                        {"category": "linear", "symbol": symbol, "limit": "50"},
                        signed=True).get("list", [])
        return lst if isinstance(lst, list) else []

    def get_position(self, symbol: str) -> dict | None:
        lst = self._get("/v5/position/list",
                        {"category": "linear", "symbol": symbol},
                        signed=True).get("list", [])
        return lst[0] if lst else None


class PaperClient:
    """Симуляция исполнения для paper-режима: ордера не уходят на биржу.

    Публичные вызовы (время, инструменты, тикер) — реальные, с Testnet.
    Приватные (ордера, позиции) симулируются локально: маркет-филл сразу по
    текущей цене, лимитки срабатывают, когда тестовый тикер пересекает уровень.
    """

    def __init__(self, cfg: BybitConfig, fee_rate: float = 0.00055):
        self.bybit = BybitClient(cfg)
        self.fee_rate = fee_rate
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.ref: dict[str, float] = {}

    def server_time(self) -> int:
        return self.bybit.server_time()

    def instruments(self, symbol: str | None = None) -> list[dict]:
        return self.bybit.instruments(symbol)

    def ticker(self, symbol: str) -> dict | None:
        try:
            t = self.bybit.ticker(symbol)
            if t:
                p = _f(t.get("lastPrice") or t.get("markPrice"))
                if p > 0:
                    self.ref[symbol] = p
                return t
        except Exception:
            pass
        rp = self.ref.get(symbol)
        return ({"lastPrice": str(rp), "markPrice": str(rp)} if rp else None)

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return {}

    def create_order(self, *, symbol: str, side: str, order_type: str, qty: float,
                     price: float | None = None, reduce_only: bool = False,
                     time_in_force: str = "GTC", order_link_id: str = "") -> dict:
        order: dict[str, Any] = {
            "orderId": f"paper_{len(self.orders) + 1}",
            "orderLinkId": order_link_id, "symbol": symbol, "side": side,
            "orderType": order_type, "qty": str(qty),
            "price": str(price) if price is not None else "0",
            "reduceOnly": reduce_only, "timeInForce": time_in_force,
            "orderStatus": "New", "cumExecQty": "0", "cumExecFee": "0",
            "avgPrice": "0", "positionIdx": 0,
        }
        if order_type == "Market":
            px = (self.ref.get(symbol) or price or 0.0)
            if px > 0:
                order.update({
                    "orderStatus": "Filled", "cumExecQty": str(qty),
                    "avgPrice": str(px),
                    "cumExecFee": str(qty * px * self.fee_rate),
                })
                self._apply_fill(symbol, side, qty, px, reduce_only)
        self.orders[order_link_id] = order
        return order

    def _apply_fill(self, symbol: str, side: str, qty: float, price: float,
                    reduce_only: bool) -> None:
        pos = self.positions.get(symbol)
        if reduce_only:
            if pos:
                pos["qty"] -= qty
                if pos["qty"] <= 1e-12:
                    self.positions.pop(symbol, None)
            return
        if pos and pos["side"] == side:
            new_qty = pos["qty"] + qty
            pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty
            pos["qty"] = new_qty
        else:
            self.positions[symbol] = {"side": side, "qty": qty, "avg": price}

    def get_order(self, symbol: str, order_link_id: str) -> dict | None:
        order = self.orders.get(order_link_id)
        if order is None:
            return None
        if (order["orderStatus"] in ("New", "PartiallyFilled")
                and order["orderType"] == "Limit"):
            t = self.ticker(symbol)
            if t:
                mark = _f(t.get("lastPrice") or t.get("markPrice"))
                px = _f(order["price"])
                if mark > 0 and ((order["side"] == "Buy" and mark <= px) or
                                 (order["side"] == "Sell" and mark >= px)):
                    qty = _f(order["qty"])
                    order.update({
                        "orderStatus": "Filled", "cumExecQty": str(qty),
                        "avgPrice": str(px),
                        "cumExecFee": str(qty * px * self.fee_rate),
                    })
                    self._apply_fill(symbol, order["side"], qty, px,
                                     order["reduceOnly"])
        return order

    def open_orders(self, symbol: str) -> list[dict]:
        return [o for o in self.orders.values()
                if o["symbol"] == symbol
                and o["orderStatus"] in ("New", "PartiallyFilled")]

    def cancel_order(self, symbol: str, order_link_id: str) -> dict:
        o = self.orders.get(order_link_id)
        if o and o["orderStatus"] in ("New", "PartiallyFilled"):
            o["orderStatus"] = "Cancelled"
            return {"status": "success"}
        return {"status": "error"}

    def get_position(self, symbol: str) -> dict | None:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        return {"side": pos["side"], "qty": str(pos["qty"]),
                "avgPrice": str(pos["avg"]), "positionStatus": "Normal"}


@dataclass
class InstrumentInfo:
    symbol: str
    qty_step: float
    min_qty: float
    tick_size: float
    max_leverage: float
    max_order_qty: float
    max_notional: float


def _parse_instrument(row: dict) -> InstrumentInfo | None:
    lot = row.get("lotSizeFilter") or {}
    prc = row.get("priceFilter") or {}
    lev = row.get("leverageFilter") or {}
    try:
        return InstrumentInfo(
            symbol=str(row.get("symbol", "")),
            qty_step=_f(lot.get("qtyStep") or 1),
            min_qty=_f(lot.get("minOrderQty") or 0),
            tick_size=_f(prc.get("tickSize") or 1),
            max_leverage=_f(lev.get("maxLeverage") or 0),
            max_order_qty=_f(lot.get("maxOrderQty") or 0),
            max_notional=_f(lot.get("maxNotionalValue") or 0),
        )
    except Exception:
        return None


# ── DCA-бот ──────────────────────────────────────────────────────────────────


class DcaBot:
    """Конвейер «журнал скринера → реальные DCA-ордера Testnet → журнал бота»."""

    def __init__(self, *, p: DcaParams, bp, client: Any, mode: str,
                 journal_path: str, state_path: str, screener_path: str,
                 trail_trigger_pct: float = 0.0, trail_step_pct: float = 0.0,
                 max_signal_age: int = 300) -> None:
        self.p = p
        self.bp = bp
        self.client = client
        self.mode = mode
        self.journal = CycleJournal(journal_path)
        self.journal_path = journal_path
        self.state_path = state_path
        self.screener_path = screener_path
        self.trail_trigger = trail_trigger_pct
        self.trail_step = trail_step_pct
        self.max_signal_age = max_signal_age
        self._instruments: dict[str, InstrumentInfo] = {}
        self._last_prices: dict[str, float] = {}
        self._load_state()

    # ── состояние ─────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        self.offset = 0
        self.processed: set[str] = set()
        self.leverage_applied: set[str] = set()
        self.cycles: dict[str, dict] = {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.offset = int(st.get("offset") or 0)
            self.processed = set(str(x) for x in (st.get("processed_ids") or []))
            self.leverage_applied = set(str(x) for x in (st.get("leverage") or []))
            self.cycles = {str(k): v for k, v in (st.get("cycles") or {}).items()}
        except (OSError, ValueError):
            pass  # первый запуск

    def _save_state(self) -> None:
        state = {
            "offset": self.offset,
            "processed_ids": list(self.processed)[-1000:],
            "leverage": sorted(self.leverage_applied),
            "cycles": {cid: c for cid, c in self.cycles.items()
                       if not c.get("closed")},
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("не удалось сохранить стейт %s: %s", self.state_path, e)

    # ── главный цикл ──────────────────────────────────────────────────────────

    def once(self) -> None:
        self._startup_reconcile()
        self._poll_signals()
        self._monitor_cycles()
        self._save_state()

    def run(self) -> None:
        logger.info("бот запущен: режим=%s сигналы=%s журнал=%s стейт=%s",
                    self.mode, self.screener_path, self.journal_path,
                    self.state_path)
        self._startup_reconcile()
        last_hb = time.monotonic()
        while True:
            try:
                self._poll_signals()
                self._monitor_cycles()
                self._save_state()
                if time.monotonic() - last_hb >= self.bp.heartbeat_sec:
                    last_hb = time.monotonic()
                    logger.info("heartbeat: открыто циклов=%d обработано сигналов=%d",
                                sum(1 for c in self.cycles.values()
                                    if not c.get("closed")),
                                len(self.processed))
                time.sleep(self.bp.monitor_interval_sec)
            except KeyboardInterrupt:
                logger.info("остановка по Ctrl+C")
                self._save_state()
                break

    # ── приём сигналов ────────────────────────────────────────────────────────

    def _poll_signals(self) -> None:
        try:
            size = os.path.getsize(self.screener_path)
        except OSError:
            return
        if size < self.offset:
            self.offset = 0  # файл обрезан/пересоздан
        if size <= self.offset:
            return
        try:
            f = open(self.screener_path, "r", encoding="utf-8")
        except OSError:
            return
        with f:
            f.seek(self.offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                self._process(rec)
            self.offset = f.tell()

    def _process(self, rec: dict) -> None:
        if rec.get("kind") not in ("signal_sent", "signal_dry_run"):
            return
        sig = rec.get("signal")
        if not isinstance(sig, dict):
            return
        self._handle_signal(sig, int(time.time() * 1000))

    def _handle_signal(self, sig: dict, recv_ts: int) -> None:
        sig_id = str(sig.get("signal_id") or "")
        symbol = str(sig.get("symbol") or "")
        side = str(sig.get("side") or "")
        if not sig_id or not symbol or side not in ("Buy", "Sell"):
            return

        if self.max_signal_age > 0:
            sig_ts = int(sig.get("ts") or 0)
            if sig_ts and recv_ts - sig_ts > self.max_signal_age * 1000:
                self._reject(sig_id, "stale", symbol=symbol,
                             age_ms=recv_ts - sig_ts)
                return
        if sig_id in self.processed:
            self._reject(sig_id, "duplicate", symbol=symbol)
            return
        if any(c.get("symbol") == symbol and not c.get("closed")
               for c in self.cycles.values()):
            self._reject(sig_id, "limit", symbol=symbol,
                         reason_detail="по символу уже открыт цикл")
            return
        open_n = sum(1 for c in self.cycles.values() if not c.get("closed"))
        if open_n >= self.bp.max_cycles:
            self._reject(sig_id, "limit", symbol=symbol,
                         reason_detail=f"открыто циклов {open_n} >= max "
                                       f"{self.bp.max_cycles}")
            return
        notional = planned_ladder_notional(self.p)
        if self.p.max_notional_usdt and notional > self.p.max_notional_usdt:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail=f"номинал лестницы {notional:.1f} > лимит "
                                       f"{self.p.max_notional_usdt}")
            return

        pt = self._testnet_price(symbol)
        self.journal.write("signal_received", signal_id=sig_id, symbol=symbol,
                           price_mainnet=sig.get("price"), price_testnet=pt,
                           ts_signal=int(sig.get("ts") or recv_ts))
        logger.info("сигнал %s %s %s (testnet=%.6g)",
                    symbol, side, sig_id, pt or 0.0)
        self._mark_processed(sig_id)
        self._open_cycle(sig, recv_ts)

    def _reject(self, sig_id: str, reason: str, symbol: str = "", **extra: Any) -> None:
        self._mark_processed(sig_id)
        self.journal.write("signal_rejected", signal_id=sig_id, symbol=symbol,
                           reason=reason, **extra)
        logger.info("сигнал %s отклонён: %s%s", sig_id, reason,
                    f" ({extra.get('reason_detail')})" if extra.get("reason_detail") else "")

    def _mark_processed(self, sig_id: str) -> None:
        self.processed.add(sig_id)
        if len(self.processed) > 1000:
            self.processed = set(list(self.processed)[-1000:])

    # ── открытие цикла ────────────────────────────────────────────────────────

    def _testnet_price(self, symbol: str) -> float | None:
        if symbol in self._last_prices:
            return self._last_prices[symbol]
        try:
            t = self.client.ticker(symbol)
            if t:
                p = _f(t.get("lastPrice") or t.get("markPrice"))
                if p > 0:
                    self._last_prices[symbol] = p
                    return p
        except Exception as e:
            logger.warning("нет тикера %s: %s", symbol, e)
        return None

    def _instrument(self, symbol: str) -> InstrumentInfo | None:
        info = self._instruments.get(symbol)
        if info is not None:
            return info
        try:
            rows = self.client.instruments(symbol)
            info = _parse_instrument(rows[0]) if rows else None
            if info and info.symbol == symbol:
                self._instruments[symbol] = info
                return info
        except Exception as e:
            self.journal.write("api_error", operation="instruments",
                               symbol=symbol, error=str(e))
        return None

    def _apply_leverage(self, symbol: str) -> None:
        if symbol in self.leverage_applied:
            return
        self.client.set_leverage(symbol, self.p.leverage)  # FR-018: один раз
        self.leverage_applied.add(symbol)
        logger.info("плечо %s → %s", symbol, self.p.leverage)

    def _open_cycle(self, sig: dict, recv_ts: int) -> None:
        symbol = str(sig["symbol"])
        side = str(sig["side"])
        sig_id = str(sig["signal_id"])
        now = int(time.time() * 1000)
        pt = self._testnet_price(symbol)
        ref = pt or _f(sig.get("price"))
        if ref <= 0:
            self._reject(sig_id, "error", symbol=symbol,
                         reason_detail="нет цены для расчёта размера входа")
            return
        info = self._instrument(symbol)
        if info is None:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail="инструмент не найден на Testnet")
            return
        if self.p.leverage > info.max_leverage:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail=f"плечо {self.p.leverage} > max "
                                       f"{info.max_leverage}")
            return

        qty = quantize_qty(self.p.entry_usdt / ref, info.qty_step)
        if qty < info.min_qty:
            qty = quantize_qty(info.min_qty, info.qty_step)
        if qty <= 0:
            qty = info.min_qty
        if info.max_order_qty and qty > info.max_order_qty:
            qty = info.max_order_qty
        if info.max_notional and qty * ref > info.max_notional:
            qty = quantize_qty(info.max_notional / ref, info.qty_step)
        if qty <= 0:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail="размер входа после ограничений равен нулю")
            return

        try:
            self._apply_leverage(symbol)
        except Exception as e:
            self._reject(sig_id, "error", symbol=symbol, error=str(e))
            return

        base = _link(sig_id)
        c: dict[str, Any] = {
            "cycle_id": sig_id,
            "signal_id": sig_id,
            "symbol": symbol,
            "side": side,
            "natr": _diag_float(sig, "natr"),
            "open_ts": recv_ts,
            "signal_ts": int(sig.get("ts") or recv_ts),
            "signal_price": _f(sig.get("price") or ref),
            "price_testnet": pt or ref,
            "qty": 0.0,
            "avg_entry": 0.0,
            "docups": 0,
            "fee": 0.0,
            "last_fill_price": ref,
            "qty_step": info.qty_step,
            "tick_size": info.tick_size,
            "min_qty": info.min_qty,
            "base_link": base,
            "links": {},
            "fills": {},
            "tp_seq": 0,
            "tp_link": None,
            "tp_price": None,
            "so_seq": 0,
            "so_link": None,
            "so_price": None,
            "so_qty": None,
            "stop_level": None,
            "trail_active": False,
            "trail_peak": None,
            "closed": False,
        }
        self.cycles[sig_id] = c

        entry_link = base
        try:
            self._place(c, side=side, order_type="Market", qty=qty, price=None,
                        reduce_only=False, tif="IOC", role="entry",
                        link=entry_link)
            order = self._wait_fill(c, entry_link, self.bp.fill_timeout_ms)
            cum = _f(order.get("cumExecQty", 0)) if order else 0.0
            if order is None or cum <= 0 or order.get("orderStatus") == "Rejected":
                raise BybitOrderError(
                    f"вход не исполнился: {order.get('orderStatus') if order else 'нет ордера'}")
            price = _f(order.get("avgPrice") or order.get("price") or ref)
            fee = _f(order.get("cumExecFee", 0))
            self._on_fill(c, entry_link, "entry", cum, price, fee, now, order)
            c["fills"][entry_link] = cum      # вход исполнен — монитор не повторяет
            c["links"].pop(entry_link, None)
        except Exception as e:
            logger.error("вход %s не исполнен: %s", symbol, e)
            self.journal.write("signal_rejected", signal_id=sig_id,
                               symbol=symbol, reason="error", error=str(e))
            self.cycles.pop(sig_id, None)
            self._save_state()

    # ── размещение ордеров ────────────────────────────────────────────────────

    def _place(self, c: dict, *, side: str, order_type: str, qty: float,
               price: float | None, reduce_only: bool, tif: str, role: str,
               link: str) -> dict:
        order = self.client.create_order(
            symbol=c["symbol"], side=side, order_type=order_type, qty=qty,
            price=price, reduce_only=reduce_only, time_in_force=tif,
            order_link_id=link)
        c["links"][link] = {"role": role, "side": side, "qty": qty,
                            "price": price, "reduce_only": reduce_only}
        c["fills"][link] = 0.0
        return order

    def _wait_fill(self, c: dict, link: str, timeout_ms: int) -> dict | None:
        deadline = time.time() + timeout_ms / 1000.0
        last: dict | None = None
        while time.time() < deadline:
            try:
                order = self.client.get_order(c["symbol"], link)
            except Exception as e:
                self.journal.write("api_error", operation="get_order",
                                   symbol=c["symbol"], link=link, error=str(e))
                order = None
            if order is not None:
                last = order
                status = order.get("orderStatus", "")
                if status in ("Filled", "PartiallyFilled", "Rejected",
                              "Cancelled", "Expired"):
                    return order
            time.sleep(0.4)
        return last

    def _safe_cancel(self, c: dict, link: str) -> None:
        c["links"].pop(link, None)
        c["fills"].pop(link, None)
        try:
            self.client.cancel_order(c["symbol"], link)
        except Exception as e:
            self.journal.write("api_error", operation="cancel_order",
                               symbol=c["symbol"], link=link, error=str(e))

    def _place_tp(self, c: dict, price: float) -> None:
        tp_side = "Sell" if c["side"] == "Buy" else "Buy"
        if c["tp_link"]:
            self._safe_cancel(c, c["tp_link"])
            c["tp_link"] = None
        c["tp_seq"] += 1
        link = _link(c["signal_id"], f"_tp{c['tp_seq']}")
        self._place(c, side=tp_side, order_type="Limit", qty=c["qty"],
                    price=price, reduce_only=True, tif="GTC", role="tp",
                    link=link)
        c["tp_link"] = link
        c["tp_price"] = price
        logger.info("TP %s → %.6g", c["symbol"], price)

    def _place_dca(self, c: dict, price: float, qty: float) -> None:
        c["so_seq"] += 1
        link = _link(c["signal_id"], f"_so{c['so_seq']}")
        self._place(c, side=c["side"], order_type="Limit", qty=qty,
                    price=price, reduce_only=False, tif="GTC", role="dca",
                    link=link)
        c["so_link"] = link
        c["so_price"] = price
        c["so_qty"] = qty
        logger.info("докупка %s #%d → %.6g x%.6g", c["symbol"],
                    c["so_seq"], price, qty)

    def _after_position_change(self, c: dict, now: int) -> None:
        p = self.p
        step_pct = effective_step_pct(p, c["natr"])
        stop_pct = effective_stop_pct(p, c["natr"])
        tp_pct = rbot.take_profit_pct_at(p, c["docups"])
        tp = rbot.tp_price(c["avg_entry"], c["side"], tp_pct, c["tick_size"])
        self._place_tp(c, tp)
        if stop_pct:
            factor = 1 - stop_pct / 100 if c["side"] == "Buy" else 1 + stop_pct / 100
            c["stop_level"] = c["avg_entry"] * factor
        if c["docups"] < p.max_docups:
            next_level = c["last_fill_price"] * (
                1 - step_pct / 100 if c["side"] == "Buy" else 1 + step_pct / 100)
            qty = quantize_qty(
                p.entry_usdt * p.multiplier ** (c["docups"] + 1) / next_level,
                c["qty_step"])
            if qty < c["min_qty"]:
                qty = c["min_qty"]
            self._place_dca(c, next_level, qty)

    # ── филлы ─────────────────────────────────────────────────────────────────

    def _on_fill(self, c: dict, link: str, role: str, qty: float, price: float,
                 fee: float, now: int, order: dict) -> None:
        if role == "entry":
            c["qty"] = qty
            c["avg_entry"] = price
            c["fee"] += fee
            c["last_fill_price"] = price
            self.journal.write("order_filled", signal_id=c["signal_id"],
                               cycle_id=c["cycle_id"], symbol=c["symbol"],
                               role="entry", mode="entry",
                               expected_price=c["signal_price"],
                               avg_fill_price=round(price, 8),
                               ts_signal=c["signal_ts"], ts_confirmed=now)
            self.journal.cycle_opened(c["cycle_id"], c["symbol"],
                                      open_ts=c["open_ts"])
            logger.info("вход %s %s x%.6g @%.6g", c["symbol"], c["side"],
                        qty, price)
            self._after_position_change(c, now)
        elif role == "dca":
            new_qty = c["qty"] + qty
            c["avg_entry"] = (c["avg_entry"] * c["qty"] + price * qty) / new_qty
            c["qty"] = new_qty
            c["docups"] += 1
            c["fee"] += fee
            c["last_fill_price"] = price
            self.journal.write("order_filled", signal_id=c["signal_id"],
                               cycle_id=c["cycle_id"], symbol=c["symbol"],
                               role="dca", mode="entry",
                               expected_price=_f(order.get("price")),
                               avg_fill_price=round(price, 8),
                               ts_signal=c["signal_ts"], ts_confirmed=now)
            logger.info("докупка #%d %s @%.6g, avg=%.6g", c["docups"],
                        c["symbol"], price, c["avg_entry"])
            self._after_position_change(c, now)
        elif role == "tp":
            self._close(c, "take_profit", price, now, fee)
        elif role == "close":
            self._close(c, c.get("close_reason") or "manual", price, now, fee)

    def _close(self, c: dict, reason: str, price: float, now: int, fee: float) -> None:
        for link, o in list(c["links"].items()):
            if o["role"] in ("tp", "dca"):
                self._safe_cancel(c, link)
        c["exit_price"] = price
        c["exit_ts"] = now
        c["fee"] += fee
        gross = ((price - c["avg_entry"]) * c["qty"] if c["side"] == "Buy"
                 else (c["avg_entry"] - price) * c["qty"])
        c["pnl"] = gross - c["fee"]
        c["closed"] = True
        self.journal.write("order_filled", signal_id=c["signal_id"],
                           cycle_id=c["cycle_id"], symbol=c["symbol"],
                           role="close", mode="close",
                           expected_price=c["tp_price"],
                           avg_fill_price=round(price, 8),
                           ts_signal=c["signal_ts"], ts_confirmed=now)
        self.journal.cycle_closed(c["cycle_id"], c["symbol"],
                                  exit_reason=reason, pnl=round(c["pnl"], 4),
                                  close_ts=now)
        logger.info("цикл %s закрыт: %s pnl=%.4f", c["symbol"], reason, c["pnl"])
        self.cycles.pop(c["cycle_id"], None)

    def _market_close(self, c: dict, reason: str) -> None:
        for link, o in list(c["links"].items()):
            if o["role"] in ("tp", "dca"):
                self._safe_cancel(c, link)
        link = _link(c["signal_id"], f"_c_{reason}")
        c["close_reason"] = reason
        exit_side = "Sell" if c["side"] == "Buy" else "Buy"
        try:
            self._place(c, side=exit_side, order_type="Market", qty=c["qty"],
                        price=None, reduce_only=True, tif="IOC", role="close",
                        link=link)
            order = self._wait_fill(c, link, self.bp.fill_timeout_ms)
            cum = _f(order.get("cumExecQty", 0)) if order else 0.0
            if order is None or cum <= 0:
                raise BybitOrderError("рыночное закрытие не исполнилось")
            price = _f(order.get("avgPrice") or order.get("price"))
            fee = _f(order.get("cumExecFee", 0))
            self._on_fill(c, link, "close", cum, price, fee,
                          int(time.time() * 1000), order)
        except Exception as e:
            self.journal.write("api_error", operation="market_close",
                               symbol=c["symbol"], reason=reason, error=str(e))
            logger.error("закрытие %s (%s) не удалось: %s",
                         c["symbol"], reason, e)

    # ── мониторинг ────────────────────────────────────────────────────────────

    def _monitor_cycles(self) -> None:
        for c in list(self.cycles.values()):
            if c.get("closed"):
                continue
            try:
                self._monitor_cycle(c)
            except Exception as e:
                self.journal.write("api_error", operation="monitor",
                                   symbol=c["symbol"], error=str(e))
                logger.error("ошибка мониторинга %s: %s", c["symbol"], e)

    def _monitor_cycle(self, c: dict) -> None:
        now = int(time.time() * 1000)
        open_list = self.client.open_orders(c["symbol"])
        seen = {str(o.get("orderLinkId")) for o in open_list}
        for link in list(c["links"].keys()):
            if link in seen:
                order = next((o for o in open_list
                              if str(o.get("orderLinkId")) == link), None)
            else:
                order = self.client.get_order(c["symbol"], link)
            if order is None:
                continue
            status = str(order.get("orderStatus", "New"))
            cum = _f(order.get("cumExecQty", 0))
            prev = c["fills"].get(link, 0.0)
            if cum > prev + 1e-9:
                role = c["links"][link]["role"]
                price = _f(order.get("avgPrice") or order.get("price"))
                fee = _f(order.get("cumExecFee", 0))
                self._on_fill(c, link, role, cum - prev, price, fee, now, order)
                c["fills"][link] = cum
            if status in ("Filled", "Cancelled", "Rejected", "Expired"):
                c["links"].pop(link, None)
        if c.get("closed"):
            return
        self._check_exits(c, now)

    def _check_exits(self, c: dict, now: int) -> None:
        p = self.p
        if now - c["open_ts"] >= p.max_hold_minutes * 60_000:
            self._market_close(c, "time_exit")
            return
        try:
            t = self.client.ticker(c["symbol"])
        except Exception:
            t = None
        if not t:
            return
        mark = _f(t.get("markPrice") or t.get("lastPrice"))
        if mark <= 0:
            return
        sl = c.get("stop_level")
        if sl:
            hit = (mark <= sl) if c["side"] == "Buy" else (mark >= sl)
            if hit:
                self._market_close(c, "hard_sl")
                return
        if p.max_cycle_loss_usdt and c["qty"] > 0:
            loss = ((c["avg_entry"] - mark) * c["qty"] if c["side"] == "Buy"
                    else (mark - c["avg_entry"]) * c["qty"])
            if loss >= p.max_cycle_loss_usdt:
                self._market_close(c, "hard_sl")
                return
        if self.trail_trigger > 0 and self.trail_step > 0:
            upct = ((mark - c["avg_entry"]) / c["avg_entry"] * 100
                    if c["side"] == "Buy"
                    else (c["avg_entry"] - mark) / c["avg_entry"] * 100)
            if not c["trail_active"] and upct >= self.trail_trigger:
                c["trail_active"] = True
                c["trail_peak"] = mark
                self.journal.write("trail_activated", cycle_id=c["cycle_id"],
                                   symbol=c["symbol"], avg_price=c["avg_entry"],
                                   trigger_pct=self.trail_trigger,
                                   peak_price=mark)
            if c["trail_active"] and c["trail_peak"]:
                if c["side"] == "Buy":
                    c["trail_peak"] = max(c["trail_peak"], mark)
                else:
                    c["trail_peak"] = min(c["trail_peak"], mark)
                retrace = ((c["trail_peak"] - mark) / c["trail_peak"] * 100
                           if c["side"] == "Buy"
                           else (mark - c["trail_peak"]) / c["trail_peak"] * 100)
                if retrace >= self.trail_step:
                    self._market_close(c, "trailing")

    # ── сверка на старте ──────────────────────────────────────────────────────

    def _startup_reconcile(self) -> None:
        active = {cid: c for cid, c in self.cycles.items() if not c.get("closed")}
        if not active:
            return
        symbols = {c["symbol"] for c in active.values()}
        exch_pos: list[rcon.ExchangePosition] = []
        exch_orders: list[rcon.ExchangeOrder] = []
        for sym in symbols:
            try:
                row = self.client.get_position(sym)
                if row:
                    exch_pos.extend(rcon.parse_positions([row]))
                exch_orders.extend(rcon.parse_orders(self.client.open_orders(sym)))
            except Exception as e:
                self.journal.write("api_error", operation="reconcile",
                                   symbol=sym, error=str(e))
        local_pos = [
            rcon.LocalPosition(symbol=c["symbol"], side=c["side"], qty=c["qty"],
                               avg_entry=c["avg_entry"], cycle_id=cid)
            for cid, c in active.items()
        ]
        local_orders = [
            rcon.LocalOrder(symbol=c["symbol"], side=o["side"], qty=o["qty"],
                            price=o.get("price"), reduce_only=o.get(
                                "reduce_only", False),
                            order_link_id=link, kind=o["role"],
                            cycle_id=cid)
            for cid, c in active.items()
            for link, o in c["links"].items()
        ]
        report = rcon.reconcile(local_pos, local_orders, exch_pos, exch_orders)
        summary = rcon.to_dict(report)
        self.journal.write("reconcile", ok=report.ok,
                           discrepancies=len(report.all),
                           actions=summary["actions"])
        logger.info(rcon.render(report))
        for d in report.position_discrepancies:
            c = active.get(d.ref_id)
            if c is None:
                continue
            if d.action == "close_local_cycle":
                self._finalize_orphan(c, "manual")
            elif d.action in ("sync_position", "sync_position_qty",
                              "sync_position_avg") and d.exchange is not None:
                c["qty"] = d.exchange.qty
                c["avg_entry"] = d.exchange.avg_price
                self.journal.write("cycle_synced", cycle_id=c["cycle_id"],
                                   symbol=c["symbol"], qty=c["qty"],
                                   avg_entry=c["avg_entry"])
        for d in report.order_discrepancies:
            if d.action == "recreate_order" and d.local is not None \
                    and d.local.kind == "dca":
                c = active.get(d.local.cycle_id)
                if c and d.local.price:
                    try:
                        self._place(c, side=c["side"], order_type="Limit",
                                    qty=d.local.qty, price=d.local.price,
                                    reduce_only=False, tif="GTC", role="dca",
                                    link=d.ref_id)
                    except Exception as e:
                        self.journal.write("api_error", operation="recreate_order",
                                           symbol=c["symbol"], link=d.ref_id,
                                           error=str(e))
            elif d.action == "cancel_exchange_order" and d.ref_id:
                try:
                    self.client.cancel_order(d.symbol, d.ref_id)
                except Exception as e:
                    self.journal.write("api_error", operation="cancel_exchange",
                                       symbol=d.symbol, link=d.ref_id,
                                       error=str(e))
        self._save_state()

    def _finalize_orphan(self, c: dict, default_reason: str) -> None:
        """Цикл, чьей позиции нет на бирже: закрыть локально (T029)."""
        reason = default_reason
        price = None
        fee = 0.0
        tp_link = c.get("tp_link")
        if tp_link:
            try:
                o = self.client.get_order(c["symbol"], tp_link)
                if o and o.get("orderStatus") == "Filled":
                    reason = "take_profit"
                    price = _f(o.get("avgPrice") or o.get("price"))
                    fee = _f(o.get("cumExecFee", 0))
            except Exception:
                pass
        if price is None or price <= 0:
            price = _f(c.get("tp_price") or c.get("last_fill_price"))
        gross = ((price - c["avg_entry"]) * c["qty"] if c["side"] == "Buy"
                 else (c["avg_entry"] - price) * c["qty"])
        pnl = gross - c["fee"] - fee
        self.journal.write("cycle_recovered", cycle_id=c["cycle_id"],
                           symbol=c["symbol"], open_ts=c.get("open_ts"))
        self.journal.cycle_closed(c["cycle_id"], c["symbol"],
                                  exit_reason=reason,
                                  pnl=round(pnl, 4),
                                  close_ts=int(time.time() * 1000))
        self.cycles.pop(c["cycle_id"], None)
        logger.info("цикл %s закрыт на старте (сверка): %s pnl=%.4f",
                    c["symbol"], reason, pnl)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _self_test(cfg_path: str) -> int:
    checks: list[tuple[str, bool, str]] = []
    dca = bot_config.read_dca_section(cfg_path)
    p = bot_config.dca_params_from_config(dca)
    checks.append(("DcaParams из config.yml валиден", True,
                   f"entry={p.entry_usdt} steps={p.max_docups} tp={p.tp_pct} "
                   f"lev={p.leverage} notional={planned_ladder_notional(p):.1f}"))

    base = _link("BTWUSDT:1:1786907820000")
    checks.append(("sanitize orderLinkId", base == "BTWUSDT_1_1786907820000", base))
    checks.append(("orderLinkId <= 36",
                   len(_link("BTWUSDT:1:1786907820000", "_so9")) <= 36,
                   _link("BTWUSDT:1:1786907820000", "_so9")))

    tp_buy = rbot.tp_price(100.0, "Buy", 1.2, 0.01)
    tp_sell = rbot.tp_price(100.0, "Sell", 1.2, 0.01)
    checks.append(("tp_price long", abs(tp_buy - 101.2) < 1e-9, str(tp_buy)))
    checks.append(("tp_price short", abs(tp_sell - 98.8) < 1e-9, str(tp_sell)))

    e0 = rbot.take_profit_pct_at(p, 0)
    e2 = rbot.take_profit_pct_at(p, 2)
    checks.append(("tp_escalation L0/L2", e0 == 1.2 and e2 == 2.0,
                   f"{e0}/{e2}"))

    qty = quantize_qty(20.0 / 0.37, 0.001)
    checks.append(("quantize_qty к шагу 0.001",
                   abs(qty - round(20.0 / 0.37 / 0.001) * 0.001) < 1e-9,
                   str(qty)))

    for name, ok, detail in checks:
        print(("OK   " if ok else "FAIL ") + name + (" — " + detail if detail else ""))
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Исполнитель DCA-бота: журнал скринера → ордера Bybit Testnet",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/config.yml",
                    help="путь к config.yml (по умолчанию config/config.yml)")
    ap.add_argument("--live", action="store_true",
                    help="реальные ордера на Bybit Testnet (нужны API-ключи); "
                         "без флага — paper-режим, ордера симулируются")
    ap.add_argument("--once", action="store_true",
                    help="один такт (сверка + сигналы + мониторинг) и выход")
    ap.add_argument("--max-signal-age", type=int, default=300,
                    help="сигналы старее N секунд считаются устаревшими "
                         "(0 — принимать любые; по умолчанию 300 = кулдаун)")
    ap.add_argument("--check-config", action="store_true",
                    help="проверить config.yml (все секции) и выйти")
    ap.add_argument("--self-test", action="store_true",
                    help="самопроверка чистой логики (без сети) и выход")
    ap.add_argument("--force", action="store_true",
                    help="не блокировать старт при расхождении часов (FR-032)")
    args = ap.parse_args(argv)

    if args.check_config:
        checks = bot_config.validate_config(args.config)
        for c in checks:
            print(("OK   " if c["ok"] else "FAIL ") + c["name"] +
                  (" — " + c["detail"] if c["detail"] else ""))
        bad = [c for c in checks if not c["ok"]]
        print(f"итог: {len(checks) - len(bad)} ok, {len(bad)} fail")
        return 0 if not bad else 1
    if args.self_test:
        return _self_test(args.config)

    dca_raw = bot_config.read_dca_section(args.config)
    p = bot_config.dca_params_from_config(dca_raw)
    bp = bot_config.bot_params_from_config(bot_config.read_section(args.config, "bot"))
    bybit_cfg = BybitConfig.from_dict(
        bot_config.read_section(args.config, "bybit"), env=os.environ)
    sc_raw = bot_config.read_section(args.config, "screener")
    screener_path = str(sc_raw.get("journal_path") or "logs/screener-events.jsonl")

    mode = "live" if args.live else "paper"
    if mode == "live" and (not bybit_cfg.api_key or not bybit_cfg.api_secret):
        logger.error("--live требует API-ключи Testnet: bybit.api_key/api_secret "
                     "в config.yml или BYBIT_API_KEY/BYBIT_API_SECRET")
        return 1

    client = BybitClient(bybit_cfg) if mode == "live" else PaperClient(
        bybit_cfg, fee_rate=p.fee_rate)

    try:
        st = client.server_time()
        skew = abs(int(st) - int(time.time() * 1000))
        logger.info("часы: Testnet − local = %d мс", int(st) - int(time.time() * 1000))
        if skew > bp.max_clock_skew_ms and not args.force:
            logger.error("расхождение часов с Testnet %d мс > лимита %d мс "
                         "(FR-032): старт запрещён", skew, bp.max_clock_skew_ms)
            return 1
    except Exception as e:
        logger.warning("не удалось проверить часы Testnet: %s", e)

    bot = DcaBot(
        p=p, bp=bp, client=client, mode=mode,
        journal_path=bp.journal_path,
        state_path=os.path.join("logs", "bot-state.json"),
        screener_path=screener_path,
        trail_trigger_pct=_f(dca_raw.get("trail_trigger_pct") or 0),
        trail_step_pct=_f(dca_raw.get("trail_step_pct") or 0),
        max_signal_age=args.max_signal_age,
    )
    if args.once:
        bot.once()
    else:
        bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
