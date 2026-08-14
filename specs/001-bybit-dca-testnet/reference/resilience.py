"""
reference/resilience.py — экспоненциальная задержка и предохранитель.

Автономный слой устойчивости к сетевым сбоям, без внешних зависимостей:

  * ExponentialBackoff — задержка между повторными попытками растёт
    экспоненциально и ограничена сверху (max_delay); опциональный jitter
    рассинхронизирует параллельные клиенты, чтобы не было «волны» повторных
    запросов после общего сбоя.
  * CircuitBreaker — предохранитель в трёх состояниях: closed (норма),
    open (разомкнут после серии сбоев — запросы мгновенно отклоняются, API
    отдыхает и не уходит в бан по rate-limit/IP), half_open (после cooldown
    пропускается пробный запрос; успех закрывает предохранитель, сбой снова
    размыкает).
  * ResilientCaller — обёртка вызова: повторяет fn() на сетевых ошибках и
    5xx/429 (exponential backoff), пропускает через предохранитель; бросает
    CircuitOpenError (предохранитель разомкнут) или MaxRetriesError (попытки
    исчерпаны). Сетевые вызовы и паузы — инъекция (fn, sleep), поэтому
    проверяется без сети и без реальных ожиданий.

HTTP-слой бросает ResponseError(status, ...) для 5xx/429; default_retryable
распознаёт их наравне с ConnectionError/TimeoutError/OSError. Любое другое
исключение не ретраится и всплывает как есть.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_resilience.py
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Sequence


class CircuitOpenError(Exception):
    """Предохранитель разомкнут: запрос не выполнялся."""


class MaxRetriesError(Exception):
    """Повторные попытки исчерпаны; последняя ошибка сохранена в cause."""


class ResponseError(Exception):
    """HTTP-ответ с ненулевым кодом (5xx/429 помечаются как retryable)."""

    def __init__(self, status: int, message: str = "",
                 body: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}".strip())
        self.status = int(status)
        self.body = body

    @property
    def retryable(self) -> bool:
        return is_retryable_status(self.status)


def is_retryable_status(status: int) -> bool:
    """429 (rate-limit) и 5xx (сервер временно недоступен) — ретраятся."""
    return status == 429 or status >= 500


def default_retryable(exc: BaseException) -> bool:
    """Что ретраить по умолчанию: сетевые ошибки и 5xx/429."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, ResponseError):
        return exc.retryable
    return False


# ---------------------------------------------------------------------------
# Экспоненциальная задержка
# ---------------------------------------------------------------------------

class ExponentialBackoff:
    """Задержка между попытками: base * factor^(attempt-1), ≤ max_delay.

    jitter в долях от расчётной задержки умножает её на (1 - jitter * u),
    u ∈ [0, 1) — дерандомизация параллельных клиентов. Для детерминизма
    можно передать random=None (jitter=0) или фиксированный генератор.
    """

    def __init__(self, base: float = 0.5, factor: float = 2.0,
                 max_delay: float = 30.0, jitter: float = 0.0,
                 random_fn: Callable[[], float] | None = None) -> None:
        if base <= 0:
            raise ValueError("base должно быть > 0")
        if factor <= 1:
            raise ValueError("factor должно быть > 1")
        if max_delay < base:
            raise ValueError("max_delay должно быть >= base")
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self._random = random_fn if random_fn is not None else random.random

    def delay(self, attempt: int) -> float:
        """Задержка перед попыткой номер `attempt` (отсчёт с 1)."""
        if attempt < 1:
            attempt = 1
        d = min(self.max_delay, self.base * self.factor ** (attempt - 1))
        if self.jitter:
            d = d * (1.0 - self.jitter * self._random())
        return round(d, 6)


# ---------------------------------------------------------------------------
# Предохранитель (circuit breaker)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Три состояния: closed / open / half_open.

    * closed: запросы идут; сбой увеличивает счётчик, при достижении
      failure_threshold предохранитель размыкается (open);
    * open: allow_request() возвращает False — вызовы мгновенно отклоняются
      (CircuitOpenError), API отдыхает cooldown секунд;
    * half_open: после cooldown пропускается до half_open_limit пробных
      запросов; record_success() возвращает состояние в closed, сбой снова
      размыкает.
    """

    def __init__(self, failure_threshold: int = 5, cooldown: float = 10.0,
                 half_open_limit: int = 1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold должно быть >= 1")
        if cooldown <= 0:
            raise ValueError("cooldown должно быть > 0")
        if half_open_limit < 1:
            raise ValueError("half_open_limit должно быть >= 1")
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.half_open_limit = half_open_limit
        self._clock = clock
        self._state = "closed"
        self._failures = 0
        self._open_until = 0.0
        self._probes = 0

    @property
    def state(self) -> str:
        self._refresh()
        return self._state

    def _refresh(self) -> None:
        if self._state == "open" and self._clock() >= self._open_until:
            self._state = "half_open"
            self._probes = 0

    def allow_request(self) -> bool:
        self._refresh()
        if self._state == "open":
            return False
        if self._state == "half_open":
            if self._probes < self.half_open_limit:
                self._probes += 1
                return True
            return False
        return True

    def record_success(self) -> None:
        self._state = "closed"
        self._failures = 0
        self._probes = 0

    def record_failure(self) -> None:
        self._probes = 0
        if self._state == "half_open":
            self._open_now()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_now()

    def _open_now(self) -> None:
        self._state = "open"
        self._open_until = self._clock() + self.cooldown


# ---------------------------------------------------------------------------
# Обёртка вызова: backoff + предохранитель
# ---------------------------------------------------------------------------

class ResilientCaller:
    """Выполняет fn() с ретраями и предохранителем.

    Параметры:
      * backoff — ExponentialBackoff (задержки между попытками);
      * breaker — CircuitBreaker (размыкание после серии сбоев);
      * max_attempts — максимум попыток (включая первую), по умолчанию 5;
      * sleep — пауза между попытками (для тестов — заглушка).
    """

    def __init__(self, backoff: ExponentialBackoff | None = None,
                 breaker: CircuitBreaker | None = None,
                 max_attempts: int = 5,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts должно быть >= 1")
        self.backoff = backoff or ExponentialBackoff()
        self.breaker = breaker or CircuitBreaker()
        self.max_attempts = max_attempts
        self._sleep = sleep

    def call(self, fn: Callable[[], Any],
             retryable: Callable[[BaseException], bool] | None = None,
             max_attempts: int | None = None) -> Any:
        """Выполнить fn() с ретраями. Возвращает результат или бросает
        CircuitOpenError / MaxRetriesError / исходное исключение."""
        retryable = retryable or default_retryable
        attempts = max_attempts if max_attempts is not None else self.max_attempts
        attempt = 0
        while True:
            if not self.breaker.allow_request():
                raise CircuitOpenError(
                    f"предохранитель {self.breaker.state}: запрос отклонён")
            try:
                result = fn()
                self.breaker.record_success()
                return result
            except CircuitOpenError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.breaker.record_failure()
                if self.breaker.state == "open":
                    raise CircuitOpenError(
                        f"предохранитель открыт после ошибки: {exc}") from exc
                if not retryable(exc):
                    raise
                attempt += 1
                if attempt >= attempts:
                    raise MaxRetriesError(
                        f"исчерпаны попытки ({attempts}): {exc}") from exc
                self._sleep(self.backoff.delay(attempt))