# Bybit DCA-бот + скринер (Testnet)

Фьючерсный DCA-бот для Bybit USDT Perpetual с собственным скринером на
WebSocket-потоке. Скринер ищет монеты с волатильностью 0.9–2.5% (1 мин),
подтверждённой трендом на 15 мин, и передаёт боту тикер. Бот применяет единые
параметры DCA (первый вход, шаг усреднения, число докупок, тейк-профит) и
фиксированное плечо.

Первая цель — связка «бот + скринер» на **Bybit Testnet**: 72 ч непрерывной
работы без пропущенных сигналов, задержка сигнал→ордер ≤ 2 с, проскальзывание
0.3–0.5% (начальные ориентиры для калибровки).

## Статус: ✅ Phase 10 closed — готов к боевому прогону

Все 11 фаз бэклога закрыты, автономная test-база без сети — **995/995 passed, 0 fail**.

| Фаза | Что закрыто |
|------|-------------|
| Phase 1–7 (MVP) | инфраструктура, WS-скринер, вход с плечом, докупки и TP, 72 ч, сводка прогона |
| Phase 8 | A/B-инструменты, единый автораннер, графики по эквити/просадкам |
| Phase 9 | синтетические журналы, экспорт сделок, лимиты бэктеста, история прогонов, расширенные метрики, валидатор конфига, сводка по символам |
| Phase 10 (Live) | сверка состояния с биржей, backoff + circuit breaker, Telegram-уведомления, ключи фильтрации прод-скринера |

## Запуск тестов

```bash
python3 tools/run_tests.py            # автономные reference-тесты + config + синтетические журналы
python3 tools/run_tests.py --journal  # дополнительно анализ живых журналов (SC-001..SC-005)
```

Ожидаемый итог: `0 fail` (юнит-секция `reference/test_*.py`,
без сети, только stdlib).

## Управление live-процессами (runsv, Termux)

Live-процессы `bot.py`, `executor.py` и `live_screener_midcap.py` управляются
через **runsv**, не вручную. Сервисы:

- `midcap-bot` — бот + исполнитель;
- `midcap-screener` — live-скринер.

```bash
sv status midcap-bot midcap-screener
sv restart midcap-bot
sv restart midcap-screener
```

Не запускать процессы вручную параллельно с runsv — дубли дают двойные ордера
на одном демо-счёте. Проверка: `pgrep -a python3`.

## Запуск исполнителя (реальные DCA-ордера, Bybit DEMO)

`executor.py` — бот-исполнитель: читает новые сигналы из `logs/screener-events.jsonl`
(ключ `screener.journal_path`), дедуплицирует их по `signal_id`, проверяет лимиты
и выставляет реальные DCA-ордера на **Bybit DEMO**, затем сопровождает циклы:
докупки по лестнице Мартингейла, поуровневый тейк-профит, стоп и выход по времени.

```bash
pip install requests                          # единственная зависимость бота

# Проверка конфига и чистой логики
python3 executor.py --check-config
python3 executor.py --self-test
```

Перед `--live`:
1. Задать `BYBIT_API_KEY`/`BYBIT_API_SECRET` через окружение
   (`/root/.bybit_demo_env`, вне репо) — ключи в файл не писать.
2. Скринер должен писать `signal_sent` в `logs/screener-events.jsonl`.
3. Сигналы старее `--max-signal-age` (по умолчанию 300 с = кулдаун) пропускаются —
   при первом прогоне по историческому журналу передайте `--max-signal-age 0`.

События бота — `logs/bot-events.jsonl` (`cycle_opened`/`cycle_closed`/
`order_filled`/`signal_rejected`/`reconcile`), стейт (offset, обработанные
сигналы, открытые циклы) — `logs/bot-state.json`. Сводка по прогону:
`python3 tools/report.py logs/screener-events.jsonl logs/bot-events.jsonl`.
При старте бот сверяет локальный стейт с биржей и закрывает циклы, которых нет
на бирже (exchange_take_profit). Расхождение часов с Testnet больше
`bot.max_clock_skew_ms` запрещает старт (FR-032, обход — `--force`).

## Структура

```
tools/                          # утилиты поверх reference
  run_tests.py                  # единый автораннер + история прогонов (T020/T025)
  stats_summary.py              # сводка и графики по символам (T028)
executor.py                     # бот-исполнитель: журнал скринера → ордера Testnet
specs/001-bybit-dca-testnet/
  spec.md                       # спецификация + расширения по фазам
  tasks.md                      # бэклог всех фаз (все [x])
  contracts/                    # signal.md, journal.md, data-model.md
  reference/                    # реализация-эталон (stdlib, тестируется без сети)
    bot.py, bot_config.py, screener.py, backtest.py, dca_cycle.py,
    pricing.py, report.py, report_charts.py, resilience.py, notifier.py,
    reconcile.py, metrics.py, cycle_journal.py, ab_*.py, run_sim.py,
    journal_sim.py, scan_universe.py, book_streamer.py, mock_execution.py
config/config.yml               # dca / bot / screener / telegram-секции
logs/                           # журналы и история прогонов (в .gitignore)
```

## Перед боевым прогоном

1. Проверить секцию `telegram` в `config/config.yml` (T031): включить `enabled`,
   указать `bot_token`/`chat_id`, чтобы получать уведомления о входах/выходах,
   TP/SL и критических ошибках API.
2. Прогнать `python3 tools/run_tests.py` — итог `0 fail`.
3. Убедиться, что `sv status midcap-bot midcap-screener` показывает `run`.
4. Калибровать пороги волатильности/проскальзывания по результатам DEMO.