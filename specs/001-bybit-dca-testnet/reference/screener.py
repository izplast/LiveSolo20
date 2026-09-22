"""
core/screener.py — скринер для DCA-бота на Bybit USDT Perpetual.

Замена присланной версии. Что сохранено из неё без изменений:
  * compute_natr  — NATR-14 по Уайлдеру, дословно;
  * отдельный процесс, отправляющий сигнал на локальный FastAPI бота (POST /signal).

Режим сигналов — трендовый/скальперский, вход по импульсу на пробой:
  * compute_uhlo возвращает БЛИЗОСТЬ цены к экстремумам окна: highs — близость
    к ХАЮ (100 = цена на самом пике), lows — близость к ЛОЮ (100 = цена на
    самом дне). Имена полей говорят сами за себя.
  * classify_color переработан: green (LONG) — цена поджалась к хаям и
    пробивает их (highs 80..100, lows 0..20) НА ОБОИХ ТФ (1м и 15м);
    red (SHORT) — цена поджалась к лоям и пробивает их (lows 80..100,
    highs 0..20) на обоих ТФ. Условия жёсткие и одинаковые для обоих
    таймфреймов, без смягчения на 15м.
  * фильтры экстремумов перевёрнуты под тренд: LONG не входим, пока 1м
    «зажат у дна» (ловля падающего ножа), SHORT не входим, пока 1м «зажат
    на пике» без импульса вниз (пороги long_lows_min / short_highs_max).
  * требование пользователя «на ТФ 1м индикаторы не должны одновременно
    показывать 0 и 100»: сигнал режется, когда UHLO 1м в «углу» — вертикальный
    рывок без единого отката в окне (highs=100 и lows=0, либо наоборот),
    причина uhlo_corner.
Индикаторы работают на строках вида [ts, open, high, low, close, ...] — индексы
k[2]/k[3]/k[4] одинаковы у Binance и у Bybit v5, поэтому код не переписывался.

Что изменено и почему (детали в CHANGES.md рядом с файлом):
  1. Источник данных — WebSocket Bybit MAINNET вместо REST-опроса Binance.
     Ордера идут на Testnet, поэтому цена сигнала должна приходить с той же
     биржи, иначе проскальзывание в журнале превращается в межбиржевой базис.
  2. Вселенная — пересечение инструментов mainnet и TESTNET: сигнал по символу,
     которого нет на тестовом контуре, гарантированно даёт отклонённый ордер.
  3. Ликвидность — топ-N по turnover24h из Bybit /v5/market/tickers вместо
     CoinGecko: снимает лимиты внешнего API и проблему сопоставления тикеров
     (1000PEPE и подобные множители называются на биржах по-разному).
  4. Добавлено отсечение сверху (natr_max) — правило «волатильнее порога
     слишком рискованно», которого в исходной версии не было вовсе.
  5. В расчёт идут только ЗАКРЫТЫЕ свечи (confirm=true у WS, отброс текущего
     бара у REST) — иначе сигнал мигает внутри минуты и невоспроизводим.
  6. Конверт сигнала: signal_id, цена и метка времени на момент сигнала.
     Без них задержка и проскальзывание не вычисляются, а это цель этапа.
     Параметры DCA из сигнала убраны — их единственный владелец бот.
  7. Состояние цвета сбрасывается при уходе в none и НЕ продвигается при
     неудачной отправке: в исходной версии монета отстреливалась один раз
     за весь прогон, а потерянный POST терял сигнал безвозвратно.
  8. Журнал событий в JSONL: у каждого кандидата конечный статус с причиной,
     фиксируются интервалы недоступности потока.

Требования: Python >= 3.10, `pip install websockets requests` (и pyyaml, если
используется config/config.yml). В Termux перед запуском: `termux-wake-lock`.

Запуск рядом с ботом в отдельной tmux-сессии:
    python3 core/screener.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal as os_signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import requests

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # websockets < 13
    from websockets import connect as ws_connect  # type: ignore[attr-defined]

try:
    from infra.logging_setup import setup_logging
except ImportError:  # автономный запуск вне дерева проекта
    import logging
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    def _kyiv_tz():
        try:
            return ZoneInfo("Europe/Kyiv")
        except Exception:  # noqa: BLE001 — нет tzdata → UTC+3 (летнее киевское)
            return timezone(timedelta(hours=3))

    def _kyiv_now():
        return datetime.now(_kyiv_tz()).strftime("%Y-%m-%d %H:%M:%S")

    class KyivFormatter(logging.Formatter):
        def __init__(self):
            super().__init__(fmt="%(asctime)s %(levelname)s %(message)s")

        def formatTime(self, record, datefmt=None):  # noqa: N802
            dt = datetime.fromtimestamp(record.created, _kyiv_tz())
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    def setup_logging(path: str):  # type: ignore[misc]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(path), logging.StreamHandler()],
        )
        for h in logging.getLogger().handlers:
            h.setFormatter(KyivFormatter())
        return logging.getLogger("screener")


logger = setup_logging("logs/screener.log")

# Данные берём с mainnet: на Testnet торгов почти нет, свечи вырожденные и
# скринер не нашёл бы ничего. Ордера при этом остаются на Testnet — контур
# исполнения задаётся конфигом бота, скринер ордеров не выставляет.
MAINNET_REST = "https://api.bybit.com"
MAINNET_WS = "wss://stream.bybit.com/v5/public/linear"
TESTNET_REST = "https://api-testnet.bybit.com"

# Диапазон NATR объявлен включительным, но арифметика double на ровной границе
# даёт 0.8999999999999879 — без допуска кандидат ровно на границе отсекался бы.
BOUNDARY_EPS = 1e-9

# Длительность тестового прогона: 72 часа непрерывной работы связки (README).
TEST_DURATION_HOURS = 72


def _fmt_kyiv_ms(ts_ms: int) -> str:
    """Метка времени (мс с эпохи) → «ГГГГ-ММ-ДД ЧЧ:ММ:СС» по киевскому времени."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo("Europe/Kyiv")
    except Exception:  # noqa: BLE001 — нет tzdata → UTC+3 (летнее киевское)
        tz = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(ts_ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class Config:
    bot_api_url: str = "http://127.0.0.1:8000"
    top_n_turnover: int = 600   # топ по обороту, из которого скринер ловит сигналы
    # Первые N символов по обороту пропускаются: это низковолатильные гиганты
    # (BTC, ETH...), на которых входная волатильность NATR почти никогда не
    # доходит до порога, а подписка на их потоки тратит ресурсы впустую.
    skip_top_volume: int = 30
    natr_period: int = 14
    natr_min: float = 0.9
    natr_max: float = 2.5          # отсечение «слишком рискованно»
    # Базовая монета из этого списка исключается из вселенной целиком:
    # индекс-токены (INXUSDT → INX), bStocks и прочее, что не ложится под
    # стоп из-за гэпов. Дополняет встроенный DEFAULT_STOCK_BLACKLIST
    # продакшн-скринера, не заменяет.
    base_coin_blacklist: list[str] = field(default_factory=list)
    # Явный blacklist символов по полному имени (INXUSDT, ...). Расширяемый:
    # каждый символ исключается из вселенной при подборе.
    blacklist: list[str] = field(default_factory=list)
    uhlo_length: int = 15
    tf_fast: str = "1"
    tf_slow: str = "15"
    cooldown_sec: int = 300
    required_leverage: float = 3.0
    # Требовать ли наличия символа на Testnet. При False вселенная строится
    # только по mainnet (для диагностики/проверки скринера вне тестнет-контура).
    require_testnet: bool = True
    # 'candidates' — всё, кроме шума (нехватка истории, NATR ниже минимума,
    # неизменный цвет); 'all' — включая шум; 'none' — только сигналы.
    reject_log: str = "candidates"
    # Анти-памп (LONG): сигнал гасится, если объём закрытой свечи выше
    # pump_volume_mult × среднего за предыдущие 20 И верхняя тень длиннее
    # pump_wick_ratio × размаха бара. 0 — защита выключена.
    pump_volume_mult: float = 0.0
    pump_wick_ratio: float = 0.5
    # SHORT не входим, пока 1м «зажат на самом пике» без импульса вниз:
    # UHLO highs 1м выше порога (цена прижата к максимумам окна). Красный
    # цвет в трендовом режиме требует highs 1м <= 20, поэтому срабатывает
    # только на границах расширенных полос. 100 — выключено.
    short_highs_max: float = 100.0
    # LONG не входим, пока 1м «зажат у самого дна» (ловля падающего ножа):
    # UHLO lows 1м выше порога (цена прижата к минимумам окна).
    # Зелёный цвет в трендовом режиме требует lows 1м <= 20, поэтому
    # срабатывает только на границах расширенных полос. 100 — выключено.
    long_lows_min: float = 100.0
    # «Чистый крипто-пул»: только символы, чья базовая монета есть в топе
    # CoinGecko по капитализации (0 — фильтр выключен).
    cg_max_rank: int = 0
    # Суточный оборот Bybit (turnover24h, USDT) не ниже этого значения
    # (0 — фильтр выключен).
    min_turnover_usdt: float = 0.0
    ws_topics_per_conn: int = 200
    ws_subscribe_batch: int = 10   # Bybit не принимает больше 10 args за раз
    ws_ping_sec: int = 15          # Bybit закрывает соединение без ping в 20 с
    # Нет ответа pong после отправленного ping дольше этого — соединение
    # считается мёртвым, шард переподключается, не дожидаясь сторожа тишины.
    ws_pong_timeout_sec: float = 10.0
    ws_stale_sec: int = 45
    # Обрыв WS дольше этого — включается REST-догонялка: закрытые свечи
    # читаются из /v5/market/kline, чтобы сигналы не терялись на время
    # разрыва (0 — догонялка выключена).
    rest_fallback_after_sec: int = 30
    # Период опроса REST в режиме догонялки: свечи 1м закрываются раз в минуту.
    rest_fallback_interval_sec: int = 60
    # Обрыв длиннее этого — в окне свечей появляется дыра, и NATR с UHLO
    # начинают считаться по разрывной истории. Тогда окно перечитывается
    # заново из REST вместо доклейки к старому.
    reseed_after_sec: int = 90
    max_clock_skew_ms: int = 3000
    post_retries: int = 3
    seed_concurrency: int = 6
    journal_path: str = "logs/screener-events.jsonl"
    # Paper-режим: сигналы НЕ отправляются на bot_api_url, а пишутся в журнал
    # (signal_dry_run) и, при включённой секции telegram в config.yml, уходят
    # уведомлением в Telegram. Ордера никуда не выставляются.
    dry_run: bool = False


def load_config(path: str = "config/config.yml") -> Config:
    """Читает config.yml, если он есть. Отсутствие файла — не повод падать."""
    raw: dict[str, Any] = {}
    try:
        import yaml  # локальный импорт: без конфига зависимость не нужна

        with open(path, "r") as f:
            raw = (yaml.safe_load(f) or {}).get("screener", {}) or {}
    except FileNotFoundError:
        logger.warning("%s не найден — работаем на значениях по умолчанию", path)
    except Exception:
        logger.exception("не удалось прочитать %s — работаем на значениях по умолчанию", path)

    # Совместимость со старым ключом: раньше был один порог снизу.
    if "natr_threshold" in raw and "natr_min" not in raw:
        raw["natr_min"] = raw.pop("natr_threshold")
        logger.warning("ключ natr_threshold устарел, переименуйте в natr_min (и задайте natr_max)")
    raw.pop("scan_interval_sec", None)  # опроса больше нет, работаем по потоку
    raw.pop("top_n_mcap", None)         # заменён на top_n_turnover

    known = {f.name for f in Config.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        logger.warning("незнакомые ключи в screener-конфиге игнорируются: %s", sorted(unknown))
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    validate_config(cfg)
    return cfg


def _is_minute_tf(value: Any) -> bool:
    """Таймфрейм обязан быть строкой с целым числом минут ('1', '15')."""
    try:
        return isinstance(value, str) and int(value) > 0 and str(int(value)) == value
    except (TypeError, ValueError):
        return False


def validate_config(cfg: Config) -> None:
    problems = []
    if not cfg.natr_min > 0:
        problems.append("natr_min должен быть больше нуля")
    if not cfg.natr_max > cfg.natr_min:
        problems.append("natr_max должен быть больше natr_min")
    if cfg.natr_period < 2:
        problems.append("natr_period >= 2")
    if cfg.uhlo_length < 2:
        problems.append("uhlo_length >= 2")
    if not 1 <= cfg.ws_subscribe_batch <= 10:
        problems.append("ws_subscribe_batch в пределах 1..10")
    if cfg.ws_ping_sec >= 20:
        problems.append("ws_ping_sec < 20: Bybit закрывает соединение без ping")
    if cfg.ws_stale_sec <= cfg.ws_ping_sec:
        problems.append("ws_stale_sec должен превышать ws_ping_sec")
    if not cfg.ws_pong_timeout_sec > 0:
        problems.append("ws_pong_timeout_sec > 0")
    if cfg.ws_pong_timeout_sec >= cfg.ws_stale_sec:
        problems.append("ws_pong_timeout_sec должен быть меньше ws_stale_sec")
    if cfg.rest_fallback_after_sec < 0:
        problems.append("rest_fallback_after_sec >= 0 (0 — догонялка выключена)")
    if cfg.rest_fallback_after_sec > 0 and cfg.rest_fallback_interval_sec < 5:
        problems.append("rest_fallback_interval_sec >= 5")
    if cfg.reject_log not in ("all", "candidates", "none"):
        problems.append("reject_log: all | candidates | none")

    if not isinstance(cfg.dry_run, bool):
        problems.append("dry_run: bool")
    if not isinstance(cfg.require_testnet, bool):
        problems.append("require_testnet: bool")
    if not _is_minute_tf(cfg.tf_fast) or not _is_minute_tf(cfg.tf_slow):
        problems.append("tf_fast/tf_slow — целые минуты в виде строки ('1', '15'); "
                        "нецелые или нецифровые значения ('D', 'W') не поддерживаются")
    elif int(cfg.tf_slow) <= int(cfg.tf_fast):
        problems.append("tf_slow должен быть старше tf_fast")
    if cfg.top_n_turnover < 1:
        problems.append("top_n_turnover >= 1")
    if cfg.skip_top_volume < 0:
        problems.append("skip_top_volume >= 0")
    if cfg.ws_topics_per_conn < 2:
        problems.append("ws_topics_per_conn >= 2 (по 2 топика на символ)")
    if not cfg.pump_volume_mult >= 0:
        problems.append("pump_volume_mult >= 0")
    if not 0 <= cfg.pump_wick_ratio <= 1:
        problems.append("pump_wick_ratio в пределах 0..1")
    if not 80 <= cfg.short_highs_max <= 100:
        problems.append("short_highs_max в пределах 80..100 (100 — выключено)")
    if not 80 <= cfg.long_lows_min <= 100:
        problems.append("long_lows_min в пределах 80..100 (100 — выключено)")
    if cfg.cg_max_rank < 0:
        problems.append("cg_max_rank >= 0")
    if cfg.min_turnover_usdt < 0:
        problems.append("min_turnover_usdt >= 0")
    if problems:
        raise ValueError("некорректный screener-конфиг:\n- " + "\n- ".join(problems))


# ---------------------------------------------------------------------------
# Индикаторы — перенесены из присланной версии без изменений
# ---------------------------------------------------------------------------

def compute_natr(klines, period=14):
    """NATR(period) = (ATR / close) * 100, сглаживание Уайлдера."""
    if len(klines) < period + 1:
        return None
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    trs = []
    for i in range(1, len(klines)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period

    last_close = closes[-1]
    if not last_close:
        return None
    return (atr / last_close) * 100


def compute_uhlo(klines, length=15):
    """UHLO — «близость цены к экстремумам окна» (поля говорят сами за себя):

      * highs  — близость к ХАЮ окна: 100 = цена на самом пике
        (все максимумы окна под/на текущем уровне), 0 = цена у дна;
      * lows   — близость к ЛОЮ окна: 100 = цена на самом дне
        (все минимумы окна над/на текущем уровне), 0 = цена у хая.

    Алгоритмически это обратная сторона Unreached Highs/Lows [LuxAlgo]: там
    хранятся экстремумы, ДО которых цена не дошла, и результат — их доля.
    Близость = 100 − доля недостигнутых экстремумов (сколько максимумов цена
    уже перекрыла / минимумов уже пробила). Цикл идентичен Pine-порту:

      1) убираем из массива unreached_highs те h, что текущий high пробил
         (high > h) — значит цена достигла этого максимума;
      2) убираем из unreached_lows те l, что текущий low пробил (low < l) —
      3) обрезаем до длины length и добавляем текущий high/low в начало;
      4) близость = 100 − 100 * размер массива недостигнутых / length.

    Каждый бар вставляет ровно один элемент и обрезает массив до length,
    поэтому результат зависит только от последних length+1 баров. Отсюда и
    возможность считать индикатор по скользящему окну, а не по всей истории.
    """
    if len(klines) < 2:
        return None

    unreached_highs, unreached_lows = [], []
    u_highs = u_lows = 0.0

    for k in klines:
        h = float(k[2])
        l = float(k[3])

        unreached_highs = [x for x in unreached_highs if h <= x]
        unreached_lows = [x for x in unreached_lows if l >= x]

        if len(unreached_highs) > length:
            unreached_highs.pop()
        if len(unreached_lows) > length:
            unreached_lows.pop()

        u_highs = 100 * len(unreached_highs) / length
        u_lows = 100 * len(unreached_lows) / length

        unreached_highs.insert(0, h)
        unreached_lows.insert(0, l)

    return {"highs": 100 - u_highs, "lows": 100 - u_lows}


# Пороги трендового/скальперского триггера (см. classify_color). Условия
# ЖЁСТКИЕ и одинаковые для обоих таймфреймов: 15м — не подтверждение с
# мягкими порогами, а равноправная часть триггера.
FAST_MIN = 80.0   # поле близости к экстремуму входа (для LONG — highs, для SHORT — lows)
FAST_MAX = 20.0   # противоположное поле (для LONG — lows, для SHORT — highs)
SLOW_MIN = 80.0   # 15м: те же пороги, что и на 1м
SLOW_MAX = 20.0   # 15м: те же пороги, что и на 1м


def classify_color(a, b):
    """Трендовый/скальперский триггер: вход по импульсу на пробой.

    Поля UHLO — БЛИЗОСТЬ цены к экстремумам окна (см. compute_uhlo):
      * highs ВЫСОКИЙ (>= 80) — цена поджалась к ХАЮ окна (на самом пике);
      * highs НИЗКИЙ  (<= 20) — цена далеко от хая окна (внизу/у дна);
      * lows  ВЫСОКИЙ (>= 80) — цена поджалась к ЛОЮ окна (на самом дне);
      * lows  НИЗКИЙ  (<= 20) — цена далеко от лоя окна (вверху/у хая).

    green (LONG) — ЦЕНА ПОДЖАЛАСЬ К ХАЯМ и пробивает их: highs >= 80,
    lows <= 20 — на обоих ТФ (1м и 15м);
    red (SHORT) — ЦЕНА ПОДЖАЛАСЬ К ЛОЯМ и пробивает их: lows >= 80,
    highs <= 20 — на обоих ТФ.

    Условия жёсткие и одинаковые для обоих таймфреймов: 15м — равноправная
    часть триггера, без смягчения.
    """
    if not a or not b:
        return "none"
    green = (
        a["highs"] >= FAST_MIN and a["lows"] <= FAST_MAX
        and b["highs"] >= SLOW_MIN and b["lows"] <= SLOW_MAX
    )
    red = (
        a["lows"] >= FAST_MIN and a["highs"] <= FAST_MAX
        and b["lows"] >= SLOW_MIN and b["highs"] <= SLOW_MAX
    )
    if green:
        return "green"
    if red:
        return "red"
    return "none"


def fast_uhlo_corner(uhlo_fast: dict | None) -> bool:
    """«Угол» UHLO на 1м: highs и lows одновременно на 0 и 100.

    Состояние (highs=100, lows=0) — вертикальный рывок вверх: цена на пике,
    все 20 баров окна обновляли хаи, ни один не сделал нового лоу. Зеркальное
    состояние (highs=0, lows=100) — вертикальный обвал. Это не «пробой»
    (противоположное поле при этом ненулевое), а вертикальная мачта без
    единого отката: вход в неё — покупка/продажа вершины движения. Такие
    сигналы режем безусловно.

    UHLO считается по целым барам (0/15 и 15/15), поэтому углы дают ровно
    0.0/100.0; допуск BOUNDARY_EPS — страховка от арифметики double.
    """
    if not uhlo_fast:
        return False
    highs = uhlo_fast.get("highs", 0.0)
    lows = uhlo_fast.get("lows", 0.0)
    return (highs <= BOUNDARY_EPS and lows >= 100 - BOUNDARY_EPS) or (
        lows <= BOUNDARY_EPS and highs >= 100 - BOUNDARY_EPS)


# ---------------------------------------------------------------------------
# Решение по кандидату
# ---------------------------------------------------------------------------

# Причины, которые в режиме 'candidates' в журнал не пишутся: это фон, а не
# события. 300 символов × 1440 минут — за 72 часа это миллионы строк на
# телефоне, поэтому отбор причин здесь не косметика.
NOISE_REASONS = {"insufficient_history", "natr_below_min", "repeat_color",
                 "no_confirm"}


@dataclass
class Decision:
    passed: bool
    reason: str = ""
    color: str = "none"
    natr: float | None = None
    uhlo_fast: dict | None = None
    uhlo_slow: dict | None = None
    details: dict[str, Any] = field(default_factory=dict)


def evaluate(fast: Sequence, slow: Sequence, cfg: Config) -> Decision:
    """Чистая функция: свечи → решение. Проверяется юнит-тестами без сети."""
    natr = compute_natr(fast, cfg.natr_period)
    if natr is None:
        return Decision(False, "insufficient_history",
                        details={"fast_bars": len(fast), "slow_bars": len(slow)})

    if natr > cfg.natr_max + BOUNDARY_EPS:
        # Ключевое отсечение этапа: слишком рискованная монета.
        return Decision(False, "natr_above_max", natr=natr,
                        details={"natr": natr, "natr_max": cfg.natr_max})
    if natr < cfg.natr_min - BOUNDARY_EPS:
        return Decision(False, "natr_below_min", natr=natr,
                        details={"natr": natr, "natr_min": cfg.natr_min})

    uhlo_fast = compute_uhlo(fast, cfg.uhlo_length)
    uhlo_slow = compute_uhlo(slow, cfg.uhlo_length)
    if uhlo_slow is None:
        return Decision(False, "uhlo_slow_missing", natr=natr, uhlo_fast=uhlo_fast,
                        details={"slow_bars": len(slow)})

    color = classify_color(uhlo_fast, uhlo_slow)
    if color == "none":
        return Decision(False, "uhlo_no_color", natr=natr,
                        uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow)

    # ТФ 1м: индикаторы не должны одновременно показывать 0 и 100 —
    # вертикальный рывок без единого отката в окне (см. fast_uhlo_corner).
    if fast_uhlo_corner(uhlo_fast):
        return Decision(False, "uhlo_corner", color=color, natr=natr,
                        uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow,
                        details={"highs_1m": uhlo_fast.get("highs"),
                                 "lows_1m": uhlo_fast.get("lows")})

    if color == "green" and pump_blocked(fast, cfg):
        return Decision(False, "pump_volume_spike", color=color, natr=natr,
                        uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow,
                        details={"volume_mult": cfg.pump_volume_mult,
                                 "wick_ratio": cfg.pump_wick_ratio})
    # LONG не входим, пока 1м зажат у дна (ловля падающего ножа). Имена
    # причины/порога — из формулировки задачи; по сути это защита от входа
    # против тренда на экстремуме.
    if color == "green" and long_blocked(uhlo_fast, cfg):
        return Decision(False, "long_overbought", color=color, natr=natr,
                        uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow,
                        details={"lows_1m": uhlo_fast.get("lows"),
                                 "long_lows_min": cfg.long_lows_min})
    # SHORT не входим, пока 1м зажат на пике без импульса вниз.
    if color == "red" and short_blocked(uhlo_fast, cfg):
        return Decision(False, "short_oversold", color=color, natr=natr,
                        uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow,
                        details={"highs_1m": uhlo_fast.get("highs"),
                                 "short_highs_max": cfg.short_highs_max})

    return Decision(True, "", color=color, natr=natr,
                    uhlo_fast=uhlo_fast, uhlo_slow=uhlo_slow)


def color_to_side(color: str) -> str:
    return "Buy" if color == "green" else "Sell"


# ---------------------------------------------------------------------------
# Фильтры сигнала (чистые функции — проверяются юнит-тестами без сети)
# ---------------------------------------------------------------------------

def _volume(row: list) -> float | None:
    """Объём свечи (индекс 5), если он есть в строке."""
    return float(row[5]) if len(row) > 5 else None


def pump_blocked(fast: Sequence, cfg: Config) -> bool:
    """Анти-памп (LONG): объёмный спайк + длинная верхняя тень.

    Сигнал гасится, если объём последней закрытой свечи выше
    pump_volume_mult × среднего за предыдущие 20 свечей И верхняя тень
    занимает больше pump_wick_ratio × размаха бара. 0 — выключено.
    """
    if cfg.pump_volume_mult <= 0:
        return False
    if len(fast) < 2:
        return False
    last_vol = _volume(fast[-1])
    if last_vol is None:
        return False
    prev = fast[-21:-1]
    prev_vols = [_volume(r) for r in prev]
    prev_vols = [v for v in prev_vols if v is not None]
    if len(prev_vols) < 2:
        return False
    avg = sum(prev_vols) / len(prev_vols)
    if last_vol <= cfg.pump_volume_mult * avg:
        return False
    o, h, l, c = (float(fast[-1][1]), float(fast[-1][2]),
                  float(fast[-1][3]), float(fast[-1][4]))
    rng = h - l
    if rng <= 0:
        return False
    wick = (h - max(o, c)) / rng
    return wick > cfg.pump_wick_ratio


def long_blocked(uhlo_fast: dict | None, cfg: Config) -> bool:
    """Анти-нож (LONG): не входим, пока 1м зажат у самого дна.

    Трендовый LONG — это импульс вверх от верха окна. Если быстрый ТФ всё
    ещё «у самого дна» (UHLO lows 1м выше long_lows_min — цена прижата к
    минимумам окна), лонг — ловля падающего ножа. С узкими полосами
    зелёного (lows 1м <= 20) срабатывает только на границах расширенных
    полос; 100 — выключено.
    """
    if cfg.long_lows_min >= 100:
        return False
    if not uhlo_fast:
        return False
    return uhlo_fast.get("lows", 0) > cfg.long_lows_min


def short_blocked(uhlo_fast: dict | None, cfg: Config) -> bool:
    """Анти-пик (SHORT): не входим, пока 1м зажат на самом пике.

    Трендовый SHORT — это импульс вниз от дна окна. Если быстрый ТФ «на
    пике» (UHLO highs 1м выше short_highs_max — цена прижата к максимумам
    окна), шорт — вход в вершину без подтверждённого пробоя вниз. С узкими
    полосами красного (highs 1м <= 20) срабатывает только на границах
    расширенных полос; 100 — выключено.
    """
    if cfg.short_highs_max >= 100:
        return False
    if not uhlo_fast:
        return False
    return uhlo_fast.get("highs", 0) > cfg.short_highs_max


# ---------------------------------------------------------------------------
# Состояние по символу
# ---------------------------------------------------------------------------

class SymbolState:
    """Окна закрытых свечей и цвет, на котором монета уже отстрелялась."""

    def __init__(self, fast_cap: int, slow_cap: int):
        self.fast: deque[list] = deque(maxlen=fast_cap)
        self.slow: deque[list] = deque(maxlen=slow_cap)
        self.last_color = "none"
        self.last_signal_ms = 0
        # Цвет ПРЕДЫДУЩЕГО закрытого бара, если тот полностью прошёл evaluate()
        # (иначе "none"). Механизм подтверждения: сигнал уходит только когда
        # одно и то же цветовое состояние держится 2 бара подряд — одиночный
        # бар в углу чаще оказывается ложным проколом (см. BTWUSDT 21.08).
        self.prev_color = "none"

    def push(self, tf: str, row: list) -> bool:
        """Добавляет закрытую свечу. False, если свеча не новая.

        Дедупликация обязательна: Bybit повторяет закрытый kline снапшотом
        после переподписки, а REST-прогрев пересекается с потоком. Без неё
        одна свеча попала бы в окно дважды и исказила NATR.
        """
        buf = self.fast if tf == "fast" else self.slow
        if buf:
            last_start = buf[-1][0]
            if row[0] < last_start:
                return False
            if row[0] == last_start:
                buf[-1] = row  # уточнение той же свечи
                return False
        buf.append(row)
        return True


# ---------------------------------------------------------------------------
# Журнал событий (JSONL)
# ---------------------------------------------------------------------------

class Journal:
    """Машиночитаемый журнал: одна строка JSON на событие."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, kind: str, **fields: Any) -> None:
        # kind и ts выставляются последними: иначе поле с таким же именем в
        # fields молча перезаписало бы их, и строка перестала бы разбираться.
        record = dict(fields)
        record["kind"] = kind
        record["ts"] = int(time.time() * 1000)
        try:
            self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("не удалось записать событие в журнал")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._f.close()


# ---------------------------------------------------------------------------
# REST-помощники
# ---------------------------------------------------------------------------

def _bybit_get(base: str, path: str, params: dict[str, Any], retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt) * (0.7 + 0.6 * random.random()))
        try:
            r = requests.get(f"{base}{path}", params=params, timeout=15)
            r.raise_for_status()
            body = r.json()
            if body.get("retCode") != 0:
                raise RuntimeError(f"retCode={body.get('retCode')} {body.get('retMsg')}")
            return body["result"]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"GET {path} не удался после {retries + 1} попыток: {last_err}")


def fetch_instruments(base: str) -> dict[str, dict]:
    """Инструменты USDT-перпетуалов с ограничениями (плечо, шаг лота, шаг цены)."""
    out: dict[str, dict] = {}
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = _bybit_get(base, "/v5/market/instruments-info", params)
        for it in res.get("list", []):
            if it.get("contractType") != "LinearPerpetual" or it.get("quoteCoin") != "USDT":
                continue
            out[it["symbol"]] = {
                "status": it.get("status"),
                "max_leverage": float(it["leverageFilter"]["maxLeverage"]),
                "qty_step": float(it["lotSizeFilter"]["qtyStep"]),
                "min_qty": float(it["lotSizeFilter"]["minOrderQty"]),
                "tick_size": float(it["priceFilter"]["tickSize"]),
            }
        cursor = res.get("nextPageCursor") or None
        if not cursor:
            return out


def fetch_turnover(base: str) -> dict[str, float]:
    res = _bybit_get(base, "/v5/market/tickers", {"category": "linear"})
    return {t["symbol"]: float(t.get("turnover24h") or 0) for t in res.get("list", [])}


COINGECKO_API = "https://api.coingecko.com/api/v3"


def _cg_get(path: str, params: dict[str, Any], retries: int = 3) -> list:
    """GET CoinGecko: в отличие от Bybit возвращает чистый JSON-массив."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt) * (0.7 + 0.6 * random.random()))
        try:
            r = requests.get(f"{COINGECKO_API}{path}", params=params, timeout=15)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, list):
                raise RuntimeError(f"CoinGecko {path}: неожиданный ответ {type(body)}")
            return body
        except Exception as e:
            last_err = e
    raise RuntimeError(f"CoinGecko GET {path} не удался после {retries + 1} попыток: {last_err}")


def fetch_cg_top(max_rank: int) -> set[str]:
    """Символы базовых монет в топе CoinGecko по капитализации.

    Фильтр «чистого крипто-пула»: отсекает токенизированные акции/ETF,
    индексные и леверидж-токены (bStocks: TSLA, NVDA...), которых нет на
    CoinGecko. Возвращает ВЕРХНИЙ регистр символов (BTC, ETH, ...).
    """
    out: set[str] = set()
    page = 1
    per_page = 250
    while len(out) < max_rank:
        rows = _cg_get("/coins/markets",
                       {"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": per_page, "page": page}, retries=2)
        if not rows:
            break
        for coin in rows:
            out.add(str(coin.get("symbol") or "").upper())
        page += 1
        if len(rows) < per_page:
            break
    return out


def fetch_klines(base: str, symbol: str, interval: str, limit: int) -> list[list]:
    """Закрытые свечи, от старых к новым.

    Bybit отдаёт список от новых к старым и включает текущую незакрытую свечу —
    её отбрасываем, иначе неполный бар занизит NATR и сдвинет UHLO.
    """
    res = _bybit_get(base, "/v5/market/kline",
                     {"category": "linear", "symbol": symbol, "interval": interval,
                      "limit": min(1000, limit + 1)})
    interval_ms = int(interval) * 60_000
    now_ms = int(time.time() * 1000)
    rows = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
             float(r[5]) if len(r) > 5 else 0.0] for r in res.get("list", [])]
    closed = [r for r in rows if r[0] + interval_ms <= now_ms]
    closed.sort(key=lambda r: r[0])
    return closed[-limit:]


