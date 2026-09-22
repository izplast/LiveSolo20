"""
screener_param_search_v3.py  [BASELINE COPY FOR BENCHMARKING]
=============================================================

Verbatim copy of the user's v3, with ONE minimal patch (marked
"TZ-PATCH" below) that is REQUIRED to run at all on pandas >= 2/3:

    feat["ts"].values  ->  naive datetime64 array
    compared against   ->  tz-aware pd.Timestamp (holdout_start, fold bounds)
    raises: TypeError: Cannot compare tz-naive and tz-aware timestamps

Reproduced on pandas 3.0.5 / numpy 2.5.2:
    ts.values >= ts.max() - Timedelta(...)  ->  TypeError

The TZ-PATCH derives all split scalars from a tz-stripped copy of the
timestamp column instead - same instants, naive dtype - and changes
nothing else. All other v3 behavior (including the known issues found in
review: deadline-based carry cost for shorts, holdout simulated for every
combo during the grid search, partial-grid plateau lookup) is preserved
so that v4-vs-v3 benchmarks measure exactly the v4 changes.

NOT financial advice - a testing methodology/tool.
"""

from __future__ import annotations

import itertools
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# narrowed from a blanket ignore - only silence noisy runtime-math warnings,
# so future pandas breaking-change warnings aren't hidden (review nitpick).
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

DATA_DIR = Path("./data")
SYMBOL_UNIVERSE_FILE = DATA_DIR / "symbol_universe.txt"

FEE_PER_SIDE = 0.00055
DEFAULT_SLIPPAGE_PER_SIDE = 0.0005
ROUND_TRIP_COST = (FEE_PER_SIDE + DEFAULT_SLIPPAGE_PER_SIDE) * 2
SHORT_CARRY_COST_PER_HOUR = 0.0001  # rough stand-in - tune per symbol/exchange funding data

HOLDOUT_DAYS = 30
MIN_TRADES = 30  # floor for expectancy_score / for a combo to be considered at all


def load_symbol_universe(n_sample: int | None = None) -> list[str]:
    if not SYMBOL_UNIVERSE_FILE.exists():
        raise FileNotFoundError(f"Missing {SYMBOL_UNIVERSE_FILE}.")
    symbols = [l.strip() for l in SYMBOL_UNIVERSE_FILE.read_text().splitlines() if l.strip()]
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
    # TZ-PATCH part 2: pandas 3 merge_asof rejects mixed time units (parquet
    # ms vs ms+Timedelta promoted to us) -> normalize at the border.
    df["ts"] = df["ts"].dt.as_unit("ns")
    if timeframe == "15m":
        df["ts"] = df["ts"] + pd.Timedelta(minutes=15)  # assume bar-open ts; confirm with real data
    return df.sort_values("ts").reset_index(drop=True)


def _assert_data_quality(df: pd.DataFrame, symbol: str) -> None:
    dupes = df["ts"].duplicated().sum()
    if dupes:
        raise ValueError(f"{symbol}: {dupes} duplicate timestamps in 1m data - fix upstream before backtesting.")
    gaps = df["ts"].diff().dropna()
    expected = pd.Timedelta(minutes=1)
    n_gaps = (gaps != expected).sum()
    if n_gaps > 0:
        print(f"[{symbol}] warning: {n_gaps} non-1min gaps in 1m series (missing bars).")
    zero_vol = (df["volume"] == 0).sum()
    if zero_vol > 0:
        print(f"[{symbol}] warning: {zero_vol} zero-volume bars.")


# ----------------------------------------------------------------------------
# INDICATORS
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


# ----------------------------------------------------------------------------
# FEATURES - built ONCE on the FULL series per (symbol, uhlo_length),
# fix #4: no separate warm-up-starved build for the holdout slice.
# ----------------------------------------------------------------------------

_feature_cache: dict[tuple[str, int], pd.DataFrame] = {}
_KEEP_COLS = [
    "ts", "open", "high", "low", "close", "volume",
    "natr", "uhlo_highs", "uhlo_lows", "uhlo_highs_15m", "uhlo_lows_15m",
    "vol_spike_ratio", "upper_wick_frac", "uhlo_corner_flag",
]


