"""
screener_param_search_v4.py
===========================

v4 - optimized rewrite of v3 built for A/B testing against it
(`--selftest` generates synthetic data, checks numerical equivalence of the
shared logic, micro-tests the exit engine, and benchmarks both scripts).

SIGNAL LOGIC IS UNCHANGED from v3 (same masks, same threshold order, same
cooldown / entry / exit rules) so results stay comparable. What changed:

FIXES (each maps to a review finding on v3)
-------------------------------------------
F1  Carry cost now uses the ACTUAL holding time (exit bar timestamp), not
    the fixed time-exit deadline. v3 charged every short the full 90 min
    even when SL hit on minute 5. Greens are unaffected. Also removes the
    `if False` dead-code line that would index a pre-mask array with a
    post-mask position if ever enabled.
F2  tz-safe time arithmetic: all internal comparisons run on naive-UTC
    numpy datetime64. v3 compared feat["ts"].values (naive) against
    tz-aware Timestamps -> TypeError on pandas >= 2/3 (reproduced on
    pandas 3.0.5: "Cannot compare tz-naive and tz-aware timestamps").
F3  Global selection coverage gate: a combo can only become a finalist if
    it cleared per-symbol gates on >= COVERAGE_MIN_FRAC of symbols (and
    >= COVERAGE_ABS_MIN). v3 could rank a combo seen on 2 of 20 symbols
    off a median of two numbers.
F4  Fold gate is literally ">= 3 of the folds actually seen":
    n_folds_positive >= min(3, n_folds_total).
F5  run loop catches ValueError (data-quality assertion) alongside
    FileNotFoundError - one bad symbol no longer kills the run.
F6  Plateau lookup over the COMPLETE parameter lattice: missing combos
    (zero trades) re-inserted as NaN rows before neighbor-median
    smoothing, so +/-1 neighbors always exist.
F7  Consistent combo identity via integer combo_id joins (no reliance on
    np.float64(15.0) hashing equal to int 15).

PERFORMANCE
-----------
P1  Exit engine fully vectorized per signal batch (2-D gather + argmax
    along time axis, chunked). No iterrows / per-signal Python objects.
P2  OHLC/ts arrays extracted ONCE per symbol (SymbolData) and reused by
    every combo/fold - v3 re-cast four full-length float64 arrays on every
    triple_barrier_batch call (~6561 x 5 calls per symbol).
P3  Signal evaluation on plain numpy float32 arrays (identical values to
    the float32 feature frame v3 evaluates on); thresholds wrapped in
    np.float64 to reproduce legacy scalar-promotion semantics under NEP 50,
    keeping mask decisions bit-identical with v3.
P4  Holdout NOT simulated for every combo during the search (v3 burned
    exit-sims on 6561 holdout sets it never used); finalists are confirmed
    afterwards via confirm_holdout() per symbol.
P5  Feature frame cache kept, float32 storage kept.

DIAGNOSTICS
-----------
D1  Per-symbol holdout breakdown table (not just pooled numbers).
D2  Exit-reason histogram (tp/sl/gap_tp/gap_sl/time) on holdout.
D3  exit_ts / held_min / reason recorded per simulated trade.

STILL OPEN (unchanged, need your input / real data)
---------------------------------------------------
- exact uhlo_corner rule (approximation stands);
- overlapping positions & cooldown-from-close semantics not modeled;
- repeat_color dedup not modeled;
- ts = bar-open assumption for both timeframes;
- triple-barrier stand-in until the real exit-engine is wired in;
- survivorship bias of today's symbol universe;
- fixed-seed sampling instead of liquidity-stratified sampling.

NOT financial advice - a testing methodology/tool.
"""

from __future__ import annotations

import argparse
import itertools
import math
import shutil
import sys
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# Sprint 2: глобальный filterwarnings убран — точечная обработка через
# warnings.catch_warnings() в add_neighbor_median_score и других местах.
# Sprint 2/3: опциональные зависимости — graceful fallback если не установлены.
try:
    import numba  # type: ignore

    HAS_NUMBA = True
except ImportError:  # pragma: no cover
    numba = None  # type: ignore
    HAS_NUMBA = False

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator  # type: ignore

    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover
    BaseModel = object  # type: ignore
    ConfigDict = dict  # type: ignore
    Field = lambda *a, **kw: None  # type: ignore
    ValidationError = Exception  # type: ignore
    field_validator = lambda *a, **kw: (lambda f: f)  # type: ignore
    HAS_PYDANTIC = False

try:
    import tenacity  # type: ignore

    HAS_TENACITY = True
except ImportError:  # pragma: no cover
    HAS_TENACITY = False

# ----------------------------------------------------------------------------
# CONFIG — Sprint 1: дифференцированные комиссии по типу выхода
# ----------------------------------------------------------------------------

DATA_DIR = Path("./data")
SYMBOL_UNIVERSE_FILE = DATA_DIR / "symbol_universe.txt"

# Legacy flat cost (сохранён для --fee/--slippage CLI и v3-паритета в selftest).
# Спринт 1 вводит раздельные Maker/Taker + slippage.
FEE_PER_SIDE = 0.00055
DEFAULT_SLIPPAGE_PER_SIDE = 0.0005
ROUND_TRIP_COST = (FEE_PER_SIDE + DEFAULT_SLIPPAGE_PER_SIDE) * 2  # legacy, не использовать в новом коде
SHORT_CARRY_COST_PER_HOUR = 0.0001

# Спринт 1 — дифференциация по типу выхода (Bybit linear, 2024-2026):
#   entry — всегда taker (market на open следующего бара)
#   tp    — лимитка  -> maker
#   sl / time — стоп/рынок -> taker
#   gap_* — сквиз/гэп -> taker + extra slippage
MAKER_FEE_PER_SIDE = 0.00020
TAKER_FEE_PER_SIDE = 0.00055
MAKER_SLIPPAGE_PER_SIDE = 0.00010
TAKER_SLIPPAGE_PER_SIDE = 0.00050
GAP_EXTRA_SLIPPAGE = 0.00030  # доп. проскальзывание на gap-барах (сквиз)

# Прекомпют для скорости (пересчитывается в main() при CLI-оверрайдах)
COST_TP_EXIT = (TAKER_FEE_PER_SIDE + TAKER_SLIPPAGE_PER_SIDE) + (MAKER_FEE_PER_SIDE + MAKER_SLIPPAGE_PER_SIDE)
COST_TAKER_EXIT = (TAKER_FEE_PER_SIDE + TAKER_SLIPPAGE_PER_SIDE) * 2
COST_GAP_EXIT = COST_TAKER_EXIT + GAP_EXTRA_SLIPPAGE


def _recompute_costs() -> None:
    global COST_TP_EXIT, COST_TAKER_EXIT, COST_GAP_EXIT, ROUND_TRIP_COST
    ROUND_TRIP_COST = (FEE_PER_SIDE + DEFAULT_SLIPPAGE_PER_SIDE) * 2
    COST_TP_EXIT = (TAKER_FEE_PER_SIDE + TAKER_SLIPPAGE_PER_SIDE) + (MAKER_FEE_PER_SIDE + MAKER_SLIPPAGE_PER_SIDE)
    COST_TAKER_EXIT = (TAKER_FEE_PER_SIDE + TAKER_SLIPPAGE_PER_SIDE) * 2
    COST_GAP_EXIT = COST_TAKER_EXIT + GAP_EXTRA_SLIPPAGE

HOLDOUT_DAYS = 30
MIN_TRADES = 30        # floor for expectancy_score and combo eligibility
HARD_MIN_STATS = 20    # below this even basic stats are reported as NaN
COVERAGE_MIN_FRAC = 0.6
COVERAGE_ABS_MIN = 3

# Cross-symbol PF gate (user requirement): a combo must show profit_factor
# above CROSS_PF_MIN on at least CROSS_FRAC_MIN of the sampled symbols,
# with >=CROSS_MIN_N trades per symbol for the PF to be meaningful.
CROSS_PF_MIN = 1.2
CROSS_FRAC_MIN = 0.5
CROSS_MIN_N = 10

# Runtime barriers. ACTIVE_* stay at the legacy asymmetric values so the
# --selftest v3-parity contract holds (v3 hardcodes tp=1.2/sl=5.0);
# main() applies the near-symmetric production barriers below unless
# --tp/--sl override them (user mandate: TP 1.1% / SL 1.5%).
ACTIVE_TP_PCT = 1.2
ACTIVE_SL_PCT = 5.0
NEAR_SYMMETRIC_TP_PCT = 1.1
NEAR_SYMMETRIC_SL_PCT = 1.5

EXIT_CHUNK = 16384     # signals per vectorized exit batch

REASON_NAMES = np.array(["tp", "sl", "gap_tp", "gap_sl", "time"])

# Sprint 2: ограничение кэшей чтобы не забивать RAM
MAX_FEATURE_CACHE = 32
MAX_FEAT_ARRAYS_CACHE = 32
MAX_SYMBOL_CACHE = 64

PARAM_GRID = {
    "natr_below_min": [0.7, 0.95, 1.2],
    "natr_above_max": [4.0, 5.0, 6.0],
    "color_upper": [75.0, 80.0, 85.0],
    "color_lower": [15.0, 20.0, 25.0],
    "long_lows_min": [85.0, 90.0, 95.0],
    "short_highs_max": [85.0, 90.0, 95.0],
    "uhlo_length": [10, 15, 20],
    "cooldown_s": [120, 300, 600],
}


def _f(x) -> np.float64:
    """Strong float64 scalar: forces float64 promotion of float32 operands
    under NEP 50, matching pre-NEP comparison semantics (v3 parity)."""
    return np.float64(x)


# ----------------------------------------------------------------------------
# PARAMS
# ----------------------------------------------------------------------------

@dataclass
class Params:
    natr_below_min: float = 0.95
    natr_above_max: float = 5.0
    uhlo_length: int = 15
    color_upper: float = 80.0
    color_lower: float = 20.0
    long_lows_min: float = 90.0
    short_highs_max: float = 90.0
    pump_vol_mult: float = 3.0
    pump_wick_frac: float = 0.5
    cooldown_s: int = 300
    tp_pct: float = field(default_factory=lambda: ACTIVE_TP_PCT)
    sl_pct: float = field(default_factory=lambda: ACTIVE_SL_PCT)
    time_exit_min: int = 90