def check_clock_skew(cfg: Config) -> int:
    """Расхождение часов с биржей (FR-032).

    Проверяются оба контура: по mainnet-времени скринер ставит метки сигналов,
    а по testnet-времени бот подписывает ордера. Расхождение на Android после
    сна — обычное дело, и без явной проверки оно выглядит как серия отказов
    биржи с непрозрачной причиной.
    """
    worst = 0
    for name, base in (("mainnet", MAINNET_REST), ("testnet", TESTNET_REST)):
        sent = time.time() * 1000
        res = _bybit_get(base, "/v5/market/time", {})
        received = time.time() * 1000
        server_ms = int(res["timeNano"]) / 1e6
        skew = int(server_ms + (received - sent) / 2 - received)
        logger.info("сдвиг часов относительно %s: %d мс", name, skew)
        if abs(skew) > abs(worst):
            worst = skew
    if abs(worst) > cfg.max_clock_skew_ms:
        raise RuntimeError(
            f"часы устройства расходятся с биржей на {worst} мс "
            f"(допустимо {cfg.max_clock_skew_ms}); синхронизируйте время до торговли"
        )
    return worst


# ---------------------------------------------------------------------------
# Скринер
# ---------------------------------------------------------------------------

class Screener:
    def __init__(self, cfg: Config, notifier=None):
        self.cfg = cfg
        self.journal = Journal(cfg.journal_path)
        self.states: dict[str, SymbolState] = {}
        self.instruments: dict[str, dict] = {}
        self.symbols: list[str] = []
        self.skew_ms = 0
        self._stop = asyncio.Event()
        self._fast_cap = max(cfg.natr_period + 2, cfg.uhlo_length * 2 + 2)
        self._slow_cap = cfg.uhlo_length * 2 + 2
        # Инъекция уведомлений: None — не слать. В dry-run и при запуске с
        # Telegram-настройками сюда подставляется TelegramNotifier (notifier.py).
        self.notifier = notifier
        # Очередь решений «закрытая свеча → evaluate»: приём из WS больше не
        # плодит по задаче на бар. Раньше 600 символов закрывали минутные
        # свечи в одну и ту же секунду, и пачка параллельных задач с
        # CPU-работой внутри блокировала цикл событий — отсюда хвосты p95
        # задержки сигнал→исполнение. Теперь решения обрабатывает один
        # воркер в порядке FIFO (порядок по символу сохранён), а recv-цикл
        # остаётся отзывчивым к ping/pong и сторожу тишины.
        self._decisions: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self._decision_worker: asyncio.Task | None = None
        self._dropped_decisions = 0

    def _notify_signal(self, payload: dict) -> None:
        """Отправка уведомления о сигнале (только в dry-run, без ордеров)."""
        if self.notifier is None:
            return
        d = payload.get("diagnostics", {})
        try:
            self.notifier.notify(
                notifier_formats(
                    "signal",
                    symbol=payload.get("symbol"),
                    side=payload.get("side"),
                    price=payload.get("price"),
                    signal_id=payload.get("signal_id"),
                    natr=d.get("natr"),
                    detection_lag_ms=d.get("detection_lag_ms"),
                    uhlo_1m=d.get("uhlo_1m"),
                    uhlo_15m=d.get("uhlo_15m"),
                    color=d.get("color"),
                    candle_start=d.get("candle_start"),
                ))
        except Exception:  # noqa: BLE001 — уведомления не роняют скринер
            logger.warning("не удалось отправить уведомление о сигнале", exc_info=True)

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.skew_ms

    def state(self, symbol: str) -> SymbolState:
        st = self.states.get(symbol)
        if st is None:
            st = SymbolState(self._fast_cap, self._slow_cap)
            self.states[symbol] = st
        return st

    # ── подготовка ─────────────────────────────────────────────────────────

    async def build_universe(self) -> list[str]:
        """Топ по обороту; торгуемость на Testnet проверяется только при require_testnet."""
        tasks = [
            asyncio.to_thread(fetch_instruments, MAINNET_REST),
            asyncio.to_thread(fetch_turnover, MAINNET_REST),
        ]
        if self.cfg.require_testnet:
            tasks.insert(1, asyncio.to_thread(fetch_instruments, TESTNET_REST))
        if self.cfg.cg_max_rank > 0:
            tasks.append(asyncio.to_thread(fetch_cg_top, self.cfg.cg_max_rank))
        results = await asyncio.gather(*tasks)
        mainnet = results[0]
        if self.cfg.require_testnet:
            testnet = results[1]
            turnover = results[2]
            cg_top = results[3] if len(results) > 3 else set()
        else:
            testnet = {}
            turnover = results[1]
            cg_top = results[2] if len(results) > 2 else set()
        self.instruments = mainnet

        ranked = sorted(turnover.items(), key=lambda kv: kv[1], reverse=True)
        selected: list[str] = []
        for i, (symbol, turn) in enumerate(ranked):
            if i < self.cfg.skip_top_volume:
                continue
            info = mainnet.get(symbol)
            if info is None:
                continue
            reason = None
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            if turn < self.cfg.min_turnover_usdt:
                reason = "below_min_turnover"
            elif symbol in self.cfg.blacklist:
                reason = "symbol_blacklisted"
            elif base in self.cfg.base_coin_blacklist:
                reason = "base_coin_blacklisted"
            elif self.cfg.cg_max_rank > 0 and base.upper() not in cg_top:
                reason = "not_in_cg_top"
            elif info["status"] != "Trading":
                reason = "not_trading"
            elif self.cfg.require_testnet:
                if symbol not in testnet:
                    # Самая коварная из отсечённых причин: символ есть на mainnet,
                    # сигнал по нему выглядит нормальным, а ордер на Testnet
                    # обречён — и это попало бы в статистику как ошибка исполнения.
                    reason = "not_on_testnet"
                elif testnet[symbol]["status"] != "Trading":
                    reason = "not_trading_on_testnet"
            if reason is None and info["max_leverage"] < self.cfg.required_leverage:
                reason = "max_leverage_below_required"
            if reason:
                self.journal.write("universe_reject", symbol=symbol, reason=reason,
                                   turnover24h=turn)
                continue
            selected.append(symbol)
            if len(selected) >= self.cfg.top_n_turnover:
                break

        if not selected:
            raise RuntimeError("вселенная пуста — проверьте фильтры конфигурации")
        logger.info("вселенная: %d символов, порог оборота %.0f USDT",
                    len(selected), turnover.get(selected[-1], 0))
        return selected

    async def seed_history(self, symbols: Sequence[str] | None = None) -> None:
        """Перечитывает окна свечей из REST: WS отдаёт только новые свечи.

        Окна ЗАМЕНЯЮТСЯ, а не дополняются. После обрыва в буфере остаётся дыра,
        а push() отбрасывает всё, что старше последней свечи, — доклейка молча
        оставила бы разрывную историю, по которой NATR и UHLO считаются неверно.
        """
        targets = list(symbols if symbols is not None else self.symbols)
        sem = asyncio.Semaphore(self.cfg.seed_concurrency)
        failures = 0

        async def one(symbol: str) -> None:
            nonlocal failures
            async with sem:
                try:
                    fast, slow = await asyncio.gather(
                        asyncio.to_thread(fetch_klines, MAINNET_REST, symbol,
                                          self.cfg.tf_fast, self._fast_cap),
                        asyncio.to_thread(fetch_klines, MAINNET_REST, symbol,
                                          self.cfg.tf_slow, self._slow_cap),
                    )
                except Exception as e:
                    # Символ не теряется: историю доберёт из потока, до тех пор
                    # будет отсекаться как insufficient_history.
                    failures += 1
                    self.journal.write("seed_failed", symbol=symbol, error=str(e))
                    return
                st = self.state(symbol)
                st.fast.clear()
                st.slow.clear()
                for row in fast:
                    st.push("fast", row)
                for row in slow:
                    st.push("slow", row)

        await asyncio.gather(*(one(s) for s in targets))
        logger.info("история прочитана для %d символов, неудач: %d", len(targets), failures)

    # ── поток ──────────────────────────────────────────────────────────────

    def _shards(self) -> list[list[str]]:
        per_conn = max(1, self.cfg.ws_topics_per_conn // 2)  # 2 топика на символ
        return [self.symbols[i:i + per_conn] for i in range(0, len(self.symbols), per_conn)]

    async def run(self) -> None:
        self.skew_ms = await asyncio.to_thread(check_clock_skew, self.cfg)
        self.symbols = await self.build_universe()

        started = self.now_ms()
        self.journal.write("stream_down", cause="startup", symbols=len(self.symbols))
        await self.seed_history()
        self.journal.write("stream_up", cause="startup", duration_ms=self.now_ms() - started,
                           symbols=len(self.symbols))

        shards = self._shards()
        logger.info("подписка: %d символов в %d соединениях", len(self.symbols), len(shards))
        try:
            await asyncio.gather(*(self._shard_loop(i, group)
                                   for i, group in enumerate(shards)))
        finally:
            await self._stop_decision_worker()

    async def _shard_loop(self, index: int, group: list[str]) -> None:
        attempt = 0
        # None, а не текущее время: интервал от старта до первого подключения уже
        # учтён парой stream_down/stream_up с причиной startup. Иначе первое же
        # соединение писало бы ещё один stream_up без парного stream_down, и
        # стартовая пауза попадала бы в сумму «слепого» времени дважды.
        down_since: int | None = None
        fallback: asyncio.Task | None = None

        try:
            while not self._stop.is_set():
                try:
                    async with ws_connect(MAINNET_WS, ping_interval=None,
                                          open_timeout=20, close_timeout=5,
                                          max_queue=2048) as ws:
                        attempt = 0
                        # Подписка раньше перечитывания истории: иначе свечи,
                        # закрывшиеся во время чтения REST, будут потеряны.
                        await self._subscribe(ws, group)
                        if down_since is not None:
                            outage_ms = self.now_ms() - down_since
                            if outage_ms > self.cfg.reseed_after_sec * 1000:
                                logger.info("shard#%d: обрыв %.0f с — перечитываю историю",
                                            index, outage_ms / 1000)
                                await self.seed_history(group)
                            # stream_up пишется только после оформления подписки и
                            # (при необходимости) перечитывания истории: «слепой»
                            # интервал закрывается, когда данные реально пошли,
                            # а не в момент открытия TCP-соединения. Если на этом
                            # участке будет ошибка, down_since не сброшен — и интервал
                            # останется одним непрерывным, без лишней пары down/up.
                            self.journal.write("stream_up", shard=index, symbols=group,
                                               duration_ms=outage_ms, cause="reconnect")
                            down_since = None
                        # Соединение восстановилось — REST-догонялка больше не нужна.
                        if fallback is not None:
                            fallback.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await fallback
                            fallback = None
                        await self._pump(ws, index)
                except Exception as e:
                    if self._stop.is_set():
                        return
                    if down_since is None:
                        down_since = self.now_ms()
                        # Интервал недоступности потока: сигналы, которые могли бы
                        # возникнуть внутри него, не считаются пропущенными.
                        self.journal.write("stream_down", shard=index, symbols=group,
                                           cause="disconnect", error=str(e))
                        # REST-догонялка включается, если обрыв затянется дольше
                        # rest_fallback_after_sec (см. _rest_fallback).
                        if self.cfg.rest_fallback_after_sec > 0 and fallback is None:
                            fallback = asyncio.create_task(
                                self._rest_fallback(group, down_since))
                    attempt += 1
                    delay = min(60.0, 1.0 * 2 ** (attempt - 1)) * (0.7 + 0.6 * random.random())
                    logger.warning("shard#%d: обрыв (%s), переподключение через %.1f с (попытка %d)",
                                   index, e, delay, attempt)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
        finally:
            if fallback is not None:
                fallback.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fallback

    async def _rest_fallback(self, group: list[str], down_since: int) -> None:
        """REST-догонялка на время обрыва WS.

        Включается через rest_fallback_after_sec после разрыва: закрытые свечи
        читаются из /v5/market/kline и прогоняются через ту же очередь решений,
        что и потоковые, — сигналы не теряются, пока соединение лежит. Отключается
        отменой задачи при восстановлении WS (rest_fallback_off в журнале).
        """
        threshold_ms = self.cfg.rest_fallback_after_sec * 1000
        delay_s = (down_since + threshold_ms - self.now_ms()) / 1000
        if delay_s > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay_s)
            if self._stop.is_set():
                return
        self.journal.write("rest_fallback_on", symbols=len(group),
                           downtime_ms=self.now_ms() - down_since)
        logger.warning("WS недоступен дольше %d с — включаю REST-догонялку (%d символов)",
                       self.cfg.rest_fallback_after_sec, len(group))
        sem = asyncio.Semaphore(max(1, self.cfg.seed_concurrency))

        async def one(symbol: str) -> int:
            """Дочитать свечи символа из REST; вернуть число ошибок (0/1)."""
            try:
                async with sem:
                    fast = await asyncio.to_thread(fetch_klines, MAINNET_REST,
                                                   symbol, self.cfg.tf_fast,
                                                   self._fast_cap)
                    slow = await asyncio.to_thread(fetch_klines, MAINNET_REST,
                                                   symbol, self.cfg.tf_slow,
                                                   self._slow_cap)
            except Exception as e:
                logger.warning("REST-догонялка %s: %s", symbol, e)
                return 1
            st = self.state(symbol)
            new_fast = [row for row in fast if st.push("fast", row)]
            for row in slow:
                st.push("slow", row)
            for row in new_fast:
                self._enqueue_decision(symbol, st, row)
            return 0

        try:
            while not self._stop.is_set():
                started = self.now_ms()
                results = await asyncio.gather(*(one(s) for s in group))
                failures = sum(results)
                if failures:
                    self.journal.write("rest_fallback_error", symbols=len(group),
                                       failures=failures)
                pause = max(1.0, self.cfg.rest_fallback_interval_sec -
                            (self.now_ms() - started) / 1000)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=pause)
        finally:
            self.journal.write("rest_fallback_off", symbols=len(group),
                               duration_ms=self.now_ms() - down_since)

    async def _subscribe(self, ws, group: Iterable[str]) -> None:
        topics = []
        for s in group:
            topics.append(f"kline.{self.cfg.tf_fast}.{s}")
            topics.append(f"kline.{self.cfg.tf_slow}.{s}")
        batch = self.cfg.ws_subscribe_batch
        for i in range(0, len(topics), batch):
            await ws.send(json.dumps({"op": "subscribe", "args": topics[i:i + batch]}))

    async def _pump(self, ws, index: int) -> None:
        """Приём сообщений с ping/pong и сторожем тишины.

        Ping уходит каждые ws_ping_sec (< 20 с — требование Bybit). Если после
        отправленного ping в течение ws_pong_timeout_sec не пришло ни одного
        pong, соединение считается мёртвым: поднимается RuntimeError, и шард
        переподключается, не дожидаясь сторожа тишины. Сторож тишины нужен
        именно на Android: после сна устройства соединение часто остаётся
        «открытым», но данные по нему не идут. Без них скринер молча
        ослепнет, а прогон будет выглядеть успешным.
        """
        last_pong = time.monotonic()

        async def pinger() -> None:
            nonlocal last_pong
            while True:
                await asyncio.sleep(self.cfg.ws_ping_sec)
                await ws.send(json.dumps({"op": "ping"}))
                sent_at = time.monotonic()
                await asyncio.sleep(self.cfg.ws_pong_timeout_sec)
                if last_pong < sent_at:
                    raise RuntimeError(
                        f"нет pong {self.cfg.ws_pong_timeout_sec} с — "
                        "соединение мёртвое")

        recv_task: asyncio.Task = asyncio.create_task(ws.recv())
        ping_task: asyncio.Task = asyncio.create_task(pinger())
        try:
            while not self._stop.is_set():
                done, _ = await asyncio.wait(
                    {recv_task, ping_task}, timeout=self.cfg.ws_stale_sec,
                    return_when=asyncio.FIRST_COMPLETED)
                if ping_task in done:
                    # Pinger завершается только исключением («нет pong») —
                    # result() пробрасывает его, и шард переподключается.
                    ping_task.result()
                if recv_task in done:
                    raw = recv_task.result()
                    recv_task = asyncio.create_task(ws.recv())
                    if self._is_pong(raw):
                        last_pong = time.monotonic()
                        continue
                    self._on_message(raw)
                if not done:
                    raise RuntimeError(
                        f"нет сообщений {self.cfg.ws_stale_sec} с — соединение мёртвое")
        finally:
            for t in (recv_task, ping_task):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    @staticmethod
    def _is_pong(raw: str | bytes) -> bool:
        """Дешёвая проверка pong-фрейма до json.loads: это служебные сообщения."""
        if isinstance(raw, bytes):
            return b'"pong"' in raw
        return isinstance(raw, str) and '"pong"' in raw

    def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("op") == "subscribe" and msg.get("success") is False:
            logger.warning("подписка отклонена: %s", msg.get("ret_msg"))
            return
        topic = msg.get("topic")
        if not topic or not msg.get("data"):
            return
        parts = topic.split(".")
        if len(parts) != 3 or parts[0] != "kline":
            return
        _, interval, symbol = parts
        tf = "fast" if interval == self.cfg.tf_fast else "slow" if interval == self.cfg.tf_slow else None
        if tf is None:
            return

        st = self.state(symbol)
        for item in msg["data"]:
            try:
                if item.get("confirm") is not True:
                    continue  # только закрытые свечи
                row = [int(item["start"]), float(item["open"]), float(item["high"]),
                       float(item["low"]), float(item["close"]), float(item.get("volume") or 0)]
                if not row[3] > 0:
                    continue
                is_new = st.push(tf, row)
            except Exception as e:
                # Один битый элемент не должен ронять весь шард: исключение из
                # цикла recv превращалось бы в фиктивный обрыв соединения и
                # лишний «слепой» интервал на 72-часовом прогоне.
                logger.warning("битое сообщение kline %s/%s: %s", symbol, interval, e)
                self.journal.write("error", where="on_message", symbol=symbol, error=str(e))
                continue
            # Решение принимается только на закрытии свечи МЛАДШЕГО ТФ:
            # старший лишь подтверждает направление.
            if is_new and tf == "fast":
                self._enqueue_decision(symbol, st, row)

    def _enqueue_decision(self, symbol: str, st: SymbolState, row: list) -> None:
        """Поставить решение в очередь без блокировки recv-цикла (put_nowait).

        Воркер запускается лениво при первой свече. Переполнение очереди
        (симптом деградации, а не штатный режим) фиксируется в журнале:
        свеча пропускается осознанно, вместо лавинообразного роста памяти.
        """
        if self._decision_worker is None or self._decision_worker.done():
            self._decision_worker = asyncio.get_running_loop().create_task(
                self._decision_worker_loop())
        try:
            self._decisions.put_nowait((symbol, st, row))
        except asyncio.QueueFull:
            self._dropped_decisions += 1
            logger.error("очередь решений переполнена: отброшено %d",
                         self._dropped_decisions)
            self.journal.write("decision_dropped", symbol=symbol,
                               dropped_total=self._dropped_decisions)

    async def _decision_worker_loop(self) -> None:
        """Единственный обработчик очереди решений: FIFO без гонок.

        Раньше на каждую закрытую свечу плодилась задача evaluate, и пачка
        минутных закрытий (600 символов в одну секунду) блокировала цикл
        событий; сериализация в одном воркере убирает эти всплески задержки.
        """
        while True:
            symbol, st, row = await self._decisions.get()
            try:
                await self._on_fast_close(symbol, st, row)
            except Exception as e:  # noqa: BLE001 — ошибка одного бара не убивает воркер
                logger.error("ошибка обработки закрытой свечи %s", symbol, exc_info=e)
                self.journal.write("error", where="on_fast_close",
                                   symbol=symbol, error=str(e))
            finally:
                self._decisions.task_done()

    async def _stop_decision_worker(self) -> None:
        w = self._decision_worker
        if w is not None:
            w.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await w
            self._decision_worker = None

    # ── решение и отправка ─────────────────────────────────────────────────

    async def _on_fast_close(self, symbol: str, st: SymbolState, trigger: list) -> None:
        ts = self.now_ms()
        fast = list(st.fast)
        slow = list(st.slow)
        d = evaluate(fast, slow, self.cfg)

        # Подтверждение 2 баров: цвет предыдущего закрытого бара засчитывается
        # только если тот полностью прошёл evaluate(); любой не-проход обнуляет
        # серию. Обновление состояния — ДО ранних выходов, чтобы серия чётко
        # отражала последний бар.
        prev_color = st.prev_color
        st.prev_color = d.color if d.passed else "none"

        if not d.passed:
            diag = dict(d.details)
            if d.natr is not None:
                diag["natr"] = round(d.natr, 3)
            if d.uhlo_fast is not None:
                diag["uhlo_fast"] = {k: round(v, 1) for k, v in d.uhlo_fast.items()}
            if d.uhlo_slow is not None:
                diag["uhlo_slow"] = {k: round(v, 1) for k, v in d.uhlo_slow.items()}
            self._log_reject(symbol, d.reason, ts, diag)
            if d.reason in ("uhlo_no_color", "uhlo_slow_missing"):
                # Сброс цвета — то, чего не хватало в исходной версии: без него
                # монета, побывавшая в none, больше никогда не выдаёт сигнал.
                st.last_color = "none"
            return

        if d.color == st.last_color:
            self._log_reject(symbol, "repeat_color", ts, {"color": d.color})
            return

        if prev_color != d.color:
            # Первый бар серии: состояние есть, но подтверждения ещё нет.
            self._log_reject(symbol, "no_confirm", ts,
                             {"color": d.color, "prev_color": prev_color})
            return

        if ts - st.last_signal_ms < self.cfg.cooldown_sec * 1000:
            # Цвет не фиксируем: после паузы сигнал должен состояться.
            self._log_reject(symbol, "cooldown", ts,
                             {"since_last_signal_ms": ts - st.last_signal_ms})
            return

        interval_ms = int(self.cfg.tf_fast) * 60_000
        payload = {
            "source": "local_screener",
            "signal_id": f"{symbol}:{self.cfg.tf_fast}:{trigger[0]}",
            "symbol": symbol,
            "side": color_to_side(d.color),
            "mode": "DCA",
            # Цена и метка времени — без них бот не посчитает ни задержку,
            # ни проскальзывание. Цена mainnet; свою Testnet-цену на момент
            # получения бот пишет сам, тогда проскальзывание разложимо на
            # базис контуров и собственно исполнение.
            "price": trigger[4],
            "price_venue": "bybit_mainnet",
            "ts": ts,
            "diagnostics": {
                "natr": round(d.natr, 4) if d.natr is not None else None,
                "uhlo_1m": d.uhlo_fast,
                "uhlo_15m": d.uhlo_slow,
                # Сырые значения LuxAlgo-UHLO (без инверсии): прямая сверка
                # с индикатором «Unreached Highs/Lows [LuxAlgo]» на TV,
                # где красная линия = Unreached Highs, зелёная = Unreached Lows.
                "uhlo_raw": {
                    "1m": {"unreached_highs": round(100 - d.uhlo_fast["highs"], 1),
                           "unreached_lows": round(100 - d.uhlo_fast["lows"], 1)},
                    "15m": {"unreached_highs": round(100 - d.uhlo_slow["highs"], 1),
                            "unreached_lows": round(100 - d.uhlo_slow["lows"], 1)},
                },
                "color": d.color,
                "candle_start": trigger[0],
                "detection_lag_ms": ts - (trigger[0] + interval_ms),
            },
        }

        if self.cfg.dry_run:
            # Paper-режим: без POST и без ордеров. Состояние продвигается
            # как при успешной доставке — иначе dry-run задыхался бы на
            # повторных сигналах того же цвета.
            st.last_color = d.color
            st.last_signal_ms = ts
            self.journal.write("signal_dry_run", status="dry_run", signal=payload)
            logger.info("dry-run сигнал %s %s natr=%.2f lag=%d мс",
                        symbol, payload["side"], d.natr or 0.0,
                        payload["diagnostics"]["detection_lag_ms"])
            self._notify_signal(payload)
            return

        # Доставка боту — запись в журнал: executor.py читает
        # logs/screener-events.jsonl напрямую и сам исполняет ордера.
        # HTTP-POST к bot_api_url — уведомление поверх журнала; его отказ
        # не теряет сигнал, поэтому состояние продвигается безусловно.
        st.last_color = d.color
        st.last_signal_ms = ts
        self.journal.write("signal_sent", status="sent", signal=payload)
        logger.info("сигнал %s %s natr=%.2f lag=%d мс",
                    symbol, payload["side"], d.natr or 0.0,
                    payload["diagnostics"]["detection_lag_ms"])
        ok, info = await asyncio.to_thread(self._post_signal, payload)
        if not ok:
            logger.warning("HTTP-уведомление бота не доставлено "
                           "(сигнал уже в журнале): %s", info)

    def _post_signal(self, payload: dict) -> tuple[bool, str]:
        url = f"{self.cfg.bot_api_url}/signal"
        last = ""
        for attempt in range(self.cfg.post_retries + 1):
            if attempt:
                time.sleep(min(2.0, 0.2 * 2 ** attempt))
            try:
                r = requests.post(url, json=payload, timeout=5)
                if 200 <= r.status_code < 300:
                    return True, r.text[:200]
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.RequestException as e:
                last = str(e)
        return False, last

    def _log_reject(self, symbol: str, reason: str, ts: int, details: dict) -> None:
        mode = self.cfg.reject_log
        if mode == "none":
            return
        if mode == "candidates" and reason in NOISE_REASONS:
            return
        self.journal.write("reject", symbol=symbol, reason=reason, at=ts, details=details)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def amain(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="DCA-скринер Bybit (WS mainnet, сигналы → бот/журнал/Telegram)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/config.yml",
                    help="путь к config.yml (по умолчанию config/config.yml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="paper-режим: сигналы в журнал (signal_dry_run) + Telegram "
                         "вместо POST на bot_api_url; ордера не выставляются")
    ap.add_argument("--telegram-test", action="store_true",
                    help="отправить тестовое сообщение в Telegram из секции "
                         "telegram config.yml и выйти")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logger.info("скринер запущен: NATR %.2f..%.2f (период %d), UHLO %d, ТФ %s/%s, топ-%d по обороту, "
                "пропуск первых %d по объёму",
                cfg.natr_min, cfg.natr_max, cfg.natr_period, cfg.uhlo_length,
                cfg.tf_fast, cfg.tf_slow, cfg.top_n_turnover, cfg.skip_top_volume)

    notifier = _build_notifier_from_config(args.config)
    if args.telegram_test:
        if notifier is None:
            logger.error("telegram-секция не настроена (enabled=false или пустой токен) — "
                         "тест невозможен")
            return 1
        ok_ = notifier.send_message(notifier_formats("telegram_test"))
        notifier.shutdown(wait=True)
        logger.info("telegram-тест: %s", "отправлено" if ok_ else "не доставлено")
        return 0 if ok_ else 1

    if args.dry_run:
        cfg.dry_run = True
        if notifier is not None:
            logger.info("dry-run: сигналы → журнал + Telegram (без ордеров)")
        else:
            logger.info("dry-run: сигналы → журнал (Telegram не настроен)")
    elif notifier is not None:
        notifier.shutdown(wait=False)

    screener = Screener(cfg, notifier=notifier)
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, screener.stop)

    start_ts = time.time()
    logger.info("тест начат: %s (Киев)", _fmt_kyiv_ms(int(start_ts * 1000)))
    logger.info("тест план: окончание %s (72 ч от старта)",
                _fmt_kyiv_ms(int(start_ts * 1000) + TEST_DURATION_HOURS * 3_600_000))

    try:
        await screener.run()
        return 0
    except Exception:
        logger.exception("скринер остановлен из-за ошибки")
        return 1
    finally:
        stop_ts = time.time()
        logger.info("тест завершён: %s (Киев), длительность %.0f мин",
                    _fmt_kyiv_ms(int(stop_ts * 1000)), (stop_ts - start_ts) / 60)
        screener.journal.close()
        if notifier is not None:
            notifier.shutdown(wait=False)


