"""
Crypto scalping screener (v2) — Binance USDT-M perpetual futures.
Refactored per review 2026-08-25.

Changes vs v2-review:
 1. UHLO: unreached_highs -> breakout_highs, mapping fixed (uhlo_low >70 = new lows = bearish -> sell)
 2. Universe: dynamic #100-700 via live_screener_midcap.build_midcap_universe()
 3. Lazy load_markets, df.empty check, timeout, state persistence, KyivFormatter
"""
import time
import logging
import json
import os
import types
import sys
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List

import numpy as np
import pandas as pd

# --- ccxt optional (для офлайн-теста без сети) ---
try:
    import ccxt  # type: ignore
except ImportError:
    ccxt = types.ModuleType("ccxt")  # type: ignore
    class _FakeEx:
        def __init__(self, *a, **k):
            self.markets = {}
            self.timeout = 10000
        def load_markets(self):
            self.markets = {}
            return {}
        def fetch_ohlcv(self, *a, **k): raise ccxt.NetworkError("ccxt not installed")  # type: ignore
        def fetch_funding_rate(self, *a, **k): raise ccxt.NetworkError("ccxt not installed")  # type: ignore
        def fetch_ticker(self, *a, **k): raise ccxt.NetworkError("ccxt not installed")  # type: ignore
    ccxt.binance = _FakeEx  # type: ignore
    ccxt.NetworkError = type("NetworkError", (Exception,), {})  # type: ignore
    ccxt.ExchangeError = type("ExchangeError", (Exception,), {})  # type: ignore
    sys.modules["ccxt"] = ccxt  # type: ignore

# --- ta optional (fallback к ручному NATR) ---
try:
    from ta.volatility import AverageTrueRange  # type: ignore
    HAS_TA = True
except ImportError:
    HAS_TA = False
    AverageTrueRange = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging — KyivFormatter (как в reference/screener.py и live_screener_midcap.py)
# ---------------------------------------------------------------------------
def _kyiv_tz():
    try:
        return ZoneInfo("Europe/Kyiv")
    except Exception:
        return timezone(timedelta(hours=3))

class KyivFormatter(logging.Formatter):
    def __init__(self):
        super().__init__(fmt="%(asctime)s [%(levelname)s] %(message)s")
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _kyiv_tz())
        return dt.strftime("%Y-%m-%d %H:%M:%S")

# setup
os.makedirs("logs", exist_ok=True)
_handler = logging.FileHandler("logs/screener_v2.log")
_handler.setFormatter(KyivFormatter())
_console = logging.StreamHandler()
_console.setFormatter(KyivFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler, _console])
log = logging.getLogger("screener_v2")

# ===== SETTINGS =====
TIMEFRAME = '1m'
LOOKBACK = 50
LENGTH_UHLO = 15

THRESHOLD_DAY_VOL = 500_000
THRESHOLD_AVG_VOL = 3_000_000
THRESHOLD_NATR = 0.9
MIN_CANDLE_VOLUME = 3_000
MIN_CANDLE = MIN_CANDLE_VOLUME  # алиас для ТЗ
MIN_PRICE = 0.01
THRESHOLD_FUNDING_ABS = 0.08     # percent

SCAN_INTERVAL_SEC = 30
COOLDOWN_SEC = 300
FRESHNESS_BARS = 2

STATE_PATH = Path("logs/screener_state.json")

# Whitelist свежих листингов (TACUSDT rank 664)
WHITELIST_V2 = {"TACUSDT": 664}

# ===== EXCHANGE — разделение инстансов (WS public / REST private) =====
# Публичный WS-инстанс — без ключей, только поток свечей
exchange = ccxt.binance({
    'enableRateLimit': True,
    'timeout': 10000,
    'recvWindow': 5000,  # Bybit/Binance требуют recvWindow для подписанных вызовов; для public не мешает
    'options': {'defaultType': 'swap'},
})
# Приватный REST-инстанс — с ключами (если заданы env), отдельный объект для ордеров/баланса
# В screener_v2 ключи не нужны (только public fetch), но разделение задокументировано для бота:
#   restPrivate = ccxt.binance({enableRateLimit: True, timeout: 10000, recvWindow: 5000, apiKey: ..., secret: ...})
# Для каждого ордера: orderLinkId = signal_id (идемпотентность) — см. bot.py/executor.py _link()
_markets_loaded = False