def build_features(symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, uhlo_length: int) -> pd.DataFrame:
    key = (symbol, uhlo_length)
    if key in _feature_cache:
        return _feature_cache[key]

    out = df_1m.copy()
    out["natr"] = natr_wilder(out, period=14)

    u1 = uhlo(out, length=uhlo_length)
    out = pd.concat([out, u1], axis=1)

    u15 = uhlo(df_15m, length=uhlo_length)
    df_15m_u = pd.concat([df_15m[["ts"]], u15], axis=1).rename(
        columns={"uhlo_highs": "uhlo_highs_15m", "uhlo_lows": "uhlo_lows_15m"}
    )
    out = pd.merge_asof(out.sort_values("ts"), df_15m_u.sort_values("ts"), on="ts", direction="backward")

    avg_vol20 = out["volume"].shift(1).rolling(20).mean().replace(0, np.nan)  # avoid inf (data-quality fix)
    out["vol_spike_ratio"] = out["volume"] / avg_vol20
    bar_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["upper_wick_frac"] = (out["high"] - out[["open", "close"]].max(axis=1)) / bar_range

    roll_max_highs = out["uhlo_highs"].rolling(uhlo_length).max()
    roll_max_lows = out["uhlo_lows"].rolling(uhlo_length).max()
    out["uhlo_corner_flag"] = (roll_max_highs >= 99) & (roll_max_lows >= 99)  # still approximate - see docstring

    out = out[_KEEP_COLS].astype(
        {c: "float32" for c in out.columns if c not in ("ts", "uhlo_corner_flag") and c in _KEEP_COLS}
    )
    out = out.reset_index(drop=True)
    _feature_cache[key] = out
    return out


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
    tp_pct: float = 1.2
    sl_pct: float = 5.0
    time_exit_min: int = 90


# ----------------------------------------------------------------------------
# VECTORIZED evaluate() - unchanged logic from v2, run once per combo total
# (not once per fold - fix, saves 4x the work).
# ----------------------------------------------------------------------------

def evaluate_vectorized(feat: pd.DataFrame, p: Params) -> tuple[np.ndarray, np.ndarray]:
    n = len(feat)
    valid = ~(feat["natr"].isna() | feat["uhlo_highs"].isna() | feat["uhlo_highs_15m"].isna())

    natr_low = valid & (feat["natr"] < p.natr_below_min)
    natr_high = valid & ~natr_low & (feat["natr"] > p.natr_above_max)
    remaining = valid & ~natr_low & ~natr_high

    green = (
        remaining
        & (feat["uhlo_highs"] >= p.color_upper) & (feat["uhlo_lows"] <= p.color_lower)
        & (feat["uhlo_highs_15m"] >= p.color_upper) & (feat["uhlo_lows_15m"] <= p.color_lower)
    )
    red = (
        remaining & ~green
        & (feat["uhlo_lows"] >= p.color_upper) & (feat["uhlo_highs"] <= p.color_lower)
        & (feat["uhlo_lows_15m"] >= p.color_upper) & (feat["uhlo_highs_15m"] <= p.color_lower)
    )

    corner = (green | red) & feat["uhlo_corner_flag"]
    green = green & ~corner
    red = red & ~corner

    # pump filter now applied symmetrically to both sides (v2 only filtered
    # green - noted as a likely oversight in review; flip this back if the
    # asymmetry was intentional).
    pump_green = green & (feat["vol_spike_ratio"] > p.pump_vol_mult) & (feat["upper_wick_frac"] > p.pump_wick_frac)
    green = green & ~pump_green
    pump_red = red & (feat["vol_spike_ratio"] > p.pump_vol_mult) & (feat["upper_wick_frac"] > p.pump_wick_frac)
    red = red & ~pump_red

    long_overbought = green & (feat["uhlo_lows"] >= p.long_lows_min)
    green = green & ~long_overbought

    short_oversold = red & (feat["uhlo_highs"] >= p.short_highs_max)
    red = red & ~short_oversold

    side = np.full(n, None, dtype=object)
    side[green.values] = "green"
    side[red.values] = "red"
    return (green.values | red.values), side


