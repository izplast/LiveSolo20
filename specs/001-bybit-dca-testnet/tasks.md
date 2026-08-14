# Tasks: DCA-бот + скринер для Bybit USDT Perpetual (прогон на Testnet)

**Input**: Дизайн-документы из `/specs/001-bybit-dca-testnet/`

**Prerequisites**: plan.md (готов), spec.md (готов), research.md, data-model.md, contracts/, quickstart.md (готовы)

**Tests**: Тесты — автономные скрипты `python3 specs/001-bybit-dca-testnet/reference/test_*.py` без pytest и без сети (по конвенции репозитория).

**Organization**: Задачи сгруппированы по user stories. Реализуются сейчас задачи отмеченные `▸`: режимы ордеров CLOSE/ADJUST, `set_take_profit`/`set_leverage_once`, WS-цикл скринера.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: можно параллельно (разные файлы, без зависимостей)
- **[Story]**: user story (US1–US4)
- Точные пути файлов в описаниях

---

## Phase 1: Setup (общая инфраструктура)

**Purpose**: артефакты плана и автономный референс-слой, на котором строятся обе user story.

- [x] T001 Сгенерировать план-артефакты: plan.md, research.md, data-model.md, contracts/signal.md, contracts/journal.md, quickstart.md в specs/001-bybit-dca-testnet/
- [x] T002 Квантование цены к шагу биржи: pricing.py + test_pricing.py в specs/001-bybit-dca-testnet/reference/ (FR-017)

---

## Phase 2: Foundational (блокирующие предпосылки)

**Purpose**: база, без которой не работают US1–US3. Скринер уже реализован (reference/screener.py); добиваем покрытие WS-цикла.

- [x] T003 [P] WS-цикл скринера в reference/screener.py: подписка (пакеты ≤ 10 топиков), приём закрытых свечей (confirm=true), сторож тишины, переподключение с exponential backoff, stream_down/stream_up (FR-001, FR-025, FR-027)
- [x] T004 [P] Тесты WS-цикла в reference/test_ws.py: подписка, _pump/_on_message, reconnect через заглушку websockets (без сети)

**Checkpoint**: WS-цикл покрыт тестами; сигналы из потока доходят до решения.

---

## Phase 3: User Story 1 — Сигнал → измеренный вход (P1) 🎯 MVP

**Goal**: сигнал скринера превращается в первый вход DCA на Testnet с журналом задержки и проскальзывания.

**Independent Test**: из синтетического сигнала бот строит корректный order request (режим entry, размер и цена к шагам, плечо один раз) — проверяется в reference/test_bot.py без сети.

### Implementation for User Story 1

- [x] T005 [P] [US1] `set_leverage_once` в reference/bot.py: установка фиксированного плеча ровно один раз на символ до первого входа (FR-018, FR-008)
- [x] T006 [P] [US1] Первый вход в reference/bot.py: order request mode=entry, размер/цена приведены к qty_step/tick_size (FR-009, FR-017)
- [x] T007 [US1] Тесты первого входа и плеча в reference/test_bot.py (сетевые вызовы — инъекция-заглушка)

**Checkpoint**: первый вход воспроизводимо строится из сигнала.

---

## Phase 4: User Story 2 — Полный цикл DCA (P2)

**Goal**: докупки с пересчётом средней цены и уровня TP, закрытие по тейку или по времени.

**Independent Test**: после «докупки» уровень TP пересчитан от новой средней цены и переносится (ADJUST); TP-ордер — reduce-only на встречной стороне (CLOSE). Проверяется в reference/test_bot.py.

### Implementation for User Story 2

- [x] T008 [P] [US2] Режимы ордеров mode ENTRY/ADJUST/CLOSE в reference/bot.py: как ордер влияет на позицию, reduce_only для close/adjust (FR-012, FR-014)
- [x] T009 [P] [US2] `set_take_profit` в reference/bot.py: TP-уровень от средней цены входа (FR-011), квантован к tick_size, встречная сторона, повторный вызов → ADJUST
- [x] T010 [US2] Тесты режимов и TP в reference/test_bot.py (включая пересчёт после докупки и шорт-симметрию)

