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
