"""
tools/screener_common.py — общая логика для лайв-скринеров (midcap, new_uhlo).

Вынесено здесь, чтобы оба скринера переиспользовали одни и те же пороги,
константы и правила реконнекта, а не поддерживали дубли.
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Константы WS / watchdog
# ---------------------------------------------------------------------------
STALE_EXIT_SEC = 180  # нет ни одной confirm=kline.1 столько секунд -> exit(1)

WS_RECONNECT_MAX_ATTEMPTS = 5  # неудачных попыток подряд -> exit(1)
WS_RECONNECT_MAX_DELAY = 60.0  # максимум паузы между попытками, секунд

# ---------------------------------------------------------------------------
# Константы вселенной / CoinGecko
# ---------------------------------------------------------------------------
UNIVERSE_TTL_SEC = 24 * 3600  # кэш allленной считается свежим 24 ч
CG_FETCH_RETRIES = 5  # число попыток при 429/ошибке сети при обходе страниц
CG_RETRY_MIN_SEC = 30  # минимальная пауза между попытками CoinGecko
CG_RETRY_MAX_SEC = 60  # максимальная пауза между попытками CoinGecko

# Алиасы базовых тикеров Bybit -> CoinGecko id, когда symbol не совпадает
# (1000NEIROCTOUSDT -> base NEIROCTO, а в CoinGecko тикер neiro, id "neiro-3").
# Применяются ДО матчинга по symbol.
SYMBOL_ALIASES: Dict[str, str] = {
    "NEIROCTO": "neiro-3",
}

# ---------------------------------------------------------------------------
# Константы сида / минимума истории (пункт ревью 12)
# ---------------------------------------------------------------------------
SEED_LIMIT_1M = 60   # забирать 1m×60 при сиде
SEED_LIMIT_15M = 40  # забирать 15m×40 при сиде


def ws_backoff_delay(attempt: int) -> float:
    """Экспоненциальный бэкофф реконнекта, plafonné на WS_RECONNECT_MAX_DELAY.

    attempt — порядковый номер неудачной попытки (1, 2, ...).
    """
    return min(
        WS_RECONNECT_MAX_DELAY,
        (1.0 * 2 ** (attempt - 1)) * (0.7 + 0.6 * random.random()),
    )


def min_history_1m(natr_period: int, uhlo_length: int) -> int:
    """Минимум закрытых 1m-баров для оценки символа (пункт ревью 12)."""
    return max(natr_period + 1, 20, uhlo_length + 2)


def min_history_15m(uhlo_length_15m: int) -> int:
    """Минимум закрытых 15m-баров для оценки символа (пункт ревью 12)."""
    return uhlo_length_15m + 2


def is_stale(last_confirm_ms: Optional[int], now_ms: int, stale_sec: int) -> bool:
    """True, если с последней confirm=kline.1 прошло больше stale_sec."""
    if last_confirm_ms is None:
        return True
    return (now_ms - last_confirm_ms) > stale_sec * 1000


def cg_fetch_with_retry(fetch, label: str = "") -> Any:
    """Запрос к CoinGecko с повтором при 429/ошибке сети (не пропускает страницу).

    ``fetch`` — вызываемый без аргументов, возвращающий результат страницы или
    возбуждающий исключение при неудаче. Возвращает результат или None после
    исчерпания попыток.
    """
    last_err: Exception | None = None
    for attempt in range(1, CG_FETCH_RETRIES + 1):
        try:
            return fetch()
        except Exception as e:
            last_err = e
            is_429 = "429" in str(e)
            if attempt < CG_FETCH_RETRIES:
                wait = random.uniform(CG_RETRY_MIN_SEC, CG_RETRY_MAX_SEC)
                log(f"CoinGecko {label} попытка {attempt}/{CG_FETCH_RETRIES} не удалась ({e}), "
                    f"повтор через {wait:.0f}s{' (429)' if is_429 else ''}")
                time.sleep(wait)
    log(f"CoinGecko {label}: исчерпаны {CG_FETCH_RETRIES} попыток, последняя ошибка: {last_err}")
    return None


# ---------------------------------------------------------------------------
# Логирование (простое, для общих функций; файлы скринеров могут иметь своё)
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