**Checkpoint**: цикл «вход → докупки → закрытие» воспроизводим в логике.

---

## Phase 5: User Story 3 — Непрерывная работа 72 ч (P3)

**Goal**: связка переживает обрывы сети и восстанавливается без потери сигналов.

**Independent Test**: тесты WS-цикла T004 (reconnect, сторож тишины, «слепые» интервалы); реальный 72-часовой прогон — вне автотестов, на устройстве.

- [x] T011 [US3] Переподключение и «слепые» интервалы в reference/screener.py (stream_down/stream_up, reseed после обрыва ≥ 90 с) — покрыто T004

**Checkpoint**: WS-цикл восстанавливается после обрыва; интервалы недоступности фиксируются в журнале.

---

## Phase 6: User Story 4 — Сводка по прогону (P4)

**Goal**: сводка для замены начальных ориентиров подтверждёнными порогами.

**Independent Test**: test_report.py строит сводку по синтетическому журналу (реализовано).

- [x] T012 [P] [US4] Сводка в reference/report.py: медиана/p95 задержки и проскальзывания, базис контуров отдельно, счётчики статусов, «слепые» интервалы, вердикты SC (FR-023, SC-009)

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: сквозная проверка без сети перед прогоном.

- [x] T013 Прогнать все автотесты reference/: test_screener.py, test_report.py, test_pricing.py, test_bot.py, test_ws.py, test_backtest.py — 0 fail
- [x] T014 Обновить reference/CHANGES.md и quickstart.md описанием бот-модуля и WS-тестов
- [x] T015 Автотест бэктеста в reference/test_backtest.py (синтетические свечи, без сети): DcaParams.validate, read_csv, aggregate_minutes, пагинация/кэш klines, fetch_instrument/fetch_universe через заглушку _http_get, полный цикл entry→dca→TP (лонг и шорт), time_exit, стоп, лимит циклов (FR-015), summarize/render, CLI --csv/--json — 93 проверки, 0 fail
- [x] T016 Полный сквозной пайплайн в reference/test_full_pipeline.py (синтетические свечи и стакан, без сети): скринер (бычий сигнал на тренде → Buy) → выставление сетки (рыночный вход + TP-лимитка + докупка so_1) → исполнение в mock-симуляторе (доливки на падающем стакане, пересчёт TP по эскалации) → закрытие по TP на отскоке (US1, US2, FR-009/010/011/012, FR-017) — 20 проверок, 0 fail
- [x] T017 Связка «скринер → симулятор» в reference/run_sim.py (без сети): направление сделки выбирает скринер (screener_side — последний валидный цвет, повтор цвета не сигнал), восстановление минутных свечей из снимков стакана (books_to_rows), диагностика отсутствия сигнала (screener_reason: insufficient_history/NATR/цвет), разбор секции screener из config.yml без pyyaml (load_screener_cfg), CLI --side auto|Buy|Sell, --candles, --seed-hours, --universe N; reference/test_run_sim.py — 18 проверок, 0 fail
- [x] T018 Накопление сделок в reference/scan_universe.py: скан всего топа из конфига (top_n_turnover, 600) за --days дней, параллельная загрузка klines (--workers 8, кэш, окно заякорено на начало UTC-суток), скринер → Backtest (параметры бота из config.yml), стоп при --target закрытых сделок, сделки в --out JSONL. Прогон: 67/600 символов, 32 закрытые сделки, win-rate 96.9%, PnL +8.81 USDT — цель 30 сделок достигнута

---

## Phase 8: Experimentation Tooling (расширение)

**Purpose**: формализация экспериментальных инструментов (A/B, автораннер, визуализация) как части архитектуры проекта.

