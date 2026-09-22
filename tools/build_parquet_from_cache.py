"""
tools/build_parquet_from_cache.py
=================================

Converts the project's Bybit kline JSON cache (data/cache/{SYMBOL}-{start_ms}-
{end_ms}-{interval}.json, rows = [open_ms, open, high, low, close, volume])
into the layout screener_param_search_v4 expects:

    data/{symbol}_1m.parquet    columns: ts, open, high, low, close, volume
    data/{symbol}_15m.parquet   15m bars resampled from 1m (ts = bar OPEN time)
    data/symbol_universe.txt    symbols with >= --min-days of 1m coverage

Only symbols whose merged 1m series spans at least --min-days are exported
(shorter histories can't survive the 30d holdout + temporal folds).

Usage:
    python tools/build_parquet_from_cache.py [--min-days 60]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
OUT_DIR = ROOT / "data"
WHITELIST = {"TACUSDT"}  # свежие листинги — не отбраковывать по 2000 барам


def load_symbol_1m(symbol: str) -> pd.DataFrame | None:
    rows: list[list] = []
    for f in CACHE_DIR.glob(f"{symbol}-*-1.json"):
        rows.extend(json.load(open(f)))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ms", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df.pop("ms").to_numpy(), unit="ms", utc=True)
    df = (df.sort_values("ts")
            .drop_duplicates("ts", keep="first")
            .reset_index(drop=True))
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if df[["open", "high", "low", "close"]].isna().any().any():
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df


def resample_15m(df_1m: pd.DataFrame) -> pd.DataFrame:
    return (
        df_1m.set_index("ts")
        .resample("15min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )


def main() -> None:
    global CACHE_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=float, default=60.0,
                    help="minimum merged 1m span to export a symbol (default 60)")
    ap.add_argument("--src", type=str, default=str(CACHE_DIR),
                    help="cache dir with {SYM}-{start}-{end}-{int}.json files")
    ap.add_argument("--universe-name", type=str, default="symbol_universe.txt",
                    help="output file name under data/ for the symbol list")
    args = ap.parse_args()

    src = Path(args.src)
    CACHE_DIR = src
    files = list(src.glob("*-1.json"))
    syms = sorted({f.name.split("-")[0] for f in files})
    print(f"{len(files)} cache files, {len(syms)} symbols in {src}")

    min_span_ms = args.min_days * 86_400_000
    universe: list[str] = []
    skipped_short, skipped_bad = [], []

    for i, sym in enumerate(syms):
        try:
            d1 = load_symbol_1m(sym)
        except (json.JSONDecodeError, ValueError) as e:
            skipped_bad.append((sym, repr(e)))
            continue
        if d1 is None or (len(d1) < 2000 and sym not in WHITELIST):
            skipped_bad.append((sym, "too few bars"))
            continue
        span = int(d1["ts"].max().value - d1["ts"].min().value) // 1_000_000
        if span < min_span_ms and sym not in WHITELIST:
            skipped_short.append(sym)
            continue
        d1.to_parquet(OUT_DIR / f"{sym}_1m.parquet", index=False)
        resample_15m(d1).to_parquet(OUT_DIR / f"{sym}_15m.parquet", index=False)
        universe.append(sym)
        if (i + 1) % 25 == 0:
            print(f"  converted {i + 1}/{len(syms)} ...")

    (OUT_DIR / args.universe_name).write_text("\n".join(universe) + "\n")
    print(f"\nexported {len(universe)} symbols "
          f"(>= {args.min_days:g}d span) -> {args.universe_name}; "
          f"skipped short: {len(skipped_short)}, bad: {len(skipped_bad)}")
    if skipped_bad[:5]:
        print("bad examples:", skipped_bad[:5])


if __name__ == "__main__":
    main()
