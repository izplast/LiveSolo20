"""
Автономные проверки устойчивости (reference/resilience.py).

Моки сетевых сбоев: fn() бросает ConnectionError / 5xx / 429 или отвечает
успехом после N попыток; sleep — заглушка без реальных пауз; часы
предохранителя — управляемые. Без сети, без зависимостей.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_resilience.py
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


rl = _load("resilience")

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def noop_sleep(_sec: float) -> None:
    pass


class Clock:
    """Управляемые часы для предохранителя."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ── ExponentialBackoff ──────────────────────────────────────────────────────

print("ExponentialBackoff")
b = rl.ExponentialBackoff(base=0.5, factor=2.0, max_delay=8.0)
ok("попытка 1 = base", b.delay(1) == 0.5, b.delay(1))
ok("попытка 2 = base*factor", b.delay(2) == 1.0, b.delay(2))
ok("попытка 3 = base*factor^2", b.delay(3) == 2.0, b.delay(3))
ok("кап на max_delay", b.delay(10) == 8.0 and b.delay(20) == 8.0,
   (b.delay(10), b.delay(20)))
ok("attempt < 1 → 1", b.delay(0) == b.delay(1))
try:
    rl.ExponentialBackoff(base=0, factor=2)
    ok("base=0 → ValueError", False)
except ValueError:
    ok("base=0 → ValueError", True)
try:
    rl.ExponentialBackoff(base=1, factor=1)
    ok("factor=1 → ValueError", False)
except ValueError:
    ok("factor=1 → ValueError", True)

bj = rl.ExponentialBackoff(base=1.0, factor=2.0, max_delay=8.0,
                           jitter=0.5, random_fn=lambda: 1.0)
ok("jitter с u=1 → задержка * (1 - 0.5)", bj.delay(1) == 0.5, bj.delay(1))
bj0 = rl.ExponentialBackoff(base=1.0, factor=2.0, max_delay=8.0,
                            jitter=0.5, random_fn=lambda: 0.0)
ok("jitter с u=0 → без изменений", bj0.delay(1) == 1.0, bj0.delay(1))

# ── CircuitBreaker ──────────────────────────────────────────────────────────

print("\nCircuitBreaker")
c = rl.CircuitBreaker(failure_threshold=3, cooldown=10.0, clock=Clock(0))
ok("изначально closed и пропускает", c.state == "closed" and c.allow_request(),
   (c.state, c.allow_request()))
c.record_failure()
c.record_failure()
ok("2 сбоя < 3 → всё ещё closed", c.state == "closed", c.state)
c.record_failure()
ok("3 сбоя → open", c.state == "open", c.state)
ok("open не пропускает", c.allow_request() is False, c.allow_request())

clock = Clock(0.0)
c = rl.CircuitBreaker(failure_threshold=2, cooldown=10.0, clock=clock)
for _ in range(2):
    c.record_failure()
ok("2 сбоя → open", c.state == "open", c.state)
clock.now = 5.0
ok("до cooldown (5 < 10) open", c.allow_request() is False and c.state == "open",
   (c.state, c.allow_request()))
clock.now = 10.0
ok("после cooldown → half_open, пробный запрос пропускается",
   c.state == "half_open" and c.allow_request() is True, (c.state, c.allow_request()))
ok("half_open_limit=1 → второй пробный отклоняется", c.allow_request() is False,
   c.allow_request())
c.record_success()
ok("успех в half_open → closed", c.state == "closed", c.state)
ok("closed снова пропускает", c.allow_request() is True, c.allow_request())

clock2 = Clock(0.0)
c = rl.CircuitBreaker(failure_threshold=2, cooldown=5.0, clock=clock2)
for _ in range(2):
    c.record_failure()
clock2.now = 5.0
c.allow_request()  # пробный запрос в half_open
c.record_failure()
ok("сбой в half_open → снова open", c.state == "open", c.state)
try:
    rl.CircuitBreaker(failure_threshold=0)
    ok("failure_threshold=0 → ValueError", False)
except ValueError:
    ok("failure_threshold=0 → ValueError", True)

# ── ResilientCaller: ретраи ─────────────────────────────────────────────────

print("\nResilientCaller: ретраи")
calls = []


def flaky_conn():
    calls.append(1)
    if len(calls) < 3:
        raise ConnectionError("network down")
    return "ok"