def ensure_markets():
    global _markets_loaded
    if _markets_loaded:
        return True
    try:
        exchange.load_markets()
        _markets_loaded = True
        log.info(f"Markets loaded: {len(exchange.markets)} symbols")
        return True
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.warning(f"load_markets failed: {e}")
        return False
    except Exception as e:
        log.warning(f"load_markets unexpected: {e}")
        return False

def resolve_swap_symbol(spot_symbol: str) -> str | None:
    """Map 'BTC/USDT' -> actual unified swap symbol (e.g. 'BTC/USDT:USDT')."""
    if not ensure_markets():
        return None
    base, quote = spot_symbol.split('/')
    candidate = f"{base}/{quote}:{quote}"
    if candidate in exchange.markets:
        return candidate
    log.warning(f"No swap market found for {spot_symbol} (tried {candidate})")
    return None

# ===== UHLO (fixed semantics) =====
# NOTE: breakout_highs = сколько из последних N хаев текущая цена пробила ВВЕРХ
#       breakout_lows  = сколько из последних N лоев пробила ВНИЗ
#       uhlo_low >70 = новые минимумы = медвежий импульс (sell), uhlo_high >70 = новые максимумы = бычий (buy)
#       Переименовано unreached -> breakout для ясности.
def calculate_uhlo(df: pd.DataFrame, length: int):
    # защита от короткой истории (TACUSDT свежий листинг) — до 15 баров возвращаем 0
    if len(df) < 15:
        return np.zeros(len(df)), np.zeros(len(df))
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    n = len(df)
    breakout_highs = np.zeros(n)
    breakout_lows = np.zeros(n)

    for i in range(length, n):
        window_highs = highs[i - length:i]
        window_lows = lows[i - length:i]
        # FIX: явно считаем пробития, было unreached_highs — теперь breakout
        breakout_highs[i] = 100 * np.sum(highs[i] > window_highs) / length
        breakout_lows[i] = 100 * np.sum(lows[i] < window_lows) / length

    return breakout_highs, breakout_lows