def apply_cooldown_global(ts: np.ndarray, signal_mask: np.ndarray, cooldown_s: int) -> np.ndarray:
    """Returns GLOBAL integer positions (into the full series), never a
    fold-relative index - this is the core of fix #1."""
    idxs = np.where(signal_mask)[0]
    kept = []
    last_ts = None
    cd = np.timedelta64(cooldown_s, "s")
    for i in idxs:
        t = ts[i]
        if last_ts is not None and (t - last_ts) < cd:
            continue
        kept.append(i)
        last_ts = t
    return np.array(kept, dtype=int)


# ----------------------------------------------------------------------------
# VECTORIZED TRIPLE-BARRIER EXIT (perf fix - no iterrows in the hot path)
# ----------------------------------------------------------------------------

def triple_barrier_batch(df_1m: pd.DataFrame, signal_positions: np.ndarray, sides: np.ndarray, p: Params) -> pd.DataFrame:
    """
    Vectorized-per-signal (numpy arrays, no iterrows/DataFrame row access in
    the loop) triple-barrier exit: entry at next bar's open, exit at
    whichever of TP/SL/time_exit hits first. Gap-aware: if the entry bar (or
    any bar) OPENS already past a barrier, fills at that open instead of the
    exact barrier price.
    """
    ts = df_1m["ts"].values
    o = df_1m["open"].values.astype(np.float64)
    h = df_1m["high"].values.astype(np.float64)
    l = df_1m["low"].values.astype(np.float64)
    c = df_1m["close"].values.astype(np.float64)
    n = len(df_1m)

    tp = p.tp_pct / 100.0
    sl = p.sl_pct / 100.0
    time_exit = pd.Timedelta(minutes=p.time_exit_min)

    rows = []
    for sig_i, side in zip(signal_positions, sides):
        entry_i = sig_i + 1
        if entry_i >= n:
            continue
        entry_price = o[entry_i]
        entry_time = ts[entry_i]
        deadline = entry_time + time_exit

        end_i = entry_i + int(p.time_exit_min) + 2  # small buffer, trimmed by ts below
        end_i = min(end_i, n)
        window_ts = ts[entry_i:end_i]
        mask = window_ts <= deadline
        if not mask.any():
            continue
        w_o, w_h, w_l, w_c = o[entry_i:end_i][mask], h[entry_i:end_i][mask], l[entry_i:end_i][mask], c[entry_i:end_i][mask]

        if side == "green":
            ret_open = (w_o - entry_price) / entry_price
            ret_low = (w_l - entry_price) / entry_price
            ret_high = (w_h - entry_price) / entry_price
        else:
            ret_open = (entry_price - w_o) / entry_price
            ret_low = (entry_price - w_h) / entry_price   # adverse move (price up for a short)
            ret_high = (entry_price - w_l) / entry_price  # favorable move (price down for a short)

        # gap-aware: if the bar's own open already breached a barrier, fill at open
        gap_sl = ret_open <= -sl
        gap_tp = ret_open >= tp
        hit_sl = (ret_low <= -sl) | gap_sl
        hit_tp = (ret_high >= tp) | gap_tp
        hit_any = hit_sl | hit_tp
        if hit_any.any():
            first = np.argmax(hit_any)
            if gap_sl[first]:
                ret = ret_open[first]
            elif gap_tp[first]:
                ret = ret_open[first]
            elif hit_sl[first]:
                ret = -sl
            else:
                ret = tp
            exit_time = window_ts[mask][first] if False else None  # kept simple; not needed downstream
        else:
            ret = (w_c[-1] - entry_price) / entry_price if side == "green" else (entry_price - w_c[-1]) / entry_price

        held_hours = (deadline - entry_time).total_seconds() / 3600.0
        cost = ROUND_TRIP_COST
        if side == "red":
            cost += SHORT_CARRY_COST_PER_HOUR * held_hours
        rows.append({"ts": pd.Timestamp(ts[sig_i]), "side": side, "net_ret": ret - cost})

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# METRICS
# ----------------------------------------------------------------------------

