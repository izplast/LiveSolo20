"""
tools/download_bybit_klines.py
==============================

Downloads Bybit linear (USDT perpetual) 1m klines into the project cache
format so tools/build_parquet_from_cache.py can consume them:

    {OUT}/{SYMBOL}-{start_ms}-{end_ms}-1.json   rows = [open_ms,o,h,l,c,v]

Batch files are 1000-bar pages walked BACKWARD from now until --days of
coverage is reached (or the listing starts). Existing batch files are kept
(resume-safe): re-running only fetches missing pages.

Usage:
    python tools/download_bybit_klines.py --top 24 --days 330 [--quote USDT]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.bybit.com"
EXCLUDE_RE = re.compile(
    r"(USDC|FDUSD|DAI|TUSD|USD1)$|(UP|DOWN|BULL|BEAR)USDT$|^[A-Z]{1,5}(USDT)?$"
)


def _get(path: str, params: dict, tries: int = 4) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(f"{API}{path}?{qs}", timeout=30) as r:
                return json.load(r)
        except Exception as e:                      # noqa: BLE001 - retry any net err
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {path} failed: {last}")


def top_liquid(n: int, quote: str) -> list[str]:
    res = _get("/v5/market/tickers", {"category": "linear"})["result"]["list"]
    rows = []
    for t in res:
        s = t["symbol"]
        if not s.endswith(quote):
            continue
        base = s[: -len(quote)]
        if EXCLUDE_RE.search(base):
            continue
        try:
            rows.append((float(t.get("turnover24h") or 0.0), s))
        except ValueError:
            continue
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


def download_symbol(sym: str, days: int, interval: str, out: Path) -> tuple[str, int, float]:
    sym_dir = out
    sym_dir.mkdir(parents=True, exist_ok=True)
    end_ms = int(time.time() * 1000)
    start_target = end_ms - days * 86_400_000

    cursor, saved, t0 = end_ms, 0, time.time()
    fails = 0
    while cursor > start_target:
        f = sym_dir / f"{sym}-{cursor - 59_999_999}-{cursor}-{interval}.json"
        if f.exists():                              # resume: page already fetched
            rows = json.loads(f.read_text())
            if len(rows) < 1000:
                break
            cursor = int(rows[-1][0]) - 1
            continue
        try:
            r = _get("/v5/market/kline", {"category": "linear", "symbol": sym,
                                          "interval": interval, "limit": 1000,
                                          "end": cursor})
        except RuntimeError:
            fails += 1                              # transient API trouble: skip the
            time.sleep(2.0)                         # page now, resume run picks it up
            if fails >= 6:
                print(f"  {sym}: giving up after {fails} consecutive failures",
                      flush=True)
                return sym, saved, time.time() - t0
            continue
        if r.get("retCode") != 0:
            return sym, saved, time.time() - t0
        lst = r["result"]["list"]                   # newest -> oldest
        if not lst:
            break
        rows = [[int(x[0]), x[1], x[2], x[3], x[4], x[5]] for x in lst]
        f.write_text(json.dumps(rows))
        saved += 1
        oldest = int(rows[-1][0])
        if len(lst) < 1000:
            break
        cursor = oldest - 1
        time.sleep(0.06)                            # ~16 req/s/thread ceiling
    return sym, saved, time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=24)
    ap.add_argument("--days", type=int, default=330)
    ap.add_argument("--interval", type=str, default="1")
    ap.add_argument("--quote", type=str, default="USDT")
    ap.add_argument("--out", type=str, default=str(ROOT / "data" / "cache_dl"))
    ap.add_argument("--symbols", type=str, default=None,
                    help="comma list overriding the liquidity ranking")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    syms = (args.symbols.split(",") if args.symbols
            else top_liquid(args.top, args.quote))
    print(f"downloading {len(syms)} symbols x <= {args.days}d of "
          f"{args.interval}m klines -> {args.out}", flush=True)

    out = Path(args.out)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for sym, n_pages, dt in ex.map(lambda s: download_symbol(
                s, args.days, args.interval, out), syms):
            done += 1
            print(f"[{done}/{len(syms)}] {sym}: +{n_pages} pages "
                  f"({dt:.0f}s)", flush=True)
    print("download complete")


if __name__ == "__main__":
    main()