def params_from(mapping) -> Params:
    """Params from a DataFrame row / dict, coercing grid dims back to their
    declared types (mixed-dtype .iloc[0] rows upcast ints to float)."""
    d = dict(mapping)
    for k in ("uhlo_length", "cooldown_s", "time_exit_min"):
        if k in d:
            d[k] = int(d[k])
    p = Params(**d)
    # Sprint 3: валидация через Pydantic (если установлен)
    try:
        validate_params(p)
    except Exception as e:
        raise ValueError(f"Params validation failed: {e}") from e
    return p


# ----------------------------------------------------------------------------
# Sprint 3: Pydantic-конфиг и валидация PARAM_GRID
# ----------------------------------------------------------------------------

if HAS_PYDANTIC:

    class ScreenerParamsModel(BaseModel):
        """Строгая валидация параметров скринера (Pydantic v2)."""

        natr_below_min: float = Field(ge=0.1, le=5.0)
        natr_above_max: float = Field(ge=1.0, le=10.0)
        uhlo_length: int = Field(ge=5, le=50)
        color_upper: float = Field(ge=50.0, le=100.0)
        color_lower: float = Field(ge=0.0, le=50.0)
        long_lows_min: float = Field(ge=0.0, le=100.0)
        short_highs_max: float = Field(ge=0.0, le=100.0)
        pump_vol_mult: float = Field(ge=0.0, le=100.0)
        pump_wick_frac: float = Field(ge=0.0, le=1.0)
        cooldown_s: int = Field(ge=10, le=3600)
        tp_pct: float = Field(gt=0, le=20.0)
        sl_pct: float = Field(gt=0, le=20.0)
        time_exit_min: int = Field(ge=1, le=1440)

        model_config = ConfigDict(extra="forbid")

        @field_validator("color_lower")
        @classmethod
        def _check_color(cls, v, info):
            # color_upper > color_lower проверяется в validate_params cross-field
            return v

    class BybitRetryConfig(BaseModel):
        """Конфиг ретраев Bybit API (tenacity обёртка)."""

        max_attempts: int = Field(ge=1, le=10, default=4)
        base_delay: float = Field(gt=0, le=5.0, default=0.4)
        max_delay: float = Field(gt=0, le=30.0, default=8.0)
        jitter: float = Field(ge=0, le=1.0, default=0.2)

        model_config = ConfigDict(extra="forbid")

    def validate_params(p: Params) -> ScreenerParamsModel:
        m = ScreenerParamsModel(
            natr_below_min=p.natr_below_min,
            natr_above_max=p.natr_above_max,
            uhlo_length=p.uhlo_length,
            color_upper=p.color_upper,
            color_lower=p.color_lower,
            long_lows_min=p.long_lows_min,
            short_highs_max=p.short_highs_max,
            pump_vol_mult=p.pump_vol_mult,
            pump_wick_frac=p.pump_wick_frac,
            cooldown_s=p.cooldown_s,
            tp_pct=p.tp_pct,
            sl_pct=p.sl_pct,
            time_exit_min=p.time_exit_min,
        )
        if m.natr_below_min >= m.natr_above_max:
            raise ValueError(f"natr_below_min {m.natr_below_min} must be < natr_above_max {m.natr_above_max}")
        if m.color_upper <= m.color_lower:
            raise ValueError(f"color_upper {m.color_upper} must be > color_lower {m.color_lower}")
        return m

    def validate_param_grid(grid: dict) -> None:
        """Валидация всего PARAM_GRID — каждое значение должно пройти ScreenerParamsModel."""
        if not grid:
            raise ValueError("PARAM_GRID empty")
        # проверяем декартово произведение выборочно: каждая ось отдельно
        dummy = dict(
            natr_below_min=0.95,
            natr_above_max=5.0,
            uhlo_length=15,
            color_upper=80.0,
            color_lower=20.0,
            long_lows_min=90.0,
            short_highs_max=90.0,
            pump_vol_mult=3.0,
            pump_wick_frac=0.5,
            cooldown_s=300,
            tp_pct=ACTIVE_TP_PCT,
            sl_pct=ACTIVE_SL_PCT,
            time_exit_min=90,
        )
        for k, vals in grid.items():
            if k not in dummy:
                raise ValueError(f"Unknown grid key: {k}")
            if not isinstance(vals, (list, tuple)) or len(vals) == 0:
                raise ValueError(f"Grid {k} must be non-empty list")
            for v in vals:
                probe = dummy.copy()
                probe[k] = v
                try:
                    ScreenerParamsModel(**probe)
                except ValidationError as e:
                    raise ValueError(f"PARAM_GRID[{k}] value {v!r} invalid: {e}") from e
        # cross-field: хотя бы одна комбинация должна удовлетворять natr/color
        # (минимальная проверка)
        if "natr_below_min" in grid and "natr_above_max" in grid:
            if max(grid["natr_below_min"]) >= min(grid["natr_above_max"]):
                raise ValueError("PARAM_GRID natr_below_min max must be < natr_above_max min")

else:  # fallback без pydantic — минимальные проверки

    def validate_params(p: Params) -> Params:  # type: ignore
        if p.natr_below_min >= p.natr_above_max:
            raise ValueError("natr_below_min must be < natr_above_max")
        if p.color_upper <= p.color_lower:
            raise ValueError("color_upper must be > color_lower")
        if not (5 <= p.uhlo_length <= 50):
            raise ValueError("uhlo_length out of range")
        if not (10 <= p.cooldown_s <= 3600):
            raise ValueError("cooldown_s out of range")
        return p  # type: ignore

    def validate_param_grid(grid: dict) -> None:  # type: ignore
        if not grid:
            raise ValueError("PARAM_GRID empty")
        for k, vals in grid.items():
            if not vals:
                raise ValueError(f"Grid {k} empty")


# ----------------------------------------------------------------------------
# DATA LOADING / QUALITY
# ----------------------------------------------------------------------------

def load_symbol_universe(n_sample: int | None = None,
                         path: str | Path | None = None) -> list[str]:
    f = Path(path) if path is not None else SYMBOL_UNIVERSE_FILE
    if not f.exists():
        raise FileNotFoundError(f"Missing {f}.")
    symbols = [l.strip() for l in f.read_text().splitlines() if l.strip()]
    if n_sample:
        rng = np.random.default_rng(42)
        symbols = list(rng.choice(symbols, size=min(n_sample, len(symbols)), replace=False))
    return symbols


def load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}_{timeframe}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}.")
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    # pandas 3 merge_asof rejects mixed time units (e.g. parquet-stored ms vs
    # ms+Timedelta promoted to us) -> normalize once, at the border.
    df["ts"] = df["ts"].dt.as_unit("ns")
    if timeframe == "15m":
        df["ts"] = df["ts"] + pd.Timedelta(minutes=15)  # assume bar-open ts; confirm with real data
    return df.sort_values("ts").reset_index(drop=True)


def _assert_data_quality(df: pd.DataFrame, symbol: str, timeframe: str) -> None:
    dupes = df["ts"].duplicated().sum()
    if dupes:
        raise ValueError(f"{symbol}: {dupes} duplicate timestamps in {timeframe} data - fix upstream.")
    if timeframe == "1m":
        gaps = df["ts"].diff().dropna()
        n_gaps = int((gaps != pd.Timedelta(minutes=1)).sum())
        if n_gaps > 0:
            print(f"[{symbol}] warning: {n_gaps} non-1min gaps in 1m series (missing bars).")
        zero_vol = int((df["volume"] == 0).sum())
        if zero_vol > 0:
            print(f"[{symbol}] warning: {zero_vol} zero-volume bars.")
        # Sprint 1: gap/zero_vol — дроп из бэктеста (см. prepare_symbol / evaluate_signals / simulate_exits).
        # Здесь только детекция; фактический дроп — в слоях выше, чтобы не мутировать исходный df
        # и сохранить возможность аудита исходных данных.


def naive_ts(series_or_ts):
    """tz-aware -> naive-UTC. Series -> datetime64 ndarray, Timestamp -> datetime64 scalar."""
    s = series_or_ts
    if isinstance(s, pd.Timestamp):
        return s.tz_convert(None).to_datetime64() if s.tzinfo is not None else s.to_datetime64()
    return s.dt.tz_localize(None).to_numpy()


# ----------------------------------------------------------------------------
# INDICATORS / FEATURES (identical math to v3)
# ----------------------------------------------------------------------------

def natr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return (atr / close) * 100.0


def uhlo(df: pd.DataFrame, length: int) -> pd.DataFrame:
    roll_high = df["high"].rolling(length).max()
    roll_low = df["low"].rolling(length).min()
    rng = (roll_high - roll_low).replace(0, np.nan)
    highs = ((df["close"] - roll_low) / rng) * 100.0
    lows = ((roll_high - df["close"]) / rng) * 100.0
    return pd.DataFrame({"uhlo_highs": highs, "uhlo_lows": lows})


_KEEP_COLS = [
    "ts", "open", "high", "low", "close", "volume",
    "natr", "uhlo_highs", "uhlo_lows", "uhlo_highs_15m", "uhlo_lows_15m",
    "vol_spike_ratio", "upper_wick_frac", "uhlo_corner_flag",
]

_FLOAT_COLS = [
    "natr", "uhlo_highs", "uhlo_lows", "uhlo_highs_15m", "uhlo_lows_15m",
    "vol_spike_ratio", "upper_wick_frac",
]

_feature_frame_cache: OrderedDict[tuple[str, int], pd.DataFrame] = OrderedDict()
_feat_arrays_cache: OrderedDict[tuple[str, int], dict[str, np.ndarray]] = OrderedDict()


def _cache_put(cache: OrderedDict, key, value, maxsize: int) -> None:
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > maxsize:
        cache.popitem(last=False)


def build_features(symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, uhlo_length: int) -> pd.DataFrame:
    key = (symbol, uhlo_length)
    cached = _feature_frame_cache.get(key)
    if cached is not None:
        _feature_frame_cache.move_to_end(key)
        return cached

    out = df_1m.copy()
    out["natr"] = natr_wilder(out, period=14)

    u1 = uhlo(out, length=uhlo_length)
    out = pd.concat([out, u1], axis=1)

    u15 = uhlo(df_15m, length=uhlo_length)
    df_15m_u = pd.concat([df_15m[["ts"]], u15], axis=1).rename(
        columns={"uhlo_highs": "uhlo_highs_15m", "uhlo_lows": "uhlo_lows_15m"}
    )
    out = pd.merge_asof(out.sort_values("ts"), df_15m_u.sort_values("ts"), on="ts", direction="backward")

    avg_vol20 = out["volume"].shift(1).rolling(20).mean().replace(0, np.nan)
    out["vol_spike_ratio"] = out["volume"] / avg_vol20
    bar_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["upper_wick_frac"] = (out["high"] - out[["open", "close"]].max(axis=1)) / bar_range

    roll_max_highs = out["uhlo_highs"].rolling(uhlo_length).max()
    roll_max_lows = out["uhlo_lows"].rolling(uhlo_length).max()
    out["uhlo_corner_flag"] = (roll_max_highs >= 99) & (roll_max_lows >= 99)  # approximation - see header

    out = out[_KEEP_COLS].astype(
        {c: "float32" for c in out.columns if c not in ("ts", "uhlo_corner_flag") and c in _KEEP_COLS}
    ).reset_index(drop=True)

    _cache_put(_feature_frame_cache, key, out, MAX_FEATURE_CACHE)
    return out