def score_returns(returns: pd.Series) -> dict:
    r = returns.dropna()
    n = len(r)
    if n < 20:
        return {"n": n, "winrate": np.nan, "avg_ret": np.nan, "sharpe": np.nan,
                "profit_factor": np.nan, "expectancy_score": np.nan}
    winrate = (r > 0).mean()
    avg_ret = r.mean()
    sharpe = r.mean() / r.std() if r.std() > 0 else np.nan
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    pf = gains / losses if losses > 0 else np.nan
    expectancy_score = avg_ret * np.sqrt(n) if n >= MIN_TRADES else np.nan
    return {"n": n, "winrate": winrate, "avg_ret": avg_ret, "sharpe": sharpe,
            "profit_factor": pf, "expectancy_score": expectancy_score}


# ----------------------------------------------------------------------------
# GRID SEARCH per symbol - signals computed ONCE on the full series (fix #1
# solved structurally: everything downstream uses global integer positions).
# ----------------------------------------------------------------------------

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


def run_symbol(symbol: str, n_folds: int = 4) -> tuple[pd.DataFrame, dict]:
    df_1m = load_ohlcv(symbol, "1m")
    df_15m = load_ohlcv(symbol, "15m")
    _assert_data_quality(df_1m, symbol)

    ts_all = df_1m["ts"]

    # --- TZ-PATCH (test harness): derive split scalars from a tz-stripped
    # copy so they can be compared against feat["ts"].values (naive numpy),
    # which raises TypeError against tz-aware Timestamps on pandas >= 2/3.
    ts_naive = ts_all.dt.tz_localize(None)
    holdout_start = ts_naive.max() - pd.Timedelta(days=HOLDOUT_DAYS)
    usable_span = (ts_naive.min(), holdout_start)
    fold_window = (holdout_start - ts_naive.min()) / n_folds
    # --- end TZ-PATCH ---

    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))

    results = []
    holdout_returns_by_combo: dict[tuple, pd.Series] = {}

    for combo in combos:
        p = Params(**dict(zip(keys, combo)))
        feat = build_features(symbol, df_1m, df_15m, p.uhlo_length)  # cached, full series

        signal_mask, side = evaluate_vectorized(feat, p)  # once per combo total
        ts_vals = feat["ts"].values

        pooled_returns = []
        per_fold_sharpe = []
        n_folds_seen = 0
        for i in range(n_folds):
            start = usable_span[0] + fold_window * i
            end = start + fold_window
            split_point = start + fold_window * 0.75  # in-sample 75%, OOS 25% of each fold
            fold_mask = (ts_vals >= split_point) & (ts_vals < end) & signal_mask
            if fold_mask.sum() == 0:
                continue
            fold_positions = np.where(fold_mask)[0]
            fold_positions = apply_cooldown_global(ts_vals, fold_mask, p.cooldown_s)
            if len(fold_positions) == 0:
                continue
            sig_sides = side[fold_positions]
            pnl = triple_barrier_batch(df_1m, fold_positions, sig_sides, p)
            if pnl.empty:
                continue
            n_folds_seen += 1
            pooled_returns.append(pnl["net_ret"])
            per_fold_sharpe.append(score_returns(pnl["net_ret"])["sharpe"])

        if not pooled_returns:
            continue
        pooled = pd.concat(pooled_returns)
        agg = score_returns(pooled)
        agg["worst_split_sharpe"] = np.nanmin(per_fold_sharpe) if per_fold_sharpe else np.nan
        agg["std_across_splits"] = np.nanstd(per_fold_sharpe) if per_fold_sharpe else np.nan
        agg["n_folds_positive"] = int(np.nansum(np.array(per_fold_sharpe) > 0))
        agg["n_folds_total"] = n_folds_seen
        agg.update(dict(zip(keys, combo)))
        results.append(agg)

        # holdout returns computed with the SAME global signal_mask/side, no
        # separate feature build -> fix #4 solved structurally.
        holdout_mask = (ts_vals >= holdout_start) & signal_mask
        if holdout_mask.sum() > 0:
            holdout_positions = apply_cooldown_global(ts_vals, holdout_mask, p.cooldown_s)
            if len(holdout_positions) > 0:
                hpnl = triple_barrier_batch(df_1m, holdout_positions, side[holdout_positions], p)
                if not hpnl.empty:
                    holdout_returns_by_combo[combo] = hpnl["net_ret"]

    res_df = pd.DataFrame(results)
    if not res_df.empty:
        res_df["symbol"] = symbol
    return res_df, holdout_returns_by_combo


