# Implementation Plan: DCA-бот + скринер для Bybit USDT Perpetual (прогон на Testnet)

**Branch**: `001-bybit-dca-testnet` | **Date**: 2026-08-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-bybit-dca-testnet/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Связка из двух автономных процессов: скринер, который на потоке Bybit mainnet отбирает
USDT-перпетуалы с волатильностью NATR-14 в диапазоне 0.9–2.5% и подтверждённым направлением
UHLO-15 на 1м и 15м, и DCA-бот, который исполняет сигналы на Testnet (первый вход, докупки
с пересчётом средней цены и уровня тейк-профита, закрытие по тейку или по времени). Цель
этапа — измерить задержку сигнал→исполнение и проскальзывание в 72-часовом прогоне, чтобы
заменить начальные ориентиры (2 с, 0.3–0.5%) подтверждёнными порогами.

Технический подход: событийная обработка закрытых свечей из WebSocket (а не REST-опрос),
квантование цен/размеров к шагам инструмента до отправки ордера, JSONL-журнал обоих
процессов как единственный источник истины для сводки, автономные референс-реализации с
автотестами без сети (заглушки вместо requests/websockets).

## Technical Context

**Language/Version**: Python 3.10+ (ref-код проверяется на Python 3.14; Termux: Python 3.11+). Асинхронный код через `asyncio`.

**Primary Dependencies**: `websockets` (WS-поток Bybit v5), `requests` (REST: instruments-info, tickers, kline, market/time), `pyyaml` (опционально, для config/config.yml). Всё доступно в Termux.

**Storage**: локальные JSONL-журналы `logs/screener-events.jsonl` и `logs/bot-events.jsonl` — машиночитаемые, единственный источник для сводки. Биржа — источник истины по позициям (FR-028).

**Testing**: автономные скрипты `python3 specs/001-bybit-dca-testnet/reference/test_*.py` без pytest и без сети; внешние зависимости подменяются заглушками до импорта модуля. Текущее покрытие: `test_screener.py` (48), `test_report.py` (41), `test_pricing.py` (25), `test_bot.py` (новый), `test_ws.py` (новый).

**Target Platform**: Termux на Android (Linux), процесс под `termux-wake-lock`. Основной контур исполнения — Bybit Testnet; чтение рыночных данных — публичный поток mainnet (FR-001, FR-031).

**Project Type**: распределённая связка из двух процессов (скринер + бот) с HTTP-контрактом сигнала; референс-реализации живут в `specs/001-bybit-dca-testnet/reference/`, рабочая копия — в `~/bybit-dca-bot` (Termux-хост).

**Performance Goals**: задержка сигнал→исполнение ≤ 2 с для 95% исполнений; восстановление потока ≤ 30 с после восстановления сети; потребление приемлемо для телефона при вселенной ~300 символов (топиков ~600, пакетов подписки по 10).

**Constraints**: только Testnet для ордеров (отказ старта при боевом контуре), не более 3 одновременных DCA-циклов, единые параметры DCA и плеча для всех инструментов, журнал без секретов, отсутствие стоп-лосса по цене (выход по времени, 4 ч), обрыв сети — норма и должен переживаться без потери сигналов.

**Scale/Scope**: вселенная ~300 символов, ~1440 сигналов/день потенциально, 72-часовой непрерывный прогон, 3 параллельных цикла.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Конституция проекта — шаблон (`/root/my-project/.specify/memory/constitution.md`), принципы не ратифицированы владельцем. Применяются принципы, зафиксированные в спеке и в `reference/CHANGES.md`:

1. **Автономность и воспроизводимость**: вся логика, проверяемая без сети, обязана иметь автотест в `reference/`; тесты — скрипты без pytest, запускаемые в Termux.
2. **Единый источник истины — журнал**: решения и причины записываются в JSONL; сводка строится только из журнала, а не из внутреннего состояния процессов.
3. **Идемпотентность и симметрия**: повторная попытка не создаёт двойного входа; шорт-логика симметрична лонгу (FR-030).
4. **Безопасность контура**: ордера — только Testnet (FR-031), секреты в журнал не пишутся (FR-024).

Нарушений не выявлено; Complexity Tracking не заполняется.

## Project Structure

### Documentation (this feature)

```text
specs/001-bybit-dca-testnet/
├── plan.md              # Этот файл
├── research.md          # Phase 0: разрешение неизвестных (см. Assumptions spec.md)
├── data-model.md        # Phase 1: сущности и состояния
├── quickstart.md        # Phase 1: сценарий валидации прогона
├── contracts/           # Phase 1: контракт сигнала и контракт журнала
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (репозиторий)

```text
specs/001-bybit-dca-testnet/reference/
├── screener.py          # Скринер: WS-цикл, индикаторы, вселенная, журнал (реализован)
├── report.py            # Сводка по журналам (реализован)
├── pricing.py           # Квантование цены к шагу биржи (реализован)
├── bot.py               # Логика ордеров DCA-бота: режимы ENTRY/ADJUST/CLOSE,
│                        #   set_take_profit, set_leverage_once (НОВОЕ)
├── test_screener.py     # 48 проверок скринера (сеть заглушена)
├── test_report.py       # 41 проверка сводки
├── test_pricing.py      # 25 проверок квантования
├── test_bot.py          # Проверки режимов ордеров и установки TP/плеча (НОВОЕ)
└── test_ws.py           # Проверки WS-цикла: подписка, приём, reconnect (НОВОЕ)

~/bybit-dca-bot/         # Рабочая копия на Termux-хосте (вне этого репозитория)
├── core/bot.py
├── core/monitor.py
├── core/screener.py     # = reference/screener.py
└── tools/report.py      # = reference/report.py
```

**Structure Decision**: выбран формат «spec-репозиторий с reference/». Логика, которую можно
проверить без сети, реализуется автономными модулями в `reference/` с тестами-скриптами;
боевая копия зеркалируется на Termux-хост. Сетевые вызовы наружу не тестируются (см. раздел
«Что осталось непроверенным» в CHANGES.md).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

Нарушений нет — таблица не заполняется.
