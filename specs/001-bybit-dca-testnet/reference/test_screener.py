"""
Автономные проверки скринера: без сети, без pytest, без установленных
requests/websockets — они подменяются заглушками до импорта модуля.

Запуск (в том числе в Termux):
    python3 specs/001-bybit-dca-testnet/reference/test_screener.py

Заглушка requests позволяет проверить главное в поведении отправки: что
состояние цвета НЕ продвигается при неудачном POST и что монета, побывавшая
в состоянии none, снова способна выдать сигнал. Обе регрессии — на реальные
дефекты предыдущей версии скринера.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types

# ── заглушки внешних зависимостей ────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _RequestException(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class _RequestException(Exception):
    pass


class _FakeRequests(types.ModuleType):
    """Заглушка requests. post_result задаётся тестом."""

    RequestException = _RequestException

    def __init__(self):
        super().__init__("requests")
        self.posted: list[dict] = []
        self.post_status = 200
        self.post_raises = False

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.posted.append({"url": url, "payload": json})
        if self.post_raises:
            raise _RequestException("connection refused")
        return _FakeResponse(self.post_status)

    def get(self, url, params=None, timeout=None):
        raise AssertionError("тесты не должны обращаться к сети через GET")


fake_requests = _FakeRequests()
sys.modules["requests"] = fake_requests

ws_client = types.ModuleType("websockets.asyncio.client")
ws_client.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("сеть в тестах не нужна"))
ws_asyncio = types.ModuleType("websockets.asyncio")
ws_asyncio.client = ws_client
ws_root = types.ModuleType("websockets")
ws_root.asyncio = ws_asyncio
sys.modules["websockets"] = ws_root
sys.modules["websockets.asyncio"] = ws_asyncio
sys.modules["websockets.asyncio.client"] = ws_client

# Модуль при импорте создаёт logs/ — уводим в временный каталог.
_tmp = tempfile.mkdtemp(prefix="screener-test-")
os.chdir(_tmp)

_spec = importlib.util.spec_from_file_location(
    "screener_under_test",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener.py"),
)
assert _spec and _spec.loader
sc = importlib.util.module_from_spec(_spec)
# Регистрация до exec_module обязательна: @dataclass ищет свой модуль в
# sys.modules, и без этого падает на разборе аннотаций.
sys.modules["screener_under_test"] = sc
_spec.loader.exec_module(sc)

# ── инфраструктура проверок ──────────────────────────────────────────────────

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def bars(n: int, start: float = 100.0, drift: float = 0.0, rng: float = 1.0,
         step_ms: int = 60_000) -> list[list]:
    """Свечи [start, open, high, low, close]; drift — % на бар, rng — размах в %."""
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100)
        out.append([i * step_ms, base, base * (1 + rng / 100), base, base * (1 + rng / 200)])
    return out


def cfg(**over) -> "sc.Config":
    c = sc.Config(**over)
    return c


# ── 1. Индикаторы и границы диапазона ────────────────────────────────────────

print("evaluate: границы NATR")
C = cfg()
up_fast = bars(60, drift=0.15, rng=1.2)
up_slow = bars(60, drift=0.15, rng=1.2, step_ms=15 * 60_000)
natr_up = sc.compute_natr(up_fast, C.natr_period)
ok(f"фикстура роста даёт NATR={natr_up:.3f} внутри {C.natr_min}..{C.natr_max}",
   natr_up is not None and C.natr_min <= natr_up <= C.natr_max, natr_up)

d = sc.evaluate(up_fast, up_slow, C)
ok("рост на 1м и 15м → сигнал green", d.passed and d.color == "green", d)
ok("green → сторона Buy", sc.color_to_side("green") == "Buy")
ok("red → сторона Sell", sc.color_to_side("red") == "Sell")

# подбираем размах так, чтобы NATR попал ровно на границы
def natr_of(rng: float) -> float:
    return sc.compute_natr(bars(60, drift=0.15, rng=rng), C.natr_period)


lo_rng = 1.2 * C.natr_min / natr_up
hi_rng = 1.2 * C.natr_max / natr_up
ok(f"NATR ровно на нижней границе ({natr_of(lo_rng):.6f}) проходит",
   sc.evaluate(bars(60, drift=0.15, rng=lo_rng), up_slow, C).reason != "natr_below_min",
   sc.evaluate(bars(60, drift=0.15, rng=lo_rng), up_slow, C))
ok(f"NATR ровно на верхней границе ({natr_of(hi_rng):.6f}) проходит",
   sc.evaluate(bars(60, drift=0.15, rng=hi_rng), up_slow, C).reason != "natr_above_max")
ok("NATR выше максимума → natr_above_max (правило «слишком рискованно»)",
   sc.evaluate(bars(60, drift=0.15, rng=hi_rng * 1.5), up_slow, C).reason == "natr_above_max")
ok("NATR ниже минимума → natr_below_min",
   sc.evaluate(bars(60, drift=0.15, rng=lo_rng * 0.5), up_slow, C).reason == "natr_below_min")
ok("нет истории → insufficient_history",
   sc.evaluate([], [], C).reason == "insufficient_history")
ok("1м вверх, 15м вниз → uhlo_no_color",
   sc.evaluate(up_fast, bars(60, drift=-0.15, rng=1.2, step_ms=15 * 60_000), C).reason == "uhlo_no_color")

print("\ncompute_uhlo: скользящее окно эквивалентно полной истории")
long_hist = bars(500, drift=0.1, rng=1.0)
ok("UHLO(20) по 500 барам == по последним 60",
   sc.compute_uhlo(long_hist, 20) == sc.compute_uhlo(long_hist[-60:], 20),
   (sc.compute_uhlo(long_hist, 20), sc.compute_uhlo(long_hist[-60:], 20)))

print("\nSymbolState: дедупликация свечей")
st = sc.SymbolState(60, 60)
r1 = [1000, 1.0, 1.1, 0.9, 1.05]
ok("новая свеча принимается", st.push("fast", r1) is True)
ok("та же свеча повторно → False", st.push("fast", list(r1)) is False)
ok("повтор уточняет последнюю свечу, длина не растёт", len(st.fast) == 1)
ok("свеча из прошлого отбрасывается", st.push("fast", [500, 1.0, 1.1, 0.9, 1.05]) is False)
ok("следующая свеча принимается", st.push("fast", [2000, 1.0, 1.1, 0.9, 1.05]) is True and len(st.fast) == 2)

# ── 2. Конфигурация ──────────────────────────────────────────────────────────

print("\nconfig")
def raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


ok("natr_max <= natr_min отвергается", raises(lambda: sc.validate_config(cfg(natr_max=0.5))))
ok("ws_ping_sec >= 20 отвергается", raises(lambda: sc.validate_config(cfg(ws_ping_sec=20))))
ok("ws_subscribe_batch > 10 отвергается", raises(lambda: sc.validate_config(cfg(ws_subscribe_batch=11))))
ok("reject_log с опечаткой отвергается", raises(lambda: sc.validate_config(cfg(reject_log="candidate"))))
ok("значения по умолчанию валидны", not raises(lambda: sc.validate_config(cfg())))
ok("отсутствие config/config.yml не валит процесс",
   isinstance(sc.load_config("config/нет-такого-файла.yml"), sc.Config))

# ── 3. Поведение при отправке сигнала ────────────────────────────────────────

print("\nотправка сигнала и состояние цвета")


def fresh_screener(**over):
    notifier = over.pop("notifier", None)
    c = cfg(journal_path=os.path.join(_tmp, "events.jsonl"), **over)
    s = sc.Screener(c, notifier=notifier)
    st = sc.SymbolState(s._fast_cap, s._slow_cap)
    for row in up_fast:
        st.push("fast", row)
    for row in up_slow:
        st.push("slow", row)
    s.states["TESTUSDT"] = st
    return s, st


def fire(s, st, color_bars=None):
    """Один прогон решения на закрытии последней свечи."""
    fast = color_bars if color_bars is not None else list(st.fast)
    trigger = fast[-1]
    return asyncio.run(s._on_fast_close("TESTUSDT", st, trigger))


fake_requests.posted.clear()
fake_requests.post_status = 200
fake_requests.post_raises = False
s, st = fresh_screener()
fire(s, st)
ok("сигнал отправлен", len(fake_requests.posted) == 1, fake_requests.posted)

payload = fake_requests.posted[0]["payload"]
ok("в конверте есть цена", isinstance(payload.get("price"), float))
ok("в конверте есть метка времени", isinstance(payload.get("ts"), int))
ok("в конверте есть signal_id", payload.get("signal_id", "").startswith("TESTUSDT:1:"))
ok("указан контур цены", payload.get("price_venue") == "bybit_mainnet")
ok("параметров DCA в конверте нет (владелец — бот)", "params" not in payload)
ok("есть диагностика для калибровки",
   {"natr", "uhlo_1m", "uhlo_15m", "detection_lag_ms"} <= set(payload.get("diagnostics", {})))
ok("цвет зафиксирован после успешной отправки", st.last_color == "green")

fake_requests.posted.clear()
fire(s, st)
ok("повтор того же цвета не отправляется", len(fake_requests.posted) == 0)

# регрессия: green → none → green
print("\nрегрессия: возврат цвета после none")
none_fast = bars(60, drift=0.0, rng=1.2)      # флэт → uhlo_no_color
st.fast.clear()
for row in none_fast:
    st.push("fast", row)
fake_requests.posted.clear()
fire(s, st)
ok("флэт → сигнала нет", len(fake_requests.posted) == 0)
ok("цвет сброшен в none", st.last_color == "none")

st.fast.clear()
for row in up_fast:
    st.push("fast", row)
st.last_signal_ms = 0                          # пауза уже прошла
fake_requests.posted.clear()
fire(s, st)
ok("green → none → green: сигнал ЕСТЬ (был баг: монета стреляла один раз)",
   len(fake_requests.posted) == 1, fake_requests.posted)

# регрессия: неудачная доставка не должна продвигать состояние
print("\nрегрессия: неудачная доставка")
s2, st2 = fresh_screener()
fake_requests.posted.clear()
fake_requests.post_raises = True
fire(s2, st2)
ok("при отказе доставки попытки повторяются", len(fake_requests.posted) == s2.cfg.post_retries + 1,
   len(fake_requests.posted))
ok("цвет НЕ зафиксирован при неудаче", st2.last_color == "none")
ok("метка последнего сигнала не сдвинута", st2.last_signal_ms == 0)

fake_requests.post_raises = False
fake_requests.posted.clear()
fire(s2, st2)
ok("на следующей свече сигнал уходит повторно", len(fake_requests.posted) == 1)
ok("после успеха цвет зафиксирован", st2.last_color == "green")

fake_requests.posted.clear()
fake_requests.post_status = 422
s3, st3 = fresh_screener()
fire(s3, st3)
ok("не-2xx считается неудачей", st3.last_color == "none")
fake_requests.post_status = 200

# пауза между сигналами
print("\nпауза между сигналами")
s4, st4 = fresh_screener(cooldown_sec=3600)
fake_requests.posted.clear()
fire(s4, st4)
ok("первый сигнал проходит", len(fake_requests.posted) == 1)
st4.last_color = "red"                          # цвет сменился, но пауза не истекла
fake_requests.posted.clear()
fire(s4, st4)
ok("внутри паузы сигнал отклонён", len(fake_requests.posted) == 0)
ok("цвет внутри паузы не переписан (сигнал состоится после)", st4.last_color == "red")

# ── 4. Журнал ────────────────────────────────────────────────────────────────

print("\nжурнал событий")
s5, st5 = fresh_screener(reject_log="candidates")
s5._log_reject("XUSDT", "natr_above_max", 1, {"natr": 9.9})
s5._log_reject("XUSDT", "insufficient_history", 1, {})
s5._log_reject("XUSDT", "repeat_color", 1, {})
s5._log_reject("XUSDT", "uhlo_no_color", 1, {})
s5.journal.close()
lines = [json.loads(x) for x in open(os.path.join(_tmp, "events.jsonl"), encoding="utf-8")]
reasons = [x["reason"] for x in lines if x["kind"] == "reject"]
ok("natr_above_max журналируется (нужен для доказательства отсечения)", "natr_above_max" in reasons)
ok("uhlo_no_color журналируется", "uhlo_no_color" in reasons)
ok("шум insufficient_history не журналируется", "insufficient_history" not in reasons)
ok("шум repeat_color не журналируется", "repeat_color" not in reasons)
ok("каждая строка — валидный JSON с kind и ts",
   all("kind" in x and "ts" in x for x in lines))

# ── 5. Paper-режим (dry-run) ─────────────────────────────────────────────────

print("\ndry-run: сигналы без POST и ордеров")
fake_requests.posted.clear()
sd, std = fresh_screener(dry_run=True)
fire(sd, std)
ok("dry-run: POST на бота НЕ идёт", len(fake_requests.posted) == 0,
   fake_requests.posted)
ok("dry-run: цвет зафиксирован (как после успешной доставки)", std.last_color == "green")
ok("dry-run: метка сигнала сдвинута", std.last_signal_ms != 0, std.last_signal_ms)
sd.journal.close()
dry_lines = [json.loads(x) for x in
             open(os.path.join(_tmp, "events.jsonl"), encoding="utf-8")]
dry_ev = [x for x in dry_lines if x["kind"] == "signal_dry_run"]
ok("dry-run: событие signal_dry_run в журнале", len(dry_ev) == 1, dry_ev)
ok("dry-run: в событии статус dry_run и конверт",
   dry_ev and dry_ev[0].get("status") == "dry_run"
   and dry_ev[0].get("signal", {}).get("symbol") == "TESTUSDT", dry_ev)

class _FakeNotifier:
    def __init__(self):
        self.sent = []
    def notify(self, text):
        self.sent.append(text)

fake_nt = _FakeNotifier()
sn, stn = fresh_screener(dry_run=True, notifier=fake_nt)
sn.journal.close()
fire(sn, stn)
ok("dry-run: уведомление ушло в notifier", len(fake_nt.sent) == 1, fake_nt.sent)
ok("dry-run: в уведомлении символ и сторона",
   fake_nt.sent and "TESTUSDT" in fake_nt.sent[0] and "Buy" in fake_nt.sent[0],
   fake_nt.sent)

ok("dry_run по умолчанию False", cfg().dry_run is False)
ok("не-булевый dry_run отвергается",
   raises(lambda: sc.validate_config(cfg(dry_run="yes"))))
ok("bool dry_run валиден", not raises(lambda: sc.validate_config(cfg(dry_run=True))))

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