def calculate_natr(df: pd.DataFrame, length=14):
    # защита от короткой истории — до 15 баров возвращаем 0, чтобы не ронять расчеты
    if len(df) < 15:
        return pd.Series([0.0]*len(df))
    if HAS_TA and AverageTrueRange is not None:
        atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=length)
        return (atr.average_true_range() / df['close']) * 100
    # fallback manual Wilder (как в live_screener_midcap.py)
    if len(df) < length + 1:
        return pd.Series([np.nan]*len(df))
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    trs = []
    for i in range(1, len(df)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    if len(trs) < length:
        return pd.Series([np.nan]*len(df))
    atr = sum(trs[:length])/length
    atr_series = [np.nan]*length
    atr_series.append(atr)
    for i in range(length, len(trs)):
        atr = (atr*(length-1)+trs[i])/length
        atr_series.append(atr)
    # pad to df length (atr_series len = len(df))
    # trs len = n-1, atr_series len = n, already aligned
    s = pd.Series(atr_series[:len(df)])
    return (s / df['close']) * 100


# ===== DATA FETCH (with df.empty check, timeout) =====
def fetch_ohlcv_df(swap_symbol: str) -> pd.DataFrame | None:
    try:
        ohlcv = exchange.fetch_ohlcv(swap_symbol, TIMEFRAME, limit=LOOKBACK)
        if ohlcv is None:
            log.warning(f"OHLCV fetch returned None for {swap_symbol}")
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        if df is None or df.empty:
            log.warning(f"OHLCV empty for {swap_symbol}")
            return None
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        if df.empty:
            return None
        return df
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.warning(f"OHLCV fetch failed for {swap_symbol}: {e}")
        return None
    except Exception as e:
        log.warning(f"OHLCV fetch unexpected for {swap_symbol}: {e}")
        return None


def get_funding_rate(swap_symbol: str) -> float | None:
    try:
        funding = exchange.fetch_funding_rate(swap_symbol)
        if funding is None:
            log.warning(f"Funding rate fetch returned None for {swap_symbol}")
            return None
        rate = funding.get('fundingRate')
        return round(rate * 100, 4) if rate is not None else None
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.warning(f"Funding rate fetch failed for {swap_symbol}: {e}")
        return None
    except Exception as e:
        log.warning(f"Funding rate unexpected None for {swap_symbol}: {e}")
        return None


def get_daily_volume(swap_symbol: str) -> float | None:
    try:
        ticker = exchange.fetch_ticker(swap_symbol)
        if ticker is None:
            log.warning(f"Ticker fetch returned None for {swap_symbol}")
            return None
        return ticker.get('quoteVolume')
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.warning(f"Ticker fetch failed for {swap_symbol}: {e}")
        return None
    except Exception as e:
        log.warning(f"Ticker unexpected None for {swap_symbol}: {e}")
        return None


def calculate_correlation(df: pd.DataFrame, btc_df: pd.DataFrame) -> float | None:
    """Correlation of RETURNS (not raw price levels) over the last 20 bars."""
    if df is None or btc_df is None or len(df) < 21 or len(btc_df) < 21:
        return None
    ret = df['close'].pct_change().iloc[-20:]
    btc_ret = btc_df['close'].pct_change().iloc[-20:]
    corr = np.corrcoef(ret, btc_ret)[0, 1]
    return round(corr, 2) if not np.isnan(corr) else None


# ===== SIGNAL STATE (cooldown + freshness) + persistence =====
last_signal_time: Dict[str, float] = {}
prev_state: Dict[str, str | None] = {}

def load_state():
    global last_signal_time, prev_state
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            last_signal_time.update({k: float(v) for k, v in data.get("last_signal_time", {}).items()})
            prev_state.update(data.get("prev_state", {}))
            log.info(f"State restored from {STATE_PATH}: {len(last_signal_time)} symbols")
        except Exception as e:
            log.warning(f"State load failed {STATE_PATH}: {e}")

def save_state():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "last_signal_time": last_signal_time,
            "prev_state": prev_state,
            "saved": datetime.now(_kyiv_tz()).isoformat()
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        log.warning(f"State save failed: {e}")


def is_fresh_and_off_cooldown(symbol: str, side: str, now: float) -> bool:
    if symbol in last_signal_time and (now - last_signal_time[symbol]) < COOLDOWN_SEC:
        return False
    if prev_state.get(symbol) == side:
        return False
    return True


# ===== UNIVERSE — dynamic #100-700 via live_screener_midcap =====
def load_universe_dynamic() -> Dict[str, str]:
    """
    Динамическая загрузка #100-700 из live_screener_midcap.build_midcap_universe().
    Маппит Bybit symbols (WLFIUSDT) -> Binance swap symbols (WLFI/USDT:USDT) через CoinGecko base.
    Fallback: если live_screener недоступен или сеть упала — кэш data/midcap_universe.json.
    Добавлены проверки if res is None для защиты от 'NoneType' при сетевых сбоях.
    """
    # 1. Пытаемся через live_screener_midcap.build_midcap_universe() напрямую (256 монет)
    try:
        # прямой импорт без spec.loader (избегает dataclass NoneType при повторном exec)
        import live_screener_midcap as lmc
        # защита от None возврата
        res = lmc.build_midcap_universe(100, 700, save_cache=None)
        if res is None:
            raise RuntimeError("build_midcap_universe returned None")
        symbols, rank_map, cg_slice = res
        if symbols is None or cg_slice is None:
            raise RuntimeError("symbols or cg_slice is None")
        if not symbols:
            raise RuntimeError("build_midcap_universe empty")
        log.info(f"Dynamic universe #100-700 via live_screener_midcap: {len(symbols)} Bybit symbols")
        # 1a. Фильтрация Testnet через restPrivate.load_markets() — исключить символы не на Testnet (XPLUSUSDT)
        try:
            # Bybit Testnet instruments
            testnet_symbols: set[str] = set()
            cursor = None
            for _ in range(5):  # пагинация, достаточно для 1000 лимита
                params = {"category": "linear", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                r = requests.get("https://api-testnet.bybit.com/v5/market/instruments-info", params=params, timeout=10)
                j = r.json()
                lst = (j.get("result") or {}).get("list") or []
                for it in lst:
                    if it.get("status") == "Trading":
                        testnet_symbols.add(it.get("symbol"))
                cursor = (j.get("result") or {}).get("nextPageCursor")
                if not cursor:
                    break
            if testnet_symbols:
                before = len(symbols)
                symbols = [s for s in symbols if s in testnet_symbols]
                log.info(f"Testnet filter: {len(symbols)}/{before} Bybit symbols remain (XPLUSUSDT filtered)")
            else:
                log.warning("Testnet symbols empty — skip filter")
        except Exception as e:
            log.warning(f"Testnet filter failed (skip): {e}")
        ensure_markets()
        swap_symbols: Dict[str, str] = {}
        for bsym in symbols:
            if bsym is None:
                continue
            base = bsym[:-4] if bsym.endswith("USDT") else str(bsym)
            import re
            base_norm = re.sub(r'^\d+', '', base)
            if not base_norm:
                continue
            spot = f"{base_norm}/USDT"
            resolved = None
            try:
                resolved = resolve_swap_symbol(spot)
            except Exception:
                resolved = None
            if resolved:
                swap_symbols[spot] = resolved
            else:
                swap_symbols[spot] = f"{base_norm}/USDT:USDT"
            if len(swap_symbols) >= 256:
                break
        if len(swap_symbols) >= 50:
            log.info(f"Dynamic universe mapped to Binance: {len(swap_symbols)} swaps (100-700)")
            return swap_symbols
        else:
            log.warning(f"Mapped only {len(swap_symbols)} swaps, fallback to raw Bybit symbols")
            return swap_symbols if swap_symbols else {f"{s[:-4]}/USDT": f"{s[:-4]}/USDT:USDT" for s in symbols[:100]}
    except Exception as e:
        import traceback
        log.warning(f"Dynamic universe via live_screener_midcap failed: {e} | {traceback.format_exc()[:500]}")

    # 2. Fallback к кэшу data/midcap_universe.json (Bybit -> маппим)
    try:
        cache = Path("data/midcap_universe.json")
        if cache is None:
            raise RuntimeError("cache is None")
        if cache.exists():
            import json as _json
            j = _json.loads(cache.read_text(encoding="utf-8"))
            if j is None:
                raise RuntimeError("cache json is None")
            symbols = j.get("symbols", []) or []
            if not symbols:
                raise RuntimeError("cache empty")
            out: Dict[str, str] = {}
            for bsym in symbols:
                if bsym is None:
                    continue
                base = bsym[:-4] if bsym.endswith("USDT") else str(bsym)
                import re
                base_norm = re.sub(r'^\d+', '', base)
                if not base_norm:
                    continue
                spot = f"{base_norm}/USDT"
                try:
                    resolved = resolve_swap_symbol(spot)
                except Exception:
                    resolved = None
                out[spot] = resolved if resolved else f"{base_norm}/USDT:USDT"
            if out:
                log.info(f"Fallback universe from cache: {len(out)} symbols")
                return out
    except Exception as e:
        log.warning(f"Cache fallback failed: {e}")

    # 3. Последний fallback: минимальный хардкод (чтобы тест без сети прошел)
    log.warning("Using minimal fallback universe (7 symbols) for offline test")
    fallback_spot = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT', 'POL/USDT']
    out = {}
    for s in fallback_spot:
        out[s] = s
    return out

# ===== SCAN ONE SYMBOL =====
def scan_market(spot_symbol: str, swap_symbol: str, btc_df: pd.DataFrame | None):
    df = fetch_ohlcv_df(swap_symbol)
    if df is None or df.empty or len(df) < LENGTH_UHLO + 1:
        return None

    breakout_high, breakout_low = calculate_uhlo(df, LENGTH_UHLO)
    current_breakout_high = breakout_high[-1]
    current_breakout_low = breakout_low[-1]

    natr_series = calculate_natr(df)
    if natr_series is None or natr_series.empty:
        return None
    current_natr = natr_series.iloc[-1]

    # pandas may return nan for last value
    if pd.isna(current_natr):
        return None

    avg_vol = df['volume'].mean() * df['close'].mean()
    daily_vol = get_daily_volume(swap_symbol)
    last_candle_volume = df['volume'].iloc[-1] * df['close'].iloc[-1]
    correlation = calculate_correlation(df, btc_df)
    funding_rate = get_funding_rate(swap_symbol)

    if None in (current_natr, daily_vol, funding_rate):
        log.info(f"{spot_symbol}: skipped (missing data — natr/daily_vol/funding fetch failed)")
        return None

    volume_ok = last_candle_volume > MIN_CANDLE_VOLUME
    funding_ok = abs(funding_rate) < THRESHOLD_FUNDING_ABS
    price_ok = df['close'].iloc[-1] > MIN_PRICE

    # FIXED MAPPING: breakout_high >70 = новые максимумы = бычий -> buy
    #                breakout_low  >70 = новые минимумы = медвежий -> sell
    buy_raw = current_breakout_high > 70
    sell_raw = current_breakout_low > 70

    base_filters_ok = (
        current_natr >= THRESHOLD_NATR
        and daily_vol > THRESHOLD_DAY_VOL
        and avg_vol > THRESHOLD_AVG_VOL
        and volume_ok
        and funding_ok
        and price_ok
    )

    now = time.time()
    buy_signal = buy_raw and base_filters_ok and is_fresh_and_off_cooldown(spot_symbol, 'buy', now)
    sell_signal = sell_raw and base_filters_ok and is_fresh_and_off_cooldown(spot_symbol, 'sell', now)

    if buy_signal or sell_signal:
        last_signal_time[spot_symbol] = now
        save_state()
    # prev_state обновляем всегда по raw, чтобы freshness работал
    prev_state[spot_symbol] = 'buy' if buy_raw else ('sell' if sell_raw else None)
    # сохраняем prev_state тоже
    if buy_raw or sell_raw:
        save_state()

    return {
        'symbol': spot_symbol,
        'timestamp': datetime.now(_kyiv_tz()).strftime('%Y-%m-%d %H:%M:%S'),
        'breakout_high': round(float(current_breakout_high), 2),
        'breakout_low': round(float(current_breakout_low), 2),
        # для совместимости со старыми ключами
        'uhlo_high': round(float(current_breakout_high), 2),
        'uhlo_low': round(float(current_breakout_low), 2),
        'natr': round(float(current_natr), 2),
        'avg_vol_usdt': round(float(avg_vol), 0),
        'daily_vol_usdt': round(float(daily_vol), 0),
        'last_candle_vol': round(float(last_candle_volume), 0),
        'correlation': correlation,
        'funding_rate': funding_rate,
        'buy_signal_raw': buy_raw,
        'sell_signal_raw': sell_raw,
        'buy_signal': buy_signal,
        'sell_signal': sell_signal,
        'base_filters_ok': base_filters_ok,
    }


def get_btc_data(btc_swap_symbol: str):
    if not btc_swap_symbol:
        return None
    return fetch_ohlcv_df(btc_swap_symbol)


# ===== MAIN LOOP =====
def run():
    load_state()
    # ленивая загрузка markets
    if not ensure_markets():
        log.error("Cannot load markets — retry in 30s")
        time.sleep(30)
        if not ensure_markets():
            log.error("Markets still failing — exiting run loop")
            return

    swap_symbols = load_universe_dynamic()
    if not swap_symbols:
        log.error("Universe empty — abort")
        return

    # логируем какие символы резолвились
    log.info(f"Universe resolved: {len(swap_symbols)} swaps (example: {list(swap_symbols.values())[:3]})")

    btc_swap = swap_symbols.get('BTC/USDT')

    log.info("=" * 80)
    log.info("SCREENER v2 refactored — continuous scan every %ss", SCAN_INTERVAL_SEC)
    log.info(
        "Filters: NATR>=%.1f%% | day_vol>%.0f | avg_vol>%.0f | candle_vol>%.0f | |funding|<%.2f%%",
        THRESHOLD_NATR, THRESHOLD_DAY_VOL, THRESHOLD_AVG_VOL, MIN_CANDLE_VOLUME, THRESHOLD_FUNDING_ABS,
    )
    log.info("UHLO: breakout_high >70 = buy (new highs), breakout_low >70 = sell (new lows)")
    log.info("=" * 80)

    while True:
        cycle_start = time.time()
        btc_df = get_btc_data(btc_swap) if btc_swap else None

        for spot_symbol, swap_symbol in swap_symbols.items():
            try:
                r = scan_market(spot_symbol, swap_symbol, btc_df)
            except Exception as e:
                log.exception(f"Unhandled error scanning {spot_symbol}: {e}")
                continue

            if r is None:
                continue

            if r['buy_signal']:
                log.info(f"BUY  {r['symbol']} | NATR={r['natr']}% funding={r['funding_rate']}% corr={r['correlation']}")
            elif r['sell_signal']:
                log.info(f"SELL {r['symbol']} | NATR={r['natr']}% funding={r['funding_rate']}% corr={r['correlation']}")
            elif (r['buy_signal_raw'] or r['sell_signal_raw']) and not r['base_filters_ok']:
                log.debug(f"{r['symbol']}: raw signal present, filters blocked it")

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, SCAN_INTERVAL_SEC - elapsed))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description="screener_v2 refactored")
    ap.add_argument("--interval", type=int, default=SCAN_INTERVAL_SEC, help="scan interval sec")
    ap.add_argument("--duration", type=int, default=0, help="duration sec, 0 = infinite")
    args = ap.parse_args()
    # override globals
    SCAN_INTERVAL_SEC = args.interval
    if args.duration and args.duration > 0:
        # run with timeout
        import threading
        def _stop_after():
            time.sleep(args.duration)
            log.info(f"Duration {args.duration}s reached — stopping")
            os._exit(0)
        threading.Thread(target=_stop_after, daemon=True).start()
    run()