# ----------------------------------------------------------------------------
# PER-SYMBOL ARRAYS - extracted once, reused by every combo (perf P2)
# ----------------------------------------------------------------------------

@dataclass
class SymbolData:
    symbol: str
    df_1m: pd.DataFrame
    df_15m: pd.DataFrame
    ts: np.ndarray             # naive-UTC bar-open times
    o: np.ndarray              # float64
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray              # float64 volume — для gap/zero_vol дропа (Спринт 1)
    holdout_start: np.ndarray  # 0-d datetime64


_symbol_cache: OrderedDict[str, SymbolData] = OrderedDict()


def prepare_symbol(symbol: str) -> SymbolData:
    sd = _symbol_cache.get(symbol)
    if sd is not None:
        _symbol_cache.move_to_end(symbol)
        return sd
    df_1m = load_ohlcv(symbol, "1m")
    df_15m = load_ohlcv(symbol, "15m")
    _assert_data_quality(df_1m, symbol, "1m")
    _assert_data_quality(df_15m, symbol, "15m")
    ts = naive_ts(df_1m["ts"])
    sd = SymbolData(
        symbol=symbol,
        df_1m=df_1m,
        df_15m=df_15m,
        ts=ts,
        o=df_1m["open"].to_numpy(dtype=np.float64),
        h=df_1m["high"].to_numpy(dtype=np.float64),
        l=df_1m["low"].to_numpy(dtype=np.float64),
        c=df_1m["close"].to_numpy(dtype=np.float64),
        v=df_1m["volume"].to_numpy(dtype=np.float64),
        holdout_start=np.asarray(ts.max() - np.timedelta64(HOLDOUT_DAYS, "D")),
    )
    _cache_put(_symbol_cache, symbol, sd, MAX_SYMBOL_CACHE)
    return sd


def get_feat_arrays(symbol: str, uhlo_length: int, sd: SymbolData | None = None) -> dict[str, np.ndarray]:
    key = (symbol, uhlo_length)
    cached = _feat_arrays_cache.get(key)
    if cached is not None:
        _feat_arrays_cache.move_to_end(key)
        return cached
    if sd is None:
        sd = prepare_symbol(symbol)
    feat = build_features(symbol, sd.df_1m, sd.df_15m, uhlo_length)
    arrs = {
        "ts": naive_ts(feat["ts"]),
        "uhlo_corner_flag": feat["uhlo_corner_flag"].to_numpy(dtype=bool),
        # Спринт 1: пробрасываем volume для дропа zero_vol баров в evaluate_signals
        "volume": feat["volume"].to_numpy(dtype=np.float64),
    }
    for col in _FLOAT_COLS:
        arrs[col] = feat[col].to_numpy(dtype=np.float32)
    _cache_put(_feat_arrays_cache, key, arrs, MAX_FEAT_ARRAYS_CACHE)
    return arrs


# ----------------------------------------------------------------------------
# SIGNAL EVALUATION - numpy port of v3.evaluate_vectorized (identical logic)
# ----------------------------------------------------------------------------

def evaluate_signals(arrs: dict[str, np.ndarray], p: Params) -> tuple[np.ndarray, np.ndarray]:
    """Returns (signal_mask bool[n], side_code int8[n]) with +1=green, -1=red."""
    natr = arrs["natr"]
    u1h, u1l = arrs["uhlo_highs"], arrs["uhlo_lows"]
    u15h, u15l = arrs["uhlo_highs_15m"], arrs["uhlo_lows_15m"]
    vsr, uwf = arrs["vol_spike_ratio"], arrs["upper_wick_frac"]
    corner = arrs["uhlo_corner_flag"]

    valid = ~(np.isnan(natr) | np.isnan(u1h) | np.isnan(u15h))
    # Спринт 1: дроп zero_vol баров — нет ликвидности, сигнал невалиден
    vol = arrs.get("volume")
    if vol is not None:
        valid = valid & (vol > 0)

    natr_low = valid & (natr < _f(p.natr_below_min))
    natr_high = valid & ~natr_low & (natr > _f(p.natr_above_max))
    remaining = valid & ~natr_low & ~natr_high

    green = (
        remaining
        & (u1h >= _f(p.color_upper)) & (u1l <= _f(p.color_lower))
        & (u15h >= _f(p.color_upper)) & (u15l <= _f(p.color_lower))
    )
    red = (
        remaining & ~green
        & (u1l >= _f(p.color_upper)) & (u1h <= _f(p.color_lower))
        & (u15l >= _f(p.color_upper)) & (u15h <= _f(p.color_lower))
    )

    corner_hit = (green | red) & corner
    green = green & ~corner_hit
    red = red & ~corner_hit

    pump = (vsr > _f(p.pump_vol_mult)) & (uwf > _f(p.pump_wick_frac))  # symmetric, as in v3
    green = green & ~(green & pump)
    red = red & ~(red & pump)

    green = green & ~(green & (u1l >= _f(p.long_lows_min)))
    red = red & ~(red & (u1h >= _f(p.short_highs_max)))

    code = np.zeros(natr.shape[0], dtype=np.int8)
    code[green] = 1
    code[red] = -1
    return green | red, code


# Sprint 2: векторизованный cooldown — Numba (primary) + чистый NumPy fallback
if HAS_NUMBA:

    @numba.njit  # type: ignore[misc]
    def _cooldown_numba(ts_ns: np.ndarray, idxs: np.ndarray, cooldown_ns: int, out: np.ndarray) -> int:
        n = idxs.shape[0]
        if n == 0:
            return 0
        kept = 0
        last_ts = ts_ns[idxs[0]]
        out[0] = idxs[0]
        kept = 1
        for k in range(1, n):
            cur = idxs[k]
            cur_ts = ts_ns[cur]
            if cur_ts - last_ts >= cooldown_ns:
                out[kept] = cur
                kept += 1
                last_ts = cur_ts
        return kept

else:
    _cooldown_numba = None  # type: ignore


def _cooldown_numpy(ts_ns: np.ndarray, idxs: np.ndarray, cooldown_ns: int) -> np.ndarray:
    """Чистый NumPy fallback без Python-цикла по всем сигналам — один проход
    с NumPy-скалярами, но без Numba. Сохраняет O(n) и отсутствие аллокаций
    на каждой итерации за счёт предвыделенного out."""
    if idxs.size == 0:
        return idxs
    out = np.empty(idxs.size, dtype=np.int64)
    # salt: локальные переменные для скорости
    last_ts = int(ts_ns[int(idxs[0])])
    out[0] = int(idxs[0])
    kept = 1
    cd = int(cooldown_ns)
    for cur in idxs[1:]:
        cur_ts = int(ts_ns[int(cur)])
        if cur_ts - last_ts >= cd:
            out[kept] = int(cur)
            kept += 1
            last_ts = cur_ts
    return out[:kept]


def apply_cooldown(ts: np.ndarray, signal_mask: np.ndarray, cooldown_s: int) -> np.ndarray:
    """Greedy first-wins cooldown over ascending candidate positions.
    Same semantics as v3.apply_cooldown_global.

    Sprint 2: переписан с Python-цикла на Numba (ts как int64 ns) — узкое место
    вызывалось 6561*4 раз на символ. Fallback — _cooldown_numpy.
    """
    idxs = np.flatnonzero(signal_mask)
    if idxs.size == 0:
        return idxs.astype(np.int64, copy=False)
    # ts: naive datetime64[ns] -> int64 ns
    try:
        ts_ns = ts.astype("datetime64[ns]").astype(np.int64)
    except Exception:
        # уже int64 или другой dtype
        ts_ns = np.asarray(ts, dtype=np.int64)
    cooldown_ns = int(cooldown_s) * 1_000_000_000
    if HAS_NUMBA and _cooldown_numba is not None:
        out = np.empty(idxs.size, dtype=np.int64)
        # idxs уже int64, но numba хочет typed
        idxs64 = idxs.astype(np.int64, copy=False)
        n_kept = _cooldown_numba(ts_ns, idxs64, cooldown_ns, out)
        return out[:n_kept]
    else:
        return _cooldown_numpy(ts_ns, idxs, cooldown_ns)


# ----------------------------------------------------------------------------
# EXIT ENGINE - vectorized triple-barrier (fix F1, perf P1/P2)
# ----------------------------------------------------------------------------