caller = rl.ResilientCaller(backoff=rl.ExponentialBackoff(base=0.1, max_delay=1.0),
                            breaker=rl.CircuitBreaker(failure_threshold=100),
                            max_attempts=5, sleep=noop_sleep)
res = caller.call(flaky_conn)
ok("3 попытки (2 сбоя + успех) → результат",
   res == "ok" and len(calls) == 3, (res, len(calls)))

calls2 = []


def flaky_429():
    calls2.append(1)
    if len(calls2) < 3:
        raise rl.ResponseError(429, "rate limit")
    return "ok"


res = caller.call(flaky_429)
ok("429 ретраится до успеха", res == "ok" and len(calls2) == 3, (res, len(calls2)))

calls3 = []


def flaky_500():
    calls3.append(1)
    if len(calls3) < 2:
        raise rl.ResponseError(500, "server error")
    return "ok"


res = caller.call(flaky_500)
ok("5xx ретраится до успеха", res == "ok" and len(calls3) == 2, (res, len(calls3)))

# не-retryable ошибка не ретраится
calls4 = []


def bad_req():
    calls4.append(1)
    raise rl.ResponseError(400, "bad request")


try:
    caller.call(bad_req)
    ok("400 (не retryable) → всплывает, без ретраев", False)
except rl.ResponseError as e:
    ok("400 (не retryable) → всплывает, без ретраев",
       e.status == 400 and len(calls4) == 1, (e.status, len(calls4)))

try:
    caller.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
    ok("ValueError (не сеть) → всплывает без ретраев", False)
except ValueError:
    ok("ValueError (не сеть) → всплывает без ретраев", True)

# исчерпание попыток
calls5 = []


def always_fail():
    calls5.append(1)
    raise ConnectionError("still down")


try:
    caller.call(always_fail, max_attempts=3)
    ok("исчерпание попыток → MaxRetriesError", False)
except rl.MaxRetriesError as e:
    ok("исчерпание попыток → MaxRetriesError",
       len(calls5) == 3 and isinstance(e.__cause__, ConnectionError),
       (len(calls5), type(e.__cause__).__name__))

# ── ResilientCaller: предохранитель ─────────────────────────────────────────

print("\nResilientCaller: предохранитель")
clock = Clock(0.0)
breaker = rl.CircuitBreaker(failure_threshold=3, cooldown=10.0, clock=clock)
caller = rl.ResilientCaller(
    backoff=rl.ExponentialBackoff(base=0.01, max_delay=0.05),
    breaker=breaker, max_attempts=2, sleep=noop_sleep)

attempts = []


def fails_always():
    attempts.append(1)
    raise rl.ResponseError(503, "unavailable")


for _ in range(4):
    try:
        caller.call(fails_always)
    except (rl.CircuitOpenError, rl.MaxRetriesError):
        pass
ok("серия сбоев → предохранитель открылся",
   breaker.state == "open", breaker.state)
try:
    caller.call(fails_always)
    ok("при open вызов отклоняется мгновенно", False)
except rl.CircuitOpenError:
    ok("при open вызов отклоняется мгновенно", True)

n_before = len(attempts)
for _ in range(5):
    try:
        caller.call(fails_always)
    except rl.CircuitOpenError:
        pass
ok("пока open, fn() не вызывается (нет ретраев и попыток)",
   len(attempts) == n_before, (len(attempts), n_before))

clock.now = 10.0
ok("после cooldown → half_open", breaker.state == "half_open", breaker.state)

# recovery: успех (первая же проба) закрывает предохранитель
ok_calls = []


def succeeds_once():
    ok_calls.append(1)
    return "recovered"


caller = rl.ResilientCaller(backoff=rl.ExponentialBackoff(base=0.01, max_delay=0.05),
                            breaker=breaker, max_attempts=2, sleep=noop_sleep)
res = caller.call(succeeds_once)
ok("успех в half_open → закрывает предохранитель",
   res == "recovered" and breaker.state == "closed", (res, breaker.state))

# кастомный предикат retryable
caller = rl.ResilientCaller(max_attempts=3, sleep=noop_sleep)
seen = []


def special():
    seen.append(1)
    if len(seen) < 2:
        raise KeyError("transient-ish")
    return "done"


res = caller.call(special, retryable=lambda e: isinstance(e, KeyError))
ok("кастомный retryable ретраит KeyError",
   res == "done" and len(seen) == 2, (res, len(seen)))

print(f"\nитог: {PASS} ok, {FAIL} fail")
raise SystemExit(1 if FAIL else 0)