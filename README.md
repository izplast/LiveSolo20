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

Все 11 фаз бэклога закрыты, автономная test-база без сети — **966/966 passed, 0 fail**.

| Фаза | Что закрыто |
|------|-------------|
| Phase 1–7 (MVP) | инфраструктура, WS-скринер, вход с плечом, докупки и TP, 72 ч, сводка прогона |
| Phase 8 | A/B-инструменты, единый автораннер, графики по эквити/просадкам |
| Phase 9 | синтетические журналы, экспорт сделок, лимиты бэктеста, история прогонов, расширенные метрики, валидатор конфига, сводка по символам |
| Phase 10 (Live) | сверка состояния с биржей, backoff + circuit breaker, **Telegram-уведомления** |

## Запуск тестов

```bash
python3 tools/run_tests.py            # все автономные тесты reference/ + анализ журналов
```

Ожидаемый итог: `966 ok, 0 fail` (юнит-секция `reference/test_*.py`,
22 файла — без сети, только stdlib).

## Структура

```
tools/                          # утилиты поверх reference
  run_tests.py                  # единый автораннер + история прогонов (T020/T025)
  stats_summary.py              # сводка и графики по символам (T028)
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
2. Прогнать `python3 tools/run_tests.py` — итог `966 ok, 0 fail`.
3. Калибровать пороги волатильности/проскальзывания по результатам Testnet.