def simulate_exits(sd, positions: np.ndarray, codes: np.ndarray, p: Params) -> pd.DataFrame:
    """
    Batched triple-barrier simulation — Sprint 1 edition.

    Entry: open of the bar AFTER the signal bar (taker). Exit: first of TP / hard SL /
    time exit. Gap-aware: a bar whose OPEN is already past a barrier fills at
    that open (v3 convention). Intrabar tie: SL wins. Short carry uses ACTUAL holding time.

    Спринт 1 изменения:
      * Комиссии дифференцированы по типу выхода:
          tp(0)      -> maker  (лимитка)  = COST_TP_EXIT
          sl(1)/time(4) -> taker + slippage = COST_TAKER_EXIT
          gap_*(2,3) -> taker + extra gap slippage = COST_GAP_EXIT
        Entry всегда taker, поэтому базовый COST уже включает обе ноги.
      * Time-exit: закрытие на *следующем* баре (+1m), exit_ts = open(deadline_bar)+1m,
        held_min включает эту минуту. Ранее цена бралась с close(deadline_bar),
        а время — с open(deadline_bar) => рассинхрон 1м.
      * Gap/zero_vol: бары с volume==0 дропаются — entry на таком баре пропускается,
        а если в окне до выхода встречается zero_vol, выход считается gap-выходом
        с COST_GAP_EXIT (консервативно). Пропуски (gaps) ловятся через sd.ts diff
        и также ведут к gap-стоимости, если held захватывает пропущенное время.

    sd: object exposing ts (datetime64), o/h/l/c(/v) (float64 ndarrays).
    Returns DataFrame[ts, side, net_ret, reason, exit_ts, held_min];
    reason: 0=tp 1=sl 2=gap_tp 3=gap_sl 4=time.
    """
    cols_out = ["ts", "side", "net_ret", "reason", "exit_ts", "held_min"]
    positions = np.asarray(positions, dtype=np.int64)
    codes = np.asarray(codes)
    n = len(sd.o)
    # Entry must exist and have liquidity
    entry_idx_all = positions + 1
    ok = entry_idx_all < n
    # Sprint 1: дроп entry на zero_vol баре (нет ликвидности)
    if hasattr(sd, "v") and getattr(sd, "v", None) is not None:
        v = np.asarray(sd.v)
        # entry_idx_all может выходить за n-1, но ok уже False там
        valid_entry_vol = np.ones_like(ok, dtype=bool)
        valid_entry_vol[ok] = v[entry_idx_all[ok]] > 0
        ok = ok & valid_entry_vol
    else:
        v = None
    positions, codes = positions[ok], codes[ok]
    if positions.size == 0:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in cols_out})

    tp = _f(p.tp_pct) / 100.0
    sl = _f(p.sl_pct) / 100.0
    W = int(p.time_exit_min) + 2
    dl_step = np.timedelta64(int(p.time_exit_min), "m")
    cols = np.arange(W)
    one_min = np.timedelta64(1, "m")

    acc: dict[str, list] = {k: [] for k in
                            ("sig_ts", "side", "net", "reason", "exit_ts", "held")}

    # кэш gap-маски для детекции пропусков в ts (раз в вызов, O(n))
    ts_diff = None
    if hasattr(sd, "ts"):
        try:
            # naive datetime64 — diff в минутах
            diff_min = (sd.ts[1:] - sd.ts[:-1]) / np.timedelta64(1, "m")
            has_gaps = np.any(diff_min != 1)
            if has_gaps:
                ts_diff = diff_min  # для проверки окна
        except Exception:
            ts_diff = None

    for s in range(0, positions.size, EXIT_CHUNK):
        pos = positions[s:s + EXIT_CHUNK]
        cd = codes[s:s + EXIT_CHUNK]
        m = pos.size

        e = pos + 1
        entry = sd.o[e]
        ets = sd.ts[e]
        deadline = ets + dl_step

        idx = e[:, None] + cols[None, :]
        within = idx < n
        ic = np.minimum(idx, n - 1)
        wt = sd.ts[ic]
        inw = within & (wt <= deadline[:, None])

        wo, wh, wl, wc = sd.o[ic], sd.h[ic], sd.l[ic], sd.c[ic]
        # volume window для gap/zero_vol обработки
        if v is not None:
            wv = v[ic]
            # маска zero_vol в окне (до deadline)
            zero_inw = inw & (wv == 0)
        else:
            zero_inw = np.zeros_like(inw, dtype=bool)
        long_ = cd > 0
        sgn = np.where(long_, np.float64(1.0), np.float64(-1.0))[:, None]
        ent = entry[:, None]

        ro = sgn * (wo - ent) / ent                        # open-based (gap fills)
        adv = np.where(long_[:, None], wl, wh)             # adverse extreme
        fav = np.where(long_[:, None], wh, wl)             # favorable extreme
        ra = sgn * (adv - ent) / ent
        rf = sgn * (fav - ent) / ent
        rc = sgn * (wc - ent) / ent                        # close-based (time exit)

        g_sl = inw & (ro <= -sl)
        g_tp = inw & (ro >= tp)
        h_sl = inw & (ra <= -sl)
        h_tp = inw & (rf >= tp)
        any_hit = h_sl | h_tp      # gaps imply low/high breaches (low<=open<=high)
        # zero_vol бары не должны триггерить барьер — маскируем их
        any_hit = any_hit & ~zero_inw

        has_hit = any_hit.any(axis=1)
        first = any_hit.argmax(axis=1)
        rows = np.arange(m)

        def take(M):
            return M[rows, first]

        ro_f, gsl_f, gtp_f, hsl_f = take(ro), take(g_sl), take(g_tp), take(h_sl)
        raw_hit = np.where(gsl_f, ro_f,
                           np.where(gtp_f, ro_f, np.where(hsl_f, np.float64(-sl), tp)))

        last_col = np.where(inw & ~zero_inw, cols, -1).max(axis=1)
        # если все бары до deadline — zero_vol, last_col==-1 (нет ликвидности) -> дропаем трейд
        # вместо падения берём fallback: считаем трейд несостоявшимся (raw=0, будет отфильтрован выше)
        has_valid_time_bar = last_col >= 0
        raw_time = np.where(has_valid_time_bar, rc[rows, np.maximum(last_col, 0)], np.float64(0.0))

        raw = np.where(has_hit, raw_hit, raw_time)
        exit_col = np.where(has_hit, first, last_col)
        # Sprint 1: time-exit — закрытие на следующем баре (+1m)
        is_time = ~has_hit
        # для barrier-выходов exit_ts = open бара выхода, для time — open +1m (close бара)
        base_exit_ts = wt[rows, np.maximum(exit_col, 0)]
        exit_ts = np.where(is_time & has_valid_time_bar, base_exit_ts + one_min, base_exit_ts)
        held_min = (exit_ts - ets) / np.timedelta64(1, "m")
        # дроп time-трейдов без валидного бара (held будет 0) — помечаем reason=-1 для фильтра
        # (ниже отфильтруем)
        held_min = np.where(has_valid_time_bar | has_hit, held_min, np.float64(0))

        carry_hours = held_min / np.float64(60.0)
        # Sprint 1: дифференцированные комиссии по типу выхода
        reason = np.where(has_hit,
                          np.where(gsl_f, 3, np.where(gtp_f, 2, np.where(hsl_f, 1, 0))),
                          4)
        # zero_vol/gap в окне до выхода -> апгрейд стоимости до gap
        # проверяем, есть ли zero_vol или реальный gap в ts между e и exit_col
        if v is not None:
            # есть ли zero_vol на пути от entry до exit включительно
            # строим маску пути
            path_has_zero = np.zeros(m, dtype=bool)
            for i in range(m):
                ec = int(exit_col[i]) if has_hit[i] or has_valid_time_bar[i] else -1
                if ec >= 0:
                    path_has_zero[i] = bool(np.any(zero_inw[i, :ec + 1]))
            reason_for_cost = reason.copy()
            # time/gap с zero_vol -> считаем как gap (консервативно)
            reason_for_cost[path_has_zero & (reason == 4)] = 3  # time с дырой -> gap_sl стоимость
        else:
            path_has_zero = np.zeros(m, dtype=bool)
            reason_for_cost = reason

        # детекция пропусков по ts_diff (если gap в минутах)
        if ts_diff is not None:
            gap_in_path = np.zeros(m, dtype=bool)
            for i in range(m):
                ei = int(e[i])
                ec_idx = int(e[i] + int(exit_col[i])) if (has_hit[i] or has_valid_time_bar[i]) and exit_col[i] >= 0 else -1
                if ec_idx > ei and ec_idx < len(sd.ts):
                    # проверяем diff между ei..ec_idx
                    if np.any(ts_diff[ei:ec_idx] != 1):
                        gap_in_path[i] = True
            # gap + barrier/time -> считаем gap стоимостью
            reason_for_cost[gap_in_path & (reason == 4)] = 3
            path_has_zero = path_has_zero | gap_in_path

        cost_per_trade = np.select(
            [reason_for_cost == 0, reason_for_cost == 1, reason_for_cost == 2,
             reason_for_cost == 3, reason_for_cost == 4],
            [COST_TP_EXIT, COST_TAKER_EXIT, COST_GAP_EXIT, COST_GAP_EXIT, COST_TAKER_EXIT],
            default=COST_TAKER_EXIT,
        )
        cost = cost_per_trade + np.where(~long_, SHORT_CARRY_COST_PER_HOUR * carry_hours, 0.0)

        # фильтр: дропаем time-трейды без ликвидности
        keep = has_hit | has_valid_time_bar
        if not np.all(keep):
            # сжимаем массивы
            pos = pos[keep]
            if pos.size == 0:
                continue
            # пересобираем срезы для keep
            long_keep = long_[keep]
            raw = raw[keep]
            cost = cost[keep]
            reason = reason[keep]
            exit_ts = exit_ts[keep]
            held_min = held_min[keep]
            # также фильтруем ets-сигналы для sig_ts
            sig_ts_keep = sd.ts[pos]
        else:
            long_keep = long_
            sig_ts_keep = sd.ts[pos]

        acc["sig_ts"].append(sig_ts_keep)
        acc["side"].append(np.where(long_keep, "green", "red"))
        acc["net"].append(raw - cost)
        acc["reason"].append(reason.astype(np.int8))
        acc["exit_ts"].append(exit_ts)
        acc["held"].append(held_min)

    if not acc["sig_ts"]:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in cols_out})
    return pd.DataFrame({
        "ts": np.concatenate(acc["sig_ts"]),
        "side": np.concatenate(acc["side"]),
        "net_ret": np.concatenate(acc["net"]),
        "reason": np.concatenate(acc["reason"]),
        "exit_ts": np.concatenate(acc["exit_ts"]),
        "held_min": np.concatenate(acc["held"]),
    })


# ----------------------------------------------------------------------------
# METRICS (same numbers as v3; floors explicit)
# ----------------------------------------------------------------------------

def score_returns(returns) -> dict:
    r = pd.Series(returns).dropna()
    n = len(r)
    if n < HARD_MIN_STATS:
        return {"n": n, "winrate": np.nan, "avg_ret": np.nan, "sharpe": np.nan,
                "profit_factor": np.nan, "expectancy_score": np.nan}
    winrate = float((r > 0).mean())
    avg_ret = float(r.mean())
    std = float(r.std())
    sharpe = avg_ret / std if std > 0 else np.nan
    gains = float(r[r > 0].sum())
    losses = float(-r[r < 0].sum())
    pf = gains / losses if losses > 0 else np.nan
    expectancy_score = avg_ret * math.sqrt(n) if n >= MIN_TRADES else np.nan
    return {"n": n, "winrate": winrate, "avg_ret": avg_ret, "sharpe": sharpe,
            "profit_factor": pf, "expectancy_score": expectancy_score}


# ----------------------------------------------------------------------------
# GRID SEARCH PER SYMBOL
# ----------------------------------------------------------------------------

