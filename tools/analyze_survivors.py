"""Post-run analysis: cross-symbol survivors + near-miss diagnostics.

Usage: .venv/bin/python tools/analyze_survivors.py <raw_pkl> [n_sample]
"""
import math
import sys

import numpy as np
import pandas as pd

PF_MIN, FRAC_MIN, MIN_N = 1.2, 0.5, 10


def main() -> None:
    raw = pd.read_pickle(sys.argv[1])
    n_sample = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    syms = raw["symbol"].unique()
    n_traded = len(syms)

    print(f"raw rows={len(raw)}  symbols traded={n_traded}/{n_sample} sampled")
    g = raw.groupby("symbol").agg(max_pf=("profit_factor", "max"),
                                  med_n=("n", "median"), max_n=("n", "max"))
    print("\n=== per-symbol best profit_factor ===")
    print(g.sort_values("max_pf", ascending=False).round(3).to_string())

    qual = raw[(raw["profit_factor"] > PF_MIN) & (raw["n"] >= MIN_N)]
    qcnt = qual.groupby("combo_id")["symbol"].nunique().rename("pf_symbols")
    need = math.ceil(FRAC_MIN * n_traded)
    print(f"\ncross-gate: PF>{PF_MIN} & n>={MIN_N} on >= {need}/{n_traded} "
          f"traded symbols ({FRAC_MIN:.0%} of sample; {n_sample - n_traded} sampled "
          f"symbols produced no trades at all)")
    if qcnt.empty:
        print("NO combo-symbol pair cleared the per-symbol PF floor anywhere.")
        return
    survivors = qcnt[qcnt >= need]
    keys = ["natr_below_min", "natr_above_max", "color_upper", "color_lower",
            "long_lows_min", "short_highs_max", "uhlo_length", "cooldown_s"]
    if not survivors.empty:
        pm = raw.drop_duplicates("combo_id").set_index("combo_id")[keys]
        out = pd.DataFrame(survivors).join(pm)
        print(f"\nSURVIVORS: {len(out)} combos")
        print(out.to_string())
    else:
        top = qcnt.sort_values(ascending=False).head(8)
        near = (qual.groupby("combo_id")
                .agg(pf_symbols=("symbol", "nunique"),
                     pf_med=("profit_factor", "median"),
                     n_med=("n", "median"))
                .loc[top.index].join(raw.drop_duplicates("combo_id")
                                     .set_index("combo_id")[keys]))
        print("\nno survivors; closest candidates by qualifying-symbol count:")
        print(near.round(3).to_string())


if __name__ == "__main__":
    main()
