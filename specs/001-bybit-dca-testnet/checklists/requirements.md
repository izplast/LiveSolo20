# Specification Quality Checklist: DCA-бот + скринер для Bybit USDT Perpetual (прогон на Testnet)

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-07-31
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`

### Итоги валидации (итерация 1)

- **Платформенные упоминания оставлены намеренно**: Bybit, USDT Perpetual, Testnet, Termux/Android — это заданные пользователем предметные ограничения этапа, а не выбор технологий реализации. Способ подключения к бирже, язык и библиотеки в спецификации не зафиксированы.
- **Три решения подтверждены владельцем** (2026-07-31) вместо маркеров `[NEEDS CLARIFICATION]`:
  1. Направление сделок — лонг и шорт по направлению подтверждённого тренда (FR-007) — подтверждено.
  2. Ценовой стоп-лосс не применяется; вместо него принудительный выход по истечении максимального времени удержания (FR-014, FR-014a) — скорректировано по решению владельца, начальное значение срока 4 часа зафиксировано в Assumptions как калибруемое.
  3. Лимит одновременных DCA-циклов — 3 (FR-015) — подтверждено.
- **Формула волатильности и определение тренда** заданы конкретными значениями по умолчанию (Assumptions), чтобы требования FR-002 и FR-004 были проверяемыми; параметры вынесены в конфигурацию для калибровки после первого прогона.

### Правка от 2026-07-31 (по итогам разбора рабочего кода скринера)

Владелец предоставил проверенный на живом потоке скринер, и по его логике приняты решения, потребовавшие правки спецификации:

- **FR-002** переписан с процентного размаха свечи на **NATR-14 по Уайлдеру**; **FR-004** — со знака приращения цены на **осциллятор недостигнутых максимумов и минимумов (UHLO, длина 15)**. Обе формулы взяты из рабочего кода, а не назначены заново.
- **FR-001 и FR-031** уточнены: рыночные данные читаются с публичного потока mainnet, ордера идут только на Testnet. Причина — на тестовом контуре почти нет торгов, и отбор по его свечам не дал бы сигналов.
- Добавлены **FR-004a** (подавление повтора на неизменном показании и повторное разрешение после ухода в неопределённое состояние), **FR-005a** (параметры DCA не едут в сигнале), **FR-006a** (вселенная как пересечение mainnet и Testnet с отбором по обороту), **FR-020a** (две цены на момент сигнала).
- **SC-004** уточнён: порог 0.3–0.5% относится к проскальзыванию от цены Testnet, базис между контурами выведен отдельной величиной. Добавлен **SC-012** (нет сигналов по символам, отсутствующим на Testnet).
- Реализация и разбор расхождений: `reference/screener.py`, `reference/CHANGES.md`, `reference/test_screener.py` (48 проверок, сеть не требуется).