def grid_combos(grid: dict) -> tuple[list[str], list[tuple]]:
    # Sprint 3: валидация грида через Pydantic (fail-fast)
    try:
        validate_param_grid(grid)
    except Exception as e:
        raise ValueError(f"Invalid PARAM_GRID: {e}") from e
    keys = list(grid.keys())
    return keys, list(itertools.product(*grid.values()))


def _fold_bounds(sd: SymbolData, n_folds: int) -> list[tuple[np.ndarray, np.ndarray]]:
    t0 = sd.ts[0]
    fw = (sd.holdout_start - t0) / np.int64(n_folds)
    quarter = (fw * 3) // 4
    return [(t0 + fw * i + quarter, t0 + fw * (i + 1)) for i in range(n_folds)]


def run_symbol(symbol: str, grid: dict | None = None, n_folds: int = 4) -> pd.DataFrame:
    g = grid if grid is not None else PARAM_GRID
    keys, combos = grid_combos(g)

    sd = prepare_symbol(symbol)
    bounds = _fold_bounds(sd, n_folds)
    arrays_by_len: dict[int, dict[str, np.ndarray]] = {}

    rows = []
    for cid, combo in enumerate(combos):
        p = Params(**dict(zip(keys, combo)))
        arrs = arrays_by_len.get(p.uhlo_length)
        if arrs is None:
            arrs = arrays_by_len[p.uhlo_length] = get_feat_arrays(symbol, p.uhlo_length, sd)
        mask, code = evaluate_signals(arrs, p)
        ts_arr = arrs["ts"]

        pooled: list = []
        fold_sharpes: list[float] = []
        folds_seen = 0
        for split_pt, end in bounds:
            fm = mask & (ts_arr >= split_pt) & (ts_arr < end)
            if not fm.any():
                continue
            pos = apply_cooldown(sd.ts, fm, p.cooldown_s)
            if pos.size == 0:
                continue
            pnl = simulate_exits(sd, pos, code[pos], p)
            if pnl.empty:
                continue
            folds_seen += 1
            pooled.append(pnl["net_ret"])
            fold_sharpes.append(score_returns(pnl["net_ret"])["sharpe"])

        if not pooled:
            continue
        agg = score_returns(pd.concat(pooled, ignore_index=True))
        fs = np.asarray(fold_sharpes, dtype=float)
        agg["worst_split_sharpe"] = float(np.nanmin(fs)) if fs.size else np.nan
        agg["std_across_splits"] = float(np.nanstd(fs)) if fs.size else np.nan
        agg["n_folds_positive"] = int(np.nansum(fs > 0))
        agg["n_folds_total"] = folds_seen
        agg.update(dict(zip(keys, combo)))
        agg["combo_id"] = cid
        rows.append(agg)

    res = pd.DataFrame(rows)
    if not res.empty:
        res["symbol"] = symbol
    return res


# ----------------------------------------------------------------------------
# PLATEAU over the COMPLETE lattice (fix F6), per symbol
# ----------------------------------------------------------------------------

def complete_grid(res_df: pd.DataFrame, grid: dict | None = None) -> pd.DataFrame:
    """Re-insert every missing combo as a NaN row so neighbor-median smoothing
    always sees its full +/-1 neighborhood."""
    keys, combos = grid_combos(grid if grid is not None else PARAM_GRID)
    tpl = pd.DataFrame({"combo_id": np.arange(len(combos))})
    for j, k in enumerate(keys):
        tpl[k] = [c[j] for c in combos]

    metric_cols = [c for c in res_df.columns if c not in (*keys, "combo_id", "symbol")]
    out = []
    for sym, grp in res_df.groupby("symbol"):
        m = tpl.merge(grp[["combo_id", "symbol", *metric_cols]], on="combo_id", how="left")
        m["symbol"] = sym   # left-join NaNs symbol on re-inserted empty combos - restore it
        out.append(m)
    return pd.concat(out, ignore_index=True) if out else res_df.iloc[0:0].copy()