- [x] T019 Формализовать инструменты A/B-тестирования и грид-свипа (reference/ab_sl.py, reference/ab_grid.py) в архитектуру проекта: общий каркас reference/ab_common.py (метрики, parse_params/parse_grid, coerce, apply_overrides, aggregate, render_table, rank_results/take_top, resolve_combos, resolve_window, load_data, run_combos); единый CLI обоих скриптов (--params, --grid, --metric/--sort, --top-k, --out/--json-out, --no-baseline) с выводом итоговых метрик и ранжирования; описание в spec.md и data-model.md, раздел в reference/CHANGES.md; reference/test_ab_tools.py — 89 проверок, 0 fail
- [x] T020 Интегрировать единый автораннер tools/run_tests.py для комплексной проверки юнит-тестов reference/ и анализа логов Testnet: прогон всех test_*.py, сводка по фазам, разбор JSONL-журналов cycle_journal/metrics, отчёт об ошибках и простоях; валидация config.yml против логики бэктеста/бота (dca → DcaParams, screener → Config, согласованность leverage/NATR); журналы → SC-001..SC-005 через reference/report.py; reference/test_run_tests.py — 37 проверок, 0 fail; суммарно 661 ok, 0 fail
- [x] T021 Добавить генератор визуал-отчётов / графиков по эквити и просадкам (reference/report_charts.py): экспорт PNG/SVG из бэктестов (backtest.py) и грид-свипа (ab_grid.py), кривые эквити, drawdown, сводные столбчатые диаграммы по метрикам; без внешних библиотек (SVG вручную, PNG — собственный writer на zlib/struct); reference/test_report_charts.py — 36 проверок, 0 fail; суммарно 697 ok, 0 fail

---

## Phase 9: Next Scope (эксплуатация и аналитика)

**Purpose**: прочность прогона Testnet (проверяемость SC на синтетике), экспорт сделок из бэктеста, консистентность конфига между ботом и симулятором, аналитика метрик.

