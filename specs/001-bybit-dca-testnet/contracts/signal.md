# Контракт сигнала: скринер → бот

**Branch**: `001-bybit-dca-testnet` | **Date**: 2026-08-05

Формат HTTP-конверта `POST {bot_api_url}/signal`. Скринер — единственный отправитель,
бот — единственный потребитель. Зафиксирован в `reference/CHANGES.md` (раздел «Контракт с ботом»).

```json
{
  "source": "local_screener",
  "signal_id": "BTCUSDT:1:1753960800000",
  "symbol": "BTCUSDT",
  "side": "Buy",
  "mode": "DCA",
  "price": 68142.5,
  "price_venue": "bybit_mainnet",
  "ts": 1753960860123,
  "diagnostics": {
    "natr": 1.42,
    "uhlo_1m": {"highs": 0.0, "lows": 100.0},
    "uhlo_15m": {"highs": 5.0, "lows": 95.0},
    "color": "green",
    "candle_start": 1753960800000,
    "detection_lag_ms": 123
  }
}
```

## Обязанности бота при приёме

1. Отсечь дубликат по `signal_id` и по открытому циклу; `signal_id` — основа `orderLinkId`.
2. В момент получения записать **свою** последнюю известную цену Testnet (`price_testnet`).
3. Параметры DCA и плечо — только из своего конфига (FR-008, FR-005a).
4. Проверить `minOrderQty`, `qtyStep`, `tickSize`, `maxLeverage` до ордера (FR-017).
5. Считать задержку от `ts`, а не от момента приёма HTTP-запроса.
6. Проверить расхождение часов с Testnet до торговли (FR-032).

## Ответ

Любой 2xx считается доставкой. Не-2xx и таймаут считаются неудачей: скринер повторяет
попытку (post_retries) и не продвигает цвет до успешной доставки.
