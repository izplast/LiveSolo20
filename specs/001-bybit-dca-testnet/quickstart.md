# Quickstart: валидация связки на Testnet

**Branch**: `001-bybit-dca-testnet` | **Date**: 2026-08-05

Проверяемые сценарии из spec.md (US1–US4) и способы их прогнать без боевого счёта.

## 0. Автотесты без сети (обязательно перед прогоном)

```bash
cd specs/001-bybit-dca-testnet/reference
python3 test_screener.py      # 48 проверок скринера
python3 test_report.py        # 41 проверка сводки
python3 test_pricing.py       # 25 проверок квантования цены
python3 test_bot.py           # режимы ордеров, set_take_profit, set_leverage_once
python3 test_ws.py            # WS-цикл: подписка, приём, reconnect
```

Все должны завершиться с `0 fail`; сеть не используется.

## 1. Логика сигнала (US1) — без сети

- `test_screener.py`: фикстура роста 1м+15м → сигнал green → POST /signal с полным конвертом;
  отсечения по волатильности и расхождению тренда; подавление повтора на неизменном цвете.
- `test_bot.py`: из сигнала бот строит order request: режим entry, размер и цена приведены к
  шагам, плечо выставляется один раз, TP-уровень считается от средней цены.

## 2. Полный цикл DCA (US2) — логика в `reference/bot.py` + `test_bot.py`

Сценарий: первый вход → докупка (пересчёт avg_entry и TP) → закрытие по тейку / по времени.
Проверяется: режимы ордеров ENTRY/ADJUST/CLOSE, `set_take_profit` даёт reduce-only TP-ордер на
встречной стороне с ценой, квантованной к шагу, `set_leverage_once` вызывает API один раз на символ.

## 3. Непрерывная работа 72 ч (US3) — WS-цикл

- `test_ws.py`: подписка (топики по 2 на символ, пакеты ≤ 10), приём закрытых свечей
  (`confirm=true`) с дедупликацией, сторож тишины, переподключение с `stream_down`/`stream_up`.
- Реальный прогон (только на устройстве): `python3 core/screener.py` в tmux под `termux-wake-lock`,
  вселенная `top_n_turnover: 20` на 15–20 минут, затем полный 72-часовой прогон.

## 4. Сводка (US4)

```bash
python3 tools/report.py logs/screener-events.jsonl logs/bot-events.jsonl
python3 tools/report.py logs/*.jsonl --json --since 2026-08-01T00:00:00
```

Ожидается: медиана и p95 задержки и проскальзывания, базис контуров отдельно, счётчики
сигналов по статусам, циклы по причине выхода, «слепые» интервалы, вердикт по SC-001…SC-005.

Форматы событий и сигнала — в `contracts/journal.md` и `contracts/signal.md`; сущности и
переходы состояний — в `data-model.md`.