- [x] T022 Сквозной прогон журналов на синтетике: reference/journal_sim.py — генератор JSONL за 72 ч (события скринера и бота по контракту contracts/journal.md) с разными сценариями (stable/violations/downtime/short), детерминирован seed; прогон через reference/report.py и сверка SC-001..SC-005/007 с ожидаемыми вердиктами; интеграция в tools/run_tests.py (секция --journal-sim, --journal-sim-seed); reference/test_journal_sim.py — 36 проверок, 0 fail; суммарно 733 ok, 0 fail
- [x] T023 Экспорт сделок из бэктеста: флаг --out-cycles в reference/backtest.py — запись закрытых циклов в JSONL/JSON (exit_ts, pnl, exit_reason, open_ts, duration_ms, symbol, side, exit_price, avg_entry, docups, fee), чтобы report_charts.py --kind equity принимал вывод бэктеста напрямую; reference/test_backtest_out.py — 21 проверка, 0 fail; суммарно 754 ok, 0 fail
- [x] T024 Лимит номинала и биржевые ограничения в бэктесте: применение max_notional_usdt и ограничений инструмента (min_qty, tick_size, max_leverage) в reference/backtest.py, как в живом боте (FR-017, FR-018); расхождение «бэктест vs бот» в этих проверках закрыть — DcaParams.max_notional_usdt + validate (leverage>0, max_notional>=0); planned_ladder_notional (план лестницы Мартингейлом); отклонение сигнала в _open_cycle по номиналу/плечу (rejected_notional/rejected_leverage); max_leverage в fetch_instrument (фолбэк 100x); Cycle.notional/margin = notional/min(leverage, max_leverage); сводка max_notional_usdt/max_margin_usdt, экспорт cycle_to_dict, CLI --max-notional-usdt; reference/test_backtest_limits.py — 24 проверки, 0 fail; test_backtest.py обновлён; суммарно 799 ok, 0 fail
- [x] T025 История прогонов автораннера: tools/run_tests.py пишет итоги в logs/test_runs.jsonl (дата, ok/fail по фазам, версия); автономная проверка парсинга/агрегации истории — запись build_history_record/write_history (ts ISO-UTC, version = git rev-parse --short HEAD фолбэк unknown, rc, sections с ok/fail по каждой секции, ok_total/fail_total), чтение read_history (битые строки пропускает), агрегация summarize_history (прогоны, провалы, итог ok/fail, проходимость по секциям); CLI --history-summary/--no-history/--history-path/--history-version; logs/ в .gitignore; reference/test_run_tests_history.py — 20 проверок, 0 fail; суммарно 819 ok, 0 fail
- [x] T026 Расширенные метрики бэктеста: Sharpe/Sortino по закрытым циклам, гистограмма длительностей удержания; вывод в backtest.py и bar-чарт через report_charts.py: _std/sharpe_ratio/sortino_ratio (None при n<2, std=0, без downside), duration_histogram (бинация длительностей в минутах, count по бинам, последний включает максимум), сводка sharpe/sortino/_duration_hist (bins через --hist-bins, default 10), render-строка Sharpe/Sortino + verbose-гистограмма; report_charts.py: --kind hist (SVG/PNG bar-чарт, load_input поддерживает {"hist": [...]}, --x-label); reference/test_backtest_metrics.py — 28 проверок, test_report_charts.py — 42 проверки, 0 fail; суммарно 853 ok, 0 fail
- [x] T027 Валидатор конфига в живом боте: reference/bot_config.py — BotParams (секция bot: max_cycles, monitor_interval_sec, fill_timeout_ms, max_clock_skew_ms, heartbeat_sec, autostart, journal_path) + bot_params_from_config; единый load_screener_cfg (ab_common/run_sim делегируют); validate_config(config.yml) — полнота обязательных ключей dca/bot/screener (фолбэки только при отсутствии файла), маппинг в DcaParams/BotParams/Config, консистентность leverage/NATR; tools/run_tests.py --config использует bot_config.validate_config; reference/test_bot_config.py — 51 проверка, 0 fail; суммарно 774 ok, 0 fail
- [ ] T028 Сводка и графики по символам: stats_summary --by-symbol с выходом JSON для report_charts; equity по каждому символу (многосерийный график)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей — выполнено
- **Foundational (Phase 2)**: зависит от Setup — блокирует US1–US3
- **US1 (Phase 3)**: зависит от Foundational (WS-поток как источник сигналов)
- **US2 (Phase 4)**: зависит от US1 (цикл начинается с первого входа)
- **US3 (Phase 5)**: переиспользует WS-цикл из Phase 2
- **US4 (Phase 6)**: зависит от форматов журнала (contracts/journal.md) — реализовано
- **Polish (Phase 7)**: зависит от всех остальных

### Within Each User Story

- Модуль логики (reference/bot.py) → тесты (reference/test_bot.py): тесты пишутся к тем же функциям, автономно
- Сетевые вызовы наружу не тестируются; поведение API-клиента — через инъекцию-заглушку

### Parallel Opportunities

- T004 и T005/T006 — разные файлы, можно параллельно
- T008 и T009 — оба в reference/bot.py, но независимые функции

---

## Implementation Strategy

### MVP First (US1)

1. Phase 1–2 готовы
2. Phase 3: первый вход + плечо (T005–T007)
3. Phase 4: цикл докупок и TP (T008–T010)
4. STOP и валидация: все reference-тесты, 0 fail

### Сейчас выполняется (из списка владельца)

- `mode CLOSE/ADJUST` → T008
- `set_take_profit` / `set_leverage_once` → T009, T005
- WS-цикл скринера → T003 (реализован), T004 (тесты)
- Метрики прогона → reference/metrics.py + test_metrics.py (21 проверка, 0 fail)
  — снапшоты скринера/симулятора, CSV в logs/test_metrics.csv, MetricsMonitor
