"""
live_screener_midcap.py — Минутный скринер mid-cap #100-1500 для Bybit USDT Perpetual (~600 монет).
WebSocket-версия (Bybit V5 Public Linear): подписка kline.1./kline.15, буферизация deque(50), realtime NATR/UHLO.

Пул: монеты ранга 100-1500 по капитализации CoinGecko, торгуемые на Bybit Linear USDT (~600 после пересечения).
Потоки:
  - WebSocket kline.1.{symbol}+kline.15.{symbol} (wss://stream.bybit.com/v5/public/linear)
    с чанками по 10 топиков/сообщение, ping-pong 15с, авто-reconnect и переподписка.
  - Буфер: collections.deque(maxlen=50) на монету для 1m и 15m, закрытые свечи only.
  - Сигналы: volume_ratio + NATR + UHLO 1m/15m, side Buy/Sell.
  - Таймер: live_100_700_timeline.csv обновляется раз в 60с по свежим WS-буферам.
Логи: live_100_700_signals.csv + консоль [SIGNAL] Rank #X | SYMBOL | Price | Vol_ratio | Time.
Через 10 часов — топ монет по количеству сигналов.

Запуск:
  python3 live_screener_midcap.py                         # 10 часов, WS mainnet
  python3 live_screener_midcap.py --testnet               # Testnet WS
  python3 live_screener_midcap.py --duration 600 --test   # 10 мин тест (dry-run синтетика 10с)
  python3 live_screener_midcap.py --dry-run --duration 120  # синтетика без сети
  pkill -f live_screener_midcap.py || true; nohup python3 live_screener_midcap.py > logs/screener_v2_live.log 2>&1 &

Требования: requests, websockets
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import deque, Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

# --- общая логика скринеров (watchdog, бэкофф, сид, вселенная) ---
from tools.screener_common import (
    CG_FETCH_RETRIES,
    SEED_LIMIT_15M,
    SEED_LIMIT_1M,
    STALE_EXIT_SEC,
    SYMBOL_ALIASES,
    UNIVERSE_TTL_SEC,
    WS_RECONNECT_MAX_ATTEMPTS,
    WS_RECONNECT_MAX_DELAY,
    cg_fetch_with_retry,
    is_stale,
    min_history_15m,
    min_history_1m,
    ws_backoff_delay,
)

# --- минутные фильтры F1/F4/F6/F7 (канон: tools/indicators.py) ---
try:
    from tools.indicators import minute_factor_gates
    HAS_FACTOR_GATES = True
except ImportError:
    minute_factor_gates = None  # type: ignore
    HAS_FACTOR_GATES = False

# --- websockets (обязателен для WS-режима) ---
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    websockets = None  # type: ignore
    HAS_WEBSOCKETS = False

# --- опционально ccxt / ccxt.pro (ТЗ требует) ---
try:
    import ccxt.pro as ccxtpro  # type: ignore
    HAS_CCXT_PRO = True
except ImportError:
    ccxtpro = None
    HAS_CCXT_PRO = False

try:
    import ccxt  # type: ignore
    HAS_CCXT = True
except ImportError:
    ccxt = None
    HAS_CCXT = False

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
COINGECKO_API = "https://api.coingecko.com/api/v3"
BYBIT_REST = "https://api.bybit.com"
BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_PUBLIC_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"

MIDCAP_START = 100
MIDCAP_END = 2500  # увеличено с 700 до 2500 для пула ~600 монет (700→254, 1500→422, 2500→~600)

# Whitelist для свежих листингов — принудительно добавляем, даже если кэш/оборот фильтрует
WHITELIST = {"TACUSDT": 664}  # rank 664 по CoinGecko #100-700

DEFAULT_INTERVAL_SEC = 60
DEFAULT_DURATION_SEC = 10 * 3600  # 10 часов
DEFAULT_CSV = "live_100_700_signals.csv"
DEFAULT_TIMELINE_CSV = "live_100_700_timeline.csv"
DEFAULT_VOL_MULT = 1.5
DEFAULT_NATR_MIN = 0.90
DEFAULT_NATR_MAX = 1.5
DEFAULT_NATR_PERIOD = 14
DEFAULT_CONFIG_PATH = "config/config.yml"
WS_CHUNK_SIZE = 10
WS_PING_INTERVAL = 15
WS_PONG_TIMEOUT = 20

# Порог паузы между тактами монитора: больше — процесс был заморожен
# (Android wake-lock/doze), пишем process_stall для последующего разбора.
PROCESS_STALL_GAP_SEC = 30.0

# Уровень логирования: INFO по умолчанию (без сырых WS-свечей), DEBUG включает детальные дампы
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # INFO | DEBUG
ENABLE_DEBUG_WS = LOG_LEVEL == "DEBUG"  # сырые kline.1 raw dumps только в DEBUG режиме

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
def log(msg: str):
    # Отключение DEBUG-WS логирования на уровне INFO (по умолчанию)
    if "[DEBUG-WS]" in msg and not ENABLE_DEBUG_WS:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Bybit REST helpers (скопированы из reference/screener.py)
# ---------------------------------------------------------------------------
def _bybit_get(base: str, path: str, params: Dict[str, Any], retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt) * (0.7 + 0.6 * random.random()))
        try:
            r = requests.get(f"{base}{path}", params=params, timeout=15)
            r.raise_for_status()
            body = r.json()
            if body.get("retCode") != 0:
                raise RuntimeError(f"retCode={body.get('retCode')} {body.get('retMsg')}")
            return body["result"]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"GET {path} failed after {retries+1}: {last_err}")


def fetch_bybit_instruments(base: str = BYBIT_REST) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    cursor: str | None = None
    while True:
        params: Dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = _bybit_get(base, "/v5/market/instruments-info", params)
        for it in res.get("list", []):
            if it.get("contractType") != "LinearPerpetual" or it.get("quoteCoin") != "USDT":
                continue
            out[it["symbol"]] = {
                "status": it.get("status"),
                "baseCoin": it.get("baseCoin"),
                "symbolType": it.get("symbolType"),  # "stock" — токенизированная акция (PLTR, MSFT, V, SNDK ...)
                "maxLeverage": float(it["leverageFilter"]["maxLeverage"]),
            }
        cursor = res.get("nextPageCursor") or None
        if not cursor:
            break
    return out


def fetch_klines_rest(base: str, symbol: str, interval: str, limit: int = 30) -> List[List[Any]]:
    """Закрытые свечи Bybit REST, от старых к новым. Отбрасываем незакрытую."""
    res = _bybit_get(base, "/v5/market/kline",
                     {"category": "linear", "symbol": symbol, "interval": interval, "limit": min(1000, limit + 1)})
    if res is None:
        log(f"fetch_klines_rest {symbol} {interval}: res is None")
        return []
    interval_ms = int(interval) * 60_000
    now_ms = int(time.time() * 1000)
    rows = []
    for r in (res.get("list", []) or []):
        # Bybit kline list: [startTime, open, high, low, close, volume, turnover]
        try:
            rows.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        except Exception:
            continue
    # фильтр незакрытых
    closed = [row for row in rows if row[0] + interval_ms <= now_ms]
    closed.sort(key=lambda x: x[0])
    return closed[-limit:]


# ---------------------------------------------------------------------------
# CoinGecko helpers — рейтинг капитализации
# ---------------------------------------------------------------------------
def _cg_get(path: str, params: Dict[str, Any], retries: int = 3):
    last_err = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt) * (0.7 + 0.6 * random.random()))
        try:
            r = requests.get(f"{COINGECKO_API}{path}", params=params, timeout=15)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                log(f"CoinGecko 429, wait {wait}s")
                time.sleep(wait)
                raise RuntimeError("429")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
    raise RuntimeError(f"CoinGecko GET {path} failed: {last_err}")


def fetch_coingecko_ranked(limit: int = 600) -> List[dict]:
    """Топ limit по market_cap_desc с rank 1..limit.

    При 429/ошибке сети повторяет страницу (не пропускает) до CG_FETCH_RETRIES
    раз с паузой 30–60 с.
    """
    out: List[dict] = []
    per_page = 250
    page = 1
    while len(out) < limit:
        need = min(per_page, limit - len(out))
        rows = cg_fetch_with_retry(
            lambda: _cg_get("/coins/markets", {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": page,
                "sparkline": False,
            }),
            f"страница {page}",
        )
        if rows is None:
            # Не пропускаем страницу: неполная вселенная сдвигает ранги и ломает
            # сопоставление префиксных тикеров (напр. 1000CATUSDT ловил
            # cat-call-agent вместо simon-s-cat). Поднимаем исключение — build
            # перейдёт на старый кэш (fallback), а не построит обрезанную вселенную.
            raise RuntimeError(
                f"CoinGecko: страница {page} недоступна после {CG_FETCH_RETRIES} попыток — "
                f"неполная вселенная запрещена, работаем по старому кэшу"
            )
        if not rows:
            break
        out.extend(rows)
        if len(rows) < per_page:
            break
        page += 1
        time.sleep(0.6)
        if len(out) >= limit:
            break
    out = out[:limit]
    return out


def normalize_bybit_base(symbol: str) -> Tuple[str, str]:
    """Возвращает (normalized, raw) base.

    Срезает только точные префиксы 1000/10000/1000000:
    1000PEPEUSDT -> (PEPE, 1000PEPE), 1000000BABYDOGEUSDT -> (BABYDOGE, 1000000BABYDOGE).
    Остальные цифры не трогаются: 0GUSDT -> (0G, 0G) (токен zero-gravity, не g-token),
    2ZUSDT -> (2Z, 2Z).
    """
    base_raw = symbol[:-4] if symbol.endswith("USDT") else symbol
    norm = re.sub(r'^(1000000|10000|1000)(?=[A-Z])', '', base_raw).upper()
    return norm, base_raw.upper()


# Числовые тикеры Bybit, которые не являются масштабирующими префиксами
# 1000/10000/1000000, но которые прежняя (ошибочная) нормализация срезала как
# префикс: 0GUSDT -> G, 2ZUSDT -> Z, 4STOCKUSDT -> STOCK. Держим их в отчёте
# явно, с корректной базой, чтобы не терять из виду.
NUMERIC_TICKERS = ("0GUSDT", "2ZUSDT", "4STOCKUSDT")


def resolve_bybit_base(bsym: str, cg_rank: Dict[str, int], cg_id: Dict[str, str],
                       cg_by_id: Dict[str, dict]) -> Tuple[str, str | None, int | None, bool]:
    """Сопоставляет Bybit-символ с монетой CoinGecko.

    Порядок: алиас по coingecko id (SYMBOL_ALIASES) -> матч по нормализованному
    base -> матч по raw base. Возвращает (base, cg_id, rank, resolved).
    """
    norm, raw = normalize_bybit_base(bsym)
    for key in (norm, raw):
        alias_id = SYMBOL_ALIASES.get(key)
        if alias_id and alias_id in cg_by_id:
            rank = int(cg_by_id[alias_id].get("market_cap_rank") or 0)
            if rank:
                return norm, alias_id, rank, True
    if norm in cg_rank:
        return norm, cg_id.get(norm), cg_rank[norm], True
    if raw in cg_rank:
        return raw, cg_id.get(raw), cg_rank[raw], True
    return norm, None, None, False


def build_prefix_report(ranked: List[dict], tradable: Dict[str, dict]) -> List[dict]:
    """Отчёт по префиксным тикерам Bybit: база, CoinGecko id и rank.

    Покрывает все торгуемые Bybit USDT-перпы, у которых нормализация срезает
    масштабирующий префикс 1000/10000/1000000, плюс NUMERIC_TICKERS. Для каждого
    возвращает {symbol, base, raw, cg_id, rank, resolved}.
    """
    cg_id: Dict[str, str] = {}
    cg_rank: Dict[str, int] = {}
    cg_by_id: Dict[str, dict] = {}
    for coin in ranked:
        sym = str(coin.get("symbol") or "").upper()
        rank = int(coin.get("market_cap_rank") or 0)
        cid = str(coin.get("id") or "")
        if cid and cid not in cg_by_id:
            cg_by_id[cid] = coin
        if not sym or not rank:
            continue
        if sym not in cg_rank or rank < cg_rank[sym]:
            cg_rank[sym] = rank
            cg_id[sym] = cid

    rows: List[dict] = []
    for bsym in sorted(tradable):
        norm, raw = normalize_bybit_base(bsym)
        if raw == norm and bsym not in NUMERIC_TICKERS:
            continue
        base, cid, rank, resolved = resolve_bybit_base(bsym, cg_rank, cg_id, cg_by_id)
        rows.append({
            "symbol": bsym,
            "base": base,
            "raw": raw,
            "cg_id": cid,
            "rank": rank,
            "resolved": resolved,
        })
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["symbol"]))
    return rows


def build_midcap_universe(rank_start: int = MIDCAP_START, rank_end: int = MIDCAP_END,
                          save_cache: str | None = "data/midcap_universe.json",
                          force: bool = False) -> Tuple[List[str], Dict[str, int], List[dict]]:
    """
    1. Грузит топ rank_end с CoinGecko
    2. Фильтрует rank_start..rank_end
    3. Пересекает с торгуемыми на Bybit USDT Perp
    Возвращает: (symbols_sorted_by_rank, rank_map_symbol->rank, cg_slice)
    """
    old_symbols: List[str] = []
    cache_path: Path | None = Path(save_cache) if save_cache else None
    if cache_path and cache_path.exists():
        try:
            old = json.loads(cache_path.read_text(encoding="utf-8"))
            old_symbols = old.get("symbols", [])
        except Exception:
            pass
    # TTL: если кэш свежее UNIVERSE_TTL_SEC — используем его, не тикая CoinGecko
    if cache_path and cache_path.exists() and old_symbols:
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age <= UNIVERSE_TTL_SEC and not force:
                rank_map = {k: int(v) for k, v in old.get("rank_map", {}).items()}
                log(f"Вселенная из кэша {cache_path} (возраст {age:.0f}s < TTL {UNIVERSE_TTL_SEC}s): "
                    f"{len(old_symbols)} символов")
                return old_symbols, rank_map, old.get("cg_slice", [])
            log(f"Кэш вселенной устарел ({age:.0f}s > TTL {UNIVERSE_TTL_SEC}s), перестраиваем...")
        except Exception as e:
            log(f"Не удалось прочитать кэш вселенной: {e}, перестраиваем...")

    log(f"Загрузка CoinGecko топ-{rank_end}...")
    try:
        ranked = fetch_coingecko_ranked(limit=rank_end)
    except Exception as e:
        ranked = []
        log(f"CoinGecko топ-{rank_end} недоступен после повторов: {e}; работаем по старому кэшу")
    if not ranked and old_symbols:
        log(f"Внимание: работаем по устаревшему кэшу вселенной ({len(old_symbols)} символов)")
        rank_map = {k: int(v) for k, v in old.get("rank_map", {}).items()}
        return old_symbols, rank_map, old.get("cg_slice", [])
    if not ranked:
        raise RuntimeError("не удалось построить вселенную и нет кэша для fallback")
    cg_slice = ranked[rank_start - 1: rank_end]
    log(f"CoinGecko: всего {len(ranked)}, срез #{rank_start}-#{rank_end}: {len(cg_slice)} "
        f"({cg_slice[0]['symbol'].upper()}#{cg_slice[0].get('market_cap_rank')} -> "
        f"{cg_slice[-1]['symbol'].upper()}#{cg_slice[-1].get('market_cap_rank')})")

    # Минимальный ранг market_cap по symbol на ВСЕМ топ-rank_end (а не только в срезе),
    # чтобы дубликаты symbol разрешались в пользу монеты с наибольшей капитализацией
    # (минимальный ранг). Ранг берётся из полного списка, а затем фильтруется по диапазону.
    cg_rank: Dict[str, int] = {}
    cg_info: Dict[str, dict] = {}
    cg_id: Dict[str, str] = {}
    cg_by_id: Dict[str, dict] = {}
    for coin in ranked:
        sym = str(coin.get("symbol") or "").upper()
        rank = int(coin.get("market_cap_rank") or 0)
        cid = str(coin.get("id") or "")
        if cid and cid not in cg_by_id:
            cg_by_id[cid] = coin
        if not sym or not rank:
            continue
        if sym not in cg_rank or rank < cg_rank[sym]:
            cg_rank[sym] = rank
            cg_info[sym] = coin
            cg_id[sym] = cid

    log(f"CoinGecko уникальных base в топ-{rank_end}: {len(cg_rank)}")

    # cg_slice — только для лога/кэша (первый и последний в срезе)
    cg_slice = ranked[rank_start - 1: rank_end]

    log(f"Загрузка Bybit instruments {BYBIT_REST}...")
    bybit_instr = fetch_bybit_instruments()
    stock_symbols = sorted(s for s, v in bybit_instr.items() if v.get("symbolType") == "stock")
    tradable = {
        s: v for s, v in bybit_instr.items()
        if v.get("status") == "Trading" and v.get("symbolType") != "stock"
    }
    log(f"Bybit USDT Perp Trading: {len(tradable)} "
        f"(исключено токенизированных акций symbolType=stock: {len(stock_symbols)} — "
        f"{', '.join(stock_symbols[:12])}{'...' if len(stock_symbols) > 12 else ''})")

    # Отчёт по префиксным типам: база, CoinGecko id, rank — без «не получено».
    prefix_rows = build_prefix_report(ranked, tradable)
    if prefix_rows:
        resolved = sum(1 for r in prefix_rows if r["resolved"])
        log(f"Префиксный отчёт: {len(prefix_rows)} символов (1000/10000/1000000 + numeric), "
            f"сопоставлено с CoinGecko: {resolved}")
        for r in prefix_rows:
            log(f"  {r['symbol']:22s} -> {r['base']:10s} "
                f"{r['cg_id'] or 'нет в CG топ':30s} rank={r['rank'] if r['rank'] is not None else '—'}")
        report_path = Path("data/prefix_report.json") if save_cache else None
        if report_path:
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(prefix_rows, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            except Exception as e:
                log(f"Не удалось сохранить префиксный отчёт: {e}")

    matched: List[Tuple[int, str, str]] = []
    prefix_matched: List[Tuple[int, str, str]] = []  # 1000/10000/1000000* -> базовый токен
    seen_bybit = set()
    out_of_range: set[str] = set()  # базовый токен есть в CG, но ранг вне [rank_start, rank_end]
    for bsym in tradable:
        norm, raw = normalize_bybit_base(bsym)
        matched_base, _cid, rank, _resolved = resolve_bybit_base(bsym, cg_rank, cg_id, cg_by_id)
        if rank is not None:
            if rank_start <= rank <= rank_end:
                if bsym not in seen_bybit:
                    matched.append((rank, bsym, matched_base))
                    seen_bybit.add(bsym)
                    if raw != norm:
                        prefix_matched.append((rank, bsym, norm))
            else:
                out_of_range.add(bsym)

    matched.sort(key=lambda x: x[0])
    if prefix_matched:
        examples = ", ".join(f"#{r} {s}→{b}" for r, s, b in prefix_matched[:8])
        log(f"По префиксам 1000/10000/1000000 сопоставлено с базовым токеном: {len(prefix_matched)} — {examples}")
    # Ограничение пула до 600 монет для контроля WS-нагрузки (~1200 топиков)
    if len(matched) > 600:
        orig = len(matched)
        matched = matched[:600]
        log(f"Пул обрезан до 600 топ-монет по rank (из {orig})")
    symbols = [bsym for _, bsym, _ in matched]
    rank_map = {bsym: rank for rank, bsym, _ in matched}

    for wl_sym, wl_rank in WHITELIST.items():
        if wl_sym not in rank_map:
            if wl_sym not in tradable:
                log(f"Whitelist {wl_sym} пропущен — нет в Bybit tradable")
                continue
            if wl_sym not in symbols:
                symbols.append(wl_sym)
                rank_map[wl_sym] = wl_rank
                matched.append((wl_rank, wl_sym, wl_sym[:-4]))
                log(f"Whitelist добавлен: {wl_sym} #{wl_rank}")

    # Дополнение до 600 монет если CoinGecko-пересечение дало меньше (гарантия пула ~600 для WS)
    # Символы, у которых базовый токен есть в CoinGecko, но ранг вне [rank_start, rank_end]
    # (например, 1000PEPEUSDT -> PEPE #60), намеренно НЕ возвращаются fill'ом — они выпали из пула.
    if len(symbols) < 600:
        need = 600 - len(symbols)
        extra_candidates = [
            s for s in sorted(tradable.keys())
            if s not in rank_map and s not in out_of_range
        ]
        # приоритет — не стоковые токены, но для достижения 600 берём любые
        fill = extra_candidates[:need]
        for s in fill:
            symbols.append(s)
            rank_map[s] = 9999
        if fill:
            log(f"Пул дополнен до {len(symbols)} из Bybit tradable (+{len(fill)} вне CoinGecko топ-{rank_end})")

    if old_symbols:
        old_set = set(old_symbols)
        new_set = set(symbols)
        added = [s for s in symbols if s not in old_set]
        removed = [s for s in old_symbols if s not in new_set]
        log(f"Вселенная пула: было {len(old_symbols)}, стало {len(symbols)} (+{len(added)} / -{len(removed)})")
        if added:
            log(f"  добавлено: {', '.join(added[:15])}")
        if removed:
            log(f"  удалено: {', '.join(removed[:15])}")

    log(f"Midcap пул #{rank_start}-#{rank_end} на Bybit: {len(symbols)} символов")
    if matched[:5]:
        log("Топ-5 по rank: " + ", ".join(f"#{r} {s}" for r, s, _ in matched[:5]))
    if matched[-5:]:
        log("Хвост-5: " + ", ".join(f"#{r} {s}" for r, s, _ in matched[-5:]))

    if save_cache and symbols:
        try:
            Path(save_cache).parent.mkdir(parents=True, exist_ok=True)
            with open(save_cache, "w", encoding="utf-8") as f:
                json.dump({"rank_start": rank_start, "rank_end": rank_end,
                           "symbols": symbols, "rank_map": rank_map,
                           "generated": datetime.now(timezone.utc).isoformat(),
                           "cg_slice_symbols": [c["symbol"] for c in cg_slice[:10]]}, f, indent=2, ensure_ascii=False)
            log(f"Кэш вселенной сохранён: {save_cache}")
        except Exception as e:
            log(f"Не удалось сохранить кэш: {e}")

    return symbols, rank_map, cg_slice


# ---------------------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------------------
def compute_natr(klines: List[List[Any]], period: int = 14) -> float | None:
    if len(klines) < period + 1:
        return 0.0
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    trs = []
    for i in range(1, len(klines)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    last_close = closes[-1]
    if not last_close:
        return None
    return (atr / last_close) * 100


def volume_ratio(klines: List[List[Any]], lookback: int = 20) -> float | None:
    if len(klines) < lookback + 1:
        return None
    vols = [float(k[5]) for k in klines]
    last_vol = vols[-1]
    prev = vols[-(lookback+1):-1]
    avg = sum(prev) / len(prev) if prev else 0
    if avg <= 0:
        return None
    return last_vol / avg


def compute_uhlo(klines: List[List[Any]], length: int = 15) -> dict | None:
    if len(klines) < 15:
        return {"highs": 0.0, "lows": 0.0}
    unreached_highs, unreached_lows = [], []
    u_highs = u_lows = 0.0
    for k in klines:
        h = float(k[2])
        l = float(k[3])
        unreached_highs = [x for x in unreached_highs if h <= x]
        unreached_lows = [x for x in unreached_lows if l >= x]
        if len(unreached_highs) > length:
            unreached_highs.pop()
        if len(unreached_lows) > length:
            unreached_lows.pop()
        u_highs = 100 * len(unreached_highs) / length
        u_lows = 100 * len(unreached_lows) / length
        unreached_highs.insert(0, h)
        unreached_lows.insert(0, l)
    return {"highs": 100 - u_highs, "lows": 100 - u_lows}


def compute_uhlo_last3(klines: List[List[Any]], length: int = 15) -> tuple:
    """UHLO-состояния трёх последних срезов за один проход.

    Возвращает (u_prev2, u_prev1, u_full) — эквивалент
    compute_uhlo(klines[:-2]), compute_uhlo(klines[:-1]), compute_uhlo(klines),
    но без тройного пересчёта (пул 600 монет, каждая оценка — 4 прохода).
    Формула состояния — та же, что в compute_uhlo этого файла.
    """
    n = len(klines)
    if n < 3:
        return (None, None, None)
    unreached_highs, unreached_lows = [], []
    states: List[dict] = []
    for idx, k in enumerate(klines):
        h = float(k[2])
        l = float(k[3])
        unreached_highs = [x for x in unreached_highs if h <= x]
        unreached_lows = [x for x in unreached_lows if l >= x]
        if len(unreached_highs) > length:
            unreached_highs.pop()
        if len(unreached_lows) > length:
            unreached_lows.pop()
        if idx >= n - 3:
            u_highs = 100 * len(unreached_highs) / length
            u_lows = 100 * len(unreached_lows) / length
            states.append({"highs": 100 - u_highs, "lows": 100 - u_lows})
        unreached_highs.insert(0, h)
        unreached_lows.insert(0, l)
    return (states[0], states[1], states[2])


def classify_color(a: dict | None, b: dict | None) -> str:
    FAST_MIN, FAST_MAX, SLOW_MIN, SLOW_MAX = 80.0, 20.0, 80.0, 20.0
    if not a or not b:
        return "none"
    green = (a["highs"] >= FAST_MIN and a["lows"] <= FAST_MAX and b["highs"] >= SLOW_MIN and b["lows"] <= SLOW_MAX)
    red = (a["lows"] >= FAST_MIN and a["highs"] <= FAST_MAX and b["lows"] >= SLOW_MIN and b["highs"] <= SLOW_MAX)
    if green:
        return "green"
    if red:
        return "red"
    return "none"


def fast_uhlo_corner(uhlo_fast: dict | None) -> bool:
    if not uhlo_fast:
        return False
    highs = uhlo_fast.get("highs", 0.0)
    lows = uhlo_fast.get("lows", 0.0)
    return (highs <= 1e-9 and lows >= 100 - 1e-9) or (lows <= 1e-9 and highs >= 100 - 1e-9)


# ---------------------------------------------------------------------------
# Сигнал
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    timestamp: str
    rank: int
    symbol: str
    price: float
    volume_ratio: float
    atr: float
    time_str: str
    ts_ms: int
    side: str = "Buy"


# ---------------------------------------------------------------------------
# CSV логгер
# ---------------------------------------------------------------------------
class CsvLogger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        need_header = not self.path.exists() or self.path.stat().st_size == 0
        self.f = open(self.path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if need_header:
            self.w.writerow(["timestamp", "rank", "symbol", "price", "volume_ratio", "atr", "time_str"])
            self.f.flush()
        self.count = 0

    def write(self, sig: Signal):
        self.w.writerow([sig.timestamp, sig.rank, sig.symbol,
                         f"{sig.price:.6f}", f"{sig.volume_ratio:.3f}", f"{sig.atr:.3f}", sig.time_str])
        self.f.flush()
        self.count += 1

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class TimelineLogger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        need_header = not self.path.exists() or self.path.stat().st_size == 0
        self.f = open(self.path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if need_header:
            self.w.writerow([
                "datetime", "timestamp", "time_str",
                "pool_size", "n_signals",
                "signals",
                "natr_passed_no_trend_count",
                "natr_passed_no_trend",
                "natr_passed_count",
            ])
            self.f.flush()
        self.count = 0

    def write(self, dt_str: str, ts_ms: int, time_str: str,
              pool_size: int, signals: List[Signal],
              natr_passed_no_trend: List[Tuple[str, int, float]],
              natr_passed_total: int):
        sig_str = ";".join(f"#{s.rank}{s.symbol}" for s in signals) if signals else ""
        no_trend_str = ";".join(f"#{rank}{sym}({natr:.2f})" for sym, rank, natr in natr_passed_no_trend) if natr_passed_no_trend else ""
        self.w.writerow([
            dt_str, ts_ms, time_str,
            pool_size, len(signals), sig_str,
            len(natr_passed_no_trend), no_trend_str,
            natr_passed_total
        ])
        self.f.flush()
        self.count += 1

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class Journal:
    def __init__(self, path: str = "logs/screener-events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a", encoding="utf-8", buffering=1)
    def write(self, kind: str, **fields):
        rec = dict(fields)
        rec["kind"] = kind
        rec["ts"] = int(time.time()*1000)
        try:
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Основной воркер — WebSocket подписка на kline
# ---------------------------------------------------------------------------
class MidcapScreener:
    def __init__(self, rank_start: int = MIDCAP_START, rank_end: int = MIDCAP_END,
                 interval_sec: int = DEFAULT_INTERVAL_SEC, duration_sec: int = DEFAULT_DURATION_SEC,
                 csv_path: str = DEFAULT_CSV, vol_mult: float = DEFAULT_VOL_MULT,
                 natr_min: float = DEFAULT_NATR_MIN, natr_max: float = DEFAULT_NATR_MAX, natr_period: int = DEFAULT_NATR_PERIOD,
                 concurrency: int = 20, dry_run: bool = False,
                  timeline_path: str = "live_100_700_timeline.csv",
                 uhlo_length: int = 15,
                 uhlo_length_15m: int = 20,
                 vwap_max_std: float = 1.5,
                 hv_corr_max: float = 0.6,
                 body_range_max: float = 0.7,
                 f1_enabled: bool = True,
                 factor_enforce: bool = False,
                 cooldown_sec: int = 300,
                 stale_exit_sec: int = STALE_EXIT_SEC,
                  ws_url: str | None = None, ws_chunk: int = WS_CHUNK_SIZE):
        self.rank_start = rank_start
        self.rank_end = rank_end
        self.interval_sec = interval_sec
        self.duration_sec = duration_sec
        self.csv_path = csv_path
        self.timeline_path = timeline_path
        self.vol_mult = vol_mult
        self.natr_min = natr_min
        self.natr_max = natr_max
        self.natr_period = natr_period
        self.uhlo_length = uhlo_length
        self.uhlo_length_15m = uhlo_length_15m
        self.vwap_max_std = vwap_max_std
        self.hv_corr_max = hv_corr_max
        self.body_range_max = body_range_max
        self.f1_enabled = f1_enabled
        self.factor_enforce = factor_enforce
        self.cooldown_sec = cooldown_sec
        self.stale_exit_sec = stale_exit_sec
        self._reconnect_attempts: int = 0  # подряд неудачных попыток WS реконнекта
        self._insufficient_history: Counter = Counter()  # оценки, пропущенные за нехваткой истории
        self.last_signal_ts: Dict[str, int] = {}
        self._bad_kline: Counter = Counter()
        # WS/watchdog состояние
        self._last_kline1_confirm_ms: int | None = None  # время последней confirm=kline.1
        self._fatal_reason: str | None = None  # причина фатального выхода (exit code 1)
        self._exit_code: int = 0
        self._last_monitor_tick: float | None = None  # прошлый такт главного цикла
        self.concurrency = concurrency
        self.dry_run = dry_run
        self.ws_url = ws_url
        self.ws_chunk = ws_chunk

        self.symbols: List[str] = []
        self.rank_map: Dict[str, int] = {}
        self.csv_logger: CsvLogger | None = None
        self.timeline_logger: TimelineLogger | None = None
        self.journal: Journal | None = None
        self.klines_cache: Dict[str, deque] = {}  # 1m
        self.klines_cache_15m: Dict[str, deque] = {}  # 15m
        self.start_ts: float = 0
        self.signals: List[Signal] = []
        self.timeline_rows: List[dict] = []
        # WS-специфичное
        self._pending_signals: List[Signal] = []
        self._lock = asyncio.Lock()  # защита pending_signals между WS и таймером
        self._ws_connected = asyncio.Event()
        self._stop = asyncio.Event()

    async def build_universe(self):
        if self.dry_run:
            log("DRY-RUN: синтетическая вселенная 20 символов #100-700")
            self.symbols = [f"TEST{i}USDT" for i in range(20)]
            self.rank_map = {s: 50 + i for i, s in enumerate(self.symbols)}
            return
        try:
            symbols, rank_map, _ = await asyncio.to_thread(build_midcap_universe, self.rank_start, self.rank_end)
            if not symbols:
                raise RuntimeError("пустая вселенная")
            self.symbols = symbols
            self.rank_map = rank_map
        except Exception as e:
            log(f"Ошибка построения вселенной: {e}, fallback к кэшу data/midcap_universe.json")
            cache = Path("data/midcap_universe.json")
            if cache.exists():
                j = json.loads(cache.read_text(encoding="utf-8"))
                self.symbols = j.get("symbols", [])
                self.rank_map = {k: int(v) for k, v in j.get("rank_map", {}).items()}
                log(f"Fallback кэш: {len(self.symbols)} символов")
            else:
                fallback = Path("data/symbol_universe.txt")
                if fallback.exists():
                    all_sym = [l.strip() for l in fallback.read_text().splitlines() if l.strip()]
                    self.symbols = all_sym[:100]
                    self.rank_map = {s: 50 + i for i, s in enumerate(self.symbols)}
                    log(f"Fallback symbol_universe.txt: {len(self.symbols)}")
                else:
                    raise

    async def seed_history(self):
        min_1m = min_history_1m(self.natr_period, self.uhlo_length)
        min_15m = min_history_15m(self.uhlo_length_15m)
        log(
            f"Seed истории M1×{SEED_LIMIT_1M}+15m×{SEED_LIMIT_15M} для {len(self.symbols)} символов "
            f"(минимум {min_1m}/{min_15m} закрытых баров, иначе символ не оценивается)..."
        )
        sem = asyncio.Semaphore(self.concurrency)
        cache_len = SEED_LIMIT_1M
        cache_len_15m = SEED_LIMIT_15M

        async def one(sym: str):
            async with sem:
                try:
                    if self.dry_run:
                        now_ms = int(time.time() * 1000)
                        rows = []
                        rows15 = []
                        price = 100 + random.uniform(-5, 5)
                        for i in range(cache_len):
                            ts = now_ms - (cache_len - i) * 60_000
                            o = price
                            h = o * (1 + random.uniform(0, 0.004))
                            l = o * (1 - random.uniform(0, 0.004))
                            c = random.uniform(l, h)
                            v = random.uniform(500, 1500)
                            rows.append([ts, o, h, l, c, v])
                            price = c
                        price15 = 100 + random.uniform(-5, 5)
                        for i in range(cache_len_15m):
                            ts = now_ms - (cache_len_15m - i) * 15*60_000
                            o = price15
                            h = o * (1 + random.uniform(0, 0.005))
                            l = o * (1 - random.uniform(0, 0.005))
                            c = random.uniform(l, h)
                            v = random.uniform(800, 1800)
                            rows15.append([ts, o, h, l, c, v])
                            price15 = c
                    else:
                        rows, rows15 = await asyncio.gather(
                            asyncio.to_thread(fetch_klines_rest, BYBIT_REST, sym, "1", cache_len),
                            asyncio.to_thread(fetch_klines_rest, BYBIT_REST, sym, "15", cache_len_15m),
                        )
                    dq = deque(rows, maxlen=50)
                    dq15 = deque(rows15, maxlen=50)
                    self.klines_cache[sym] = dq
                    self.klines_cache_15m[sym] = dq15
                except Exception as e:
                    log(f"seed {sym} failed: {e}")
                    self.klines_cache[sym] = deque(maxlen=50)
                    self.klines_cache_15m[sym] = deque(maxlen=50)

        await asyncio.gather(*(one(s) for s in self.symbols))
        ok = sum(1 for v in self.klines_cache.values() if len(v) >= min_1m)
        ok15 = sum(1 for v in self.klines_cache_15m.values() if len(v) >= min_15m)
        ins = sum(self._insufficient_history.values())
        log(
            f"Seed готово: {ok}/{len(self.symbols)} M1 и {ok15}/{len(self.symbols)} 15m с достаточной историей; "
            f"пропущено оценок за нехваткой истории: {ins}"
        )

    # -----------------------------------------------------------------------
    # Вычисление сигнала из текущих кэшей (без сети)
    # -----------------------------------------------------------------------
    def _evaluate_symbol(self, sym: str) -> Tuple[Signal | None, Tuple[str,int,float] | None, bool, str | None]:
        """Чистая функция без побочных эффектов: возвращает reject_reason для RejectedCoins."""
        dq = self.klines_cache.get(sym)
        dq15 = self.klines_cache_15m.get(sym)
        if not dq or not dq15:
            return (None, None, False, "no_history")
        # Только закрытые бары: WS пишет в deque и confirm=false обновления
        # формирующейся свечи (перезапись dq[-1]), поэтому режем по wall-clock.
        # Иначе объём последнего бара неполный, а UHLO/NATR «подглядывают»
        # внутрь минуты; 15m-бар в момент 1m-confirm почти всегда формирующийся.
        now_ms = int(time.time() * 1000)
        klines = [r for r in dq if r[0] + 60_000 <= now_ms]
        klines15 = [r for r in dq15 if r[0] + 900_000 <= now_ms]
        # Минимумы (пункт ревью 12): 1m >= max(natr_period+1, 20, uhlo_length+2),
        # 15m >= uhlo_length_15m+2. Недостаточно истории — символ не оценивается.
        min_1m = min_history_1m(self.natr_period, self.uhlo_length)
        min_15m = min_history_15m(self.uhlo_length_15m)
        if len(klines) < min_1m or len(klines15) < min_15m:
            self._insufficient_history[sym] += 1
            return (None, None, False, "no_history")
        vr = volume_ratio(klines)
        natr = compute_natr(klines, period=self.natr_period)
        if vr is None or vr < self.vol_mult:
            return (None, None, False, f"vol<{self.vol_mult}")
        if natr is None or natr < self.natr_min:
            return (None, None, False, f"NATR<{self.natr_min}")
        if natr > self.natr_max:
            return (None, None, False, f"NATR>{self.natr_max}")
        # Дальше NATR+объём заведомо пройдены — отдельного флага не нужно.
        rank = self.rank_map.get(sym, 0)
        natr_v = float(natr)
        # UHLO 1m для трёх срезов за один проход (было 3 пересчёта).
        # extreme-фильтр: 2 предыдущие закрытые свечи не в углу 100/0.
        try:
            uhlo_prev2, uhlo_prev1, uhlo1 = compute_uhlo_last3(klines, self.uhlo_length)
        except Exception:
            uhlo_prev2 = uhlo_prev1 = uhlo1 = None
        uhlo15 = compute_uhlo(klines15, length=self.uhlo_length_15m)
        for uh in (uhlo_prev1, uhlo_prev2):
            if uh and ((uh.get("highs", 0) >= 99.9 and uh.get("lows", 0) <= 0.1) or (uh.get("lows", 0) >= 99.9 and uh.get("highs", 0) <= 0.1)):
                return (None, (sym, rank, natr_v), True, "rejected_uhlo_100_0_extreme")
        color = classify_color(uhlo1, uhlo15)
        if fast_uhlo_corner(uhlo1):
            color = "none"
            return (None, (sym, rank, natr_v), True, "uhlo_corner")
        if color not in ("green", "red"):
            return (None, (sym, rank, natr_v), True, "NATR_no_trend")
        # Дальше только тренд green/red.
        side = "Buy" if color == "green" else "Sell"
        ts_ms = int(klines[-1][0])
        # Кулдаун по символу: в устойчивом тренде сигнал был бы каждую
        # минуту (дедуп symbol:ts_ms от этого не спасает) — режем повторы.
        last_ts = self.last_signal_ts.get(sym)
        if last_ts is not None and ts_ms - last_ts < self.cooldown_sec * 1000:
            return (None, None, True, "cooldown")
        # Минутные фильтры F6/F4/F7/F1: вето перегретых входов.
        # Недостаток данных = пропуск (pass), явный перегрев = reject.
        # Валидация на 180 прод-сигналах (TP1.2/SL2.5/90м): veto 92% при
        # win% vetoed 61.2 vs kept 60.0 — как жёсткое вето не годится,
        # поэтому дефолт shadow (factor_enforce=False): считаем и пишем
        # в diagnostics/journal, сигнал идёт дальше.
        if HAS_FACTOR_GATES:
            passed, reason, _fvals = minute_factor_gates(
                klines, side,
                vwap_max_std=self.vwap_max_std,
                hv_corr_max=self.hv_corr_max,
                body_range_max=self.body_range_max,
                f1_enabled=self.f1_enabled,
            )
            if not passed and self.factor_enforce:
                return (None, None, True, reason)
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        time_str = datetime.now().strftime("%H:%M:%S")
        price = float(klines[-1][4])
        sig = Signal(timestamp=ts_str, rank=rank, symbol=sym,
                     price=price, volume_ratio=float(vr) if vr else 0,
                     atr=float(natr) if natr else 0,
                     time_str=time_str, ts_ms=ts_ms, side=side)
        return (sig, None, True, None)

    def _snapshot_timeline_state(self) -> Tuple[List[Tuple[str,int,float]], int]:
        # Локальный сбор RejectedCoins — без глобального состояния, исключает Race Condition
        rejected_cnt: Counter = Counter()
        rejected_examples: Dict[str, str] = {}
        natr_no_trend: List[Tuple[str,int,float]] = []
        natr_total = 0
        for sym in self.symbols:
            _, no_trend, natr_pass, reason = self._evaluate_symbol(sym)
            if reason:
                rejected_cnt[reason] += 1
                if len(rejected_examples) < 10 and sym not in rejected_examples:
                    rejected_examples[sym] = reason
            if no_trend:
                natr_no_trend.append(no_trend)
            if natr_pass:
                natr_total += 1
        # JSONL каждую минуту для аудита
        if self.journal and rejected_cnt:
            try:
                self.journal.write("rejected_snapshot", counts=dict(rejected_cnt), examples=rejected_examples, pool=len(self.symbols))
            except Exception:
                pass
        # Консоль троттлится в _timeline_loop (раз в 5 минут), здесь только сохраняем для доступа
        self._last_rejected_cnt = rejected_cnt  # type: ignore
        self._last_rejected_examples = rejected_examples  # type: ignore
        return natr_no_trend, natr_total

    async def _emit_signal(self, sig: Signal):
        # защита от дублей в пределах минуты — сигнал определяется ts_ms+symbol
        async with self._lock:
            # дедупликация по signal_id в pending + уже отправленных
            sig_id = f"{sig.symbol}:1:{sig.ts_ms}"
            if any(f"{s.symbol}:1:{s.ts_ms}" == sig_id for s in self._pending_signals):
                return
            if any(f"{s.symbol}:1:{s.ts_ms}" == sig_id for s in self.signals):
                return
            # dry-run не имеет timeline-цикла, который чистит pending.
            if not self.dry_run:
                self._pending_signals.append(sig)
        # сразу пишем в csv/журнал/консоль (realtime), timeline заберёт агрегат позже
        assert self.csv_logger is not None
        self.csv_logger.write(sig)
        self.signals.append(sig)
        # — расширенное логирование diagnostics (UHLO 1m/15m, color, natr) —
        diagnostics = None
        try:
            dq = self.klines_cache.get(sig.symbol)
            dq15 = self.klines_cache_15m.get(sig.symbol)
            uhlo1 = compute_uhlo(list(dq), length=self.uhlo_length) if dq and len(dq) >= 15 else None
            uhlo15 = compute_uhlo(list(dq15), length=self.uhlo_length_15m) if dq15 and len(dq15) >= 15 else None
            color = "green" if sig.side == "Buy" else "red" if sig.side == "Sell" else "none"
            diagnostics = {
                "uhlo_1m": uhlo1,
                "uhlo_15m": uhlo15,
                "uhlo_raw": {"1m": uhlo1, "15m": uhlo15},
                "color": color,
                "natr": sig.atr,
            }
            # Shadow-режим F-фильтров: что бы решило вето (не блокирует сигнал).
            if HAS_FACTOR_GATES and dq and len(dq) >= 2:
                try:
                    _passed, _reason, _fvals = minute_factor_gates(
                        list(dq), sig.side,
                        vwap_max_std=self.vwap_max_std,
                        hv_corr_max=self.hv_corr_max,
                        body_range_max=self.body_range_max,
                        f1_enabled=self.f1_enabled,
                    )
                    diagnostics["factor_shadow"] = {
                        "passed": _passed, "reason": _reason,
                        "values": {k: (round(v, 4) if v is not None else None)
                                   for k, v in _fvals.items()},
                        "enforced": self.factor_enforce,
                    }
                except Exception:
                    pass
        except Exception:
            diagnostics = None
        try:
            signal_id = f"{sig.symbol}:1:{sig.ts_ms}"
            sig_payload = {
                "signal_id": signal_id,
                "symbol": sig.symbol,
                "side": sig.side,
                "price": sig.price,
                "ts": sig.ts_ms,
                "rank": sig.rank,
                "atr": sig.atr,
                "volume_ratio": sig.volume_ratio,
            }
            if diagnostics:
                sig_payload["diagnostics"] = diagnostics
            self.journal.write("signal_sent", signal=sig_payload)
            # Кулдаун ставится только после фактической отправки сигнала:
            # снапшот/оценка символа больше не «съедают» окно кулдауна.
            self.last_signal_ts[sig.symbol] = sig.ts_ms
        except Exception:
            pass
        print(f"[{sig.timestamp}] [SIGNAL] Rank #{sig.rank} | {sig.symbol} | Price: {sig.price:.4f} | "
              f"Vol_ratio: {sig.volume_ratio:.2f} | ATR: {sig.atr:.3f} | Side: {sig.side} | Time: {sig.time_str}",
              flush=True)
        log(f"WS сигнал {sig.symbol} {sig.side} NATR={sig.atr:.2f} vol={sig.volume_ratio:.2f}")

    def _apply_kline(self, symbol: str, interval: str, k: dict):
        """Обновить deque из WS-сообщения. k содержит start, open, high, low, close, volume, confirm."""
        try:
            start = int(k.get("start") or k.get("startTime") or 0)
            # Bybit WS иногда шлёт строки
            o = float(k.get("open") or 0)
            h = float(k.get("high") or 0)
            l = float(k.get("low") or 0)
            c = float(k.get("close") or 0)
            v = float(k.get("volume") or 0)
            confirm = bool(k.get("confirm"))
        except Exception:
            # Битая свеча: считаем (раньше молча терялась — при битом потоке
            # по символу узнавали только по no_history), сводка в timeline-лог.
            self._bad_kline[symbol] += 1
            return None
        if start == 0 or o == 0:
            return None
        row = [start, o, h, l, c, v]
        dq: deque | None = None
        if interval == "1":
            dq = self.klines_cache.get(symbol)
            if dq is None:
                dq = deque(maxlen=50)
                self.klines_cache[symbol] = dq
        elif interval == "15":
            dq = self.klines_cache_15m.get(symbol)
            if dq is None:
                dq = deque(maxlen=50)
                self.klines_cache_15m[symbol] = dq
        else:
            return None
        # дедупликация/обновление формирующейся свечи
        if dq and len(dq) and dq[-1][0] == start:
            dq[-1] = row
        elif not dq or len(dq) == 0 or start > dq[-1][0]:
            dq.append(row)
        else:
            # старше — игнор
            return None
        return confirm

    async def _handle_ws_message(self, raw: str | bytes):
        # [DEBUG-WS] — диагностика затора deque(50): логируем kline.1 и ловим тихие падения
        try:
            try:
                msg = json.loads(raw)
            except Exception:
                return
            # pong
            if isinstance(msg, dict) and msg.get("op") == "pong":
                return
            if isinstance(msg, dict) and msg.get("success") is False:
                log(f"[DEBUG-WS] WS error: {msg}")
                return
            topic = msg.get("topic") or ""
            if not topic.startswith("kline."):
                return
            # topic: kline.1.BTCUSDT или kline.15.BTCUSDT
            try:
                _, interval, symbol = topic.split(".", 2)
            except ValueError:
                return
            data = msg.get("data") or []
            if not isinstance(data, list):
                data = [data]
            topic_is_k1 = topic.startswith("kline.1.")
            # детальный дамп сырых свечей — только в DEBUG режиме
            if ENABLE_DEBUG_WS and topic_is_k1:
                log(f"[DEBUG-WS] kline.1 {symbol} {len(data)} bar(s) raw={str(raw)[:180]}")
            # Обрабатываем закрытые/формирующиеся свечи в любом режиме;
            # confirm=kline.1 сбрасывает таймер stale-watchdog.
            for k in data:
                if not isinstance(k, dict):
                    continue
                try:
                    confirm = self._apply_kline(symbol, interval, k)
                except Exception:
                    import traceback
                    log(f"_apply_kline failed {symbol} {interval}: {traceback.format_exc()}")
                    continue
                if interval == "1" and confirm is True:
                    # метка последней подтверждённой 1m-свечи — для stale-watchdog
                    self._last_kline1_confirm_ms = int(time.time() * 1000)
                    if topic_is_k1:
                        try:
                            sig, _, _, _ = self._evaluate_symbol(symbol)
                        except Exception:
                            import traceback
                            log(f"_evaluate_symbol failed {symbol}: {traceback.format_exc()}")
                            continue
                        if sig:
                            await self._emit_signal(sig)
        except Exception:
            import traceback
            log(f"[DEBUG-WS] _handle_ws_message unhandled: {traceback.format_exc()} raw={str(raw)[:300]}")

    async def _subscribe_chunks(self, ws):
        # подписка на kline.1 и kline.15 для всего пула
        args_all = [f"kline.1.{s}" for s in self.symbols] + [f"kline.15.{s}" for s in self.symbols]
        chunk = max(1, self.ws_chunk)
        total_chunks = (len(args_all) + chunk - 1) // chunk
        for idx in range(0, len(args_all), chunk):
            args = args_all[idx: idx + chunk]
            payload = {"op": "subscribe", "args": args}
            await ws.send(json.dumps(payload))
            log(f"WS подписка chunk {idx//chunk+1}/{total_chunks}: {len(args)} топиков (kline.1 + kline.15)")
            await asyncio.sleep(0.15)

    async def _ws_loop(self):
        if not HAS_WEBSOCKETS:
            log("websockets не установлен — WS-режим недоступен, fallback к REST-опросу")
            await self._rest_fallback_loop()
            return
        ws_url = self.ws_url or BYBIT_WS_PUBLIC
        attempt = 0
        while not self._stop.is_set():
            try:
                log(f"WS подключение: {ws_url} (попытка {attempt+1})")
                async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, close_timeout=5, max_queue=2048) as ws:
                    attempt = 0
                    self._reconnect_attempts = 0
                    self._ws_connected.set()
                    log(f"WS подключен: {ws_url}, подписка на {len(self.symbols)} символов x2 интервала...")
                    await self._subscribe_chunks(ws)
                    # часы stale-watchdog начинают тикнуть с момента завершения подписки
                    # (ждём первые confirm=kline.1), а не со старта процесса.
                    self._last_kline1_confirm_ms = int(time.time() * 1000)
                    log(f"WS подписка завершена: {len(self.symbols)*2} топиков, ожидание свечей kline.1/kline.15")
                    # ping задача
                    async def pinger():
                        while not self._stop.is_set():
                            await asyncio.sleep(WS_PING_INTERVAL)
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                            except Exception:
                                return
                    pinger_task = asyncio.create_task(pinger())
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=WS_PONG_TIMEOUT + WS_PING_INTERVAL)
                            except asyncio.TimeoutError:
                                raise RuntimeError("WS таймаут — нет сообщений дольше лимита")
                            # обработка
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="ignore")
                            if '"pong"' in str(raw):
                                continue
                            await self._handle_ws_message(raw)
                    finally:
                        pinger_task.cancel()
                        try:
                            await pinger_task
                        except asyncio.CancelledError:
                            pass
                        self._ws_connected.clear()
            except asyncio.CancelledError:
                log("WS loop отменён")
                return
            except Exception as e:
                self._ws_connected.clear()
                if self._stop.is_set():
                    return
                self._reconnect_attempts += 1
                attempt = self._reconnect_attempts
                if self._reconnect_attempts >= WS_RECONNECT_MAX_ATTEMPTS:
                    self._fatal_reason = "ws_reconnect_exhausted"
                    self._exit_code = 1
                    try:
                        self.journal.write(
                            "ws_reconnect_exhausted",
                            attempts=self._reconnect_attempts,
                            max_attempts=WS_RECONNECT_MAX_ATTEMPTS,
                            delay_max_sec=WS_RECONNECT_MAX_DELAY,
                            error=str(e),
                        )
                    except Exception:
                        pass
                    log(
                        f"WS реконнект исчерпан ({self._reconnect_attempts} неудач подряд) — выход код 1"
                    )
                    self._stop.set()
                    return
                delay = ws_backoff_delay(attempt)
                log(
                    f"WS оборвался ({e}), reconnect через {delay:.1f}с "
                    f"(попытка {attempt}/{WS_RECONNECT_MAX_ATTEMPTS})"
                )
                # переподписка произойдёт в следующей итерации цикла
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _rest_fallback_loop(self):
        """Фолбэк если websockets отсутствует — старый REST опрос раз в interval_sec."""
        log("REST-фолбэк: опрос раз в 60с")
        sem = asyncio.Semaphore(self.concurrency)
        while not self._stop.is_set():
            t0 = time.time()
            # эмулируем poll_once через REST но без WS
            new_signals, natr_no_trend, natr_total = await self._poll_once_rest(sem)
            for s in new_signals:
                await self._emit_signal(s)
            # timeline сразу? оставим таймеру отдельно, но fallback пишет напрямую раз в интервал
            await asyncio.sleep(max(0, self.interval_sec - (time.time() - t0)))

    async def _poll_once_rest(self, sem: asyncio.Semaphore) -> Tuple[List[Signal], List[Tuple[str,int,float]], int]:
        new_signals: List[Signal] = []
        natr_no_trend: List[Tuple[str,int,float]] = []
        natr_total = 0
        async def fetch_one(sym: str):
            async with sem:
                try:
                    dq = self.klines_cache.get(sym)
                    dq15 = self.klines_cache_15m.get(sym)
                    if dq is None or dq15 is None:
                        return (None, None, False)
                    rows = await asyncio.to_thread(fetch_klines_rest, BYBIT_REST, sym, "1", 2)
                    if not rows:
                        return (None, None, False)
                    last = rows[-1]
                    is_new = False
                    if dq and dq[-1][0] == last[0]:
                        dq[-1] = last
                    elif not dq or last[0] > dq[-1][0]:
                        dq.append(last)
                        is_new = True
                    else:
                        return (None, None, False)
                    need_15m = False
                    if not dq15:
                        need_15m = True
                    else:
                        if last[0] - (dq15[-1][0] if dq15 else 0) >= 15*60_000:
                            need_15m = True
                    if need_15m:
                        try:
                            rows15 = await asyncio.to_thread(fetch_klines_rest, BYBIT_REST, sym, "15", 30)
                            if rows15:
                                dq15.clear()
                                for r in rows15:
                                    dq15.append(r)
                        except Exception as e15:
                            log(f"poll {sym} 15m fetch failed: {e15}")
                    if not is_new:
                        return (None, None, False)
                    klines = list(dq)
                    klines15 = list(dq15)
                    vr = volume_ratio(klines)
                    natr = compute_natr(klines, period=self.natr_period)
                    natr_pass = natr is not None and natr >= self.natr_min and natr <= self.natr_max and (vr is not None and vr >= self.vol_mult)
                    uhlo1 = compute_uhlo(klines, length=self.uhlo_length)
                    uhlo15 = compute_uhlo(klines15, length=self.uhlo_length_15m) if klines15 else None
                    color = classify_color(uhlo1, uhlo15)
                    if fast_uhlo_corner(uhlo1):
                        color = "none"
                    trend_ok = color in ("green", "red")
                    if natr_pass and not trend_ok:
                        return (None, (sym, self.rank_map.get(sym, 0), float(natr)), True)
                    if natr_pass and trend_ok:
                        side = "Buy" if color == "green" else "Sell"
                        last_ts = self.last_signal_ts.get(sym)
                        if last_ts is not None and last[0] - last_ts < self.cooldown_sec * 1000:
                            return (None, None, natr_pass)
                        if HAS_FACTOR_GATES and self.factor_enforce:
                            passed, _reason, _fvals = minute_factor_gates(
                                klines, side,
                                vwap_max_std=self.vwap_max_std,
                                hv_corr_max=self.hv_corr_max,
                                body_range_max=self.body_range_max,
                                f1_enabled=self.f1_enabled,
                            )
                            if not passed:
                                return (None, None, natr_pass)
                        rank = self.rank_map.get(sym, 0)
                        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        time_str = datetime.now().strftime("%H:%M:%S")
                        sig = Signal(timestamp=ts_str, rank=rank, symbol=sym,
                                     price=float(last[4]), volume_ratio=float(vr) if vr else 0,
                                     atr=float(natr) if natr else 0,
                                     time_str=time_str, ts_ms=last[0], side=side)
                        return (sig, None, True)
                    return (None, None, natr_pass)
                except Exception as e:
                    log(f"poll {sym} failed: {e}")
                    return (None, None, False)
        results = await asyncio.gather(*(fetch_one(s) for s in self.symbols))
        for sig, no_trend, natr_pass in results:
            if sig:
                new_signals.append(sig)
            if no_trend:
                natr_no_trend.append(no_trend)
            if natr_pass:
                natr_total += 1
        return new_signals, natr_no_trend, natr_total

    def _check_process_stall(self, now: float) -> float | None:
        """Зафиксировать паузу между тактами главного цикла монитора.

        now — time.monotonic() на входе такта. Если с прошлого такта прошло
        больше PROCESS_STALL_GAP_SEC, пишем событие process_stall в журнал и
        WARNING в лог: по нему видно, помог ли wake-lock.
        """
        last = self._last_monitor_tick
        self._last_monitor_tick = now
        if last is None:
            return None
        gap = now - last
        if gap > PROCESS_STALL_GAP_SEC:
            gap_sec = round(gap, 1)
            if self.journal:
                self.journal.write("process_stall", gap_sec=gap_sec)
            log(f"WARNING process_stall: пауза монитора {gap:.1f} с "
                f"(> {PROCESS_STALL_GAP_SEC:.0f} с)")
            return gap
        return None

    async def _timeline_loop(self, end_ts: float):
        # выравнивание до следующей минуты
        now = time.time()
        next_min = (int(now // 60) + 1) * 60
        wait0 = max(0, next_min - now)
        if wait0 > 0 and not self.dry_run:
            log(f"Timeline таймер: ожидание {wait0:.1f}s до следующего 60с-тика...")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait0)
                return
            except asyncio.TimeoutError:
                pass
        iteration = 0
        while not self._stop.is_set() and time.time() < end_ts:
            iteration += 1
            t0 = time.time()
            # snapshot timeline состояния из WS-буферов
            natr_no_trend, natr_total = self._snapshot_timeline_state()
            # Stale-watchdog: если давно нет confirm=kline.1 — фатальный выход,
            # runsv перезапустит процесс. Проверка в таймере, не в WS-цикле.
            now_ms = int(time.time() * 1000)
            if self._last_kline1_confirm_ms is not None and is_stale(
                self._last_kline1_confirm_ms, now_ms, self.stale_exit_sec
            ):
                self._fatal_reason = "ws_stale_exit"
                self._exit_code = 1
                try:
                    self.journal.write(
                        "ws_stale_exit",
                        last_confirm_ms=self._last_kline1_confirm_ms,
                        now_ms=now_ms,
                        stale_sec=self.stale_exit_sec,
                    )
                except Exception:
                    pass
                log(f"WS stale: нет confirm=kline.1 уже {self.stale_exit_sec}с — выход код 1")
                self._stop.set()
                return
            # RejectedCoins — троттлинг консоли раз в 5 минут, JSONL каждую минуту уже в _snapshot
            if iteration % 5 == 0 and hasattr(self, '_last_rejected_cnt') and self._last_rejected_cnt:
                top = self._last_rejected_cnt.most_common(3)
                log(f"RejectedCoins: {dict(self._last_rejected_cnt)} | топ: {top} | примеры: {list(self._last_rejected_examples.items())[:3]}")
            if iteration % 5 == 0 and self._bad_kline:
                log(f"BadKline: битых WS-свечей {sum(self._bad_kline.values())} по {len(self._bad_kline)} символам | топ: {self._bad_kline.most_common(5)}")
            if iteration % 5 == 0 and self._insufficient_history:
                total = sum(self._insufficient_history.values())
                top = self._insufficient_history.most_common(3)
                log(f"NeedsHistory: {total} оценок пропущено за нехваткой истории | топ: {top}")
            # забрать накопленные сигналы за минуту
            async with self._lock:
                minute_signals = list(self._pending_signals)
                self._pending_signals.clear()
            dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ts_ms = int(time.time()*1000)
            time_str = datetime.now().strftime("%H:%M:%S")
            assert self.timeline_logger is not None
            self.timeline_logger.write(dt_str, ts_ms, time_str, len(self.symbols), minute_signals, natr_no_trend, natr_total)
            self.timeline_rows.append({
                "datetime": dt_str, "pool": len(self.symbols),
                "signals": [s.symbol for s in minute_signals],
                "natr_no_trend": [f"#{r}{s}({n:.2f})" for s, r, n in natr_no_trend],
            })
            # логирование итерации
            if minute_signals:
                log(f"Timeline {iteration}: {len(minute_signals)} сигнал(ов), NATR-без-тренда {len(natr_no_trend)}, всего NATR {natr_total}")
            else:
                if iteration % 5 == 0 or natr_no_trend:
                    log(f"Timeline {iteration}: 0 сигналов, NATR-без-тренда {len(natr_no_trend)}, всего NATR {natr_total}")
                elif iteration == 1:
                    log(f"Timeline {iteration}: 0 сигналов, NATR-без-тренда 0 — WS работает, ждём закрытые свечи...")
            # сон до следующего 60с тика
            elapsed = time.time() - t0
            sleep_for = self.interval_sec - elapsed
            if sleep_for < 1:
                sleep_for = self.interval_sec
            # учитываем дрейф — спим до следующей минутной границы
            now2 = time.time()
            next_tick = (int(now2 // self.interval_sec) + 1) * self.interval_sec
            sleep_for = max(0.5, next_tick - now2)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                continue

    async def run(self):
        await self.build_universe()
        if not self.symbols:
            log("Нет символов для мониторинга — выход")
            return
        await self.seed_history()
        self.csv_logger = CsvLogger(self.csv_path)
        self.timeline_logger = TimelineLogger(self.timeline_path)
        self.journal = Journal("logs/screener-events.jsonl")
        mode = "DRY-RUN" if self.dry_run else f"WS {self.ws_url or BYBIT_WS_PUBLIC}"
        log(f"Мониторинг запущен: {mode}, длительность {self.duration_sec}s "
            f"({self.duration_sec/3600:.1f}ч), пул {len(self.symbols)} # {self.rank_start}-{self.rank_end}")
        log(f"CSV сигналов: {self.csv_path}, Timeline: {self.timeline_path}, vol_mult={self.vol_mult}, NATR={self.natr_min}..{self.natr_max}, UHLO={self.uhlo_length}/{self.uhlo_length_15m} (1м/15м), chunk={self.ws_chunk}")

        self.start_ts = time.time()
        end_ts = self.start_ts + self.duration_sec

        # dry-run — синтетика без WS (старый интервальный генератор)
        if self.dry_run:
            log("DRY-RUN: синтетический генератор без WS (интервал {}с)".format(self.interval_sec))
            try:
                iteration = 0
                while time.time() < end_ts and not self._stop.is_set():
                    iteration += 1
                    t0 = time.time()
                    # синтетическое обновление кэшей
                    for sym in self.symbols:
                        dq = self.klines_cache.get(sym)
                        dq15 = self.klines_cache_15m.get(sym)
                        if dq is None or len(dq)==0:
                            continue
                        last_close = float(dq[-1][4])
                        is_pump = random.random() < 0.04
                        vol_mult = random.uniform(3.0, 5.0) if is_pump else random.uniform(0.5, 1.8)
                        natr_val = random.uniform(1.2, 3.0) if is_pump else random.uniform(0.3, 1.0)
                        now_ms = int(time.time() // 60_000 * 60_000)
                        o = last_close
                        h = o * (1 + natr_val/100 * random.uniform(0.5, 1.2))
                        l = o * (1 - natr_val/100 * random.uniform(0.3, 0.8))
                        c = random.uniform(l, h)
                        avg_vol = sum(float(k[5]) for k in list(dq)[-20:]) / min(20, len(dq)) if dq else 1000
                        v = avg_vol * vol_mult
                        new_row = [now_ms, o, h, l, c, v]
                        if dq and dq[-1][0] == now_ms:
                            dq[-1] = new_row
                        else:
                            dq.append(new_row)
                    # сигналы
                    natr_no_trend: List[Tuple[str,int,float]] = []
                    natr_total = 0
                    minute_signals: List[Signal] = []
                    for sym in self.symbols:
                        sig, no_trend, natr_pass, _ = self._evaluate_symbol(sym)
                        if no_trend:
                            natr_no_trend.append(no_trend)
                        if natr_pass:
                            natr_total += 1
                        if sig:
                            minute_signals.append(sig)
                            await self._emit_signal(sig)
                    dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ts_ms = int(time.time()*1000)
                    time_str = datetime.now().strftime("%H:%M:%S")
                    self.timeline_logger.write(dt_str, ts_ms, time_str, len(self.symbols), minute_signals, natr_no_trend, natr_total)
                    self.timeline_rows.append({"datetime": dt_str, "pool": len(self.symbols), "signals": [s.symbol for s in minute_signals], "natr_no_trend": []})
                    elapsed = time.time() - t0
                    if minute_signals:
                        log(f"Итерация {iteration}: {len(minute_signals)} сигнал(ов), NATR-но-нет-тренда {len(natr_no_trend)}, генерация {elapsed:.2f}s")
                    remaining = self.interval_sec - (time.time() - t0)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    if time.time() >= end_ts:
                        break
            finally:
                if self.csv_logger:
                    self.csv_logger.close()
                if self.timeline_logger:
                    self.timeline_logger.close()
                if self.journal:
                    self.journal.close()
                self.report()
                self.save_report_json()
            return self._exit_code

        # WS-режим
        self._stop.clear()
        ws_task = asyncio.create_task(self._ws_loop())
        timeline_task = asyncio.create_task(self._timeline_loop(end_ts))
        try:
            while time.time() < end_ts and not self._stop.is_set():
                self._check_process_stall(time.monotonic())
                await asyncio.sleep(0.5)
                if ws_task.done() and not self._stop.is_set():
                    # WS упал без reconnect — перезапустим
                    if ws_task.exception():
                        log(f"WS задача упала: {ws_task.exception()}, перезапуск через 2с")
                        await asyncio.sleep(2)
                        ws_task = asyncio.create_task(self._ws_loop())
        except asyncio.CancelledError:
            log("Мониторинг отменён")
        except KeyboardInterrupt:
            log("Прервано пользователем")
        finally:
            self._stop.set()
            for t in (ws_task, timeline_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            if self.csv_logger:
                self.csv_logger.close()
            if self.timeline_logger:
                self.timeline_logger.close()
            if self.journal:
                self.journal.close()
            self.report()
            self.save_report_json()
        return self._exit_code

    def report(self):
        now_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log("=" * 60)
        log(f"[{now_dt}] Итог {len(self.signals)} сигнал(ов) за {(time.time()-self.start_ts)/3600:.2f}ч")
        if self.timeline_rows:
            log(f"Timeline записей: {len(self.timeline_rows)}, файл: {self.timeline_path}")
            no_trend_counter = Counter()
            for row in self.timeline_rows:
                for item in row.get("natr_no_trend", []):
                    m = re.match(r"#\d+([A-Z0-9]+)", item)
                    if m:
                        no_trend_counter[m.group(1)] += 1
                    else:
                        no_trend_counter[item] += 1
            if no_trend_counter:
                top_no_trend = no_trend_counter.most_common(10)
                print(f"\n[{now_dt}] === ТОП монет прошедших NATR но НЕ тренд 1м/15м (доп.графа) ===")
                for i, (sym, n) in enumerate(top_no_trend, 1):
                    print(f"[{now_dt}] {i:2d}. {sym:12s} | появлений: {n:3d}")
                log(f"Доп.графа — монет с NATR без тренда всего: {len(no_trend_counter)} уникальных, {sum(no_trend_counter.values())} появлений")

        if not self.signals:
            print(f"[{now_dt}] Топ-монет нет — сигналов не было (рынок спокоен или пороги высоки).")
            log("Топ-монет нет — сигналов не было")
            return
        cnt = Counter(s.symbol for s in self.signals)
        rank_lookup = self.rank_map
        top = cnt.most_common(15)
        print(f"\n[{now_dt}] === ТОП монет #{self.rank_start}-{self.rank_end} по количеству качественных сигналов ===")
        for i, (sym, n) in enumerate(top, 1):
            rank = rank_lookup.get(sym, "?")
            sigs = [s for s in self.signals if s.symbol == sym]
            avg_vol = sum(s.volume_ratio for s in sigs) / len(sigs)
            avg_atr = sum(s.atr for s in sigs) / len(sigs)
            print(f"[{now_dt}] {i:2d}. Rank #{rank:3} | {sym:12s} | сигналов: {n:3d} | avg Vol_ratio: {avg_vol:.2f} | avg ATR: {avg_atr:.3f}")
        print(f"[{now_dt}] " + "=" * 60)

        # Коридоры ранга — динамические от rank_start..rank_end (были
        # захардкожены 100-300/301-500/501-700 и молча теряли хвост пула).
        buckets = defaultdict(int)
        lo, hi = self.rank_start, self.rank_end
        span = max(1, hi - lo + 1)
        bounds = [lo + span * i // 3 for i in range(4)]
        labels = [f"{bounds[i]}-{bounds[i+1]-1}" for i in range(3)]
        labels[-1] = f"{bounds[2]}-{hi}"
        for s in self.signals:
            r = s.rank
            if r < lo or r > hi:
                buckets["вне пула"] += 1
            elif r < bounds[1]:
                buckets[labels[0]] += 1
            elif r < bounds[2]:
                buckets[labels[1]] += 1
            else:
                buckets[labels[2]] += 1
        if buckets:
            print(f"[{now_dt}] По коридорам ранга:")
            for k in labels + (["вне пула"] if buckets.get("вне пула") else []):
                print(f"[{now_dt}]   #{k}: {buckets[k]} сигналов")

    def save_report_json(self, path: str = "live_100_700_report.json"):
        try:
            cnt = Counter(s.symbol for s in self.signals)
            data = {
                "generated": datetime.now(timezone.utc).isoformat(),
                "rank_range": f"{self.rank_start}-{self.rank_end}",
                "duration_sec": self.duration_sec,
                "interval_sec": self.interval_sec,
                "pool_size": len(self.symbols),
                "total_signals": len(self.signals),
                "top": [{"symbol": sym, "rank": self.rank_map.get(sym), "count": n,
                         "avg_vol_ratio": round(sum(s.volume_ratio for s in self.signals if s.symbol == sym)/n, 3) if n else 0,
                         "avg_atr": round(sum(s.atr for s in self.signals if s.symbol == sym)/n, 3) if n else 0}
                        for sym, n in cnt.most_common(20)],
                "csv": self.csv_path,
            }
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"Отчёт JSON: {path}")
        except Exception as e:
            log(f"Не удалось сохранить JSON отчёт: {e}")


# ---------------------------------------------------------------------------
# Конфиг и CLI
# ---------------------------------------------------------------------------
def load_screener_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Секция screener из config.yml. Нет файла/ключа — пустой dict (дефолты)."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("screener", {}) or {}
    except FileNotFoundError:
        log(f"config: {path} не найден — используем значения по умолчанию")
        return {}
    except Exception as e:
        log(f"config: не удалось прочитать {path} ({e}) — используем значения по умолчанию")
        return {}


def resolve_natr_thresholds(args, cfg: dict) -> Tuple[float, float]:
    """CLI > config.yml > DEFAULT_NATR_*; возвращает (min, max)."""
    min_raw = args.natr_min if args.natr_min is not None else cfg.get("natr_min", DEFAULT_NATR_MIN)
    max_raw = args.natr_max if args.natr_max is not None else cfg.get("natr_max", DEFAULT_NATR_MAX)
    try:
        natr_min, natr_max = float(min_raw), float(max_raw)
    except (TypeError, ValueError):
        raise SystemExit(f"некорректные NATR-пороги: {min_raw!r}..{max_raw!r}")
    if natr_min > natr_max:
        raise SystemExit(f"некорректные NATR-пороги: min={natr_min} > max={natr_max}")
    return natr_min, natr_max


def natr_source(args, cfg: dict, key: str) -> str:
    if getattr(args, key) is not None:
        return "CLI"
    return "config" if key in cfg else "default"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=f"Midcap #{MIDCAP_START}-{MIDCAP_END} screener WS (kline.1) — с timeline для бота")
    ap.add_argument("--rank-start", type=int, default=MIDCAP_START, help="начало ранга (100)")
    ap.add_argument("--rank-end", type=int, default=MIDCAP_END, help="конец ранга (700)")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SEC, help="таймер timeline сек (60)")
    ap.add_argument("--duration", type=int, default=DEFAULT_DURATION_SEC, help="длительность сек (36000=10ч)")
    ap.add_argument("--csv", type=str, default=DEFAULT_CSV, help="путь к CSV сигналов")
    ap.add_argument("--timeline", type=str, default=DEFAULT_TIMELINE_CSV, help="путь к timeline CSV для бота")
    ap.add_argument("--config", type=str, default=DEFAULT_CONFIG_PATH,
                    help="путь к config.yml (NATR-пороги; CLI переопределяет)")
    ap.add_argument("--vol-mult", type=float, default=DEFAULT_VOL_MULT, help="порог volume_ratio")
    ap.add_argument("--natr-min", type=float, default=None,
                    help=f"мин NATR (по умолчанию из config.yml, иначе {DEFAULT_NATR_MIN})")
    ap.add_argument("--natr-max", type=float, default=None,
                    help=f"макс NATR (по умолчанию из config.yml, иначе {DEFAULT_NATR_MAX})")
    ap.add_argument("--uhlo-length", type=int, default=15, help="длина UHLO для тренда 1м")
    ap.add_argument("--uhlo-length-15m", type=int, default=20, help="длина UHLO для тренда 15м")
    ap.add_argument("--vwap-max-std", type=float, default=1.5, help="F6: макс отклонение close от VWAP20 в std (0 — выкл)")
    ap.add_argument("--hv-corr-max", type=float, default=0.6, help="F4: макс corr(high,volume,10) (1.0 — выкл)")
    ap.add_argument("--body-range-max", type=float, default=0.7, help="F7: макс тело/диапазон свечи (1.0 — выкл)")
    ap.add_argument("--no-f1", action="store_true", help="выключить F1 vol_reversal вето")
    ap.add_argument("--factor-enforce", action="store_true", help="F-фильтры как жёсткое вето (дефолт: shadow, только лог)")
    ap.add_argument("--cooldown-sec", type=int, default=300, help="пауза между сигналами по одному символу (0 — выкл)")
    ap.add_argument("--stale-exit-sec", type=int, default=STALE_EXIT_SEC, help="выход код 1, если нет confirm=kline.1 столько секунд (deфолт 180)")
    ap.add_argument("--concurrency", type=int, default=20, help="параллельных REST-запросов для seed")
    ap.add_argument("--ws-url", type=str, default=None, help="переопределить WS URL (по умолчанию mainnet)")
    ap.add_argument("--testnet", action="store_true", help="использовать Testnet WS wss://stream-testnet.bybit.com/v5/public/linear")
    ap.add_argument("--ws-chunk", type=int, default=WS_CHUNK_SIZE, help="топиков на одно WS subscribe-сообщение (10)")
    ap.add_argument("--dry-run", action="store_true", help="синтетика без сети (демо)")
    ap.add_argument("--test", action="store_true", help="быстрый тест: duration 300с, interval 10с")
    return ap.parse_args(argv)


async def amain(argv=None):
    args = parse_args(argv)
    if args.test:
        args.duration = 300
        args.interval = 10
        args.dry_run = True
        log("TEST режим: 5 мин, интервал 10с, dry-run")
    ws_url = args.ws_url
    if not ws_url and args.testnet:
        ws_url = BYBIT_WS_PUBLIC_TESTNET
    cfg = load_screener_config(args.config)
    natr_min, natr_max = resolve_natr_thresholds(args, cfg)
    log(f"NATR пороги: min={natr_min} ({natr_source(args, cfg, 'natr_min')}), "
        f"max={natr_max} ({natr_source(args, cfg, 'natr_max')}), config={args.config}")
    screener = MidcapScreener(
        rank_start=args.rank_start, rank_end=args.rank_end,
        interval_sec=args.interval, duration_sec=args.duration,
        csv_path=args.csv, timeline_path=args.timeline,
        vol_mult=args.vol_mult, natr_min=natr_min, natr_max=natr_max, uhlo_length=args.uhlo_length,
        uhlo_length_15m=args.uhlo_length_15m,
        vwap_max_std=args.vwap_max_std, hv_corr_max=args.hv_corr_max,
        body_range_max=args.body_range_max, f1_enabled=not args.no_f1,
        factor_enforce=args.factor_enforce, cooldown_sec=args.cooldown_sec,
        stale_exit_sec=args.stale_exit_sec,
        concurrency=args.concurrency, dry_run=args.dry_run,
        ws_url=ws_url, ws_chunk=args.ws_chunk,
    )
    await screener.run()
    return self._exit_code


def main():
    try:
        sys.exit(asyncio.run(amain()) or 0)
    except KeyboardInterrupt:
        log("Завершено по Ctrl+C")

if __name__ == "__main__":
    main()
