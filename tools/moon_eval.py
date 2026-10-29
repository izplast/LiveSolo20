#!/usr/bin/env python3
"""Moon Score historical evaluation (research only).

Fixed formula, no parameter search. Reads the midcap universe, uses Bybit
linear mainnet 5m klines for the last 90 days, caches them on disk, builds
MoonScore from buyer pressure / volume burst / price compression percentiles,
and evaluates long signals against random entries on a chronological 70/30
split.

Nothing here touches live trading code or places orders.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scipy import stats as scipy_stats
except Exception:  # pragma: no cover
    scipy_stats = None

try:
    from zoneinfo import ZoneInfo

    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # pragma: no cover
    KYIV = timezone(timedelta(hours=2))

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = ROOT / "data" / "midcap_universe.json"
CACHE_DIR = ROOT / "data" / "cache_moon5m"
LEGACY_CACHE_DIRS = (ROOT / "data" / "cache", ROOT / "data" / "cache_dl")
MIDCAP_LOG = ROOT / "logs" / "screener-events.jsonl"
REPORT_JSON = ROOT / "logs" / "moon_eval_report.json"

API = "https://api.bybit.com"
CATEGORY = "linear"
INTERVAL = "5"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
BAR_MS = 300_000
BARS_PER_HOUR = 12
BARS_PER_DAY = 288
PCT_WINDOW = 7 * BARS_PER_DAY
ATR_LEN = 14
FEE_PCT = 0.11
DEDUP_MS = 6 * HOUR_MS
DIP_BARS = 36
DIP_LEVEL = 50.0
CROSS_LEVEL = 66.0
UPPER_LEVEL = 85.0
CLIMAX_LEVEL = 86.0
BONFERRONI_P = 0.0125
HORIZONS = (1, 2, 4, 8)
RANDOM_TRIES = 3
RANDOM_SPAN_BARS = BARS_PER_DAY


def log(msg: str) -> None:
    print(msg, flush=True)


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _kyiv(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=KYIV).strftime("%Y-%m-%d %H:%M %Z")


def _fmt_ts(ms: int) -> str:
    return f"{_kyiv(ms)} / {_utc(ms)} UTC"


def _get(path: str, params: dict[str, Any], tries: int = 6) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(f"{API}{path}?{qs}", timeout=25) as resp:
                payload = json.load(resp)
            code = payload.get("retCode")
            if code == 0:
                return payload
            if code in (10006, 10018):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"retCode={code} {payload.get('retMsg')}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET {path} failed: {last}")


def _page_name(sym: str, cursor: int) -> str:
    return f"{sym}-{cursor - 4_999_999}-{cursor}-5.json"


def download_symbol(
    sym: str, start_ms: int, end_ms: int, out: Path, pause: float
) -> tuple[str, int, int]:
    folder = out / sym
    folder.mkdir(parents=True, exist_ok=True)
    cursor = end_ms
    pages = 0
    while cursor > start_ms:
        f = folder / _page_name(sym, cursor)
        if f.exists():
            try:
                rows = json.loads(f.read_text())
            except json.JSONDecodeError:
                f.unlink()
                continue
            if len(rows) < 1000:
                break
            cursor = int(rows[-1][0]) - 1
            continue
        payload = _get(
            "/v5/market/kline",
            {
                "category": CATEGORY,
                "symbol": sym,
                "interval": INTERVAL,
                "limit": 1000,
                "end": cursor,
            },
        )
        lst = payload["result"]["list"]
        if not lst:
            break
        rows = [
            [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
            for r in lst
        ]
        f.write_text(json.dumps(rows))
        pages += 1
        if len(lst) < 1000:
            break
        cursor = int(rows[-1][0]) - 1
        time.sleep(pause)
    return sym, pages, cursor


def reuse_legacy_5m(sym: str) -> list[list[Any]] | None:
    pages: list[list[Any]] = []
    for folder in LEGACY_CACHE_DIRS:
        if not folder.exists():
            continue
        for f in folder.glob(f"{sym}-*-5.json"):
            try:
                pages.extend(json.loads(f.read_text()))
            except Exception:  # noqa: BLE001
                continue
    if not pages:
        return None
    return pages


def load_symbol_rows(sym: str, start_ms: int) -> list[list[Any]] | None:
    page_dir = CACHE_DIR / sym
    raw: list[list[Any]] = []
    if page_dir.exists():
        for f in page_dir.glob("*-5.json"):
            try:
                raw.extend(json.loads(f.read_text()))
            except Exception:  # noqa: BLE001
                continue
    if not raw:
        legacy = reuse_legacy_5m(sym)
        if legacy:
            raw = legacy
    if not raw:
        return None
    by_ts: dict[int, list[Any]] = {}
    for r in raw:
        try:
            ts = int(r[0])
        except (TypeError, ValueError):
            continue
        if ts < start_ms - DAY_MS:
            continue
        by_ts[ts] = [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
    rows = [by_ts[k] for k in sorted(by_ts)]
    return rows or None


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder_atr(df: pd.DataFrame, length: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def pct_rank(series: pd.Series, window: int) -> pd.Series:
    ranks = series.rolling(window + 1, min_periods=window + 1).rank(method="min")
    return (ranks - 1.0) / float(window) * 100.0


def prepare_symbol(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    rng = high - low
    buy_vol = np.where(rng > 0, volume * (close - low) / rng, 0.5 * volume)
    buy_vol_s = pd.Series(buy_vol, index=df.index)
    pressure = ema(buy_vol_s, 12) / ema(volume, 12)
    pressure = pressure.clip(0.0, 1.0)
    burst = ema(volume, 3) / ema(volume, 96)
    compression = (high.rolling(24, min_periods=24).max() - low.rolling(24, min_periods=24).min()) / close * 100.0

    p_p = pct_rank(pressure, PCT_WINDOW)
    p_r = pct_rank(burst, PCT_WINDOW)
    p_s_inv = 100.0 - pct_rank(compression, PCT_WINDOW)
    moon = (p_p + p_r + p_s_inv) / 3.0

    atr = wilder_atr(df, ATR_LEN)
    atr_pct = atr / close * 100.0

    vol_base = ema(volume, 200)
    vol_std = volume.rolling(200, min_periods=200).std(ddof=0)
    vol_spike = volume > (vol_base + 2.0 * vol_std)

    hour_open = (df["ts"] // HOUR_MS) * HOUR_MS
    grouped = df.groupby(hour_open)
    h1 = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    h1_ema = h1["close"].ewm(span=200, adjust=False, min_periods=200).mean()
    h1_above = (h1["close"] > h1_ema).astype(bool)
    h1_key = ((df["ts"] + BAR_MS) // HOUR_MS) * HOUR_MS - HOUR_MS
    h1_trend = h1_key.map(h1_above).eq(True)

    df = df.copy()
    df["pressure"] = pressure
    df["burst"] = burst
    df["compression"] = compression
    df["p_p"] = p_p
    df["p_r"] = p_r
    df["p_s_inv"] = p_s_inv
    df["moon"] = moon
    df["atr_pct"] = atr_pct
    df["vol_spike"] = vol_spike.fillna(False)
    df["h1_trend"] = h1_trend
    return df


def signal_mask(df: pd.DataFrame) -> dict[str, np.ndarray]:
    score = df["moon"]
    prev = score.shift(1)
    cross = (prev <= CROSS_LEVEL) & (score > CROSS_LEVEL) & (score <= UPPER_LEVEL)
    dipped = score.rolling(DIP_BARS, min_periods=DIP_BARS).min().shift(1) <= DIP_LEVEL
    base = cross & dipped
    vol_ok = df["vol_spike"].to_numpy(dtype=bool)
    trend_ok = df["h1_trend"].to_numpy(dtype=bool)
    base_arr = base.fillna(False).to_numpy(dtype=bool)
    v1 = base_arr & vol_ok
    v2 = v1 & trend_ok
    confirm = (
        (score > CROSS_LEVEL)
        & (score.shift(1) > CROSS_LEVEL)
        & (score.shift(2) <= CROSS_LEVEL)
        & (score <= UPPER_LEVEL)
    )
    dipped3 = score.rolling(DIP_BARS, min_periods=DIP_BARS).min().shift(2) <= DIP_LEVEL
    v3 = confirm.fillna(False).to_numpy(dtype=bool) & dipped3.fillna(False).to_numpy(dtype=bool) & vol_ok & trend_ok
    climax = (score >= CLIMAX_LEVEL) & (score.shift(1) < CLIMAX_LEVEL)
    return {
        "V0": base_arr,
        "V1": v1,
        "V2": v2,
        "V3": v3,
        "Climax": climax.fillna(False).to_numpy(dtype=bool),
    }


def dedup_indices(mask: np.ndarray, ts: np.ndarray, gap_ms: int = DEDUP_MS) -> list[int]:
    out: list[int] = []
    last = -10**18
    for i in np.flatnonzero(mask):
        t = int(ts[i])
        if t - last >= gap_ms:
            out.append(int(i))
            last = t
    return out


def random_indices(rng: np.random.Generator, center: int, n: int, tries: int = RANDOM_TRIES) -> list[int]:
    lo = max(0, center - RANDOM_SPAN_BARS)
    hi = min(n - 8 * BARS_PER_HOUR, center + RANDOM_SPAN_BARS)
    if hi < lo:
        return []
    return [int(v) for v in rng.integers(lo, hi + 1, size=tries)]


def forward_record(
    i: int,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    atr_pct: np.ndarray,
    n: int,
) -> dict[str, float] | None:
    e = i + 1
    end = e + 8 * BARS_PER_HOUR - 1
    if end >= n:
        return None
    entry = float(o[e])
    a = float(atr_pct[i])
    if entry <= 0 or not math.isfinite(a) or a <= 0:
        return None
    rec: dict[str, float] = {"entry_price": entry, "atr_pct": a}
    for hrs in HORIZONS:
        j = e + hrs * BARS_PER_HOUR - 1
        ret = (float(c[j]) / entry - 1.0) * 100.0
        rec[f"ret{hrs}"] = ret
        rec[f"ret{hrs}_atr"] = ret / a
    mfe = (float(np.max(h[e : end + 1])) / entry - 1.0) * 100.0
    mae = (float(np.min(l[e : end + 1])) / entry - 1.0) * 100.0
    rec["mfe"] = mfe
    rec["mae"] = mae
    rec["mfe_atr"] = mfe / a
    rec["mae_atr"] = mae / a
    return rec


def collect_symbol(
    sym: str,
    df: pd.DataFrame,
    split_ts: int,
    rng: np.random.Generator,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    masks = signal_mask(df)
    ts = df["ts"].to_numpy(dtype=np.int64)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    atr = df["atr_pct"].to_numpy(dtype=float)
    n = len(df)
    signals: dict[str, list[dict[str, Any]]] = {k: [] for k in masks}
    delay_ref: list[dict[str, Any]] = []
    for variant, mask in masks.items():
        idxs = dedup_indices(mask, ts)
        for i in idxs:
            rec = forward_record(i, o, h, l, c, atr, n)
            if rec is None:
                continue
            e = i + 1
            base_vals: dict[str, list[float]] = {
                f"ret{hrs}": [] for hrs in HORIZONS
            }
            for hrs in HORIZONS:
                base_vals[f"ret{hrs}_atr"] = []
            for key in ("mfe", "mae", "mfe_atr", "mae_atr"):
                base_vals[key] = []
            for j in random_indices(rng, e, n):
                br = forward_record(j - 1, o, h, l, c, atr, n)
                if br is None:
                    continue
                for key in base_vals:
                    base_vals[key].append(br[key])
            item = {
                "symbol": sym,
                "variant": variant,
                "signal_ts": int(ts[i]),
                "signal_close_ts": int(ts[i]) + BAR_MS,
                "entry_ts": int(ts[e]),
                "entry_price": rec["entry_price"],
                "atr_pct": rec["atr_pct"],
                "split": "train" if int(ts[e]) < split_ts else "test",
                "moon": float(df["moon"].iloc[i]),
                "n_base": len(base_vals.get("ret8", [])),
            }
            for key in ("ret1", "ret2", "ret4", "ret8", "ret1_atr", "ret2_atr", "ret4_atr", "ret8_atr", "mfe", "mae", "mfe_atr", "mae_atr"):
                item[key] = rec[key]
                item[f"base_{key}"] = float(np.mean(base_vals[key])) if base_vals[key] else float("nan")
            signals[variant].append(item)
            if variant in ("V0", "V3"):
                delay_ref.append(item)
    return signals, {"delay": delay_ref}


def aggregate(records: list[dict[str, Any]], days: float) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(records), "days": round(days, 2), "per_day": round(len(records) / days, 3) if days else None}
    if not records:
        return out
    for hrs in HORIZONS:
        vals = np.array([r[f"ret{hrs}"] for r in records], dtype=float)
        vals_atr = np.array([r[f"ret{hrs}_atr"] for r in records], dtype=float)
        bases = np.array([r[f"base_ret{hrs}"] for r in records], dtype=float)
        diff = vals - bases
        mask = np.isfinite(diff)
        p = None
        if mask.sum() >= 8 and scipy_stats is not None:
            try:
                p = float(scipy_stats.wilcoxon(diff[mask], zero_method="wilcox").pvalue)
            except Exception:  # noqa: BLE001
                p = None
        out[f"ret{hrs}"] = {
            "mean_pct": float(np.nanmean(vals)),
            "median_pct": float(np.nanmedian(vals)),
            "mean_atr": float(np.nanmean(vals_atr)),
            "median_atr": float(np.nanmedian(vals_atr)),
            "hit_rate": float(np.mean(vals > 0)),
            "base_mean_pct": float(np.nanmean(bases)),
            "diff_mean_pct": float(np.nanmean(diff)),
            "diff_median_pct": float(np.nanmedian(diff)),
            "p_value": p,
            "fee_covered": bool(np.nanmean(vals) > FEE_PCT),
            "n_paired": int(mask.sum()),
        }
    for key in ("mfe", "mae", "mfe_atr", "mae_atr"):
        out[key] = {"mean": float(np.nanmean([r[key] for r in records])),
                    "median": float(np.nanmedian([r[key] for r in records]))}
    return out


def simulate_exit(records: list[dict[str, Any]], df: pd.DataFrame, fee_pct: float = FEE_PCT) -> dict[str, Any]:
    o = df["open"].to_numpy(dtype=float)
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=np.int64)
    index = {int(t): k for k, t in enumerate(ts)}
    results = []
    for r in records:
        i = index.get(int(r["signal_ts"]))
        if i is None:
            continue
        e = i + 1
        end = e + 8 * BARS_PER_HOUR - 1
        if end >= len(o):
            continue
        entry = float(o[e])
        a = float(r["atr_pct"])
        sl = entry * (1.0 - 2.0 * a / 100.0)
        tp = entry * (1.0 + 3.0 * a / 100.0)
        exit_price = float(cl[end])
        reason = "timeout"
        for j in range(e, end + 1):
            if float(lo[j]) <= sl:
                exit_price = sl
                reason = "sl"
                break
            if float(hi[j]) >= tp:
                exit_price = tp
                reason = "tp"
                break
        gross = (exit_price / entry - 1.0) * 100.0
        results.append({"gross": gross, "net": gross - fee_pct, "reason": reason, "mfe": (float(np.max(hi[e:end+1]))/entry-1)*100, "mae": (float(np.min(lo[e:end+1]))/entry-1)*100})
    if not results:
        return {"n": 0}
    nets = np.array([x["net"] for x in results])
    wins = nets[nets > 0]
    losses = -nets[nets < 0]
    pf = float(wins.sum() / losses.sum()) if losses.sum() > 0 else None
    return {
        "n": len(results),
        "mean_net_pct": float(nets.mean()),
        "median_net_pct": float(np.median(nets)),
        "win_rate": float(np.mean(nets > 0)),
        "sum_net_pct": float(nets.sum()),
        "pf": pf,
        "tp_rate": float(np.mean([x["reason"] == "tp" for x in results])),
        "sl_rate": float(np.mean([x["reason"] == "sl" for x in results])),
        "timeout_rate": float(np.mean([x["reason"] == "timeout" for x in results])),
    }


def load_midcap_times() -> tuple[dict[str, list[int]], tuple[int, int] | None]:
    times: dict[str, list[int]] = {}
    lo = hi = None
    try:
        with open(MIDCAP_LOG, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "signal_sent":
                    continue
                sig = rec.get("signal") or {}
                sym = sig.get("symbol") or rec.get("symbol")
                ts = rec.get("ts") or sig.get("ts")
                if not sym or ts is None:
                    continue
                t = int(ts)
                times.setdefault(str(sym), []).append(t)
                lo = t if lo is None else min(lo, t)
                hi = t if hi is None else max(hi, t)
    except FileNotFoundError:
        pass
    for sym in times:
        times[sym].sort()
    if lo is None:
        return times, None
    return times, (int(lo), int(hi))


def overlap_fraction(records: list[dict[str, Any]], midcap: dict[str, list[int]], window_ms: int = 30 * 60_000) -> dict[str, Any]:
    matched = 0
    total = 0
    for r in records:
        arr = midcap.get(r["symbol"])
        if not arr:
            continue
        total += 1
        ts = int(r["signal_close_ts"])
        pos = int(np.searchsorted(arr, ts))
        found = False
        for k in (pos - 1, pos):
            if 0 <= k < len(arr) and abs(int(arr[k]) - ts) <= window_ms:
                found = True
                break
        if found:
            matched += 1
    return {"matched": matched, "total": total, "fraction": (matched / total) if total else None}


def stat_line(label: str, agg: dict[str, Any]) -> str:
    if not agg.get("n"):
        return f"{label:<8} n=0"
    r8 = agg["ret8"]
    p = r8.get("p_value")
    return (
        f"{label:<8} n={agg['n']:<5} /day={agg['per_day']:<5} "
        f"mean8h={r8['mean_pct']:+.3f}% ({r8['mean_atr']:+.2f} ATR) "
        f"base={r8['base_mean_pct']:+.3f}% diff={r8['diff_mean_pct']:+.3f}% "
        f"p={('nan' if p is None else f'{p:.4g}')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Moon Score historical evaluation (research only)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pause", type=float, default=0.05)
    ap.add_argument("--symbols", type=str, default=None)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    universe = [str(s) for s in json.loads(UNIVERSE.read_text())["symbols"]]
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        universe = [s for s in universe if s in wanted]
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * DAY_MS
    split_ts = start_ms + int(0.7 * (now_ms - start_ms))
    log(f"universe={len(universe)} days={args.days} start={_fmt_ts(start_ms)} split={_fmt_ts(split_ts)} end={_fmt_ts(now_ms)}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not args.skip_download:
        started = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(download_symbol, s, start_ms, now_ms, CACHE_DIR, args.pause) for s in universe]
            for fut in futures:
                try:
                    sym, pages, _ = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log(f"download error: {exc}")
                    continue
                done += 1
                if done % 25 == 0 or done == len(universe):
                    log(f"downloaded {done}/{len(universe)} symbols ({time.time()-started:.0f}s, last {sym} +{pages})")

    rng = np.random.default_rng(args.seed)
    all_records: dict[str, list[dict[str, Any]]] = {v: [] for v in ("V0", "V1", "V2", "V3", "Climax")}
    delay_records: list[dict[str, Any]] = []
    used = 0
    skipped = 0
    for k, sym in enumerate(universe, 1):
        rows = load_symbol_rows(sym, start_ms)
        if not rows or len(rows) < PCT_WINDOW + 120:
            skipped += 1
            continue
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df[df["ts"] >= start_ms - DAY_MS].reset_index(drop=True)
        if len(df) < PCT_WINDOW + 120:
            skipped += 1
            continue
        try:
            df = prepare_symbol(df)
        except Exception as exc:  # noqa: BLE001
            log(f"prepare error {sym}: {exc}")
            skipped += 1
            continue
        if not np.isfinite(df["moon"].to_numpy(dtype=float)).any():
            skipped += 1
            continue
        used += 1
        sigs, extra = collect_symbol(sym, df, split_ts, rng)
        for variant in all_records:
            all_records[variant].extend(sigs.get(variant, []))
        delay_records.extend(extra.get("delay", []))
        if k % 50 == 0:
            log(f"processed {k}/{len(universe)} symbols (used={used} skipped={skipped})")

    log(f"symbols used={used} skipped={skipped}")

    train_days = (split_ts - start_ms) / DAY_MS
    test_days = (now_ms - split_ts) / DAY_MS
    midcap, midcap_cov = load_midcap_times()
    report: dict[str, Any] = {
        "generated_utc": _utc(now_ms),
        "days": args.days,
        "universe": len(universe),
        "symbols_used": used,
        "symbols_skipped": skipped,
        "start": _fmt_ts(start_ms),
        "split": _fmt_ts(split_ts),
        "end": _fmt_ts(now_ms),
        "train_days": round(train_days, 2),
        "test_days": round(test_days, 2),
        "fee_pct": FEE_PCT,
        "formula": {
            "buy_vol": "volume*(close-low)/(high-low), high==low -> 0.5*volume",
            "P": "EMA12(buy_vol)/EMA12(volume)",
            "R": "EMA3(volume)/EMA96(volume)",
            "S": "(rolling24 max(high)-min(low))/close*100",
            "percentile_window_days": 7,
            "moon": "(pP+pR+(100-pS))/3",
            "signal": "cross 66 up, <=85, dip<=50 within prior 36 bars, dedup 6h",
        },
        "variants": {},
        "climax": {},
        "strategy": {},
        "v3_delay": {},
        "midcap_overlap": {},
    }

    log("\n=== Moon Score: train/test by variant ===")
    for variant in ("V0", "V1", "V2", "V3"):
        recs = all_records[variant]
        train = [r for r in recs if r["split"] == "train"]
        test = [r for r in recs if r["split"] == "test"]
        agg_all = aggregate(recs, train_days + test_days)
        agg_train = aggregate(train, train_days)
        agg_test = aggregate(test, test_days)
        report["variants"][variant] = {"all": agg_all, "train": agg_train, "test": agg_test,
                                       "horizons": {h: {"train": agg_train.get(f"ret{h}"), "test": agg_test.get(f"ret{h}")} for h in HORIZONS}}
        log(stat_line(f"{variant} all", agg_all))
        log(stat_line(f"{variant} train", agg_train))
        log(stat_line(f"{variant} test", agg_test))
        for h in HORIZONS:
            tr = agg_train.get(f"ret{h}") or {}
            te = agg_test.get(f"ret{h}") or {}
            log(
                f"  {h}h: train {tr.get('mean_pct', float('nan')):+.3f}% / {tr.get('mean_atr', float('nan')):+.2f} ATR "
                f"diff {tr.get('diff_mean_pct', float('nan')):+.3f}% p={tr.get('p_value') if tr.get('p_value') is not None else 'nan'} "
                f"| test {te.get('mean_pct', float('nan')):+.3f}% / {te.get('mean_atr', float('nan')):+.2f} ATR "
                f"diff {te.get('diff_mean_pct', float('nan')):+.3f}% p={te.get('p_value') if te.get('p_value') is not None else 'nan'}"
            )
        mfe = agg_test.get("mfe", {})
        mae = agg_test.get("mae", {})
        log(f"  test MFE8h={mfe.get('mean', float('nan')):+.3f}% MAE8h={mae.get('mean', float('nan')):+.3f}%")

    climax = all_records["Climax"]
    climax_train = [r for r in climax if r["split"] == "train"]
    climax_test = [r for r in climax if r["split"] == "test"]
    report["climax"] = {
        "all": aggregate(climax, train_days + test_days),
        "train": aggregate(climax_train, train_days),
        "test": aggregate(climax_test, test_days),
    }
    log("\n=== Climax (MoonScore >= 86) ===")
    for split, recs, days in (("train", climax_train, train_days), ("test", climax_test, test_days)):
        agg = report["climax"][split]
        if agg.get("n"):
            r8 = agg["ret8"]
            log(
                f"{split:<6} n={agg['n']:<5} /day={agg['per_day']:<5} "
                f"mean1/2/4/8h="
                f"{agg['ret1']['mean_pct']:+.3f}/{agg['ret2']['mean_pct']:+.3f}/"
                f"{agg['ret4']['mean_pct']:+.3f}/{r8['mean_pct']:+.3f}% "
                f"8h={r8['mean_atr']:+.2f} ATR hit={r8['hit_rate']*100:.1f}% "
                f"MFE/MAE={agg['mfe']['mean']:+.2f}/{agg['mae']['mean']:+.2f}% "
                f"p={r8.get('p_value') if r8.get('p_value') is not None else 'nan'}"
            )
    if climax_test:
        neg = np.mean([r["ret8"] < 0 for r in climax_test])
        report["climax"]["test_negative_8h_share"] = float(neg)
        log(f"climax test share of negative 8h returns: {neg*100:.1f}%")

    log("\n=== Strategy 2xATR stop / 3xATR TP / 8h timeout ===")
    for variant in ("V0", "V1", "V2", "V3"):
        tr = report["variants"][variant]["train"].get("ret8") or {}
        p = tr.get("p_value")
        diff = tr.get("diff_mean_pct")
        positive = bool(diff is not None and diff > 0)
        if p is None or p >= BONFERRONI_P or not positive:
            report["strategy"][variant] = {
                "skipped": "train advantage not significant/positive",
                "train_p": p,
                "train_diff_pct": diff,
            }
            log(
                f"{variant}: skipped, train p={p if p is not None else 'nan'}, "
                f"diff={diff if diff is not None else float('nan'):+.3f}% "
                f"(need p<{BONFERRONI_P} and diff>0)"
            )
            continue
        train_recs = [r for r in all_records[variant] if r["split"] == "train"]
        test_recs = [r for r in all_records[variant] if r["split"] == "test"]
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for r in train_recs + test_recs:
            by_symbol.setdefault(r["symbol"], []).append(r)
        sim_train: list[dict[str, Any]] = []
        sim_test: list[dict[str, Any]] = []
        for sym, recs in by_symbol.items():
            rows = load_symbol_rows(sym, start_ms)
            if not rows:
                continue
            sdf = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            sdf = sdf[sdf["ts"] >= start_ms - DAY_MS].reset_index(drop=True)
            sdf = prepare_symbol(sdf)
            tr_sim = simulate_exit([r for r in recs if r["split"] == "train"], sdf)
            te_sim = simulate_exit([r for r in recs if r["split"] == "test"], sdf)
            if tr_sim.get("n"):
                sim_train.append(tr_sim)
            if te_sim.get("n"):
                sim_test.append(te_sim)
        def merge_sim(parts: list[dict[str, Any]]) -> dict[str, Any]:
            n = sum(p["n"] for p in parts)
            if not n:
                return {"n": 0}
            return {
                "n": n,
                "mean_net_pct": sum(p["mean_net_pct"] * p["n"] for p in parts) / n,
                "sum_net_pct": sum(p["sum_net_pct"] for p in parts),
                "win_rate": sum(p["win_rate"] * p["n"] for p in parts) / n,
                "tp_rate": sum(p["tp_rate"] * p["n"] for p in parts) / n,
                "sl_rate": sum(p["sl_rate"] * p["n"] for p in parts) / n,
                "timeout_rate": sum(p["timeout_rate"] * p["n"] for p in parts) / n,
                "pf": None,
            }
        report["strategy"][variant] = {"train_p": p, "train": merge_sim(sim_train), "test": merge_sim(sim_test)}
        for split, merged in (("train", merge_sim(sim_train)), ("test", merge_sim(sim_test))):
            if merged.get("n"):
                log(
                    f"{variant} {split:<5} n={merged['n']:<5} mean_net={merged['mean_net_pct']:+.3f}% "
                    f"sum={merged['sum_net_pct']:+.2f}% win={merged['win_rate']*100:.1f}% "
                    f"TP/SL/TO={merged['tp_rate']*100:.1f}/{merged['sl_rate']*100:.1f}/{merged['timeout_rate']*100:.1f}%"
                )

    log("\n=== V3 delay vs V0 ===")
    v0 = [r for r in delay_records if r["variant"] == "V0"]
    v3 = [r for r in delay_records if r["variant"] == "V3"]
    v0_by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in v0:
        v0_by_sym.setdefault(r["symbol"], []).append(r)
    for arr in v0_by_sym.values():
        arr.sort(key=lambda x: x["entry_ts"])
    deltas: list[float] = []
    moves: list[float] = []
    for r in v3:
        arr = v0_by_sym.get(r["symbol"], [])
        prior = [x for x in arr if x["entry_ts"] <= r["entry_ts"] and r["entry_ts"] - x["entry_ts"] <= 6 * HOUR_MS]
        if not prior:
            continue
        ref = prior[-1]
        deltas.append((r["entry_ts"] - ref["entry_ts"]) / 60_000.0)
        moves.append((r["entry_price"] / ref["entry_price"] - 1.0) * 100.0)
    if deltas:
        report["v3_delay"] = {
            "matched": len(deltas),
            "v3_total": len(v3),
            "median_delay_min": float(np.median(deltas)),
            "median_move_pct": float(np.median(moves)),
            "mean_move_pct": float(np.mean(moves)),
        }
        log(
            f"matched {len(deltas)}/{len(v3)} V3 signals; median delay={np.median(deltas):.1f} min, "
            f"median move already passed={np.median(moves):+.3f}%"
        )
    else:
        log("no matched V3/V0 signals")

    log("\n=== Overlap with midcap signal_sent (+/-30 min) ===")
    for variant in ("V0", "V1", "V2", "V3"):
        recs = all_records[variant]
        ov_all = overlap_fraction(recs, midcap)
        cov = None
        if midcap_cov:
            cov_recs = [r for r in recs if midcap_cov[0] - 30 * 60_000 <= r["signal_close_ts"] <= midcap_cov[1] + 30 * 60_000]
            cov = overlap_fraction(cov_recs, midcap)
        report["midcap_overlap"][variant] = {"all": ov_all, "within_log_coverage": cov}
        if ov_all.get("fraction") is not None:
            frac = ov_all["fraction"]
            extra = ""
            if cov and cov.get("fraction") is not None:
                extra = f" | inside log coverage {cov['matched']}/{cov['total']}={cov['fraction']*100:.1f}%"
            log(f"{variant}: {ov_all['matched']}/{ov_all['total']}={frac*100:.1f}%{extra}")

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    log(f"\nreport -> {REPORT_JSON}")

    v0_test = report["variants"]["V0"]["test"]
    r8 = v0_test.get("ret8") or {}
    p8 = r8.get("p_value")
    conclusion = (
        f"V0 test: n={v0_test.get('n', 0)} ({v0_test.get('per_day')}/day), "
        f"8h mean {r8.get('mean_pct', float('nan')):+.3f}%, base diff {r8.get('diff_mean_pct', float('nan')):+.3f}%, "
        f"p={p8 if p8 is not None else 'nan'}"
    )
    log("\nRESULT: " + conclusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
