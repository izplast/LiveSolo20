# Data Model: DCA-бот + скринер на Bybit Testnet

**Branch**: `001-bybit-dca-testnet` | **Date**: 2026-08-05

Сущности из spec.md (Key Entities), их поля и переходы. Форматы событий журнала — в
`contracts/journal.md`; формат сигнала — в `contracts/signal.md`.

## Инструмент (кандидат)

| Поле | Тип | Правила |
|---|---|---|
| symbol | str | USDT Perpetual, торгуется и на mainnet, и на Testnet |
| status | str | `Trading` на обоих контурах (FR-006a) |
| max_leverage | float | ≥ настроенного фиксированного плеча (иначе `max_leverage_below_required`) |
| qty_step | float | шаг количества |
| min_qty | float | минимальный размер |
| tick_size | float | шаг цены (FR-017) |
| natr | float | NATR-14, 0.9..2.5 включительно (с допуском 1e-9) |
| uhlo_1m / uhlo_15m | {highs, lows} | длины 20; направление согласовано (FR-004) |
| turnover24h | float | для ранжирования ликвидности |

## Сигнал

| Поле | Тип | Правила |
|---|---|---|
| signal_id | str | `{symbol}:{tf}:{candle_start}` — детерминированный, идемпотентность (FR-030) |
| symbol / side | str | Buy/Sell по направлению подтверждённого тренда (FR-007) |
| price / price_venue | float / str | цена на момент сигнала, контур mainnet |
| ts | int | метка времени сигнала (мс) |
| diagnostics | dict | natr, uhlo_1m, uhlo_15m, color, candle_start, detection_lag_ms |

Параметры DCA и плечо в сигнале отсутствуют (FR-005a).

## DCA-цикл

| Поле | Тип | Правила |
|---|---|---|
| cycle_id | str | уникальный идентификатор сделки |
| symbol | str | инструмент; один цикл на символ (FR-016) |
| executions | list[Исполнение] | первый вход, докупки, закрытие |
| avg_entry | float | средняя цена входа, пересчитывается после каждой докупки (FR-010) |
| tp_level | float | отсчитывается от avg_entry, обновляется после докупки (FR-011) |
| docup_count | int | ≤ max_docups (FR-013) |
| state | enum | `open` → `closed(take_profit | time_exit) | requires_attention` |
| pnl | float | фиксируется при закрытии |
| open_ts / close_ts | int | длительность цикла ≤ max_hold_time (FR-014) |

## Исполнение (ордер)

| Поле | Тип | Правила |
|---|---|---|
| role | enum | `entry` | `dca` | `close` |
| mode | enum | `entry` | `adjust` | `close` — как ордер влияет на позицию |
| symbol / side / qty | | размер кратен qty_step, ≥ min_qty (FR-017) |
| expected_price | float | цена Testnet на момент сигнала |
| avg_fill_price | float | фактическая средняя цена исполнения |
| ts_signal / ts_confirmed | int | задержка = ts_confirmed − ts_signal (FR-019) |
| slippage_pct | float | (avg_fill − expected)/expected, со знаком (FR-020) |
| order_link_id | str | на основе signal_id — идемпотентность |

## Конфигурация

Единый набор DCA-параметров (размер первого входа, шаг усреднения, max докупок, тейк-профит),
фиксированное плечо, границы волатильности, таймфреймы, лимит циклов (3), допустимое
проскальзывание, max_hold_time (4 ч), контур Testnet. Один владелец — бот (FR-008).

## A/B-эксперимент (инструменты ab_sl.py / ab_grid.py)

Сущность для offline-сверки параметров стратегии до тестнета (Phase 8). Инструменты
сравнивают комбинации параметров на ОДНОМ наборе минутных свечей; каркас — в
`reference/ab_common.py`.

| Поле | Тип | Правила |
|---|---|---|
| combo | dict[str, str] | переопределения `bot.<поле>` / `screener.<поле>` поверх config.yml |
| label | str | подпись комбинации; `Baseline (config.yml)` для дефолтов конфига |
| params | dict | комбо как есть (для `--out`) |
| metrics | dict | сводка по закрытым циклам всех символов |
| metrics.wr | float | доля прибыльных циклов (pnl > 0), % |
| metrics.total / n_sl | int | число закрытых циклов / вышедших по SL (stop, hard_loss_limit, dynamic_sl) |
| metrics.avg_tp / avg_sl | float | средний PnL по TP-выходам / по SL-выходам, $ |
| metrics.mdd | float | максимальная просадка кривой реализованного PnL, $ |
| metrics.total_pnl | float | суммарный реализованный PnL, $ |

Метрики считаются по закрытым циклам, объединённым по всем символам прогона
(`aggregate` в ab_common.py). Порядок строк — рейтинг по `--metric` (лучший
первый; для `avg_sl`/`mdd` — меньший лучше), `--top-k` ограничивает таблицу.
JSON `--out` содержит все комбинации без ограничения топа в ранжированном виде.

## Переходы состояний цикла

```text
[получен сигнал, слот свободен]
        │
        v
   entry (первый вход) ──► open
        │                    │ цена − шаг усреднения → dca (докупка)
        │                    │   → пересчёт avg_entry и tp_level
        │                    │ макс. докупок → докупки остановлены
        │                    ▼
        │              closed (take_profit | time_exit)
        │              или requires_attention (ручной разбор)
        ▼
   duplicate / limit / exchange_limits / error  (сигнал не стал циклом)
```
