"""
reference/book_streamer.py — мост «стакан Bybit → DcaCycle».

Получает полные снимки стакана с публичного REST Bybit /v5/market/orderbook
через стандартную библиотеку (urllib — как в backtest.py) и кладёт их в
asyncio.Queue. Потребитель берёт самый свежий снимок через
mock_execution.drain_to_latest и передаёт в DcaCycle.step(book, book['ts']).

Снимки полные (REST отдаёт весь стакан целиком), поэтому дельта-слияние не
нужно: опрос с паузой и отбрасывание устаревших элементов очереди дают
честный поток «последний стакан». Формат выхода совместим с контрактом
mock-исполнения: {bids: [[price, qty], ...], asks: [...]} — float, биды по
убыванию, аски по возрастанию.

Модуль намеренно не импортирует внешние зависимости (websockets/ccxt): сетевой
слой опционален, логика разбора проверяется оффлайн тестом с поддельным HTTP.

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_book_streamer.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request

BYBIT_REST = "https://api.bybit.com"
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com"
DEFAULT_LIMIT = 50


class BookStreamerError(Exception):
    pass


def parse_orderbook(result: dict) -> dict:
    """result от /v5/market/orderbook → стакан в контракте mock-исполнения.

    Bybit отдаёт цены и объёмы строками, уровни — без сортировки; здесь
    приводим к float и сортируем (биды по убыванию, аски по возрастанию),
    чтобы match_snapshot_pure шёл по уровням в правильном порядке.
    """
    bids = [[float(p), float(q)] for p, q in result.get("b", [])]
    asks = [[float(p), float(q)] for p, q in result.get("a", [])]
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {"bids": bids, "asks": asks, "ts": int(result.get("ts", 0) or 0)}


def fetch_orderbook(base: str, symbol: str, limit: int = DEFAULT_LIMIT,
                    retries: int = 3, timeout: float = 15.0) -> dict:
    """Полный снимок стакана REST-запросом (синхронно, без зависимостей)."""
    query = urllib.parse.urlencode(
        {"category": "linear", "symbol": symbol, "limit": limit})
    url = f"{base}/v5/market/orderbook?{query}"
    last: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("retCode") != 0:
                raise BookStreamerError(
                    f"retCode={body.get('retCode')} {body.get('retMsg')}")
            book = parse_orderbook(body["result"])
            if not book["bids"] or not book["asks"]:
                raise BookStreamerError(f"пустой стакан {symbol}")
            return book
        except Exception as e:  # noqa: BLE001
            last = e
    raise BookStreamerError(
        f"GET {url} не удался после {retries + 1} попыток: {last}")


async def poll_orderbook(queue: asyncio.Queue, base: str, symbol: str,
                         interval_sec: float = 2.0, limit: int = DEFAULT_LIMIT,
                         retries: int = 3, max_failures: int = 10) -> None:
    """Фоновый опрос стакана: свежий снимок в queue каждые interval_sec.

    Ошибки сети не роняют цикл: после неудачи ждём следующий такт и пробуем
    снова. Если max_failures попыток подряд не удались — поднимаем
    BookStreamerError: связь с биржей потеряна, потребитель решает дальше сам.
    Отмена через CancelledError (например, SIGINT) — тихое завершение.
    """
    consecutive = 0
    while True:
        try:
            book = fetch_orderbook(base, symbol, limit, retries)
            consecutive = 0
            queue.put_nowait(book)
        except BookStreamerError:
            consecutive += 1
            if consecutive >= max_failures:
                raise
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Запись/чтение стакана (JSONL) — для оффлайн-реплея симулятора
# ---------------------------------------------------------------------------

RECORD_SCHEMA = {"symbol", "ts", "bids", "asks"}


def write_record(path: str, symbol: str, book: dict) -> None:
    """Одна строка JSON на снимок: {symbol, ts, bids, asks}."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"symbol": symbol, "ts": book["ts"],
                            "bids": book["bids"], "asks": book["asks"]}) + "\n")


def read_records(path: str) -> list[dict]:
    """Стакан из JSONL в контракте mock-исполнения (игнорируя мусорные строки)."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rows.append({"bids": rec["bids"], "asks": rec["asks"],
                         "ts": int(rec["ts"]),
                         "symbol": rec.get("symbol", "")})
    return rows
