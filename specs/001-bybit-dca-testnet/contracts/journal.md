# Контракт журнала: единый формат событий для сводки

**Branch**: `001-bybit-dca-testnet` | **Date**: 2026-08-05

JSONL: одна строка JSON на событие, поля `kind` и `ts` обязательны. Пишут два процесса;
`reference/report.py` разбирает оба файла и игнорирует незнакомые `kind` и битые строки.

## Пишет скринер (reference/screener.py)

| kind | поля |
|---|---|
| signal_sent | `signal: {signal_id, symbol, side, price, price_venue, ts, diagnostics}` |
| signal_failed | `signal: {...}, error` |
| reject | `symbol, reason, turnover24h?` — причины `natr_above_max`, `uhlo_no_color` и др. |
| universe_reject | `symbol, reason` — `not_on_testnet`, `max_leverage_below_required`, ... |
| stream_down | `cause (startup|disconnect), shard?, symbols?` — начало «слепого» интервала |
| stream_up | `cause, duration_ms, shard?, symbols?` |
| seed_failed | `symbol, error` |
| error | `where, error` |

## Пишет бот (контракт из reference/CHANGES.md)

| kind | поля |
|---|---|
| signal_received | `signal_id, symbol, price_mainnet, price_testnet, ts_signal` |
| signal_rejected | `signal_id, reason` — `duplicate | limit | exchange_limits | error` |
| order_filled | `signal_id, cycle_id, symbol, role (entry|dca|close), mode (entry|adjust|close), expected_price, avg_fill_price, ts_signal, ts_confirmed` |
| cycle_opened | `cycle_id, symbol, open_ts` — открытие цикла: `open_ts` — точная метка начала цикла (epoch-мс, момент приёма сигнала, по которому бот завёл цикл) |
| cycle_recovered | `cycle_id, symbol, open_ts` — восстановленный после перезапуска цикл; `open_ts` берётся из `createdTime` позиции |
| cycle_closed | `cycle_id, symbol, exit_reason (take_profit|hard_sl|be_stop|trailing|time_exit|manual), pnl, open_ts, close_ts, duration_ms` — `close_ts` — точная метка закрытия, `duration_ms = close_ts − open_ts` (длительность удержания). `be_stop` — выход по стопу безубытка в режиме Smart Timeout (break_even) |
| trail_activated | `cycle_id, symbol, avg_price, trigger_pct, peak_price, trail_level` — трейлинг-тейк-профит достиг порога прибыли; после этого выход при откате на `trail_step_pct` от пика |
| timeout_smart | `cycle_id, symbol, mode (break_even|passive_wait), held_sec, avg_price, price, gross_pnl_pct, net_pnl_pct, be_stop, be_fee_pct, threshold_pct, dca_locked` — Smart Timeout (FR-014): истёк `max_hold_hours`, позиция НЕ сбрасывается по рынку. `break_even` — прибыль после комиссий выше порога, стоп подтянут к безубытку `be_stop`; `passive_wait` — просадка, доливки запрещены (`dca_locked: true`), ждём TP или Hard SL |
| stream_down / stream_up | «слепые» интервалы бота |

`open_ts`/`close_ts` — epoch-мс на часовой шкале биржи (с поправкой `skew_ms`), а
не метка записи события. Сводка (`tools/stats_summary.py`) считает длительность
удержания именно по ним, поэтому оба поля обязаны попадать в журнал, а не только
в текстовый `bot.log`.

## Правила

- Секреты, подписи запросов — в журнал не пишутся (FR-024).
- `ts` выставляется процессом записи в момент события; `ts` из payload не перезаписывает его.
- Каждый сигнал имеет конечный статус (FR-022): исполнен / отсечён / дубликат / по лимиту /
  по биржевым ограничениям / неисполнен из-за ошибки.