def _read_flat_section(path: str, section: str) -> dict:
    """Плоская секция из YAML-подобного файла без pyyaml (как bot_config.read_section).

    Для telegram-настроек: ключи bot_token/chat_id/parse_mode — строки,
    enabled — bool. Списки не нужны.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {}
    out: dict = {}
    in_section = False
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip(" ")) == 0 and stripped.endswith(":"):
            in_section = stripped == f"{section}:"
            continue
        if not in_section:
            continue
        if "#" in line:
            line = line.split("#", 1)[0].rstrip()
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = _parse_scalar(value)
    return out


def _parse_scalar(value: str):
    v = value.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        v = v[1:-1]
    if v == "":
        return ""
    if v == "true":
        return True
    if v == "false":
        return False
    return v


def _build_notifier_from_config(config_path: str):
    """TelegramNotifier из секции telegram config.yml; None — не настроен."""
    try:
        import importlib.util as _iu

        _dir = os.path.dirname(os.path.abspath(__file__))
        spec = _iu.spec_from_file_location("notifier_for_screener",
                                           os.path.join(_dir, "notifier.py"))
        assert spec and spec.loader
        nt = _iu.module_from_spec(spec)
        sys.modules["notifier_for_screener"] = nt
        spec.loader.exec_module(nt)
    except Exception:  # noqa: BLE001
        logger.warning("notifier.py недоступен — уведомления выключены", exc_info=True)
        return None

    try:
        import yaml  # локальный импорт: без конфига не нужен

        with open(config_path) as f:
            raw = (yaml.safe_load(f) or {}).get("telegram", {}) or {}
    except Exception:  # noqa: BLE001
        raw = _read_flat_section(config_path, "telegram")

    try:
        params = nt.telegram_params_from_config(raw)
        if not params.enabled:
            logger.info("telegram-уведомления выключены (enabled=false)")
            return None
        return nt.TelegramNotifier(params)
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram-настройки некорректны: %s", e)
        return None


def notifier_formats(what: str, **kw) -> str:
    """Лёгкий мост к форматтерам notifier для CLI без жёсткого импорта."""
    try:
        import importlib.util as _iu

        _dir = os.path.dirname(os.path.abspath(__file__))
        spec = _iu.spec_from_file_location("notifier_formats_mod",
                                           os.path.join(_dir, "notifier.py"))
        assert spec and spec.loader
        nt = _iu.module_from_spec(spec)
        sys.modules["notifier_formats_mod"] = nt
        spec.loader.exec_module(nt)
        if what == "telegram_test":
            return nt.format_telegram_test()
        if what == "signal":
            return nt.format_signal(
                symbol=kw.get("symbol"), side=kw.get("side"),
                price=kw.get("price"), signal_id=kw.get("signal_id"),
                natr=kw.get("natr"), detection_lag_ms=kw.get("detection_lag_ms"),
                uhlo_1m=kw.get("uhlo_1m"), uhlo_15m=kw.get("uhlo_15m"),
                color=kw.get("color"), candle_start=kw.get("candle_start"))
        return str(what)
    except Exception:  # noqa: BLE001
        return "🔔 Тест уведомлений: скринер работает"


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