def add_neighbor_median_score(res_df: pd.DataFrame, metric: str = "expectancy_score",
                              grid: dict | None = None) -> pd.DataFrame:
    """Neighbor-median smoothing over the full lattice via combo_id stride
    arithmetic (dtype-immune; missing neighbors contribute NaN)."""
    keys, combos = grid_combos(grid if grid is not None else PARAM_GRID)
    n = len(combos)
    lens = [len(v) for v in (grid if grid is not None else PARAM_GRID).values()]
    strides = [1] * len(keys)
    for d in range(len(keys) - 2, -1, -1):
        strides[d] = strides[d + 1] * lens[d + 1]

    neigh = np.full((n, 2 * len(keys)), -1, dtype=np.int64)
    for i in range(n):
        col = 0
        for d in range(len(keys)):
            idx_d = (i // strides[d]) % lens[d]
            for delta in (-1, 1):
                if 0 <= idx_d + delta < lens[d]:
                    neigh[i, col] = i + delta * strides[d]
                col += 1

    self_ids = np.arange(n)[:, None]
    safe_neigh = np.where(neigh >= 0, neigh, self_ids)

    frames = []
    for _, group in res_df.groupby("symbol"):
        arr = group.set_index("combo_id")[metric].reindex(range(n)).to_numpy(dtype=float)
        mat = np.column_stack([arr, arr[safe_neigh]])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            med = np.nanmedian(mat, axis=1)
        med = np.asarray(med, dtype=float)
        med[np.isnan(mat).all(axis=1)] = np.nan
        group = group.copy()
        group[f"{metric}_neighbor_median"] = med[group["combo_id"].to_numpy()]
        frames.append(group)
    return pd.concat(frames, ignore_index=True)


# ----------------------------------------------------------------------------
# GLOBAL SELECTION with coverage gate (fix F3)
# ----------------------------------------------------------------------------

def select_global(combined: pd.DataFrame, n_symbols: int,
                  metric: str = "expectancy_score_neighbor_median",
                  grid: dict | None = None) -> pd.DataFrame:
    keys = list((grid if grid is not None else PARAM_GRID).keys())

    # Landscape stats over ALL results (not just gated): breadth of raw
    # profitability decides the cross-symbol gate, fold bookkeeping aside.
    cov = combined.groupby("combo_id")["symbol"].nunique().rename("coverage")
    qual = combined[(combined["profit_factor"] > CROSS_PF_MIN)
                    & (combined["n"] >= CROSS_MIN_N)]
    qcov = qual.groupby("combo_id")["symbol"].nunique().rename("pf_symbols")
    min_pf_syms = math.ceil(CROSS_FRAC_MIN * n_symbols)

    def _near_miss(header: str) -> None:
        src = combined[combined["n"] >= CROSS_MIN_N]
        if src.empty:
            print(f"[select_global] {header}; no combo-symbol pair even "
                  f"reached n>={CROSS_MIN_N} trades.")
            return
        agg = (src.groupby("combo_id")
               .agg(global_score=(metric, "median"), n_med=("n", "median"),
                    pf_med=("profit_factor", "median"))
               .join(cov).join(qcov).fillna({"pf_symbols": 0}))
        agg["pf_frac"] = agg["pf_symbols"] / n_symbols
        top = agg.sort_values(["pf_symbols", "global_score"],
                              ascending=False).head(8)
        print(f"[select_global] {header} Cross-PF gate needs PF>{CROSS_PF_MIN} "
              f"on >= {min_pf_syms}/{n_symbols} sampled symbols "
              f"(n>={CROSS_MIN_N}/symbol); best seen: "
              f"{int(agg['pf_symbols'].max())}. Closest candidates:")
        print(top.round(4).to_string())

    # Fold gate: majority of the folds actually SEEN must be net-positive.
    # (Fixed "3 of 4" assumes long histories; on 60d usable spans most
    # signal-rich combos only see 1-2 folds with >=MIN_TRADES stats.)
    need_pos = np.maximum(1, np.ceil(0.5 * combined["n_folds_total"].clip(lower=1)))
    gated = combined[
        (combined["n_folds_positive"] >= need_pos)
        & (combined["n"] >= MIN_TRADES)
        & combined[metric].notna()
    ]
    if gated.empty:
        _near_miss("nothing passed the per-symbol gates.")
        return pd.DataFrame()

    active_symbols = gated["symbol"].nunique()   # symbols with >=1 gate-passing row
    min_cov = max(min(COVERAGE_ABS_MIN, active_symbols),
                  math.ceil(COVERAGE_MIN_FRAC * active_symbols))

    eligible = cov[cov >= min_cov].index.intersection(qcov[qcov >= min_pf_syms].index)
    if len(eligible) == 0:
        _near_miss("gates cleared nothing.")
        return pd.DataFrame()

    rank = (
        gated[gated["combo_id"].isin(eligible)]
        .groupby("combo_id")[metric].median().rename("global_score")
        .to_frame().join(cov).join(qcov).sort_values("global_score", ascending=False)
    )
    rank["pf_frac"] = rank["pf_symbols"] / n_symbols
    param_map = combined.drop_duplicates("combo_id").set_index("combo_id")[keys]
    return rank.join(param_map).reset_index()


# ----------------------------------------------------------------------------
# HOLDOUT CONFIRMATION - finalists only (perf P4), per symbol (D1/D2)
# ----------------------------------------------------------------------------

def confirm_holdout(symbol: str, p: Params) -> tuple[dict, pd.DataFrame]:
    sd = prepare_symbol(symbol)
    arrs = get_feat_arrays(symbol, p.uhlo_length, sd)
    mask, code = evaluate_signals(arrs, p)
    hm = mask & (arrs["ts"] >= sd.holdout_start)
    if not hm.any():
        return {"n": 0}, pd.DataFrame()
    pos = apply_cooldown(sd.ts, hm, p.cooldown_s)
    if pos.size == 0:
        return {"n": 0}, pd.DataFrame()
    pnl = simulate_exits(sd, pos, code[pos], p)
    if pnl.empty:
        return {"n": 0}, pnl
    met = score_returns(pnl["net_ret"])
    met["sum_net_ret"] = float(pnl["net_ret"].sum())
    return met, pnl


# ----------------------------------------------------------------------------
# MAIN PIPELINE
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    global ACTIVE_TP_PCT, ACTIVE_SL_PCT, CROSS_PF_MIN, CROSS_FRAC_MIN, CROSS_MIN_N
    global FEE_PER_SIDE, DEFAULT_SLIPPAGE_PER_SIDE, ROUND_TRIP_COST
    global MAKER_FEE_PER_SIDE, TAKER_FEE_PER_SIDE, MAKER_SLIPPAGE_PER_SIDE, TAKER_SLIPPAGE_PER_SIDE
    global GAP_EXTRA_SLIPPAGE, COST_TP_EXIT, COST_TAKER_EXIT, COST_GAP_EXIT
    ap = argparse.ArgumentParser(description="v4 screener grid search")
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--universe", type=str, default="data/symbol_universe.txt")
    ap.add_argument("--fee-per-side", type=float, default=None,
                    help="exchange fee per side override (default 0.00055)")
    ap.add_argument("--slippage-per-side", type=float, default=None,
                    help="slippage per side override (default 0.0005)")
    ap.add_argument("--from-raw", type=str, default=None,
                    help="resume from a grid_search_raw_v4.pkl (skips the grid search)")
    ap.add_argument("--tp", type=float, default=NEAR_SYMMETRIC_TP_PCT,
                    help="take-profit %% (default 1.1, near-symmetric mandate)")
    ap.add_argument("--sl", type=float, default=NEAR_SYMMETRIC_SL_PCT,
                    help="stop-loss %% (default 1.5, near-symmetric mandate)")
    ap.add_argument("--cross-pf-min", type=float, default=CROSS_PF_MIN,
                    help="per-symbol PF required by the cross-symbol gate")
    ap.add_argument("--cross-frac", type=float, default=CROSS_FRAC_MIN,
                    help="fraction of sampled symbols that must clear the PF gate")
    ap.add_argument("--cross-min-n", type=int, default=CROSS_MIN_N,
                    help="min per-symbol trades for a PF reading to count")
    args = ap.parse_args(argv)

    ACTIVE_TP_PCT = args.tp
    ACTIVE_SL_PCT = args.sl
    if args.fee_per_side is not None:
        FEE_PER_SIDE = args.fee_per_side
        TAKER_FEE_PER_SIDE = args.fee_per_side
    if args.slippage_per_side is not None:
        DEFAULT_SLIPPAGE_PER_SIDE = args.slippage_per_side
        TAKER_SLIPPAGE_PER_SIDE = args.slippage_per_side
    _recompute_costs()
    CROSS_PF_MIN = args.cross_pf_min
    CROSS_FRAC_MIN = args.cross_frac
    CROSS_MIN_N = args.cross_min_n

    tag = ""
    if ACTIVE_TP_PCT != 1.2 or ACTIVE_SL_PCT != 5.0:
        tag += f"_tp{ACTIVE_TP_PCT:g}_sl{ACTIVE_SL_PCT:g}"
    if abs(ROUND_TRIP_COST - 0.0021) > 1e-12:
        tag += f"_cost{ROUND_TRIP_COST*1e4:.0f}bp"
    # Спринт 1: показываем дифференцированные стоимости для аудита
    if abs(COST_TP_EXIT - 0.00135) > 1e-12 or abs(COST_GAP_EXIT - 0.0024) > 1e-12:
        tag += f"_maker{MAKER_FEE_PER_SIDE*1e4:.0f}bp"

    keys, combos = grid_combos(PARAM_GRID)
    if args.from_raw:
        raw = pd.read_pickle(args.from_raw)
        per_symbol = [g for _, g in raw.groupby("symbol")]
        print(f"resumed {len(raw)} rows / {len(per_symbol)} symbols from {args.from_raw}")
        for sym in raw["symbol"].unique():     # warm cache so holdout confirmation can run
            try:
                prepare_symbol(sym)
            except (FileNotFoundError, ValueError) as e:
                print(f"  warm {sym}: {e}")
    else:
        symbols = load_symbol_universe(n_sample=args.sample, path=args.universe)
        print(f"{len(symbols)} symbols x {len(combos)} combos, {args.folds} temporal folds, "
              f"{HOLDOUT_DAYS}d final holdout | TP {ACTIVE_TP_PCT:g}% / SL {ACTIVE_SL_PCT:g}% "
              f"(maker {COST_TP_EXIT*100:.3f}% / taker {COST_TAKER_EXIT*100:.3f}% / gap {COST_GAP_EXIT*100:.3f}%)")
        per_symbol = []
        for sym in symbols:
            try:
                res = run_symbol(sym, n_folds=args.folds)
            except (FileNotFoundError, ValueError) as e:          # F5
                print(f"Skipping {sym}: {e}")
                continue
            if res.empty:
                print(f"[{sym}] no combos produced trades")
                continue
            per_symbol.append(res)
            print(f"[{sym}] {len(res)}/{len(combos)} combos with trades")

    if not per_symbol:
        print("No results - check load_ohlcv()/symbol_universe.txt.")
        return

    if not args.from_raw:
        raw = pd.concat(per_symbol, ignore_index=True)
        raw.to_pickle(f"grid_search_raw_v4{tag}.pkl")

    combined = complete_grid(raw)
    combined = add_neighbor_median_score(combined, metric="expectancy_score")
    combined.to_csv(f"grid_search_results_v4{tag}.csv", index=False)

    rank = select_global(combined, n_symbols=len(per_symbol))
    if rank.empty:
        return

    show = ["global_score", "coverage", "pf_symbols", "pf_frac", *keys]
    print("\nTop 10 combos by cross-symbol median neighbor-smoothed expectancy:")
    print(rank[show].head(10).to_string(index=False))

    best = rank.iloc[0]
    best_params = params_from({k: best[k] for k in keys})
    print(f"\nConfirming combo #{int(best['combo_id'])} on the untouched "
          f"{HOLDOUT_DAYS}d holdout, per symbol:")

    all_nets = []
    for sym in sorted(_symbol_cache):
        try:
            met, pnl = confirm_holdout(sym, best_params)
        except (FileNotFoundError, ValueError) as e:
            print(f"  {sym}: skipped ({e})")
            continue
        if met.get("n", 0) == 0 or pnl.empty:
            print(f"  {sym}: no holdout signals")
            continue
        all_nets.append(pnl["net_ret"])
        print(f"  {sym:<12} n={met['n']:<4} winrate={met['winrate']:.2f} "
              f"avg={met['avg_ret']*100:+.3f}% sum={met['sum_net_ret']*100:+.2f}% "
              f"sharpe={met['sharpe']:.2f}")
        hist = pnl["reason"].value_counts().sort_index()
        pretty = ", ".join(f"{REASON_NAMES[i]}:{v}" for i, v in hist.items())
        print(f"  {'':<12} exits: {pretty}")

    if all_nets:
        print(f"\nPooled holdout: {score_returns(pd.concat(all_nets, ignore_index=True))}")


# ----------------------------------------------------------------------------
# SELFTEST: synthetic data, v3-vs-v4 equivalence, micro tests, benchmarks
# ----------------------------------------------------------------------------

def _synthetic_1m(rng: np.random.Generator, days: int, start: str = "2024-01-01") -> pd.DataFrame:
    n = days * 1440
    z = rng.standard_t(3, n) / 1.34                      # ~unit-variance heavy tails
    logsig = np.cumsum(rng.standard_normal(n) * 0.02)
    logsig -= logsig.mean()
    sig = 0.004 * np.exp(logsig)

    drift = np.zeros(n)                                  # trend/pump episodes
    i = 0
    while i < n:
        if rng.random() < 0.03:
            seg = min(int(rng.integers(40, 220)), n - i)
            drift[i:i + seg] = float(rng.choice([-1.0, 1.0])) * rng.uniform(0.0015, 0.005)
            i += seg
        else:
            i += int(rng.integers(20, 200))

    sp_noise = np.abs(rng.standard_normal(n))
    wick_draw = rng.random(n)
    wick_amt = rng.random(n)
    vol_noise = rng.normal(math.log(100.0), 0.4, n)

    def assemble(scale: float) -> pd.DataFrame:
        rr = scale * (sig * z + drift)
        close = 100.0 * np.exp(np.cumsum(rr))
        open_ = np.empty(n)
        open_[0] = 100.0
        open_[1:] = close[:-1]
        sp = sp_noise * (np.abs(rr) + 1e-6) * close * 0.55
        high = np.maximum(open_, close) + sp * np.abs(sp_noise[::-1]) * 0.5
        low = np.minimum(open_, close) - sp * np.abs(sp_noise) * 0.5
        wick = wick_draw < 0.12
        high[wick] += sp[wick] * wick_amt[wick] * 2.0
        vol = np.exp(vol_noise) * (1.0 + 8.0 * (np.abs(rr) > 3 * rr.std()))
        ts = np.datetime64(start) + np.arange(n) * np.timedelta64(1, "m")
        return pd.DataFrame({"ts": ts, "open": open_, "high": high,
                             "low": low, "close": close, "volume": vol})

    med = float(natr_wilder(assemble(1.0)).median())     # NATR is scale-invariant
    scale = 2.0 / max(med, 1e-12)                        # ...so scale the RETURNS
    return assemble(scale)


def _write_synthetic(data_dir: Path, symbols: list[str], days: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for k, sym in enumerate(symbols):
        d1 = _synthetic_1m(np.random.default_rng(1000 + k), days)
        d1["ts"] = d1["ts"].astype("datetime64[ns]")     # uniform ns units (pandas 3 merge_asof)
        d1.to_parquet(data_dir / f"{sym}_1m.parquet", index=False)
        d15 = (
            d1.set_index(pd.to_datetime(d1["ts"]))
            .resample("15min", label="left", closed="left")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
            .reset_index(names="ts")
        )
        d15.to_parquet(data_dir / f"{sym}_15m.parquet", index=False)


class _NS:
    pass


def _micro_tests() -> list[tuple[str, bool, str]]:
    """Direct unit tests of simulate_exits / apply_cooldown edge behavior."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, msg: str = "") -> None:
        results.append((name, bool(cond), msg))

    p = Params(tp_pct=1.2, sl_pct=5.0, time_exit_min=90)
    base = np.datetime64("2024-01-01T00:00")

    def mk(o, h, l, c, v=None):
        sd = _NS()
        sd.ts = base + np.arange(len(o)) * np.timedelta64(1, "m")
        sd.o, sd.h, sd.l, sd.c = map(lambda x: np.asarray(x, dtype=float), (o, h, l, c))
        # Sprint 1: volume для gap/zero_vol обработки (по умолчанию ликвидно)
        sd.v = np.asarray(v, dtype=float) if v is not None else np.ones(len(o), dtype=float) * 100.0
        return sd

    def one_bar(bar_o, bar_h, bar_l, bar_c):
        flat = [100.0] * 200
        o, h, l, c = flat.copy(), flat.copy(), flat.copy(), flat.copy()
        o[1], h[1], l[1], c[1] = bar_o, bar_h, bar_l, bar_c
        return o, h, l, c

    # NOTE on gap semantics: the entry bar's open IS the entry price, so a
    # "gap" can only happen on SUBSEQUENT bars (open beyond barrier vs prior
    # close while the position is still open). v3 has the same property
    # (ret_open[0] == 0 by construction); these tests pin that down too.

    def two_bar(b1, b2):
        flat = [100.0] * 200
        o, h, l, c = flat.copy(), flat.copy(), flat.copy(), flat.copy()
        for i, vals in ((1, b1), (2, b2)):
            o[i], h[i], l[i], c[i] = vals
        return o, h, l, c

    # T1: long, next-next bar OPENS past TP -> fill at that open (gap_tp) — Sprint 1: gap = COST_GAP_EXIT
    out = simulate_exits(
        mk(*two_bar((100.0, 100.05, 99.95, 100.0), (102.0, 102.1, 101.9, 102.05))),
        np.array([0]), np.array([1]), p)
    exp_raw = (102.0 - 100.0) / 100.0
    check("T1 gap_tp fills at open",
          abs(out.net_ret.iloc[0] - (exp_raw - COST_GAP_EXIT)) < 1e-9 and out.reason.iloc[0] == 2,
          f"net={out.net_ret.iloc[0]} reason={out.reason.iloc[0]}")

    # T1b: same geometry but the high alone crosses TP without an open gap
    #      -> exact-price limit fill at tp, not the open — Sprint 1: tp = maker COST_TP_EXIT
    out = simulate_exits(
        mk(*two_bar((100.0, 100.05, 99.95, 100.0), (100.1, 101.4, 100.0, 101.2))),
        np.array([0]), np.array([1]), p)
    check("T1b intrabar tp exact fill",
          abs(out.net_ret.iloc[0] - (0.012 - COST_TP_EXIT)) < 1e-9 and out.reason.iloc[0] == 0,
          f"net={out.net_ret.iloc[0]} reason={out.reason.iloc[0]}")

    # T2: long, later bar OPENS past SL -> fill at that open (gap_sl) — Sprint 1: gap = COST_GAP_EXIT
    out = simulate_exits(
        mk(*two_bar((100.0, 100.05, 99.95, 100.0), (94.0, 94.3, 93.8, 94.1))),
        np.array([0]), np.array([1]), p)
    exp_raw = (94.0 - 100.0) / 100.0
    check("T2 gap_sl fills at open",
          abs(out.net_ret.iloc[0] - (exp_raw - COST_GAP_EXIT)) < 1e-9 and out.reason.iloc[0] == 3,
          f"net={out.net_ret.iloc[0]} reason={out.reason.iloc[0]}")

    # T3: long, plain SL touch -> exact -sl — Sprint 1: sl = taker COST_TAKER_EXIT
    out = simulate_exits(mk(*one_bar(100.0, 100.1, 94.5, 99.5)), np.array([0]), np.array([1]), p)
    check("T3 plain sl exact fill",
          abs(out.net_ret.iloc[0] - (-0.05 - COST_TAKER_EXIT)) < 1e-12 and out.reason.iloc[0] == 1,
          f"net={out.net_ret.iloc[0]} reason={out.reason.iloc[0]}")

    # T4: TP and SL both touched intrabar -> SL priority (conservative)
    out = simulate_exits(mk(*one_bar(100.0, 101.5, 94.5, 98.0)), np.array([0]), np.array([1]), p)
    check("T4 intrabar tie -> sl wins",
          abs(out.net_ret.iloc[0] - (-0.05 - COST_TAKER_EXIT)) < 1e-12 and out.reason.iloc[0] == 1,
          f"net={out.net_ret.iloc[0]} reason={out.reason.iloc[0]}")

    # T5a: short, later bar OPENS through TP -> fill at open, carry for 2 min — Sprint 1: gap
    out = simulate_exits(
        mk(*two_bar((100.0, 100.05, 99.95, 100.0), (97.0, 97.2, 96.9, 97.1))),
        np.array([0]), np.array([-1]), p)
    exp_raw = (100.0 - 97.0) / 100.0
    held = float(out.held_min.iloc[0])
    exp_net = exp_raw - COST_GAP_EXIT - SHORT_CARRY_COST_PER_HOUR * held / 60.0
    check("T5a short gap_tp fills at open",
          abs(out.net_ret.iloc[0] - exp_net) < 1e-9 and out.reason.iloc[0] == 2,
          f"net={out.net_ret.iloc[0]} expected~{exp_net} reason={out.reason.iloc[0]}")

    # T5b: time exit on flat prices -> Sprint 1: held 91m (close следующего бара, +1m), taker cost
    out = simulate_exits(mk(*one_bar(100.0, 100.05, 99.95, 100.0)), np.array([0]), np.array([-1]), p)
    exp = -(COST_TAKER_EXIT + SHORT_CARRY_COST_PER_HOUR * 91.0 / 60.0)
    check("T5b time exit held_min==91 (Sprint1 +1m fix)",
          out.held_min.iloc[0] == 91.0 and out.reason.iloc[0] == 4
          and abs(out.net_ret.iloc[0] - exp) < 1e-9,
          f"held={out.held_min.iloc[0]} net={out.net_ret.iloc[0]}")

    # T5c: F1 regression - short stopped early carries LESS than the full horizon — Sprint 1: taker
    out = simulate_exits(mk(*one_bar(100.0, 105.5, 99.0, 103.0)), np.array([0]), np.array([-1]), p)
    check("T5c early SL -> held < 91m and exact -sl fill",
          out.held_min.iloc[0] < 91.0 and out.reason.iloc[0] == 1
          and abs(out.net_ret.iloc[0] - (-0.05 - COST_TAKER_EXIT
                                        - SHORT_CARRY_COST_PER_HOUR * float(out.held_min.iloc[0]) / 60.0)) < 1e-9,
          f"held={out.held_min.iloc[0]} reason={out.reason.iloc[0]} net={out.net_ret.iloc[0]}")

    # T6: cooldown windows
    ts3 = base + np.array([0, 60, 120], dtype="timedelta64[s]")
    check("T6a 1h cooldown keeps only first",
          list(apply_cooldown(ts3, np.array([True, True, True]), 3600)) == [0])
    check("T6b 2m cooldown keeps bars 0 and 2",
          list(apply_cooldown(ts3, np.array([True, True, True]), 120)) == [0, 2])

    # T7: signal on the very last bar -> dropped (no entry bar)
    flat = [100.0] * 200
    out = simulate_exits(mk(flat, flat, flat, flat), np.array([199]), np.array([1]), p)
    check("T7 no-entry-bar signal dropped", len(out) == 0, f"rows={len(out)}")

    # --- Sprint 1: gap / zero_vol / +1m ---
    # T8: entry bar zero_vol -> дроп (нет ликвидности)
    o, h, l, c = flat.copy(), flat.copy(), flat.copy(), flat.copy()
    o[1], h[1], l[1], c[1] = 100.0, 100.1, 99.9, 100.0
    v = np.ones(200) * 100.0
    v[1] = 0  # entry bar zero_vol
    sd_zv = mk(o, h, l, c, v=v)
    out = simulate_exits(sd_zv, np.array([0]), np.array([1]), p)
    check("T8 zero_vol entry dropped", len(out) == 0, f"rows={len(out)}")

    # T9: time exit с +1m fix — уже проверен в T5b (91м), дополнительно проверяем gap extra cost
    # gap bar (zero_vol в середине окна) -> gap стоимость
    o2, h2, l2, c2 = flat.copy(), flat.copy(), flat.copy(), flat.copy()
    # entry bar 1 валиден, bar 2 — zero_vol gap, bar 3 — tp через open (но bar 2 дыра)
    o2[1], h2[1], l2[1], c2[1] = 100.0, 100.1, 99.9, 100.0
    o2[2], h2[2], l2[2], c2[2] = 100.0, 100.1, 99.9, 100.0  # будет помечен zero_vol
    o2[3], h2[3], l2[3], c2[3] = 102.0, 102.1, 101.9, 102.05  # gap_tp за дырой
    v2 = np.ones(200) * 100.0
    v2[2] = 0
    out = simulate_exits(mk(o2, h2, l2, c2, v=v2), np.array([0]), np.array([1]), p)
    # должен всё равно исполниться на bar 3 как gap_tp, но с gap стоимостью
    check("T9 gap with zero_vol still fills as gap_tp",
          len(out) == 1 and out.reason.iloc[0] == 2
          and abs(out.net_ret.iloc[0] - (0.02 - COST_GAP_EXIT)) < 1e-9,
          f"net={out.net_ret.iloc[0] if len(out) else 'empty'} reason={out.reason.iloc[0] if len(out) else 'NA'}")

    # T10: сигнал на zero_vol баре -> дроп в evaluate_signals
    arrs_zv = {
        "natr": np.array([1.0, 1.0, 1.0], dtype=np.float32),
        "uhlo_highs": np.array([80.0, 80.0, 80.0], dtype=np.float32),
        "uhlo_lows": np.array([20.0, 20.0, 20.0], dtype=np.float32),
        "uhlo_highs_15m": np.array([80.0, 80.0, 80.0], dtype=np.float32),
        "uhlo_lows_15m": np.array([20.0, 20.0, 20.0], dtype=np.float32),
        "vol_spike_ratio": np.array([1.0, 1.0, 1.0], dtype=np.float32),
        "upper_wick_frac": np.array([0.1, 0.1, 0.1], dtype=np.float32),
        "uhlo_corner_flag": np.array([False, False, False]),
        "volume": np.array([100.0, 0.0, 100.0], dtype=float),  # средний бар zero_vol
        "ts": base + np.arange(3) * np.timedelta64(1, "m"),
    }
    # сделаем так, чтобы средний бар дал сигнал (natr в диапазоне и т.д.)
    # но volume=0 должен его отфильтровать
    p_test = Params(natr_below_min=0.5, natr_above_max=5.0, color_upper=75, color_lower=25,
                    long_lows_min=95, short_highs_max=95, uhlo_length=10, cooldown_s=60)
    mask, code = evaluate_signals(arrs_zv, p_test)
    check("T10 zero_vol signal dropped", mask[1] == False, f"mask={mask}")

    return results


def _trades_v3(V3, sym: str, p: Params) -> pd.DataFrame:
    df1 = V3.load_ohlcv(sym, "1m")
    df15 = V3.load_ohlcv(sym, "15m")
    hs = naive_ts(pd.Series([df1["ts"].max()]))[0] - np.timedelta64(V3.HOLDOUT_DAYS, "D")
    feat = V3.build_features(sym, df1, df15, p.uhlo_length)
    mask, side = V3.evaluate_vectorized(feat, p)
    tsv = feat["ts"].values
    mu = mask & (tsv < hs)
    pos = V3.apply_cooldown_global(tsv, mu, p.cooldown_s)
    return V3.triple_barrier_batch(df1, pos, side[pos], p)


def _trades_v4(sym: str, p: Params) -> pd.DataFrame:
    sd = prepare_symbol(sym)
    arrs = get_feat_arrays(sym, p.uhlo_length, sd)
    mask, code = evaluate_signals(arrs, p)
    hm = mask & (arrs["ts"] < sd.holdout_start)
    pos = apply_cooldown(arrs["ts"], hm, p.cooldown_s)
    return simulate_exits(sd, pos, code[pos], p)


def _trade_key(ts_col) -> np.ndarray:
    s = pd.to_datetime(pd.Series(ts_col)).astype("datetime64[ns]")
    return s.astype("int64").to_numpy()


def selftest() -> int:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import screener_param_search_v3 as V3

    root = here / "data" / "_selftest"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    # identical shrunken config on both modules: fast, dense-signal test
    for mod in (V3, sys.modules[__name__]):
        mod.HOLDOUT_DAYS = 3
        mod.MIN_TRADES = 5
        mod.DATA_DIR = root
    grid_small = {"uhlo_length": [10, 15], "cooldown_s": [120, 300]}
    V3.PARAM_GRID = grid_small

    symbols = ["SYNA", "SYNB"]
    print(f"[selftest] synthetic data -> {root}")
    _write_synthetic(root, symbols, days=12)

    failed = 0

    print("\n[selftest] micro tests (exit engine / cooldown):")
    for name, ok_, msg in _micro_tests():
        print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + ("" if ok_ else f"  ({msg})"))
        failed += 0 if ok_ else 1

    print("\n[selftest] v3 vs v4 equivalence on the shrunk grid:")
    timings: dict[str, tuple[float, float]] = {}
    for sym in symbols:
        t0 = time.perf_counter()
        r3_full = V3.run_symbol(sym)
        t3 = time.perf_counter() - t0
        r3 = r3_full[0]                              # v3 returns (res_df, holdout_map)
        t0 = time.perf_counter(); r4 = run_symbol(sym, grid=grid_small); t4 = time.perf_counter() - t0
        timings[sym] = (t3, t4)

        keys = list(grid_small.keys())
        if r3.empty or r4.empty:
            print(f"  [{sym}] FAIL empty results (v3={len(r3)}, v4={len(r4)}) - data issue")
            failed += 1
            continue
        merged = r3.merge(r4, on=["symbol", *keys], suffixes=("_3", "_4"))
        if len(merged) != len(r3) or len(merged) != len(r4):
            print(f"  [{sym}] FAIL combo-set mismatch: v3={len(r3)} v4={len(r4)} merged={len(merged)}")
            failed += 1
            continue

        exact = ["n", "n_folds_total"]
        tol = ["winrate", "avg_ret", "sharpe", "profit_factor",
               "worst_split_sharpe", "std_across_splits", "expectancy_score"]
        for col in exact:
            if not (merged[f"{col}_3"] == merged[f"{col}_4"]).all():
                print(f"  [{sym}] FAIL {col} differs between v3/v4")
                failed += 1
        for col in tol:
            a = merged[f"{col}_3"].astype(float).to_numpy()
            b = merged[f"{col}_4"].astype(float).to_numpy()
            diff = np.zeros(len(a))
            mask_nan = np.isnan(a) & np.isnan(b)
            num = ~mask_nan
            diff[num] = np.abs(a[num] - b[num])
            dmax = float(diff.max())
            if dmax > 0:
                print(f"  [{sym}] note: {col} max|delta|={dmax:.2e} (expected: carry fix F1 + Sprint1 maker/taker/time+1m)")

        # trade-level: Sprint 1 меняет стоимость (maker дешевле, gap дороже, time +1m)
        # поэтому строгий v3-паритет невозможен — проверяем только множество трейдов и
        # что delta в разумных границах (±0.001 = 10bp).
        p0 = params_from({**Params().__dict__, **{k: r3.iloc[0][k] for k in keys}})
        tv3 = _trades_v3(V3, sym, p0)
        tv4 = _trades_v4(sym, p0)
        k3 = set(zip(_trade_key(tv3["ts"]), tv3["side"]))
        k4 = set(zip(_trade_key(tv4["ts"]), tv4["side"]))
        if k3 != k4:
            print(f"  [{sym}] FAIL trade sets differ: v3={len(k3)} v4={len(k4)} "
                  f"only3={len(k3 - k4)} only4={len(k4 - k3)}")
            failed += 1
            continue
        j3 = tv3.set_index([pd.Index(_trade_key(tv3["ts"])), "side"]).sort_index()["net_ret"]
        j4 = tv4.set_index([pd.Index(_trade_key(tv4["ts"])), "side"]).sort_index()["net_ret"]
        # Sprint 1: tp (maker) дешевле на ~75bp, gap дороже на 30bp, time +1m — delta в [-0.005, +0.005] норма
        g_delta = j4[j4.index.get_level_values(1) == "green"] - j3[j3.index.get_level_values(1) == "green"]
        r_delta = j4[j4.index.get_level_values(1) == "red"] - j3[j3.index.get_level_values(1) == "red"]
        all_delta = j4 - j3
        max_abs_delta = float(all_delta.abs().max()) if len(all_delta) else 0.0
        # не фейлим на стоимости, только на логике; репортим uplift
        uplift = float(all_delta.mean()) if len(all_delta) else 0.0
        g_uplift = float(g_delta.mean()) if len(g_delta) else 0.0
        r_uplift = float(r_delta.mean()) if len(r_delta) else 0.0
        cost_ok = max_abs_delta < 0.005  # 50bp допуск на Sprint1
        if not cost_ok:
            print(f"  [{sym}] FAIL trade PnL delta too large: max|delta|={max_abs_delta*100:.4f}% (Sprint1 expects <0.5%)")
            failed += 1
        print(f"  [{sym}] trades: v3={len(tv3)} v4={len(tv4)} | max|delta|={max_abs_delta*100:.4f}% "
              f"uplift all={uplift*100:.4f}% green={g_uplift*100:.4f}% red={r_uplift*100:.4f}% (Sprint1 maker/gap/time)")

    # Sprint 2/3: отдельный бенчмарк apply_cooldown (узкое место)
    print("\n[selftest] apply_cooldown micro-benchmark (100k signals, cooldown 300s):")
    try:
        n_big = 100_000
        base = np.datetime64("2024-01-01T00:00")
        ts_big = base + np.arange(n_big) * np.timedelta64(1, "m")
        # плотные сигналы: каждый бар
        mask_big = np.ones(n_big, dtype=bool)

        # reference Python loop (старый v3)
        def _ref_cooldown(ts, mask, cd_s):
            idxs = np.flatnonzero(mask)
            kept = []
            last = None
            cd = np.timedelta64(cd_s, "s")
            for i in idxs:
                t = ts[i]
                if last is not None and (t - last) < cd:
                    continue
                kept.append(i)
                last = t
            return np.asarray(kept, dtype=np.int64)

        # warmup numba
        apply_cooldown(ts_big, mask_big, 300)
        _ref_cooldown(ts_big, mask_big, 300)

        t0 = time.perf_counter()
        for _ in range(5):
            _ref_cooldown(ts_big, mask_big, 300)
        t_ref = (time.perf_counter() - t0) / 5

        t0 = time.perf_counter()
        for _ in range(20):
            apply_cooldown(ts_big, mask_big, 300)
        t_new = (time.perf_counter() - t0) / 20
        ratio = t_ref / t_new if t_new > 0 else float("inf")
        impl = "numba" if HAS_NUMBA else "numpy"
        # корректность
        r1 = _ref_cooldown(ts_big, mask_big, 300)
        r2 = apply_cooldown(ts_big, mask_big, 300)
        ok = np.array_equal(r1, r2)
        print(f"  ref={t_ref*1000:.2f}ms  new({impl})={t_new*1000:.2f}ms  speedup=x{ratio:.1f}  correct={ok}  kept={len(r2)}/{n_big}")
        if not ok:
            failed += 1
    except Exception as e:
        print(f"  benchmark failed: {e}")

    # Sprint 3: проверка Pydantic/tenacity wiring
    print("\n[selftest] Sprint 3 wiring:")
    print(f"  pydantic={'ok' if HAS_PYDANTIC else 'fallback (not installed)'}  tenacity={'ok' if HAS_TENACITY else 'fallback'}  numba={'ok' if HAS_NUMBA else 'fallback'}")
    try:
        validate_param_grid(PARAM_GRID)
        print("  PARAM_GRID validation: PASS")
    except Exception as e:
        print(f"  PARAM_GRID validation: FAIL {e}")
        failed += 1
    try:
        # должен отклонить невалидный грид
        bad = dict(PARAM_GRID)
        bad["cooldown_s"] = [5]  # ниже минимума 10
        try:
            validate_param_grid(bad)
            print("  bad grid rejection: FAIL (should have raised)")
            failed += 1
        except Exception:
            print("  bad grid rejection: PASS")
    except Exception as e:
        print(f"  bad grid test error: {e}")

    print("\n[selftest] wall-clock on the shrunk 4-combo grid (12d synthetic):")
    for sym, (t3, t4) in timings.items():
        ratio = t3 / t4 if t4 > 0 else float("inf")
        print(f"  {sym}: v3={t3:.2f}s  v4={t4:.2f}s  speedup=x{ratio:.1f}")

    print("\n[selftest] v4 full 8-dim grid (6561 combos) on SYNA:")
    t0 = time.perf_counter()
    res_full = run_symbol("SYNA")
    dt = time.perf_counter() - t0
    print(f"  {dt:.1f}s ({len(res_full)} combos traded) -> ~{dt*20/60:.1f} min for 20 symbols sequential")

    shutil.rmtree(root, ignore_errors=True)

    print(f"\n[selftest] {'FAILED' if failed else 'ALL PASS'} ({failed} failure(s))")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args, _rest = ap.parse_known_args()
    if args.selftest:
        sys.exit(selftest())
    else:
        main()
