"""
reference/notifier.py — уведомления в Telegram о событиях DCA-бота.

Автономный слой без внешних зависимостей (только stdlib): форматирование
сообщений (HTML / MarkdownV2) и отправка через Bot API `sendMessage` с
повторами на сетевых сбоях (ResilientCaller из resilience.py, T030).

События, о которых бот уведомляет:
  * открытие цикла (вход исполнен) — cycle_opened;
  * закрытие цикла (выход по TP/SL/времени) — cycle_closed;
  * фиксация TP / срабатывание SL — tp_hit / sl_hit;
  * критическая ошибка API — api_error;
  * срабатывание предохранителя — circuit_open.

Отправка неблокирующая: notify() кладёт сообщение в пул потоков (по умолчанию
один воркер — Telegram не любит параллельные sendMessage из одного чата) и
возвращается сразу. Ошибки сети не роняют бота: отправка оборачивается в
try/except, при недоступности API сообщение молча теряется (журнал — задача
слушателя).

Сетевой вызов и паузы — инъекция (post/sleep), поэтому тестируется без сети.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_notifier.py
"""

from __future__ import annotations

import html as _html
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from resilience import CircuitBreaker, ExponentialBackoff, ResilientCaller, ResponseError

TELEGRAM_API = "https://api.telegram.org"

# parse_mode: HTML — экранирование через html.escape; MarkdownV2 — ручное.
_PARSE_MODES = ("HTML", "MarkdownV2", "")


def escape_html(text: object) -> str:
    """Экранирование для parse_mode=HTML (унаследованное от html.escape)."""
    return _html.escape(str(text), quote=False)


def escape_markdown_v2(text: object) -> str:
    """Экранирование спецсимволов MarkdownV2: _ * [ ] ( ) ~ ` > # + - = | { } . !"""
    out: list[str] = []
    for ch in str(text):
        if ch in "_*[]()~`>#+-=|{}.!":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def escape_auto(text: object, parse_mode: str) -> str:
    if parse_mode == "HTML":
        return escape_html(text)
    if parse_mode == "MarkdownV2":
        return escape_markdown_v2(text)
    return str(text)


def _fmt_price(v: float | None) -> str:
    return f"{v:.6g}" if isinstance(v, (int, float)) else "—"


def _fmt_pnl(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}"


def _fmt_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    seconds = int(ms / 1000)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


# ---------------------------------------------------------------------------
# Форматирование событий
# ---------------------------------------------------------------------------

def _wrap(title: str, body_lines: list[str], parse_mode: str = "HTML") -> str:
    if parse_mode == "MarkdownV2":
        b = "\n".join(body_lines)
        return f"*{title}*\n{b}"
    b = "\n".join(body_lines)
    return f"<b>{title}</b>\n{b}"


