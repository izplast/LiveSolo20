"""
Автономные проверки скринера: без сети, без pytest, без установленных
requests/websockets — они подменяются заглушками до импорта модуля.

Запуск (в том числе в Termux):
    python3 specs/001-bybit-dca-testnet/reference/test_screener.py

Заглушка requests позволяет проверить главное в поведении отправки: что
канал доставки боту — журнал (signal_sent пишется всегда, HTTP-POST — лишь
уведомление поверх журнала и не влияет на состояние) и что монета, побывавшая
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
         step_ms: int = 60_000, amp: float = 0.5, freq: float = 5.0) -> list[list]:
    """Свечи [start, open, high, low, close]; drift — % на бар, rng — размах в %.

    Волнистая база (amp % с периодом freq баров) вместо идеально прямой:
    на прямой UHLO 1м стоит в «углу» (0 и 100 одновременно) и сигнал
    режется фильтром uhlo_corner. amp=0 → идеально прямая фикстура.
    """
    import math
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100 + amp * math.sin(2 * math.pi * i / freq) / 100)
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

print("\nфильтр: 1м UHLO не должен одновременно показывать 0 и 100 (uhlo_corner)")
def straight(n: int, drift: float = 0.15, step_ms: int = 60_000):
    return bars(n, drift=drift, rng=1.2, step_ms=step_ms, amp=0.0)

ok("прямая монотонная мачта вверх → UHLO 1м ровно (highs 100, lows 0)",
   sc.compute_uhlo(straight(60), 20) == {"highs": 100.0, "lows": 0.0},
   sc.compute_uhlo(straight(60), 20))
ok("прямая мачта вниз → UHLO 1м ровно (highs 0, lows 100)",
   sc.compute_uhlo(straight(60, drift=-0.15), 20) == {"highs": 0.0, "lows": 100.0},
   sc.compute_uhlo(straight(60, drift=-0.15), 20))
ok("мачта вверх → сигнал green режется (uhlo_corner)",
   sc.evaluate(straight(60), straight(60, step_ms=15 * 60_000), C).reason == "uhlo_corner")
ok("мачта вниз → сигнал red режется (uhlo_corner)",
   sc.evaluate(straight(60, drift=-0.15), straight(60, drift=-0.15, step_ms=15 * 60_000), C).reason == "uhlo_corner")
ok("uhlo_corner несёт цвет и значения 1м",
   (lambda d: d.color == "green" and d.uhlo_fast.get("highs") == 100.0
    and d.uhlo_fast.get("lows") == 0.0)(sc.evaluate(straight(60), straight(60, step_ms=15 * 60_000), C)))
ok("с откатом каждые 10 баров угол снят → сигнал проходит",
   sc.evaluate(up_fast, up_slow, C).passed, sc.evaluate(up_fast, up_slow, C))
ok("fast_uhlo_corner без данных → False", sc.fast_uhlo_corner(None) is False)
ok("fast_uhlo_corner: не-угол (60/40) → False", sc.fast_uhlo_corner({"highs": 60.0, "lows": 40.0}) is False)
ok("fast_uhlo_corner: угол вверх (100/0) → True", sc.fast_uhlo_corner({"highs": 100.0, "lows": 0.0}) is True)
ok("fast_uhlo_corner: угол вниз (0/100) → True", sc.fast_uhlo_corner({"highs": 0.0, "lows": 100.0}) is True)

print("\nclassify_color: жёсткий триггер (1м и 15м одинаково: highs 80..100, lows 0..20)")
ok("green: оба ТФ у хаёв (highs 85+, lows 10) → green",
   sc.classify_color({"highs": 85.0, "lows": 10.0}, {"highs": 85.0, "lows": 10.0}) == "green")
ok("green: 15м ниже 80 (highs 72) → none — смягчения больше нет",
   sc.classify_color({"highs": 85.0, "lows": 10.0}, {"highs": 72.0, "lows": 28.0}) == "none")
ok("green: 15м на границе 80/20 → green",
   sc.classify_color({"highs": 85.0, "lows": 10.0}, {"highs": 80.0, "lows": 20.0}) == "green")
ok("red: оба ТФ у лоу (lows 85+, highs 10) → red",
   sc.classify_color({"lows": 85.0, "highs": 10.0}, {"lows": 85.0, "highs": 10.0}) == "red")
ok("red: 15м выше 20 (lows 28) → none — смягчения больше нет",
   sc.classify_color({"lows": 85.0, "highs": 10.0}, {"lows": 72.0, "highs": 28.0}) == "none")
ok("red: противоречие ТФ (1м у хаёв, 15м у лоу) → none",
   sc.classify_color({"highs": 85.0, "lows": 10.0}, {"lows": 85.0, "highs": 10.0}) == "none")
ok("нет данных → none", sc.classify_color(None, {"highs": 85.0, "lows": 10.0}) == "none")

# ── 1b. Фильтры сигнала: анти-памп и перепроданность (T033) ──────────────────

print("\nфильтры: анти-памп (pump_volume_mult / pump_wick_ratio)")
def raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def with_vol(rows, vol=None):
    """Свечи с объёмом в индексе 5; vol — скаляр или функция i→объём."""
    out = []
    for i, r in enumerate(rows):
        v = vol(i) if callable(vol) else (vol if vol is not None else 100.0)
        out.append(list(r) + [v])
    return out


def set_upper_wick(row, frac=0.9):
    """Длинная верхняя тень: high поднимается, close остаётся у лоу."""
    r = list(row)
    o, h, l, c = r[1], r[2], r[3], r[4]
    span = max(h - l, 1e-9)
    r[2] = c + frac * span   # high = close + frac*размах → тень (high-close)/span = frac
    return r


fast_v = with_vol(up_fast)
slow_v = with_vol(up_slow, vol=lambda i: 100.0)
ok("с объёмом сигнал green остаётся",
   sc.evaluate(fast_v, slow_v, cfg()).passed, sc.evaluate(fast_v, slow_v, cfg()))
spiked = list(fast_v)
spiked[-1] = list(spiked[-1])
spiked[-1][5] = 1_000_000.0          # объём × 10000 от среднего
# лоу уходит под окно: иначе размашистый спайк-бар даёт UHLO 1м в «углу»
# (highs 100 / lows 0) и режется uhlo_corner раньше, чем анти-памп фильтр.
# 0.99 — при uhlo_length=15: lows 1м 6.7 (не угол, но и не выше порога 20).
spiked[-1][3] = spiked[-1][4] * 0.99
spiked[-1] = set_upper_wick(spiked[-1])
d = sc.evaluate(spiked, slow_v, cfg(pump_volume_mult=3.0, pump_wick_ratio=0.5))
ok("объёмный спайк + длинная верхняя тень → pump_volume_spike",
   d.reason == "pump_volume_spike", d)
d0 = sc.evaluate(spiked, slow_v, cfg(pump_volume_mult=0.0))
ok("pump_volume_mult=0 → защита выключена", d0.passed, d0)
spiked_no_wick = list(fast_v)
spiked_no_wick[-1] = list(spiked_no_wick[-1])
spiked_no_wick[-1][5] = 1_000_000.0  # объёмный спайк БЕЗ длинной тени
spiked_no_wick[-1][3] = spiked_no_wick[-1][4] * 0.99  # и без углового UHLO (highs 100 / lows 0)
ok("спайк объёма без верхней тени → не pump",
   sc.evaluate(spiked_no_wick, slow_v, cfg(pump_volume_mult=3.0,
                                           pump_wick_ratio=0.6)).passed)

print("\nфильтры: экстремумы (short_highs_max / long_lows_min)")
ok("short_highs_max=100 → выключено",
   sc.short_blocked({"highs": 95.0}, cfg(short_highs_max=100.0)) is False)
ok("SHORT на пике (highs выше порога) → заблокирован",
   sc.short_blocked({"highs": 95.0}, cfg(short_highs_max=90.0)) is True)
ok("SHORT не на пике (highs на/ниже порога) → разрешён",
   sc.short_blocked({"highs": 85.0}, cfg(short_highs_max=90.0)) is False)
ok("short_blocked без uhlo → False", sc.short_blocked(None, cfg(short_highs_max=90.0)) is False)
ok("long_lows_min=100 → выключено",
   sc.long_blocked({"lows": 95.0}, cfg(long_lows_min=100.0)) is False)
ok("LONG у дна (lows выше порога) → заблокирован",
   sc.long_blocked({"lows": 95.0}, cfg(long_lows_min=90.0)) is True)
ok("LONG не у дна (lows на/ниже порога) → разрешён",
   sc.long_blocked({"lows": 85.0}, cfg(long_lows_min=90.0)) is False)
ok("long_blocked без uhlo → False", sc.long_blocked(None, cfg(long_lows_min=90.0)) is False)

print("\nконфиг: новые ключи фильтрации")
ok("неотрицательный pump_volume_mult валиден", not raises(lambda: sc.validate_config(cfg(pump_volume_mult=3.0))))
ok("отрицательный pump_volume_mult отвергается", raises(lambda: sc.validate_config(cfg(pump_volume_mult=-1))))
ok("pump_wick_ratio > 1 отвергается", raises(lambda: sc.validate_config(cfg(pump_wick_ratio=1.5))))
ok("short_highs_max < 80 отвергается", raises(lambda: sc.validate_config(cfg(short_highs_max=70.0))))
ok("short_highs_max=100 (выкл) валиден", not raises(lambda: sc.validate_config(cfg(short_highs_max=100.0))))
ok("long_lows_min < 80 отвергается", raises(lambda: sc.validate_config(cfg(long_lows_min=70.0))))
ok("long_lows_min=100 (выкл) валиден", not raises(lambda: sc.validate_config(cfg(long_lows_min=100.0))))
ok("отрицательный min_turnover_usdt отвергается", raises(lambda: sc.validate_config(cfg(min_turnover_usdt=-5))))
ok("отрицательный cg_max_rank отвергается", raises(lambda: sc.validate_config(cfg(cg_max_rank=-1))))

print("\ncompute_uhlo: скользящее окно эквивалентно полной истории")
long_hist = bars(500, drift=0.1, rng=1.0, amp=0.0)  # монотонно: окно UHLO самодостаточно
ok("UHLO(15) по 500 барам == по последним 60",
   sc.compute_uhlo(long_hist, 15) == sc.compute_uhlo(long_hist[-60:], 15),
   (sc.compute_uhlo(long_hist, 15), sc.compute_uhlo(long_hist[-60:], 15)))

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


def fire2(s, st):
    """Два закрытия подряд: первый бар становится в серию (no_confirm),
    второй подтверждает и отправляет сигнал."""
    fire(s, st)
    return fire(s, st)


def _journal_lines():
    """Строки журнала скринера (общий файл _tmp/events.jsonl)."""
    try:
        with open(os.path.join(_tmp, "events.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


fake_requests.posted.clear()
fake_requests.post_status = 200
fake_requests.post_raises = False
s, st = fresh_screener()
fire2(s, st)
ok("сигнал отправлен (после подтверждения 2 баров)", len(fake_requests.posted) == 1, fake_requests.posted)

payload = fake_requests.posted[0]["payload"]
ok("в конверте есть цена", isinstance(payload.get("price"), float))
ok("в конверте есть метка времени", isinstance(payload.get("ts"), int))
ok("в конверте есть signal_id", payload.get("signal_id", "").startswith("TESTUSDT:1:"))
ok("указан контур цены", payload.get("price_venue") == "bybit_mainnet")
ok("параметров DCA в конверте нет (владелец — бот)", "params" not in payload)
ok("есть диагностика для калибровки",
   {"natr", "uhlo_1m", "uhlo_15m", "detection_lag_ms"} <= set(payload.get("diagnostics", {})))
raw = payload["diagnostics"].get("uhlo_raw")
ok("в диагностике есть сырые UHLO (семантика TV LuxAlgo)",
   isinstance(raw, dict) and set(raw) == {"1m", "15m"}
   and set(raw["1m"]) == {"unreached_highs", "unreached_lows"}
   and set(raw["15m"]) == {"unreached_highs", "unreached_lows"}, raw)
ok("цвет зафиксирован после успешной отправки", st.last_color == "green")

# подтверждение сигнала: одиночный бар в состоянии не отправляет сигнал
print("\nподтверждение сигнала (2 бара подряд)")
s6, st6 = fresh_screener(reject_log="all")
fake_requests.posted.clear()
fire(s6, st6)                                   # первый бар серии
ok("первый бар серии сигнал НЕ отправляет", len(fake_requests.posted) == 0,
   fake_requests.posted)
ok("первый бар серии: reject no_confirm в журнале",
   any(x.get("reason") == "no_confirm" for x in _journal_lines()))
fire(s6, st6)                                   # второй бар подтверждает
ok("второй подряд бар с тем же цветом отправляет сигнал",
   len(fake_requests.posted) == 1, fake_requests.posted)

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
fire2(s, st)
ok("green → none → green: сигнал ЕСТЬ (был баг: монета стреляла один раз)",
   len(fake_requests.posted) == 1, fake_requests.posted)

# регрессия: журнал — канал доставки, HTTP-POST — уведомление поверх журнала
print("\nрегрессия: отказ HTTP-уведомления не теряет сигнал")
s2, st2 = fresh_screener()
fake_requests.posted.clear()
fake_requests.post_raises = True
fire2(s2, st2)
ok("при отказе POST сигнал всё равно в журнале (signal_sent)",
   any(x["kind"] == "signal_sent" for x in _journal_lines()), _journal_lines())
ok("цвет зафиксирован (журнал — доставка)", st2.last_color == "green", st2.last_color)
ok("метка последнего сигнала сдвинута", st2.last_signal_ms != 0, st2.last_signal_ms)
ok("записей signal_failed нет", not any(x["kind"] == "signal_failed" for x in _journal_lines()),
   _journal_lines())

fake_requests.post_raises = False
fake_requests.posted.clear()
fire(s2, st2)
ok("повтор того же цвета не отправляется (цвет уже зафиксирован)",
   len(fake_requests.posted) == 0, fake_requests.posted)

fake_requests.posted.clear()
fake_requests.post_status = 422
s3, st3 = fresh_screener()
fire2(s3, st3)
ok("не-2xx тоже не теряет сигнал: цвет зафиксирован", st3.last_color == "green", st3.last_color)
fake_requests.post_status = 200

# пауза между сигналами
print("\nпауза между сигналами")
s4, st4 = fresh_screener(cooldown_sec=3600)
fake_requests.posted.clear()
fire2(s4, st4)
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
s5._log_reject("XUSDT", "uhlo_no_color", 1, {"natr": 1.4, "uhlo_fast": {"highs": 5.0, "lows": 65.0}})
s5.journal.close()
lines = [json.loads(x) for x in open(os.path.join(_tmp, "events.jsonl"), encoding="utf-8")]
reasons = [x["reason"] for x in lines if x["kind"] == "reject"]
ok("natr_above_max журналируется (нужен для доказательства отсечения)", "natr_above_max" in reasons)
ok("uhlo_no_color журналируется", "uhlo_no_color" in reasons)
ok("шум insufficient_history не журналируется", "insufficient_history" not in reasons)
ok("шум repeat_color не журналируется", "repeat_color" not in reasons)
ok("каждая строка — валидный JSON с kind и ts",
   all("kind" in x and "ts" in x for x in lines))
u_rej = next(x for x in reversed(lines)
             if x.get("kind") == "reject" and x["reason"] == "uhlo_no_color")
ok("в деталях режекта есть natr и uhlo-значения",
   u_rej["details"].get("natr") == 1.4
   and u_rej["details"].get("uhlo_fast") == {"highs": 5.0, "lows": 65.0})

# ── 5. Paper-режим (dry-run) ─────────────────────────────────────────────────

print("\ndry-run: сигналы без POST и ордеров")
fake_requests.posted.clear()
sd, std = fresh_screener(dry_run=True)
fire2(sd, std)
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
fire2(sn, stn)
ok("dry-run: уведомление ушло в notifier", len(fake_nt.sent) == 1, fake_nt.sent)
ok("dry-run: в уведомлении символ и сторона",
   fake_nt.sent and "TESTUSDT" in fake_nt.sent[0] and "Buy" in fake_nt.sent[0],
   fake_nt.sent)

ok("dry_run по умолчанию False", cfg().dry_run is False)
ok("не-булевый dry_run отвергается",
   raises(lambda: sc.validate_config(cfg(dry_run="yes"))))
ok("bool dry_run валиден", not raises(lambda: sc.validate_config(cfg(dry_run=True))))

# ── 6. Вселенная: require_testnet ─────────────────────────────────────────────

print("\nвселенная: require_testnet (testnet-ограничение снимается)")

_mainnet_inst = {"BTCUSDT": {"status": "Trading", "max_leverage": 10.0},
                 "DOGEUSDT": {"status": "Trading", "max_leverage": 10.0},
                 "MEMEUSDT": {"status": "Trading", "max_leverage": 10.0}}
_testnet_inst = {"BTCUSDT": {"status": "Trading", "max_leverage": 10.0}}
_turnover = {"BTCUSDT": 5_000_000.0, "DOGEUSDT": 4_000_000.0,
             "MEMEUSDT": 3_000_000.0}

_real_fetch_inst = sc.fetch_instruments
_real_fetch_turn = sc.fetch_turnover
sc.fetch_instruments = lambda base: (_mainnet_inst if "testnet" not in base else _testnet_inst)
sc.fetch_turnover = lambda base: dict(_turnover)


async def _universe(require_testnet):
    s = sc.Screener(cfg(require_testnet=require_testnet, skip_top_volume=0,
                        min_turnover_usdt=2_000_000.0, top_n_turnover=600))
    return await s.build_universe()


uni_req = asyncio.run(_universe(True))
uni_any = asyncio.run(_universe(False))
ok("require_testnet=True: символ без Testnet отсеян (not_on_testnet)",
   uni_req == ["BTCUSDT"], uni_req)
ok("require_testnet=False: символ без Testnet допущен",
   uni_any == ["BTCUSDT", "DOGEUSDT", "MEMEUSDT"], uni_any)
ok("require_testnet по умолчанию True", cfg().require_testnet is True)
ok("не-булевый require_testnet отвергается",
   raises(lambda: sc.validate_config(cfg(require_testnet="no"))))
ok("bool require_testnet валиден",
   not raises(lambda: sc.validate_config(cfg(require_testnet=False))))

sc.fetch_instruments = _real_fetch_inst
sc.fetch_turnover = _real_fetch_turn

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
