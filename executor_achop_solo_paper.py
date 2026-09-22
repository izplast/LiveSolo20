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
в paper-режиме исполнение симулируется по публичным котировкам Mainnet
(реальное движение рынка), ордера на биржу не уходят.

Два режима:
  * paper (по умолчанию) — проверка конвейера «журнал → решение → журнал»,
    ордера не выставляются; нужен только публичный API;
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
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

# Sprint 3: tenacity для ретраев Bybit API (нормальное логирование через before_sleep)
try:
    import tenacity  # type: ignore
    from tenacity import (  # type: ignore
        before_sleep_log,
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    HAS_TENACITY = True
except ImportError:  # pragma: no cover
    HAS_TENACITY = False
    tenacity = None  # type: ignore

    def retry(*a, **kw):  # type: ignore
        def deco(fn):
            return fn

        return deco

    def retry_if_exception(*a, **kw):  # type: ignore
        return None

    def stop_after_attempt(*a, **kw):  # type: ignore
        return None

    def wait_exponential_jitter(*a, **kw):  # type: ignore
        return None

    def before_sleep_log(*a, **kw):  # type: ignore
        return None

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
    # Изоляция логов: bot4 (achop_solo) -> bot4.log, achop -> bot3.log, paper -> paper_dca.log
    _bn = os.path.basename(__file__)
    if "achop_solo" in _bn or "bot4" in _bn:
        _log_file = "logs/bot4.log"
    elif "achop" in _bn:
        _log_file = "logs/bot3.log"
    elif "paper" in _bn:
        _log_file = "logs/paper_dca.log"
    else:
        _log_file = "logs/bot.log"
    try:
        fh = logging.FileHandler(_log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        _logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    _logger.addHandler(sh)
    _logger.propagate = False


_setup_logging()

from pricing import quantize_price, quantize_qty  # noqa: E402
import bot as rbot  # noqa: E402
import bot_config  # noqa: E402
from resilience import (  # noqa: E402
    CircuitBreaker, ExponentialBackoff, ResilientCaller, ResponseError,
    CircuitOpenError, MaxRetriesError,
)
from cycle_journal import CycleJournal  # noqa: E402
import reconcile as rcon  # noqa: E402

# ACHOP индикатор (изолированный модуль)
try:
    from tools.indicators import compute_achop  # noqa: E402
except ImportError:
    try:
        from indicators import compute_achop  # noqa: E402
    except ImportError:
        compute_achop = None  # type: ignore

backtest = bot_config.backtest
DcaParams = backtest.DcaParams
effective_step_pct = backtest.effective_step_pct
effective_stop_pct = backtest.effective_stop_pct
planned_ladder_notional = backtest.planned_ladder_notional

logger = _logger

# ── Soft DCA paper constants (только для executor_dca_paper.py) ──────────────
# BO/SO объёмы и риск — изолированно от live-бота (live берёт из config.yml)
# Solo ACHOP: BO 20, без усреднения
PAPER_BO_USDT: float = 20.0
PAPER_SO1_USDT: float = 0.0
PAPER_MAX_CYCLE_LOSS_USDT: float = 5.0
SOFT_DCA_TRIGGER_PCT: float = 0.6  # просадка для маркет-триггера SO_1 (обновлено с 1.2% на 0.6%)

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


# Sprint 3: tenacity-обёртка с нормальным логированием (поверх ResilientCaller)
# Если tenacity не установлен — no-op, работает только circuit breaker.
if HAS_TENACITY:

    def _tenacity_retry(fn, *, max_attempts: int = 4):
        dec = retry(
            retry=retry_if_exception(_bybit_retryable),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=0.4, max=8, jitter=2),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        return dec(fn)

else:  # fallback

    def _tenacity_retry(fn, *, max_attempts: int = 4):  # type: ignore
        return fn


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

    def _call_resilient(self, fn, *, max_attempts: int | None = None) -> Any:
        """Единая точка ретраев: tenacity (логирование) + ResilientCaller (breaker)."""
        # tenacity снаружи — логирует каждую паузу перед повтором
        def _inner():
            return self.caller.call(fn, retryable=_bybit_retryable, max_attempts=max_attempts)  # type: ignore

        if HAS_TENACITY:
            # tenacity ретраит только retryable исключения — остальные сразу всплывают
            dec = retry(
                retry=retry_if_exception(_bybit_retryable),
                stop=stop_after_attempt(max_attempts or self.caller.max_attempts),
                wait=wait_exponential_jitter(initial=0.4, max=8, jitter=2),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            )
            return dec(_inner)()
        else:
            return _inner()

    def _get(self, path: str, params: dict | None = None,
             signed: bool = False) -> dict:
        params = dict(params or {})
        if not signed:
            def fn():
                r = requests.get(self.base + path, params=params, timeout=10)
                return self._check(r)
            return self._call_resilient(fn)
        qs = urllib.parse.urlencode(sorted(params.items()))
        ts = int(time.time() * 1000)

        def fn():
            r = requests.get(self.base + path + "?" + qs, timeout=10,
                             headers=self._headers(ts, self._sign(ts, qs)))
            return self._check(r)
        return self._call_resilient(fn)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body, separators=(",", ":"))
        ts = int(time.time() * 1000)

        def fn():
            r = requests.post(self.base + path, data=data, timeout=10,
                              headers=self._headers(ts, self._sign(ts, data)))
            return self._check(r)
        return self._call_resilient(fn)

    # публичные
    def server_time(self) -> int:
        # поле time лежит в корне ответа, а не в result — читаем сырой JSON.
        def fn():
            r = requests.get(self.base + "/v5/market/time", timeout=10)
            if not 200 <= r.status_code < 300:
                raise ResponseError(r.status_code, r.text[:200])
            return int(r.json().get("time", 0))
        return self._call_resilient(fn)

    def instruments(self, symbol: str | None = None) -> list[dict]:
        p = {"category": "linear"}
        if symbol:
            p["symbol"] = symbol
        return self._get("/v5/market/instruments-info", p).get("list", [])

    def ticker(self, symbol: str) -> dict | None:
        lst = self._get("/v5/market/tickers",
                        {"category": "linear", "symbol": symbol}).get("list", [])
        return lst[0] if lst else None

    def orderbook(self, symbol: str, limit: int = 1) -> dict:
        """Лучшие уровни стакана: {"bids": [(px, qty)], "asks": [(px, qty)]}.

        Нужен для maker-выходов и фильтра проскальзывания: решение о том,
        крестить спред маркетом, принимается по фактической глубине top-of-book.
        """
        r = self._get("/v5/market/orderbook",
                      {"category": "linear", "symbol": symbol,
                       "limit": str(max(1, min(200, limit)))})
        book = r or {}

        def levels(key: str) -> list[tuple[float, float]]:
            out = []
            for row in book.get(key) or []:
                try:
                    px, qty = _f(row[0]), _f(row[1])
                except Exception:  # noqa: BLE001 — битый уровень пропускаем
                    continue
                if px > 0 and qty > 0:
                    out.append((px, qty))
            return out

        return {"bids": levels("b"), "asks": levels("a")}

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


class MainnetQuoteFeed:
    """Публичный поток котировок Mainnet для paper-симуляции: WS + REST-фолбэк.

    Фоновый поток со своим event loop: подписка tickers.<symbol> по мере
    запросов символов, app-level ping каждые ping_sec с контролем pong,
    backoff-реконнект. Пока соединения нет дольше rest_fallback_after_sec,
    цены освежаются одним REST-запросом /v5/market/tickers (весь linear
    сразу, ~1 запрос). Основной поток бота только читает готовый словарь
    цен — запись ведёт один поток, чтение атомарно под GIL, блокировок на
    горячем пути нет.
    """

    WS_URL = "wss://stream.bybit.com/v5/public/linear"
    REST_URL = "https://api.bybit.com/v5/market/tickers"

    def __init__(self, ttl_ms: int = 3000, ping_sec: float = 15,
                 stale_pong_sec: float = 20, rest_fallback_after_sec: float = 30,
                 rest_interval_sec: float = 2.0) -> None:
        self.ttl_ms = ttl_ms
        self.ping_sec = ping_sec
        self.stale_pong_sec = stale_pong_sec
        self.rest_fallback_after_sec = rest_fallback_after_sec
        self.rest_interval_sec = rest_interval_sec
        self._prices: dict[str, tuple[int, float]] = {}
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ── API основного потока ──────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="quote-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)

    def touch(self, symbol: str) -> None:
        """Попросить котировки символа (идемпотентно, без блокировки надолго)."""
        with self._lock:
            if symbol not in self._wanted:
                self._wanted.add(symbol)
                self._pending.append(symbol)

    def last_price(self, symbol: str) -> float | None:
        """Свежая цена из кэша или None (читатель никогда не ходит в сеть)."""
        self.touch(symbol)
        hit = self._prices.get(symbol)
        if not hit or int(time.time() * 1000) - hit[0] > self.ttl_ms:
            return None
        return hit[1]

    # ── фоновый поток ─────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.warning("websockets не установлен — котировки только по "
                           "REST-фолбэку")
            self._loop_rest_only()
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(websockets))
        except Exception as e:  # noqa: BLE001 — фид не должен ронять процесс
            logger.error("лента котировок остановлена: %s", e)

    def _loop_rest_only(self) -> None:
        """Без websockets: тупо опрашиваем REST раз в rest_interval_sec."""
        while not self._stop_evt.is_set():
            n = self._rest_snapshot()
            if n == 0:
                time.sleep(self.rest_interval_sec)

    async def _main(self, websockets: Any) -> None:
        attempt = 0
        down_since: float | None = None
        fallback: asyncio.Task | None = None
        while not self._stop_evt.is_set():
            try:
                async with websockets.connect(
                        self.WS_URL, ping_interval=None, open_timeout=10,
                        close_timeout=5, max_queue=256) as ws:
                    attempt = 0
                    with self._lock:
                        self._pending.extend(
                            s for s in self._wanted if s not in self._subscribed)
                    await self._flush_subscribe(ws)
                    if down_since is not None:
                        logger.info("лента котировок восстановилась")
                        down_since = None
                    if fallback is not None:
                        fallback.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await fallback
                        fallback = None
                    await self._pump(ws)
            except Exception as e:
                if self._stop_evt.is_set():
                    return
                with self._lock:
                    self._subscribed.clear()   # после реконнекта подписка заново
                if down_since is None:
                    down_since = time.monotonic()
                    logger.warning("лента котировок оборвалась (%s)", e)
                    if self.rest_fallback_after_sec > 0 and fallback is None:
                        fallback = asyncio.create_task(self._rest_fallback(down_since))
                attempt += 1
                delay = min(60.0, 1.0 * 2 ** (attempt - 1)) * (0.7 + 0.6 * random.random())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_wait(), timeout=delay)
        if fallback is not None:
            fallback.cancel()

    async def _stop_wait(self) -> None:
        while not self._stop_evt.is_set():
            await asyncio.sleep(0.1)

    async def _flush_subscribe(self, ws: Any) -> None:
        while True:
            with self._lock:
                batch, self._pending = self._pending[:10], self._pending[10:]
            if not batch:
                return
            await ws.send(json.dumps(
                {"op": "subscribe", "args": [f"tickers.{s}" for s in batch]}))
            with self._lock:
                self._subscribed.update(batch)

    async def _pump(self, ws: Any) -> None:
        """Приём тикеров с подпиской новых символов, ping/pong и сторожем."""
        last_pong = time.monotonic()

        async def pinger() -> None:
            nonlocal last_pong
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"op": "ping"}))
                sent_at = time.monotonic()
                await asyncio.sleep(self.stale_pong_sec)
                if last_pong < sent_at:
                    raise RuntimeError("нет pong — соединение мёртвое")

        async def subscriber() -> None:
            while True:
                await asyncio.sleep(0.3)
                with self._lock:
                    dirty = list(self._pending)
                    self._pending.clear()
                for i in range(0, len(dirty), 10):
                    chunk = dirty[i:i + 10]
                    await ws.send(json.dumps(
                        {"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}))
                    with self._lock:
                        self._subscribed.update(chunk)

        recv_task: asyncio.Task = asyncio.create_task(ws.recv())
        aux = [asyncio.create_task(pinger()), asyncio.create_task(subscriber())]
        try:
            while not self._stop_evt.is_set():
                done, _ = await asyncio.wait(
                    {recv_task, *aux}, timeout=self.stale_pong_sec * 3,
                    return_when=asyncio.FIRST_COMPLETED)
                for t in aux:
                    if t in done:
                        t.result()
                if recv_task in done:
                    raw = recv_task.result()
                    recv_task = asyncio.create_task(ws.recv())
                    if '"pong"' in str(raw):
                        last_pong = time.monotonic()
                        continue
                    self._on_ticker(raw)
                if not done:
                    raise RuntimeError("лента молчит дольше лимита")
        finally:
            recv_task.cancel()
            for t in aux:
                t.cancel()
            await asyncio.gather(recv_task, *aux, return_exceptions=True)

    def _on_ticker(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        topic = msg.get("topic") or ""
        if not topic.startswith("tickers."):
            return
        symbol = topic.split(".", 1)[1]
        data = msg.get("data") or {}
        try:
            px = _f(data.get("lastPrice"))
        except Exception:  # noqa: BLE001
            return
        if px > 0:
            self._prices[symbol] = (int(time.time() * 1000), px)

    async def _rest_fallback(self, down_since: float) -> None:
        """Пока WS лежит дольше порога — освежать цены одним REST-запросом."""
        delay = self.rest_fallback_after_sec - (time.monotonic() - down_since)
        if delay > 0:
            await asyncio.sleep(delay)
        logger.warning("WS котировок недоступен дольше %.0f с — REST-фолбэк",
                       self.rest_fallback_after_sec)
        while not self._stop_evt.is_set():
            self._rest_snapshot()
            await asyncio.sleep(self.rest_interval_sec)

    def _rest_snapshot(self) -> int:
        """Один запрос /v5/market/tickers → обновить цены всех нужных символов."""
        wanted = set(self._wanted)
        if not wanted:
            return 0
        try:
            r = requests.get(self.REST_URL, params={"category": "linear"},
                             timeout=5)
            rows = ((r.json() or {}).get("result") or {}).get("list") or []
        except Exception as e:  # noqa: BLE001
            logger.debug("REST-фолбэк котировок не удался: %s", e)
            return 0
        now = int(time.time() * 1000)
        n = 0
        for row in rows:
            sym = row.get("symbol")
            if sym not in wanted:
                continue
            px = _f(row.get("lastPrice") or row.get("markPrice"))
            if px > 0:
                self._prices[sym] = (now, px)
                n += 1
        return n


class MainnetPublic:
    """Котировки Mainnet для paper-симуляции: WS-лента + REST-фолбэк.

    Зачем не тестнет: стаканы Testnet по альткоинам заморожены (BEATUSDT
    3.09 против 0.13 на mainnet), из-за этого TP-лимитки не пересекались,
    а time_exit закрывался ровно по цене входа — PnL состоял из одних
    комиссий. Симуляция по mainnet считает PnL по реальному движению рынка.

    Источник цены — MainnetQuoteFeed (публичный WS tickers, при обрыве —
    один общий REST-запрос). Если ленты нет вовсе (feed=None или цена не
    успела прийти), работает прежний точечный REST-запрос с TTL-кэшем.
    """

    BASE = "https://api.bybit.com"

    def __init__(self, feed: MainnetQuoteFeed | None = None,
                 timeout: float = 5.0, ttl_ms: int = 1500):
        self.feed = feed
        self.timeout = timeout
        self.ttl_ms = ttl_ms
        self._cache: dict[str, tuple[int, float]] = {}

    def last_price(self, symbol: str) -> float | None:
        if self.feed is not None:
            px = self.feed.last_price(symbol)
            if px is not None:
                return px
        now = int(time.time() * 1000)
        hit = self._cache.get(symbol)
        if hit and now - hit[0] < self.ttl_ms:
            return hit[1]
        p: float | None = None
        try:
            r = requests.get(f"{self.BASE}/v5/market/tickers",
                             params={"category": "linear", "symbol": symbol},
                             timeout=self.timeout)
            lst = ((r.json() or {}).get("result") or {}).get("list") or []
            if lst:
                p = _f(lst[0].get("lastPrice") or lst[0].get("markPrice"))
                if p <= 0:
                    p = None
        except Exception:
            p = None
        if p is not None:
            self._cache[symbol] = (now, p)
        return p


class PaperClient:
    """Симуляция исполнения для paper-режима: ордера не уходят на биржу.

    Публичные вызовы (время, инструменты) — реальные, с Testnet; котировки
    для симуляции — публичный Mainnet (см. MainnetPublic). Приватные
    (ордера, позиции) симулируются локально: маркет-филл сразу по текущей
    цене, лимитки срабатывают, когда mainnet-тикер пересекает уровень.
    """

    def __init__(self, cfg: BybitConfig, fee_rate: float = 0.00055,
                 feed: "MainnetQuoteFeed | None" = None):
        self.bybit = BybitClient(cfg)
        self.mainnet = MainnetPublic(feed=feed)
        self.fee_rate = fee_rate
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.ref: dict[str, float] = {}

    def server_time(self) -> int:
        return self.bybit.server_time()

    def instruments(self, symbol: str | None = None) -> list[dict]:
        return self.bybit.instruments(symbol)

    def ticker(self, symbol: str) -> dict | None:
        # Котировки симуляции — публичный Mainnet; фолбэк на тестнет-тикер,
        # если mainnet недоступен (иначе симуляция ослепла бы целиком).
        p = self.mainnet.last_price(symbol)
        if p is None:
            try:
                t = self.bybit.ticker(symbol)
                if t:
                    p = _f(t.get("lastPrice") or t.get("markPrice")) or None
            except Exception:
                p = None
        if p is not None:
            self.ref[symbol] = p
            return {"lastPrice": str(p), "markPrice": str(p)}
        rp = self.ref.get(symbol)
        return ({"lastPrice": str(rp), "markPrice": str(rp)} if rp else None)

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return {}

    def orderbook(self, symbol: str, limit: int = 1) -> dict:
        """Синтетический стакан вокруг mainnet-цены (спред 0.04%).

        В paper-режиме глубина не моделируется — важен уровень цены:
        maker-выход ставится у лучшего уровня, фильтр проскальзывания
        оценивает полуспред по той же синтетике.
        """
        px = _f((self.ticker(symbol) or {}).get("lastPrice") or 0)
        if px <= 0:
            return {"bids": [], "asks": []}
        half = max(px * 0.0002, 1e-12)
        qty = 10.0 ** 9
        return {"bids": [(px - half, qty)], "asks": [(px + half, qty)]}

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

    def anomaly_reason(self, price: float, entry_usdt: float) -> str | None:
        """Жёсткая валидация min_qty/qty_step относительно текущей цены.

        Возвращает строку-причину аномалии или None если шаг в порядке.
        Проверяет что минимальный лот и шаг не раздувают номинал в разы
        относительно entry_usdt — защита от замороженных стаканов как
        OPGUSDT (testnet 628 vs mainnet 0.10, min_qty=1 → номинал 628 USDT
        при entry 20).
        """
        if price <= 0 or entry_usdt <= 0:
            return None
        # min_qty * price — минимальный номинал одним ордером
        min_notional = self.min_qty * price
        step_notional = self.qty_step * price
        # Порог 5× entry для min_qty — ловит OPG: 1*628=628 > 20*5=100
        # Для шага порог выше (10×), т.к. для высоковатых инструментов
        # (BTC 60k *0.001=60 > 20*2) это нормально — минимальный лот уже
        # превышает entry, а шаг сам по себе не должен блокировать.
        if self.min_qty > 0 and min_notional > entry_usdt * 5:
            return (f"min_qty {self.min_qty:g}*price {price:.6g}="
                    f"{min_notional:.1f} USDT >> entry {entry_usdt:.1f} "
                    f"(>5×, аномальный лот/цена, вероятно замороженный стакан)")
        if self.qty_step > 0 and step_notional > entry_usdt * 10:
            return (f"qty_step {self.qty_step:g}*price {price:.6g}="
                    f"{step_notional:.1f} USDT > entry*10 — шаг лота аномален")
        # Доп. проверка: minNotional биржи тоже не должен быть >> entry
        # (иногда minNotionalValue >> entry из-за кривого инструмента)
        return None


def _validate_instrument_size(
    info: InstrumentInfo, price: float, entry_usdt: float
) -> str | None:
    """Обёртка над InstrumentInfo.anomaly_reason для вызова из DcaBot."""
    return info.anomaly_reason(price, entry_usdt)


def _price_divergence_reason(
    signal_price: float | None, venue_price: float | None,
    threshold: float = 5.0
) -> str | None:
    """Проверка расхождения цены сигнала (mainnet) и venue (testnet).

    Если обе цены >0 и отношение > threshold (в любую сторону) — вероятно
    замороженный/битый стакан testnet (OPGUSDT: 0.10 vs 628 ~ 6000×).
    """
    if signal_price and venue_price and signal_price > 0 and venue_price > 0:
        ratio = max(venue_price, signal_price) / min(venue_price, signal_price)
        if ratio > threshold:
            return (f"расхождение цен signal {signal_price:.6g} vs "
                    f"venue {venue_price:.6g} = {ratio:.1f}× > {threshold:g}× — "
                    f"вероятно замороженный стакан testnet")
    return None


# ── ACHOP фильтр (только для bot3) ───────────────────────────────────────────
BYBIT_REST_ACHOP = "https://api.bybit.com"

def _fetch_klines_achop(symbol: str, interval: str = "1", limit: int = 30) -> list:
    """Короткий REST-фетч 1m свечей для ACHOP (публичный Bybit, без подписи)."""
    try:
        r = requests.get(f"{BYBIT_REST_ACHOP}/v5/market/kline",
                         params={"category": "linear", "symbol": symbol, "interval": interval, "limit": min(1000, limit + 1)},
                         timeout=5)
        r.raise_for_status()
        body = r.json()
        if body.get("retCode") != 0:
            return []
        rows = []
        interval_ms = int(interval) * 60_000
        now_ms = int(time.time() * 1000)
        for row in (body.get("result", {}).get("list", []) or []):
            try:
                rows.append([int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
            except Exception:
                continue
        closed = [x for x in rows if x[0] + interval_ms <= now_ms]
        closed.sort(key=lambda x: x[0])
        return closed[-limit:]
    except Exception:
        return []


def _achop_for_symbol(symbol: str) -> float | None:
    """ACHOP по 1m буферу (deque 50 аналог) — адаптивный length через NATR."""
    if compute_achop is None:
        return None
    candles = _fetch_klines_achop(symbol, "1", 30)
    if len(candles) < 15:
        return None
    try:
        return compute_achop(candles, cycle_part=0.15)
    except Exception:
        return None


def _parse_instrument(row: dict) -> InstrumentInfo | None:
    lot = row.get("lotSizeFilter") or {}
    prc = row.get("priceFilter") or {}
    lev = row.get("leverageFilter") or {}
    try:
        info = InstrumentInfo(
            symbol=str(row.get("symbol", "")),
            qty_step=_f(lot.get("qtyStep") or 1),
            min_qty=_f(lot.get("minOrderQty") or 0),
            tick_size=_f(prc.get("tickSize") or 1),
            max_leverage=_f(lev.get("maxLeverage") or 0),
            max_order_qty=_f(lot.get("maxOrderQty") or 0),
            max_notional=_f(lot.get("maxNotionalValue") or 0),
        )
        # Базовая валидация шагов: биржа не должна отдавать 0/отрицательные шаги
        if info.qty_step <= 0 or info.tick_size <= 0:
            logger.warning("битый инструмент %s: qty_step=%s tick=%s",
                           info.symbol, info.qty_step, info.tick_size)
            return None
        if info.min_qty < 0 or info.qty_step < 0:
            return None
        return info
    except Exception:
        return None


# ── DCA-бот ──────────────────────────────────────────────────────────────────


class DcaBot:
    """Конвейер «журнал скринера → реальные DCA-ордера Testnet → журнал бота»."""

    def __init__(self, *, p: DcaParams, bp, client: Any, mode: str,
                 journal_path: str, state_path: str, screener_path: str,
                 trail_trigger_pct: float = 0.0, trail_step_pct: float = 0.0,
                 max_signal_age: int = 300,
                 exit_maker_enabled: bool = True,
                 exit_maker_timeout_sec: float = 15.0,
                 max_exit_slippage_pct: float = 0.5,
                 exit_force_after_sec: float = 180.0,
                 signal_poll_sec: float = 0.5) -> None:
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
        # ── защита от проскальзывания на выходах (SC-004: p95 close до 10%) ──
        # Выход сначала пробует стать maker'ом (PostOnly-лимитка у лучшей
        # встречной цены) с таймаутом; маркет-ордер разрешён только если
        # полуспред стакана укладывается в max_exit_slippage_pct, иначе
        # попытка maker повторяется до дедлайна exit_force_after_sec, после
        # которого позиция закрывается маркетом безусловно (страховка от
        # «вечного» выхода по стопу/таймеру).
        self.exit_maker_enabled = bool(exit_maker_enabled)
        self.exit_maker_timeout_sec = max(0.0, _f(exit_maker_timeout_sec))
        self.max_exit_slippage_pct = max(0.0, _f(max_exit_slippage_pct))
        self.exit_force_after_sec = max(0.0, _f(exit_force_after_sec))
        self.signal_poll_sec = max(0.05, _f(signal_poll_sec) or 0.5)
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
        # Двухчастотный цикл: новые сигналы вычитываются часто
        # (signal_poll_sec, по умолчанию 0.5 с — иначе 3-секундный такт
        # монитора добавлял к задержке сигнал→вход в среднем 1.5 с и в p95
        # выдавал >2 с), сопровождение циклов — реже (monitor_interval_sec).
        next_signal_poll = 0.0
        next_monitor = 0.0
        while True:
            try:
                now = time.monotonic()
                if now >= next_signal_poll:
                    self._poll_signals()
                    next_signal_poll = now + max(0.05, self.signal_poll_sec)
                if now >= next_monitor:
                    self._monitor_cycles()
                    self._save_state()
                    next_monitor = now + self.bp.monitor_interval_sec
                    if now - last_hb >= self.bp.heartbeat_sec:
                        last_hb = now
                        logger.info("heartbeat: открыто циклов=%d обработано сигналов=%d",
                                    sum(1 for c in self.cycles.values()
                                        if not c.get("closed")),
                                    len(self.processed))
                time.sleep(0.05)
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
        # ── защита от замороженного стакана testnet (OPGUSDT: 628 vs 0.10) ──
        # Жесткая валидация цены venue относительно цены сигнала (mainnet)
        div_reason = _price_divergence_reason(
            _f(sig.get("price")), pt, threshold=5.0)
        if div_reason:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail=div_reason)
            return
        # Ранняя проверка лота по текущей цене (до открытия цикла):
        # если у инструмента min_qty*price аномален — отклонить сразу,
        # не дожидаясь расчёта qty в _open_cycle.
        if pt and pt > 0:
            try:
                pre_info = self._instrument(symbol)
                if pre_info is not None:
                    _pre_entry = PAPER_BO_USDT if "paper" in os.path.basename(__file__) else self.p.entry_usdt
                    pre_anomaly = _validate_instrument_size(
                        pre_info, pt, _pre_entry)
                    if pre_anomaly:
                        self._reject(sig_id, "exchange_limits", symbol=symbol,
                                     reason_detail=pre_anomaly)
                        return
            except Exception:
                pass  # инструмент недоступен — проверим в _open_cycle

        # ── ACHOP фильтр (только для bot3: 42.0-58.0 боковик — пропуск) ──
        if "achop" in os.path.basename(__file__) and compute_achop is not None:
            try:
                achop_val = _achop_for_symbol(symbol)
                if achop_val is not None:
                    if 42.0 <= achop_val <= 58.0:
                        self._reject(sig_id, "ACHOP Sideways", symbol=symbol,
                                     reason_detail=f"ACHOP {achop_val:.1f} in 42.0-58.0 боковик")
                        logger.info("ACHOP фильтр %s %.1f в боковике 42.0-58.0 — пропуск", symbol, achop_val)
                        return
                    else:
                        logger.info("ACHOP %s %.1f вне боковика — вход разрешён", symbol, achop_val)
            except Exception as e:
                logger.debug("ACHOP расчёт не удался %s: %s", symbol, e)

        self.journal.write("signal_received", signal_id=sig_id, symbol=symbol,
                           price_mainnet=sig.get("price"),
                           price_venue=sig.get("price_venue") or "bybit_mainnet",
                           price_testnet=pt,
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
        # ── жёсткая валидация лота относительно текущей цены (1114/906) ──
        # Проверка расхождения цен повторно (на случай прямого вызова _open_cycle)
        sig_price = _f(sig.get("price"))
        div_reason = _price_divergence_reason(sig_price, ref if pt else None, threshold=5.0)
        # Если ref взят из signal_price (pt отсутствует), сравнение не нужно
        if pt is not None:
            # pt уже в ref, но проверяем именно pt vs signal
            div_reason = _price_divergence_reason(sig_price, pt, threshold=5.0)
            if div_reason:
                self._reject(sig_id, "exchange_limits", symbol=symbol,
                             reason_detail=div_reason)
                return
        # Paper: BO=10 USDT изолированно от live (20 USDT)
        _entry_usdt = PAPER_BO_USDT if "paper" in os.path.basename(__file__) else self.p.entry_usdt
        anomaly = _validate_instrument_size(info, ref, _entry_usdt)
        if anomaly:
            self._reject(sig_id, "exchange_limits", symbol=symbol,
                         reason_detail=anomaly)
            return

        qty = quantize_qty(_entry_usdt / ref, info.qty_step)
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
        # Paper Soft DCA: только маркет-триггер SO_1 на -1.2%, лимитки отключены
        if "paper" in os.path.basename(__file__):
            return
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
                               expected_price=c["price_testnet"],
                               avg_fill_price=round(price, 8),
                               price_venue="bybit_testnet",
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
                               price_venue="bybit_testnet",
                               ts_signal=c["signal_ts"], ts_confirmed=now)
            logger.info("докупка #%d %s @%.6g, avg=%.6g", c["docups"],
                        c["symbol"], price, c["avg_entry"])
            self._after_position_change(c, now)
        elif role == "tp":
            self._close(c, "take_profit", price, now, fee)
        elif role == "close":
            self._close(c, c.get("close_reason") or "manual", price, now, fee)
        elif role == "close_maker":
            # Частичный филл maker-выхода, замеченный монитором: списать
            # объём с позиции; полная финализация — в потоке выхода.
            self._apply_close_fill(c, qty, price, fee, role="close_maker")
            if _f(c.get("qty")) <= 0:
                self._finalize_close(c, c.get("close_reason") or "manual")

    # ── выход с защитой от проскальзывания ────────────────────────────────────

    def _market_close(self, c: dict, reason: str) -> None:
        """Закрыть позицию, не кроша её в тонкий стакан.

        1. Maker-фаза: PostOnly-лимитка у лучшей встречной цены с таймаутом
           exit_maker_timeout_sec; частичные филлы списываются с позиции.
        2. Маркет разрешён только при полуспреде <= max_exit_slippage_pct;
           иначе попытка maker повторяется до дедлайна exit_force_after_sec
           (событие exit_deferred в журнале).
        3. После дедлайна маркет уходит безусловно: стоп/таймер обязаны
           исполняться. Неисполнение не роняет цикл — монитор повторит.
        """
        for link, o in list(c["links"].items()):
            if o["role"] in ("tp", "dca"):
                self._safe_cancel(c, link)
        c["close_reason"] = reason
        exit_side = "Sell" if c["side"] == "Buy" else "Buy"
        deadline = time.time() + self.exit_force_after_sec
        maker_seq = int(c.get("maker_seq") or 0)
        while True:
            if _f(c.get("qty")) <= 0:
                self._finalize_close(c, c.get("close_reason") or reason)
                return
            if self.exit_maker_enabled and self.exit_maker_timeout_sec > 0:
                maker_seq += 1
                c["maker_seq"] = maker_seq
                if self._maker_close_attempt(c, exit_side, maker_seq):
                    return
                if _f(c.get("qty")) <= 0:
                    self._finalize_close(c, c.get("close_reason") or reason)
                    return
            book = self._expected_exit_price(c, exit_side)
            if book is None:
                spread_ok = False          # пустой стакан — крестить нечем
            else:
                _, _, half_spread_pct = book
                spread_ok = (self.max_exit_slippage_pct <= 0 or
                             half_spread_pct <= self.max_exit_slippage_pct)
            if not spread_ok and time.time() < deadline:
                if book is not None:
                    self.journal.write(
                        "exit_deferred", cycle_id=c["cycle_id"],
                        symbol=c["symbol"], reason=reason,
                        best_price=round(book[0], 10),
                        mid_price=round(book[1], 10),
                        half_spread_pct=round(book[2], 4),
                        limit_pct=self.max_exit_slippage_pct,
                        deadline_in_sec=round(deadline - time.time(), 1))
                    logger.warning(
                        "выход %s (%s) отложен: полуспред %.3f%% > %.3f%% — "
                        "жду maker-филл вместо маркет-ордера",
                        c["symbol"], reason, book[2],
                        self.max_exit_slippage_pct)
                time.sleep(1.0)            # не крутить горячий цикл по пустому стакану
                continue
            if self._market_close_once(c, exit_side):
                return
            logger.error("закрытие %s (%s) не исполнилось — повтор на следующем "
                         "такте монитора", c["symbol"], reason)
            return

    def _safe_orderbook(self, symbol: str) -> dict:
        try:
            ob = self.client.orderbook(symbol)
        except Exception as e:  # noqa: BLE001
            self.journal.write("api_error", operation="orderbook",
                               symbol=symbol, error=str(e))
            return {"bids": [], "asks": []}
        return ob if isinstance(ob, dict) else {"bids": [], "asks": []}

    @staticmethod
    def _best_exit_price(ob: dict, exit_side: str) -> float:
        """Лучшая встречная цена для выхода: bid для продажи, ask для покупки."""
        levels = ob.get("bids") if exit_side == "Sell" else ob.get("asks")
        if not levels:
            return 0.0
        return _f(levels[0][0])

    def _expected_exit_price(self, c: dict, exit_side: str):
        """(лучшая цена выхода, mid, полуспред %) или None, если стакана нет."""
        ob = self._safe_orderbook(c["symbol"])
        best = self._best_exit_price(ob, exit_side)
        opposite = self._best_exit_price(
            ob, "Buy" if exit_side == "Sell" else "Sell")
        if best <= 0 or opposite <= 0:
            return None
        mid = (best + opposite) / 2
        if mid <= 0:
            return None
        half_spread_pct = abs(mid - best) / mid * 100
        return best, mid, half_spread_pct

    def _maker_close_attempt(self, c: dict, exit_side: str, seq: int) -> bool:
        """Одна попытка maker-выхода: PostOnly-лимитка у лучшей встречной цены.

        True — позиция полностью вышла. Частичный филл учитывается, остаток
        закрывает следующая попытка либо маркет-фаза. Отказ PostOnly
        (ордер пересёк бы спред) — обычный исход, ведёт к проверке спреда.
        """
        ob = self._safe_orderbook(c["symbol"])
        px = self._best_exit_price(ob, exit_side)
        if px <= 0:
            return False
        px = quantize_price(px, c["tick_size"])
        link = _link(c["signal_id"], f"_mk{seq}")
        try:
            self._place(c, side=exit_side, order_type="Limit", qty=_f(c["qty"]),
                        price=px, reduce_only=True, tif="PostOnly",
                        role="close_maker", link=link)
        except Exception as e:  # noqa: BLE001
            self.journal.write("api_error", operation="place_maker_close",
                               symbol=c["symbol"], error=str(e))
            return False
        order = self._wait_fill(c, link, int(self.exit_maker_timeout_sec * 1000))
        status = str((order or {}).get("orderStatus") or "")
        if status != "Filled":
            self._safe_cancel(c, link)     # снять остаток зависшей лимитки
            try:
                # Филл мог пройти между последним опросом и отменой: дочитать
                # финальный cumExecQty, иначе позиция разойдётся с биржей.
                o2 = self.client.get_order(c["symbol"], link)
                if o2 is not None:
                    order = o2
            except Exception:  # noqa: BLE001 — останется последний снапшот
                pass
        cum = _f(order.get("cumExecQty")) if order else 0.0
        if cum > 0:
            price = _f(order.get("avgPrice") or px)
            fee = _f(order.get("cumExecFee"))
            self._apply_close_fill(c, cum, price, fee, role="close_maker")
        if status == "Filled" or _f(c.get("qty")) <= 0:
            self._finalize_close(c, c.get("close_reason") or "manual")
            return True
        return False

    def _market_close_once(self, c: dict, exit_side: str) -> bool:
        """Один маркет-IOC на остаток позиции. True — позиция вышла."""
        reason = c.get("close_reason") or "manual"
        expected = self._expected_exit_price(c, exit_side)
        link = _link(c["signal_id"], f"_c_{reason}")
        try:
            self._place(c, side=exit_side, order_type="Market",
                        qty=_f(c["qty"]), price=None, reduce_only=True,
                        tif="IOC", role="close", link=link)
            order = self._wait_fill(c, link, self.bp.fill_timeout_ms)
            cum = _f(order.get("cumExecQty")) if order else 0.0
            if order is None or cum <= 0:
                raise BybitOrderError("рыночное закрытие не исполнилось")
            price = _f(order.get("avgPrice") or order.get("price"))
            fee = _f(order.get("cumExecFee"))
            # Контроль факта: если исполнились хуже ожидаемой цены сильнее
            # лимита — зафиксировать нарушение (позиция уже вышла, но метрика
            # должна показывать, где фильтр не спас).
            if expected is not None and self.max_exit_slippage_pct > 0 \
                    and expected[0] > 0:
                adverse = ((expected[0] - price) / expected[0] * 100
                           if exit_side == "Sell"
                           else (price - expected[0]) / expected[0] * 100)
                if adverse > self.max_exit_slippage_pct:
                    self.journal.write("exit_slippage_breach",
                                       cycle_id=c["cycle_id"],
                                       symbol=c["symbol"],
                                       expected_price=round(expected[0], 10),
                                       filled_price=round(price, 10),
                                       slippage_pct=round(adverse, 4),
                                       limit_pct=self.max_exit_slippage_pct)
            self._apply_close_fill(c, cum, price, fee, role="close")
        except Exception as e:  # noqa: BLE001
            self.journal.write("api_error", operation="market_close",
                               symbol=c["symbol"], reason=reason, error=str(e))
            logger.error("закрытие %s (%s) не удалось: %s",
                         c["symbol"], reason, e)
            return False
        if _f(c.get("qty")) <= 0:
            self._finalize_close(c, reason)
            return True
        return False

    def _apply_close_fill(self, c: dict, qty: float, price: float,
                          fee: float, role: str) -> None:
        """Списать часть позиции (поддержка частичных филлов выхода)."""
        now = int(time.time() * 1000)
        qty = min(qty, _f(c.get("qty")))
        if qty <= 0:
            return
        gross = ((price - c["avg_entry"]) * qty if c["side"] == "Buy"
                 else (c["avg_entry"] - price) * qty)
        c["realized_pnl"] = _f(c.get("realized_pnl")) + gross
        c["fee"] += fee
        c["qty"] = round(_f(c.get("qty")) - qty, 12)
        c["exit_price"] = price
        c["exit_ts"] = now
        self.journal.write("order_filled", signal_id=c["signal_id"],
                           cycle_id=c["cycle_id"], symbol=c["symbol"],
                           role=role, mode="close",
                           expected_price=c.get("tp_price"),
                           avg_fill_price=round(price, 8),
                           qty_closed=qty, qty_left=c["qty"],
                           price_venue="bybit_testnet",
                           ts_signal=c["signal_ts"], ts_confirmed=now)
        logger.info("закрытие %s: %.6g @%.6g, осталось %.6g",
                    c["symbol"], qty, price, c["qty"])

    def _finalize_close(self, c: dict, reason: str) -> None:
        """Финализация цикла после полного выхода: PnL, журнал, удаление."""
        pnl = _f(c.get("realized_pnl")) - c["fee"]
        c["pnl"] = pnl
        c["closed"] = True
        self.journal.cycle_closed(c["cycle_id"], c["symbol"],
                                  exit_reason=reason, pnl=round(pnl, 4),
                                  close_ts=int(time.time() * 1000))
        logger.info("цикл %s закрыт: %s pnl=%.4f", c["symbol"], reason, pnl)
        self.cycles.pop(c["cycle_id"], None)

    def _close(self, c: dict, reason: str, price: float, now: int, fee: float) -> None:
        """Выход по TP-лимитке/чужому ордеру: полный объём одной ценой."""
        for link, o in list(c["links"].items()):
            if o["role"] in ("tp", "dca"):
                self._safe_cancel(c, link)
        c["close_reason"] = reason
        c["exit_ts"] = now
        self._apply_close_fill(c, _f(c["qty"]), price, fee, role="close")
        self._finalize_close(c, reason)

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

    def _try_soft_dca(self, c: dict, mark: float, now: int) -> bool:
        """Soft DCA маркет-триггер -1.2% (paper-only). Возврат True если сработал."""
        if "paper" not in os.path.basename(__file__):
            return False
        if c.get("so_seq", 0) != 0:
            return False
        # Дополнительная защита по docups (факт исполнения)
        if c.get("docups", 0) != 0:
            return False
        if c.get("closed") or c.get("qty", 0) <= 0:
            return False
        avg = _f(c.get("avg_entry"))
        if avg <= 0:
            return False
        triggered = False
        # Порог 0.6%: Buy mark <= avg*0.994, Sell mark >= avg*1.006
        _thr = SOFT_DCA_TRIGGER_PCT / 100.0
        if c["side"] == "Buy" and mark <= avg * (1 - _thr):
            triggered = True
        elif c["side"] == "Sell" and mark >= avg * (1 + _thr):
            triggered = True
        if not triggered:
            return False
        # Расчётqty SO_1 = 10 USDT / mark
        qty = quantize_qty(PAPER_SO1_USDT / mark, c["qty_step"])
        if qty < _f(c.get("min_qty", 0)):
            qty = quantize_qty(_f(c.get("min_qty")), c["qty_step"])
        if qty <= 0:
            logger.warning("Soft DCA %s: qty=0, пропуск", c["symbol"])
            return False
        link = _link(c["signal_id"], "_so1")
        # Защита от дубля link
        if link in c.get("links", {}):
            return False
        logger.info("Soft DCA триггер %s %s mark=%.6g avg=%.6g -> SO_1 маркет %.6g",
                     c["symbol"], c["side"], mark, avg, qty)
        try:
            self._place(c, side=c["side"], order_type="Market", qty=qty,
                        price=None, reduce_only=False, tif="IOC",
                        role="dca", link=link)
            order = self._wait_fill(c, link, self.bp.fill_timeout_ms)
            cum = _f(order.get("cumExecQty", 0)) if order else 0.0
            if order is None or cum <= 0 or order.get("orderStatus") == "Rejected":
                raise BybitOrderError(f"SO_1 не исполнился: {order}")
            price = _f(order.get("avgPrice") or order.get("price") or mark)
            fee = _f(order.get("cumExecFee", 0))
            # Взвешенная средняя
            old_qty = _f(c["qty"])
            old_avg = _f(c["avg_entry"])
            new_qty = old_qty + cum
            c["avg_entry"] = (old_avg * old_qty + price * cum) / new_qty if new_qty > 0 else old_avg
            c["qty"] = new_qty
            c["fee"] = _f(c.get("fee", 0)) + fee
            c["last_fill_price"] = price
            # Фиксация одноразового срабатывания + сброс трейлинга от новой Avg
            c["so_seq"] = 1
            c["docups"] = 1
            c["trail_active"] = False
            c["trail_peak"] = None
            c["fills"][link] = cum
            # Журнал
            _thr_exp = SOFT_DCA_TRIGGER_PCT / 100.0
            self.journal.write("order_filled", signal_id=c["signal_id"],
                               cycle_id=c["cycle_id"], symbol=c["symbol"],
                               role="dca", mode="soft_dca",
                               expected_price=round(avg * (1 - _thr_exp), 8) if c["side"] == "Buy" else round(avg * (1 + _thr_exp), 8),
                               avg_fill_price=round(price, 8),
                               price_venue="bybit_testnet" if self.mode == "live" else "bybit_mainnet",
                               ts_signal=c["signal_ts"], ts_confirmed=now,
                               soft_dca=True, trigger_pct=SOFT_DCA_TRIGGER_PCT)
            logger.info("Soft DCA исполнен %s SO_1 @%.6g qty=%.6g new_avg=%.6g", c["symbol"], price, cum, c["avg_entry"])
            # Пересчёт TP и stop от новой средней, лимитка SO не ставится (paper)
            self._after_position_change(c, now)
            self._save_state()
            return True
        except Exception as e:
            logger.error("Soft DCA %s SO_1 ошибка: %s", c["symbol"], e)
            self.journal.write("api_error", operation="soft_dca", symbol=c["symbol"], error=str(e))
            # Блокируем повтор даже при ошибке чтобы не спамить, но so_seq не ставим — пусть ретрай на следующем такте
            # c["links"].pop(link, None) уже внутри _place, оставляем so_seq=0 для ретрая
            try:
                c["links"].pop(link, None)
                c["fills"].pop(link, None)
            except Exception:
                pass
            return False

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
        # Soft DCA — проверяем ДО стопа/трейлинга, маркет-добор на -1.2%
        if self._try_soft_dca(c, mark, now):
            # После доливки TP уже пересчитан от новой Avg, трейлинг сброшен
            # Повторно проверяем выходы на этом же такте (стоп/трейлинг от новой Avg)
            # но стоп уже от новой Avg через _after_position_change, так что продолжим
            # к проверке стопа ниже — mark может сразу быть в профите
            pass
            # не return — даём шанс сразу уйти в трейлинг если отскок уже есть
        sl = c.get("stop_level")
        if sl:
            hit = (mark <= sl) if c["side"] == "Buy" else (mark >= sl)
            if hit:
                self._market_close(c, "hard_sl")
                return
        # Paper: жёсткий лимит 6.5 USDT, Live: из конфига 5.0
        _max_loss = PAPER_MAX_CYCLE_LOSS_USDT if "paper" in os.path.basename(__file__) else p.max_cycle_loss_usdt
        if _max_loss and c["qty"] > 0:
            loss = ((c["avg_entry"] - mark) * c["qty"] if c["side"] == "Buy"
                    else (mark - c["avg_entry"]) * c["qty"])
            if loss >= _max_loss:
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
    # Изоляция BO/риск: paper/achop/bot4 используют свои константы
    _bn = os.path.basename(__file__)
    _is_achop_solo = "achop_solo" in _bn or "bot4" in _bn
    _is_achop = "achop" in _bn and not _is_achop_solo
    _is_paper = "paper" in _bn
    if _is_paper or _is_achop or _is_achop_solo:
        p.entry_usdt = PAPER_BO_USDT
        p.max_cycle_loss_usdt = PAPER_MAX_CYCLE_LOSS_USDT
        tag = "achop_solo" if _is_achop_solo else ("achop" if _is_achop else "paper")
        mode_str = "Solo" if _is_achop_solo else "Soft DCA"
        logger.info("%s %s режим: BO=%.1f SO1=%.1f MAX_LOSS=%.1f",
                     tag, mode_str,
                     PAPER_BO_USDT, PAPER_SO1_USDT, PAPER_MAX_CYCLE_LOSS_USDT)
    bot_section = bot_config.read_section(args.config, "bot")
    bp = bot_config.bot_params_from_config(bot_section)
    bybit_cfg = BybitConfig.from_dict(
        bot_config.read_section(args.config, "bybit"), env=os.environ)
    sc_raw = bot_config.read_section(args.config, "screener")
    screener_path = str(sc_raw.get("journal_path") or "logs/screener-events.jsonl")

    mode = "live" if args.live else "paper"
    if mode == "live" and (not bybit_cfg.api_key or not bybit_cfg.api_secret):
        logger.error("--live требует API-ключи Testnet: bybit.api_key/api_secret "
                     "в config.yml или BYBIT_API_KEY/BYBIT_API_SECRET")
        return 1

    # Лента публичных котировок Mainnet для paper-симуляции: PnL считается
    # по реальному движению рынка, а не по замороженным стаканам Testnet.
    feed = None
    if mode == "paper":
        feed = MainnetQuoteFeed(
            ttl_ms=int(_f(dca_raw.get("quote_feed_ttl_ms") or 3000)),
            rest_fallback_after_sec=_f(dca_raw.get("quote_rest_fallback_sec") or 30),
        )
        feed.start()

    client = BybitClient(bybit_cfg) if mode == "live" else PaperClient(
        bybit_cfg, fee_rate=p.fee_rate, feed=feed)

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

    # Изоляция файлов: achop_solo -> bot4, achop -> bot3, paper -> paper-dca
    if _is_achop_solo:
        _journal_path = "logs/bot4-achop-solo-events.jsonl"
        _state_path = os.path.join("logs", "bot4-state.json")
    elif _is_achop:
        _journal_path = "logs/bot3-achop-events.jsonl"
        _state_path = os.path.join("logs", "bot3-state.json")
    elif _is_paper:
        _journal_path = "logs/paper-dca-events.jsonl"
        _state_path = os.path.join("logs", "paper-bot-state.json")
    else:
        _journal_path = bp.journal_path
        _state_path = os.path.join("logs", "bot-state.json")
    if _is_paper or _is_achop or _is_achop_solo:
        tag2 = "achop_solo" if _is_achop_solo else ("achop" if _is_achop else "paper")
        logger.info("%s изоляция: journal=%s state=%s screener=%s", tag2, _journal_path, _state_path, screener_path)
    bot = DcaBot(
        p=p, bp=bp, client=client, mode=mode,
        journal_path=_journal_path,
        state_path=_state_path,
        screener_path=screener_path,
        trail_trigger_pct=_f(dca_raw.get("trail_trigger_pct") or 0),
        trail_step_pct=_f(dca_raw.get("trail_step_pct") or 0),
        max_signal_age=args.max_signal_age,
        exit_maker_enabled=bool(dca_raw.get("exit_maker_enabled", True)),
        exit_maker_timeout_sec=_f(dca_raw.get("exit_maker_timeout_sec") or 15),
        max_exit_slippage_pct=_f(dca_raw.get("max_exit_slippage_pct") or 0.5),
        exit_force_after_sec=_f(dca_raw.get("exit_force_after_sec") or 180),
        signal_poll_sec=_f(bot_section.get("signal_poll_sec") or 0.5),
    )
    try:
        if args.once:
            bot.once()
        else:
            bot.run()
    finally:
        if feed is not None:
            feed.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