def format_cycle_opened(symbol: str, side: str, price: float, qty: float,
                        cycle_id: str, parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    return _wrap(
        "🟢 DCA: вход",
        [f"Символ: {e(symbol)}  ({e(side)})",
         f"Цена: {_fmt_price(price)}",
         f"Объём: {qty:.6g}",
         f"Цикл: {e(cycle_id)}"],
        parse_mode)


def format_cycle_closed(symbol: str, exit_reason: str, pnl: float | None,
                        duration_ms: int | None, cycle_id: str,
                        parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    icon = "🔴" if pnl is not None and pnl < 0 else "⚪"
    return _wrap(
        f"{icon} DCA: выход ({e(exit_reason)})",
        [f"Символ: {e(symbol)}",
         f"PnL: {_fmt_pnl(pnl)} USDT",
         f"Удержание: {_fmt_duration(duration_ms)}",
         f"Цикл: {e(cycle_id)}"],
        parse_mode)


def format_tp_hit(symbol: str, price: float, pnl: float,
                  cycle_id: str, parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    return _wrap(
        "🎯 TP зафиксирован",
        [f"Символ: {e(symbol)}",
         f"Цена: {_fmt_price(price)}",
         f"PnL: {_fmt_pnl(pnl)} USDT",
         f"Цикл: {e(cycle_id)}"],
        parse_mode)


def format_sl_hit(symbol: str, price: float, pnl: float,
                  cycle_id: str, parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    return _wrap(
        "⛔ SL сработал",
        [f"Символ: {e(symbol)}",
         f"Цена: {_fmt_price(price)}",
         f"PnL: {_fmt_pnl(pnl)} USDT",
         f"Цикл: {e(cycle_id)}"],
        parse_mode)


def format_api_error(operation: str, detail: str,
                     parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    return _wrap(
        "❗ Ошибка API",
        [f"Операция: {e(operation)}",
         f"Детали: {e(detail)}"],
        parse_mode)


def format_circuit_open(symbol: str | None, error: str,
                        parse_mode: str = "HTML") -> str:
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    sym_line = f"Символ: {e(symbol)}" if symbol else "Все символы"
    return _wrap(
        "🛑 Предохранитель открыт",
        [sym_line, f"Причина: {e(error)}"],
        parse_mode)


def format_signal(symbol: str, side: str, price: float,
                  signal_id: str, natr: float | None = None,
                  detection_lag_ms: int | None = None,
                  parse_mode: str = "HTML") -> str:
    """Dry-run: скринер нашёл кандидата, но ордер не выставляется."""
    e = lambda s: escape_auto(s, parse_mode)  # noqa: E731
    lines = [f"Символ: {e(symbol)}  ({e(side)})",
             f"Цена: {_fmt_price(price)}"]
    if natr is not None:
        lines.append(f"NATR: {natr:.2f}%")
    if detection_lag_ms is not None:
        lines.append(f"Задержка: {detection_lag_ms} мс")
    lines.append(f"ID: {e(signal_id)}")
    return _wrap("📈 DCA-сигнал (dry-run)", lines, parse_mode)


def format_telegram_test(parse_mode: str = "HTML") -> str:
    """Проверка соединения с ботом и каналом (--telegram-test)."""
    return _wrap("🔔 Тест уведомлений",
                 ["Скринер работает, канал открыт."], parse_mode)


# ---------------------------------------------------------------------------
# Параметры
# ---------------------------------------------------------------------------

@dataclass
class TelegramParams:
    """Секция telegram конфига бота."""

    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False
    parse_mode: str = "HTML"

    def validate(self) -> None:
        problems = []
        if self.parse_mode not in _PARSE_MODES:
            problems.append(f"parse_mode один из {_PARSE_MODES}")
        if self.enabled:
            if not self.bot_token:
                problems.append("bot_token не пуст при enabled")
            if not self.chat_id:
                problems.append("chat_id не пуст при enabled")
        if problems:
            raise ValueError("некорректная секция telegram:\n- " + "\n- ".join(problems))


def telegram_params_from_config(tg: dict) -> TelegramParams:
    p = TelegramParams(
        bot_token=str(tg.get("bot_token", "")),
        chat_id=str(tg.get("chat_id", "")),
        enabled=bool(tg.get("enabled", False)),
        parse_mode=str(tg.get("parse_mode", "HTML")),
    )
    p.validate()
    return p


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def _build_payload(text: str, chat_id: str, parse_mode: str = "HTML") -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return payload


def _http_post(url: str, form: dict, timeout: float = 15.0) -> dict:
    """POST form-данных → JSON-ответ Telegram Bot API (sendMessage)."""
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    if not body.get("ok"):
        raise ResponseError(r.status, f"Telegram: {body.get('description', '')}")
    return body


class TelegramNotifier:
    """Отправка уведомлений в Telegram. Неблокирующая: notify() → пул потоков.

    send_message() — синхронная отправка одного сообщения (с ретраями на
    сетевых сбоях через ResilientCaller); notify() — неблокирующая, кладёт
    отправку в ThreadPoolExecutor (по умолчанию один воркер). Ошибки сети
    перехватываются и не роняют бота.
    """

    def __init__(self, params: TelegramParams,
                 post: Callable[[str, dict, float], dict] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = 3) -> None:
        self.params = params
        self._post = post or _http_post
        self._caller = ResilientCaller(
            backoff=ExponentialBackoff(base=0.5, max_delay=4.0),
            breaker=CircuitBreaker(failure_threshold=5, cooldown=30.0),
            max_attempts=max_attempts, sleep=sleep,
        )
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="telegram")
        self._sent = 0
        self._failed = 0

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def url(self) -> str:
        return f"{TELEGRAM_API}/bot{self.params.bot_token}/sendMessage"

    def send_message(self, text: str, parse_mode: str | None = None) -> bool:
        """Синхронная отправка одного сообщения. True — отправлено."""
        if not self.params.enabled:
            return False
        pm = parse_mode if parse_mode is not None else self.params.parse_mode
        payload = _build_payload(text, self.params.chat_id, pm)
        try:
            self._caller.call(lambda: self._post(self.url, payload, 15.0))
            self._sent += 1
            return True
        except Exception:  # noqa: BLE001 — сеть недоступна, не роняем бота
            self._failed += 1
            return False

    def notify(self, text: str, parse_mode: str | None = None) -> bool:
        """Неблокирующая отправка: кладёт сообщение в пул потоков."""
        if not self.params.enabled:
            return False
        self._executor.submit(self.send_message, text, parse_mode)
        return True

    def shutdown(self, wait: bool = False) -> None:
        try:
            self._executor.shutdown(wait=wait)
        except Exception:  # noqa: BLE001
            pass


def close_all(notifier: TelegramNotifier) -> None:
    notifier.shutdown(wait=True)