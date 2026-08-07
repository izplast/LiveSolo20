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
| cycle_opened | `cycle_id, symbol` |
| cycle_closed | `cycle_id, symbol, exit_reason (take_profit|time_exit|manual), pnl` |
| stream_down / stream_up | «слепые» интервалы бота |

## Правила

- Секреты, подписи запросов — в журнал не пишутся (FR-024).
- `ts` выставляется процессом записи в момент события; `ts` из payload не перезаписывает его.
- Каждый сигнал имеет конечный статус (FR-022): исполнен / отсечён / дубликат / по лимиту /
  по биржевым ограничениям / неисполнен из-за ошибки.