# ----------------------------------------------------------------------------
# PLATEAU (neighbor-median), computed PER SYMBOL on the FULL unfiltered set
# (fix #2 + #3), aggregated across symbols only afterward.
# ----------------------------------------------------------------------------

def add_neighbor_median_score(res_df: pd.DataFrame, metric: str = "expectancy_score") -> pd.DataFrame:
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    value_to_idx = [{v: i for i, v in enumerate(vals)} for vals in values]

    out_frames = []
    for symbol, group in res_df.groupby("symbol"):
        lookup = {tuple(row[k] for k in keys): row[metric] for _, row in group.iterrows()}
        smoothed = []
        for _, row in group.iterrows():
            base = tuple(row[k] for k in keys)
            neighbor_vals = [lookup[base]]
            idxs = [value_to_idx[d][base[d]] for d in range(len(keys))]
            for dim in range(len(keys)):
                for delta in (-1, 1):
                    new_idxs = idxs.copy()
                    new_idxs[dim] += delta
                    if 0 <= new_idxs[dim] < len(values[dim]):
                        nk = tuple(values[d][new_idxs[d]] if d == dim else base[d] for d in range(len(keys)))
                        if nk in lookup:
                            neighbor_vals.append(lookup[nk])
            smoothed.append(np.nanmedian(neighbor_vals))
        group = group.copy()
        group[f"{metric}_neighbor_median"] = smoothed
        out_frames.append(group)
    return pd.concat(out_frames, ignore_index=True)


# ----------------------------------------------------------------------------
# MAIN - global combo selection across the symbol pool, then holdout
# confirmation across ALL sampled symbols (fix for selection bias).
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    symbols = load_symbol_universe(n_sample=20)
    keys = list(PARAM_GRID.keys())

    per_symbol_results = []
    per_symbol_holdout: dict[str, dict[tuple, pd.Series]] = {}

    for sym in symbols:
        try:
            res, holdout = run_symbol(sym)
        except FileNotFoundError as e:
            print(f"Skipping {sym}: {e}")
            continue
        if res.empty:
            continue
        per_symbol_results.append(res)
        per_symbol_holdout[sym] = holdout

    if not per_symbol_results:
        print("No results - check load_ohlcv()/symbol_universe.txt.")
    else:
        combined = pd.concat(per_symbol_results, ignore_index=True)
        combined = add_neighbor_median_score(combined, metric="expectancy_score")

        # gate literally >=3 of n_folds_total (simplified per review)
        combined_gated = combined[
            (combined["n_folds_positive"] >= 3) & (combined["n"] >= MIN_TRADES)
        ]

        # global combo score: median of neighbor-median across the symbol pool
        global_rank = (
            combined_gated.groupby(keys)["expectancy_score_neighbor_median"]
            .median()
            .reset_index()
            .sort_values("expectancy_score_neighbor_median", ascending=False)
        )
        combined.to_csv("grid_search_results_v3.csv", index=False)
        print("\nTop 10 combos by cross-symbol median neighbor-smoothed expectancy:")
        print(global_rank.head(10).to_string(index=False))

        if not global_rank.empty:
            best_combo = tuple(global_rank.iloc[0][k] for k in keys)
            print(f"\nConfirming best combo on holdout across all {len(symbols)} sampled symbols...")
            all_holdout_rets = []
            for sym, combo_map in per_symbol_holdout.items():
                if best_combo in combo_map:
                    all_holdout_rets.append(combo_map[best_combo])
            if all_holdout_rets:
                pooled_holdout = pd.concat(all_holdout_rets)
                print(f"Pooled holdout score across symbols: {score_returns(pooled_holdout)}")
            else:
                print("No holdout signals for the best combo across the sampled symbols.")
