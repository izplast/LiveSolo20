"""
Автономные проверки Telegram-уведомлений (reference/notifier.py).

Моки API Telegram: post() записывает вызовы и возвращает {"ok": true} или
бросает сетевые ошибки; sleep — заглушка. Без сети, без зависимостей.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_notifier.py
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


rl = _load("resilience")     # notifier импортирует resilience
nt = _load("notifier")

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


TP = nt.TelegramParams


def make_post(failures: int = 0, exc: type[Exception] = ConnectionError):
    """Мок post(): первые `failures` вызовов бросают exc, дальше ok."""
    calls: list[tuple[str, dict, float]] = []

    def post(url: str, form: dict, timeout: float = 15.0) -> dict:
        calls.append((url, form, timeout))
        if len(calls) <= failures:
            raise exc("mock network error")
        return {"ok": True, "result": {"message_id": len(calls)}}

    return post, calls


# ── экранирование ───────────────────────────────────────────────────────────

print("escape")
ok("HTML экранирует < > &",
   nt.escape_html("<b>5 & 3</b>") == "&lt;b&gt;5 &amp; 3&lt;/b&gt;",
   nt.escape_html("<b>5 & 3</b>"))
ok("MarkdownV2 экранирует спецсимволы",
   nt.escape_markdown_v2("a_b*c[d]e") == "a\\_b\\*c\\[d\\]e",
   nt.escape_markdown_v2("a_b*c[d]e"))
ok("escape_auto: HTML vs MarkdownV2 vs raw",
   nt.escape_auto("x&y", "HTML") == "x&amp;y"
   and nt.escape_auto("x_y", "MarkdownV2") == "x\\_y"
   and nt.escape_auto("x&y", "") == "x&y",
   "")

# ── форматирование событий ──────────────────────────────────────────────────

print("\nformat_*")
m = nt.format_cycle_opened("BTCUSDT", "Buy", 65000.5, 0.001, "C1")
ok("вход: символ, сторона, цена, объём, цикл",
   "BTCUSDT" in m and "Buy" in m and "65000.5" in m and "C1" in m, m)
m = nt.format_cycle_closed("BTCUSDT", "take_profit", 0.64, 300_000, "C1")
ok("выход: причина, PnL со знаком, удержание",
   "take_profit" in m and "+0.64" in m and "5м" in m, m)
m = nt.format_tp_hit("BTCUSDT", 66000.0, 1.20, "C1")
ok("TP: 🎯 и цена/PnL",
   "🎯" in m and "66000" in m and "+1.20" in m, m)
m = nt.format_sl_hit("BTCUSDT", 62000.0, -2.50, "C1")
ok("SL: ⛔ и отрицательный PnL",
   "⛔" in m and "-2.50" in m, m)
m = nt.format_api_error("place_order", "rate limit 429")
ok("ошибка API: операция и детали",
   "place_order" in m and "429" in m, m)
m = nt.format_circuit_open("BTCUSDT", "5 сбоев подряд")
ok("предохранитель: символ и причина",
   "BTCUSDT" in m and "5 сбоев подряд" in m, m)
m = nt.format_circuit_open(None, "сбой")
ok("предохранитель без символа → 'Все символы'", "Все символы" in m, m)
m = nt.format_cycle_opened("A_B", "Buy", 1.0, 1.0, "C1", "MarkdownV2")
ok("MarkdownV2: спецсимвол символа экранирован", "A\\_B" in m, m)
m = nt.format_signal("TESTUSDT", "Buy", 65000.5, "TESTUSDT:1:12345",
                     natr=1.2, detection_lag_ms=800)
ok("сигнал (dry-run): символ, сторона, цена, NATR, задержка, ID",
   "TESTUSDT" in m and "Buy" in m and "65000.5" in m
   and "NATR: 1.20%" in m and "800 мс" in m and "TESTUSDT:1:12345" in m, m)
ok("сигнал: dry-run метка", "dry-run" in m, m)
m = nt.format_signal("BTCUSDT", "Sell", 30000.0, "B:1:2")
ok("сигнал без NATR/задержки",
   "BTCUSDT" in m and "NATR" not in m and "мс" not in m, m)
m = nt.format_telegram_test()
ok("тест уведомлений: текст", "Тест уведомлений" in m, m)

# ── параметры ───────────────────────────────────────────────────────────────

print("\nTelegramParams")
p = TP(bot_token="t", chat_id="c", enabled=False)
ok("disabled без token/chat → валиден", p.validate() is None)
p = TP(bot_token="t", chat_id="c", enabled=True)
ok("enabled с token/chat → валиден", p.validate() is None)
try:
    TP(bot_token="", chat_id="c", enabled=True).validate()
    ok("enabled без token → ValueError", False)
except ValueError:
    ok("enabled без token → ValueError", True)
try:
    TP(bot_token="t", chat_id="", enabled=True).validate()
    ok("enabled без chat_id → ValueError", False)
except ValueError:
    ok("enabled без chat_id → ValueError", True)
try:
    TP(bot_token="t", chat_id="c", enabled=True,
       parse_mode="XML").validate()
    ok("неизвестный parse_mode → ValueError", False)
except ValueError:
    ok("неизвестный parse_mode → ValueError", True)

pc = nt.telegram_params_from_config({"enabled": True, "bot_token": "t",
                                     "chat_id": "c", "parse_mode": "MarkdownV2"})
ok("telegram_params_from_config маппит",
   pc.enabled is True and pc.bot_token == "t" and pc.chat_id == "c"
   and pc.parse_mode == "MarkdownV2", pc)
pc = nt.telegram_params_from_config({})
ok("пустая секция → disabled с дефолтами",
   pc.enabled is False and pc.parse_mode == "HTML", pc)

# ── отправка ────────────────────────────────────────────────────────────────

print("\nTelegramNotifier.send_message")
post, calls = make_post()
n = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=True),
                        post=post, sleep=noop_sleep)
ok("send_message → True", n.send_message("hello") is True, (n.sent, n.failed))
ok("один вызов post с url/payload",
   len(calls) == 1 and calls[0][0].endswith("/botB/sendMessage")
   and calls[0][1] == {"chat_id": "CH", "text": "hello", "parse_mode": "HTML"},
   calls)
ok("счётчики: sent=1 failed=0", n.sent == 1 and n.failed == 0, (n.sent, n.failed))

post2, calls2 = make_post(failures=2, exc=ConnectionError)
n2 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=True),
                         post=post2, sleep=noop_sleep)
ok("сетевые сбои ретраятся до успеха (3 вызова)",
   n2.send_message("retry me") is True and len(calls2) == 3,
   (n2.sent, n2.failed, len(calls2)))

post3, calls3 = make_post(failures=99, exc=rl.ResponseError(503, "down"))
n3 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=True),
                         post=post3, sleep=noop_sleep, max_attempts=2)
ok("неустранимый сбой → False, не падает",
   n3.send_message("x") is False and n3.failed == 1, (n3.sent, n3.failed))
ok("попыток не больше max_attempts",
   len(calls3) <= 2, len(calls3))

post4, calls4 = make_post()
n4 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=False),
                         post=post4, sleep=noop_sleep)
ok("disabled → send_message не отправляет",
   n4.send_message("x") is False and len(calls4) == 0, (n4.sent, n4.failed))

post5, calls5 = make_post()
n5 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=True),
                         post=post5, sleep=noop_sleep)
ok("parse_mode override в payload",
   n5.send_message("m", parse_mode="MarkdownV2") is True
   and calls5[0][1]["parse_mode"] == "MarkdownV2", calls5)

print("\nTelegramNotifier.notify (неблокирующий)")
post6, calls6 = make_post()
n6 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=True),
                         post=post6, sleep=noop_sleep)
ok("notify → True сразу (неблокирующий)", n6.notify("async msg") is True,
   (n6.sent, n6.failed))
n6.shutdown(wait=True)
ok("после shutdown сообщение ушло",
   n6.sent == 1 and len(calls6) == 1, (n6.sent, len(calls6)))

post7, calls7 = make_post()
n7 = nt.TelegramNotifier(TP(bot_token="B", chat_id="CH", enabled=False),
                         post=post7, sleep=noop_sleep)
ok("notify при disabled → False", n7.notify("x") is False, n7.sent)
n7.shutdown(wait=True)

print(f"\nитог: {PASS} ok, {FAIL} fail")
raise SystemExit(1 if FAIL else 0)