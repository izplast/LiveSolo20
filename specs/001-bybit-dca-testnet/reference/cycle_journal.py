"""
reference/cycle_journal.py — журнал событий бота: открытие/закрытие DCA-цикла.

Модуль-референс для `~/bybit-dca-bot/core/cycle_journal.py` (боевая копия на
Termux-хосте). Пишет в `logs/bot-events.jsonl` (ключ `bot.journal_path`) два
структурированных события с точными метками времени, чтобы сводка
`tools/stats_summary.py` считала длительность удержания не по текстовым логам,
а по журналу.

При исполнении первого входа бот вызывает `cycle_opened(cycle_id, symbol)`:

    {"kind": "cycle_opened", "cycle_id": "...", "symbol": "BTCUSDT",
     "open_ts": 1753960860123, "ts": 1753960860123}

При закрытии позиции (TP/SL/trailing/time_exit/manual) — `cycle_closed(...)`:

    {"kind": "cycle_closed", "cycle_id": "...", "symbol": "BTCUSDT",
     "exit_reason": "take_profit", "pnl": 0.64,
     "open_ts": 1753960860123, "close_ts": 1753961400000,
     "duration_ms": 539877, "ts": 1753961400000}

`trailing` — выход по трейлинг-тейк-профиту: прибыль достигла порога
`trail_trigger_pct`, после чего цена откатилась на `trail_step_pct` от пика.

Правила:

- `open_ts` / `close_ts` — epoch-мс в момент события, выставляются самим
  ботом (монитором цикла), а не парсятся из текста лога.
- `duration_ms = close_ts - open_ts`; если бот уже знает `open_ts`, он обязан
  продублировать его в `cycle_closed`, чтобы сводка не зависела от порядка
  строк в файле.
- Длительность переживает смерть процесса: при старте открытые циклы
  восстанавливаются из уже записанных `cycle_opened` (на Android смерть
  процесса — ожидаемое событие, см. config `exchange_take_profit`).
- `exit_reason`: take_profit | hard_sl | trailing | time_exit | manual.
- Секреты и подписи в журнал не попадают (FR-024).

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_cycle_journal.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

# Причины выхода, которые умеет разбирать tools/stats_summary.py.
EXIT_REASONS = ("take_profit", "hard_sl", "trailing", "time_exit", "manual")


def now_ms() -> int:
    """Текущее время в epoch-мс — метка журнального события."""
    return int(time.time() * 1000)


class CycleJournal:
    """Журнал событий бота: одна строка JSON на событие, append с флашем.

    Слой хранит только метки открытия циклов; всё остальное — сырая запись
    через `write()`. Не обращается к бирже, поэтому тестируется без сети.
    """

    def __init__(self, path: str, now: Callable[[], int] = now_ms) -> None:
        self.path = path
        self._now = now
        # cycle_id -> open_ts: живые циклы. Восстанавливается из файла, чтобы
        # duration_ms считался корректно после перезапуска процесса.
        self._open_ts: dict[str, int] = {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "a", buffering=1, encoding="utf-8")
        self._recover()

    def _recover(self) -> None:
        """Вернуть состояние открытых циклов из ранее записанных строк."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # обрыв записи — не причина падать при старте
                    if not isinstance(rec, dict):
                        continue
                    cid = rec.get("cycle_id")
                    if not isinstance(cid, str):
                        continue
                    if rec.get("kind") == "cycle_opened":
                        open_ts = rec.get("open_ts", rec.get("ts"))
                        if isinstance(open_ts, (int, float)):
                            self._open_ts[cid] = int(open_ts)
                    elif rec.get("kind") == "cycle_closed":
                        self._open_ts.pop(cid, None)
        except FileNotFoundError:
            pass  # первый запуск — журнала ещё нет

    def write(self, kind: str, **fields: Any) -> None:
        """Записать произвольное событие. kind и ts ставятся последними, чтобы
        одноимённые поля из fields не перезаписывали их (см. CHANGES.md)."""
        rec = dict(fields)
        rec["kind"] = kind
        rec["ts"] = self._now()
        try:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # журнал не должен ронять бота на неожиданной ошибке записи

    def cycle_opened(self, cycle_id: str, symbol: str, open_ts: int | None = None) -> None:
        """Зафиксировать момент открытия позиции/начала цикла (вход исполнен)."""
        open_ts = self._now() if open_ts is None else int(open_ts)
        self._open_ts[cycle_id] = open_ts
        self.write("cycle_opened", cycle_id=cycle_id, symbol=symbol, open_ts=open_ts)

    def cycle_closed(self, cycle_id: str, symbol: str, exit_reason: str,
                     pnl: float | None = None, close_ts: int | None = None) -> None:
        """Зафиксировать закрытие цикла: время окончания и длительность.

        exit_reason: take_profit | hard_sl | trailing | time_exit | manual. pnl — в USDT.
        open_ts берётся из слоя журнала (в т.ч. восстановленный после
        перезапуска) и дублируется в событие для независимой от порядка
        обработки строк сводкой.
        """
        if exit_reason not in EXIT_REASONS:
            raise ValueError(
                f"exit_reason должен быть одним из {EXIT_REASONS}, "
                f"получено {exit_reason!r}"
            )
        close_ts = self._now() if close_ts is None else int(close_ts)
        open_ts = self._open_ts.pop(cycle_id, None)
        fields: dict[str, Any] = {
            "cycle_id": cycle_id,
            "symbol": symbol,
            "exit_reason": exit_reason,
            "pnl": pnl,
            "close_ts": close_ts,
        }
        if open_ts is not None:
            fields["open_ts"] = open_ts
            fields["duration_ms"] = max(0, close_ts - open_ts)
        self.write("cycle_closed", **fields)

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass
