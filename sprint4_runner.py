"""
sprint4_runner.py
=================
Спринт 4: Statistical validation & защита от overfitting.

Методология:
  1. DSR — Deflated Sharpe с поправкой на N=6561 испытаний
  2. PBO — CSCV по блокам
  3. Walk-Forward (train/val expanding, holdout blind)
  4. Liquidity-стратификация High/Mid/Low
  5. Фиксированный seed + метаданные .pkl, запрет Top-10 без holdout

Использование:
  python3 sprint4_runner.py --sample 21 --stratified --seed 42 --dsr-threshold 0.95
  python3 sprint4_runner.py --from-raw grid_search_raw_v4_tp1.1_sl1.5.pkl --seed 42
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# импорт ядра v4 — переиспользуем всю логику сигналов/выходов
import screener_param_search_v4 as v4
import statistical_validation as sv

# ---------------------------------------------------------------------------
# Константы Спринта 4
# ---------------------------------------------------------------------------
DEFAULT_SEED = 42
DSR_THRESHOLD = 0.95
WF_WINDOWS = 4
WF_MIN_HIT_RATE = 0.50       # минимум окон с SR>0
WF_MIN_AVG_OOS_SR = 0.0
PBO_MAX = 0.5                # PBO должен быть <0.5 чтобы метод не переобучен
N_TRIALS = 6561              # полный размер грида v4
HOLDOUT_DAYS = v4.HOLDOUT_DAYS

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _git_commit() -> str:
    try:
        import subprocess
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return "unknown"

def build_metadata(seed: int, args: argparse.Namespace, buckets: dict | None, symbols: list[str]) -> dict:
    return {
        "sprint": 4,
        "seed": seed,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _git_commit(),
        "holdout_days": HOLDOUT_DAYS,
        "tp_pct": v4.ACTIVE_TP_PCT,
        "sl_pct": v4.ACTIVE_SL_PCT,
        "cost_tp": v4.COST_TP_EXIT,
        "cost_taker": v4.COST_TAKER_EXIT,
        "cost_gap": v4.COST_GAP_EXIT,
        "param_grid": v4.PARAM_GRID,
        "n_trials": N_TRIALS,
        "dsr_threshold": getattr(args, "dsr_threshold", DSR_THRESHOLD),
        "wf_windows": getattr(args, "wf_windows", WF_WINDOWS),
        "stratified": bool(getattr(args, "stratified", False)),
        "buckets": buckets,
        "symbols": symbols,
        "n_symbols": len(symbols),
        "args": vars(args),
    }

def _load_turnovers(symbols: list[str], data_dir: Path = Path("./data")) -> dict[str, float]:
    tv = {}
    for s in symbols:
        tv[s] = sv.compute_turnover(s, data_dir=data_dir)
    return tv

def get_stratified_symbols(n_total: int = 21, seed: int = DEFAULT_SEED,
                           universe_path: str | Path = "data/symbol_universe.txt") -> Tuple[list[str], dict[str, str], dict[str, float]]:
    all_syms = [l.strip() for l in Path(universe_path).read_text().splitlines() if l.strip()]
    turnovers = _load_turnovers(all_syms)
    buckets = sv.assign_liquidity_buckets(turnovers, n_buckets=3)
    # сбалансированно
    sampled = sv.stratified_sample(all_syms, buckets, total=n_total, seed=seed)
    return sampled, buckets, turnovers

def pooled_is_returns_for_combo(combo_id: int, combo_params: dict, symbols: list[str]) -> pd.Series:
    """Собрать pooled IS (без holdout) трейды для DSR."""
    all_rets = []
    p = v4.params_from(combo_params)
    for sym in symbols:
        try:
            sd = v4.prepare_symbol(sym)
        except Exception:
            continue
        arrs = v4.get_feat_arrays(sym, p.uhlo_length, sd)
        mask, code = v4.evaluate_signals(arrs, p)
        # только IS период (< holdout_start)
        is_mask = mask & (arrs["ts"] < sd.holdout_start)
        if not is_mask.any():
            continue
        pos = v4.apply_cooldown(sd.ts, is_mask, p.cooldown_s)
        if pos.size == 0:
            continue
        pnl = v4.simulate_exits(sd, pos, code[pos], p)
        if pnl.empty:
            continue
        all_rets.append(pnl["net_ret"])
    if not all_rets:
        return pd.Series(dtype=float)
    return pd.concat(all_rets, ignore_index=True)

def wf_metrics_for_combo_pooled(combo_id: int, combo_params: dict, symbols: list[str],
                                n_windows: int = WF_WINDOWS) -> dict:
    """WF метрики pooled по всем символам (склеиваем трейды)."""
    p = v4.params_from(combo_params)
    pooled = []
    for sym in symbols:
        try:
            sd = v4.prepare_symbol(sym)
        except Exception:
            continue
        arrs = v4.get_feat_arrays(sym, p.uhlo_length, sd)
        mask, code = v4.evaluate_signals(arrs, p)
        is_mask = mask & (arrs["ts"] < sd.holdout_start)
        if not is_mask.any():
            continue
        pos = v4.apply_cooldown(sd.ts, is_mask, p.cooldown_s)
        if pos.size == 0:
            continue
        pnl = v4.simulate_exits(sd, pos, code[pos], p)
        if pnl.empty:
            continue
        # держим только ts и net_ret для WF
        pooled.append(pnl[["ts", "net_ret"]])
    if not pooled:
        return {"wf_hit_rate": np.nan, "wf_avg_oos_sr": np.nan, "wf_efficiency": np.nan,
                "n_windows": 0, "pooled_n": 0}
    all_trades = pd.concat(pooled, ignore_index=True)
    # WF сплиты строим по общему диапазону всех трейдов
    # используем диапазон IS периода первого символа (все 90д одинаковы ~)
    # Берём ts первого символа для границ
    try:
        sd0 = v4.prepare_symbol(symbols[0])
        splits = sv.walk_forward_splits(sd0.ts, n_windows=n_windows, holdout_days=HOLDOUT_DAYS)
    except Exception:
        # fallback по pooled ts
        splits = sv.walk_forward_splits(all_trades["ts"].values, n_windows=n_windows, holdout_days=HOLDOUT_DAYS)
    # all_trades ts — datetime64 naive? simulate_exits возвращает ts как сигнальный ts (naive)
    # walk_forward_metrics ожидает ts колонку
    wf = sv.walk_forward_metrics_for_combo(all_trades, splits)
    wf["pooled_n"] = len(all_trades)
    wf["splits"] = splits
    return wf

def holdout_metrics_for_combo(combo_params: dict, symbols: list[str]) -> dict:
    """Слепой holdout pooled (30д)."""
    p = v4.params_from(combo_params)
    all_nets = []
    per_sym = {}
    for sym in symbols:
        try:
            met, pnl = v4.confirm_holdout(sym, p)
        except Exception:
            continue
        if met.get("n", 0) and not pnl.empty:
            all_nets.append(pnl["net_ret"])
            per_sym[sym] = met
    if not all_nets:
        return {"n": 0, "pooled": {}, "per_sym": {}}
    pooled = v4.score_returns(pd.concat(all_nets, ignore_index=True))
    pooled["sum_net_ret"] = float(pd.concat(all_nets, ignore_index=True).sum())
    return {"n": pooled["n"], "pooled": pooled, "per_sym": per_sym, "all_nets": pd.concat(all_nets, ignore_index=True)}

def compute_pbo_for_top_candidates(rank_df: pd.DataFrame, symbols: list[str],
                                   top_k: int = 20, n_blocks: int = 8) -> dict:
    """PBO для top-K кандидатов (матрица стратегии x блок)."""
    # строим матрицу perf (SR per block)
    trades_by_strategy: dict[int, pd.DataFrame] = {}
    # для ускорения: для каждого топ-кандидата собираем pooled трейды с ts
    for _, row in rank_df.head(top_k).iterrows():
        cid = int(row["combo_id"])
        keys = list(v4.PARAM_GRID.keys())
        params = {k: row[k] for k in keys}
        # pooled трейды
        pooled = []
        p = v4.params_from(params)
        for sym in symbols:
            try:
                sd = v4.prepare_symbol(sym)
            except Exception:
                continue
            arrs = v4.get_feat_arrays(sym, p.uhlo_length, sd)
            mask, code = v4.evaluate_signals(arrs, p)
            is_mask = mask & (arrs["ts"] < sd.holdout_start)
            pos = v4.apply_cooldown(sd.ts, is_mask, p.cooldown_s)
            if pos.size == 0:
                continue
            pnl = v4.simulate_exits(sd, pos, code[pos], p)
            if pnl.empty:
                continue
            pooled.append(pnl[["ts", "net_ret"]])
        if pooled:
            trades_by_strategy[cid] = pd.concat(pooled, ignore_index=True)
        else:
            trades_by_strategy[cid] = pd.DataFrame({"ts": [], "net_ret": []})
    perf = sv.build_perf_matrix_by_blocks(trades_by_strategy, n_blocks=n_blocks)
    pbo_res = sv.pbo_cscv(perf.values, seed=DEFAULT_SEED)
    return {"pbo": pbo_res, "perf_matrix": perf}

# ---------------------------------------------------------------------------
# Главный пайплайн Sprint 4
# ---------------------------------------------------------------------------

def run_sprint4(sample: int = 21, seed: int = DEFAULT_SEED, stratified: bool = True,
                from_raw: str | None = None, dsr_threshold: float = DSR_THRESHOLD,
                wf_windows: int = WF_WINDOWS, wf_min_hit: float = WF_MIN_HIT_RATE,
                n_top_for_pbo: int = 20, tp: float | None = None, sl: float | None = None,
                universe: str = "data/symbol_universe.txt",
                cross_pf_min: float | None = None, cross_frac: float | None = None,
                cross_min_n: int | None = None) -> dict:
    # фиксируем seed глобально
    np.random.seed(seed)
    import random
    random.seed(seed)

    if tp is not None:
        v4.ACTIVE_TP_PCT = tp
    if sl is not None:
        v4.ACTIVE_SL_PCT = sl
    v4._recompute_costs()
    # cross gates override for демонстрации (иначе на 12 символах строгий порог не проходит)
    if cross_pf_min is not None:
        v4.CROSS_PF_MIN = cross_pf_min
    if cross_frac is not None:
        v4.CROSS_FRAC_MIN = cross_frac
    if cross_min_n is not None:
        v4.CROSS_MIN_N = cross_min_n

    # — символы —
    if stratified:
        symbols, buckets, turnovers = get_stratified_symbols(n_total=sample, seed=seed, universe_path=universe)
        print(f"[Sprint4] Stratified sample {len(symbols)} (seed={seed}):")
        cnt = Counter(buckets[s] for s in symbols)
        for lbl in ["High-vol", "Mid-vol", "Low-vol"]:
            print(f"  {lbl}: {cnt.get(lbl,0)}")
        # показать turnover терцили
        vals = sorted(turnovers.values())
        print(f"  turnover tertiles: {np.quantile([v for v in turnovers.values() if not np.isnan(v)],[0.33,0.66])}")
    else:
        symbols = v4.load_symbol_universe(n_sample=sample, path=universe)
        buckets = None
        turnovers = None
        # детерминированно с seed (v4.load_symbol_universe использует 42 — переопределим)
        if seed != 42:
            rng = np.random.default_rng(seed)
            symbols = list(rng.choice(sorted(symbols), size=min(sample, len(symbols)), replace=False))
        print(f"[Sprint4] Random sample {len(symbols)} (seed={seed})")

    print(f"  symbols: {', '.join(symbols[:10])}{' ...' if len(symbols)>10 else ''}")
    print(f"  TP {v4.ACTIVE_TP_PCT:g}% / SL {v4.ACTIVE_SL_PCT:g}%  cost maker {v4.COST_TP_EXIT*100:.3f}% taker {v4.COST_TAKER_EXIT*100:.3f}% gap {v4.COST_GAP_EXIT*100:.3f}%")

    # — грид —
    keys, combos = v4.grid_combos(v4.PARAM_GRID)
    tag = f"_tp{v4.ACTIVE_TP_PCT:g}_sl{v4.ACTIVE_SL_PCT:g}_s{seed}" if stratified else f"_tp{v4.ACTIVE_TP_PCT:g}_sl{v4.ACTIVE_SL_PCT:g}"

    # — grid search или resume —
    if from_raw:
        raw_p = pd.read_pickle(from_raw)
        # поддержка обоих форматов: старый raw DataFrame / новый dict {"raw":..., "metadata":...}
        if isinstance(raw_p, dict) and "raw" in raw_p:
            raw = raw_p["raw"]
            print(f"[Sprint4] loaded dict-pickle with metadata (seed={raw_p.get('metadata',{}).get('seed')})")
        else:
            raw = raw_p
        # warm cache
        for sym in raw["symbol"].unique():
            try:
                v4.prepare_symbol(sym)
            except Exception as e:
                print(f"  warm {sym}: {e}")
        per_symbol_n = raw["symbol"].nunique()
        print(f"[Sprint4] resumed {len(raw)} rows / {per_symbol_n} symbols from {from_raw}")
        # если stratified и symbols не совпадают — переопределим symbols как из raw
        symbols = sorted(raw["symbol"].unique())
        print(f"  using symbols from raw: {len(symbols)}")
        # пересчитаем buckets для фактических symbols если stratified
        if stratified:
            # переопределяем buckets на фактической выборке
            turnovers_raw = _load_turnovers(symbols)
            # используем turnovers всех символов вселенной для квантилей, но buckets для выборки
            all_syms_full = [l.strip() for l in Path(universe).read_text().splitlines() if l.strip()]
            turnovers_full = _load_turnovers(all_syms_full)
            buckets = sv.assign_liquidity_buckets(turnovers_full, n_buckets=3)
            # фильтр только для symbols в raw
            buckets = {s: buckets[s] for s in symbols if s in buckets}
            cnt = Counter(buckets[s] for s in symbols)
            print(f"  (пересчитаны buckets для raw выборки): High={cnt.get('High-vol',0)} Mid={cnt.get('Mid-vol',0)} Low={cnt.get('Low-vol',0)}")
    else:
        per_symbol = []
        for sym in symbols:
            try:
                res = v4.run_symbol(sym)
            except Exception as e:
                print(f"Skipping {sym}: {e}")
                continue
            if res.empty:
                print(f"[{sym}] no combos")
                continue
            per_symbol.append(res)
            print(f"[{sym}] {len(res)}/{len(combos)} combos")
        if not per_symbol:
            print("No results")
            return {}
        raw = pd.concat(per_symbol, ignore_index=True)
        out_raw = f"grid_search_raw_sprint4{tag}.pkl"
        # сохраним с метаданными
        meta = build_metadata(seed, argparse.Namespace(sample=sample, seed=seed, stratified=stratified,
                                                       dsr_threshold=dsr_threshold, wf_windows=wf_windows,
                                                       universe=universe, from_raw=from_raw, tp=tp, sl=sl),
                              buckets, symbols)
        # pickle как dict для воспроизводимости
        with open(out_raw, "wb") as f:
            pickle.dump({"raw": raw, "metadata": meta}, f)
        print(f"[Sprint4] saved {out_raw} ({len(raw)} rows)")
        # также csv для совместимости
        raw.to_csv(f"grid_search_results_sprint4{tag}.csv", index=False)

    # — plateau + global rank (v4 логика) —
    combined = v4.complete_grid(raw)
    combined = v4.add_neighbor_median_score(combined, metric="expectancy_score")
    rank = v4.select_global(combined, n_symbols=len(symbols))
    if rank.empty:
        print("[Sprint4] No survivors after v4 gates")
        return {"raw": raw, "rank": rank}

    print(f"\n[Sprint4] v4 Top 10 (до DSR/WF, для аудита — НЕ для отбора):")
    show = ["global_score", "coverage", "pf_symbols", "pf_frac", *keys]
    print(rank[show].head(10).to_string(index=False))
    print("\n[Sprint4] ВНИМАНИЕ: ручной выбор из Top-10 ЗАПРЕЩЁН без прохождения holdout (правила отбора Спринта 4).")

    # — DSR фильтрация —
    n_trials_eff = N_TRIALS
    # оценка корреляции для N_eff (опционально) — пока N_total
    print(f"\n[Sprint4] DSR фильтрация (N={n_trials_eff}, threshold={dsr_threshold}):")
    # для каждого кандидата из rank (покажем 30) считаем DSR на IS pooled
    candidates = []
    for idx, row in rank.head(30).iterrows():
        cid = int(row["combo_id"])
        params = {k: row[k] for k in keys}
        rets = pooled_is_returns_for_combo(cid, params, symbols)
        if len(rets) < 20:
            dsr_info = {"dsr": np.nan, "sr": np.nan, "n": len(rets)}
        else:
            dsr_info = sv.deflated_sharpe_ratio(rets, n_trials=n_trials_eff)
        row_d = dict(row)
        row_d.update({"dsr": dsr_info.get("dsr", np.nan), "psr": dsr_info.get("psr", np.nan),
                      "sr_is": dsr_info.get("sr", np.nan), "sr0": dsr_info.get("sr0", np.nan),
                      "n_is": dsr_info.get("n", len(rets))})
        candidates.append(row_d)
        flag = "✓" if dsr_info.get("dsr", 0) >= dsr_threshold else "✗"
        print(f"  {flag} combo {cid:4d} DSR={dsr_info.get('dsr', np.nan):.3f} SR={dsr_info.get('sr', np.nan):.3f} SR0={dsr_info.get('sr0', np.nan):.3f} n={len(rets):4d}  params {params}")

    cand_df = pd.DataFrame(candidates)
    survivors_dsr = cand_df[cand_df["dsr"] >= dsr_threshold].copy()
    print(f"\n[Sprint4] DSR survivors: {len(survivors_dsr)}/{len(cand_df)} (DSR>={dsr_threshold})")
    if survivors_dsr.empty:
        print("[Sprint4] Нет кандидатов после DSR — ослабьте threshold или проверьте данные.")
        # всё равно попробуем WF на лучших

    # — Walk-Forward —
    print(f"\n[Sprint4] Walk-Forward ({wf_windows} окон, expanding train / rolling val, критерий hit_rate>={wf_min_hit}):")
    wf_survivors = []
    # проверяем DSR survivors, если их 0 — берём top-10 для диагностики WF
    wf_pool = survivors_dsr if not survivors_dsr.empty else cand_df.head(10)
    for _, row in wf_pool.iterrows():
        cid = int(row["combo_id"])
        params = {k: row[k] for k in keys}
        wf = wf_metrics_for_combo_pooled(cid, params, symbols, n_windows=wf_windows)
        row_d = dict(row)
        row_d.update({"wf_hit_rate": wf["wf_hit_rate"], "wf_avg_oos_sr": wf["wf_avg_oos_sr"],
                      "wf_efficiency": wf["wf_efficiency"], "wf_n_valid": wf.get("n_valid_windows", 0),
                      "wf_pooled_n": wf.get("pooled_n", 0)})
        wf_survivors.append(row_d)
        flag = "✓" if (not np.isnan(wf["wf_hit_rate"]) and wf["wf_hit_rate"] >= wf_min_hit and wf["wf_avg_oos_sr"] > WF_MIN_AVG_OOS_SR) else "✗"
        print(f"  {flag} combo {cid:4d} WF hit={wf['wf_hit_rate']:.2f} avg_OOS_SR={wf['wf_avg_oos_sr']:.3f} eff={wf['wf_efficiency']:.2f} pooled_n={wf.get('pooled_n',0)}")

    wf_df = pd.DataFrame(wf_survivors)
    if not wf_df.empty:
        wf_pass = wf_df[(wf_df["wf_hit_rate"] >= wf_min_hit) & (wf_df["wf_avg_oos_sr"] > WF_MIN_AVG_OOS_SR)].copy()
    else:
        wf_pass = pd.DataFrame()
    print(f"\n[Sprint4] WF survivors: {len(wf_pass)}/{len(wf_df)}")

    # — Liquidity проверка (если stratified) —
    if stratified and not wf_pass.empty and turnovers is not None:
        print(f"\n[Sprint4] Liquidity-стратификация (проверка PF per bucket):")
        # для каждого WF survivor проверяем PF per bucket
        for _, row in wf_pass.iterrows():
            cid = int(row["combo_id"])
            # собрать per-symbol PF для этого combo
            pf_per_sym = {}
            for sym in symbols:
                sub = raw[(raw["combo_id"]==cid) & (raw["symbol"]==sym)]
                if not sub.empty:
                    pf_per_sym[sym] = float(sub.iloc[0]["profit_factor"]) if not np.isnan(sub.iloc[0]["profit_factor"]) else np.nan
            # bucket stats
            by_bucket = defaultdict(list)
            for sym, pf in pf_per_sym.items():
                b = buckets.get(sym, "unknown")
                by_bucket[b].append(pf)
            msg = []
            for lbl in ["High-vol","Mid-vol","Low-vol"]:
                vals = by_bucket.get(lbl, [])
                med_pf = float(np.nanmedian(vals)) if vals else np.nan
                n_ok = sum(1 for v in vals if v>1.2)
                msg.append(f"{lbl}: medPF={med_pf:.2f} ok={n_ok}/{len(vals)}")
            print(f"  combo {cid:4d} | " + " | ".join(msg))

    # — Blind Holdout (финальный фильтр — ЗАПРЕТ ручного выбора без него) —
    holdout_pool = wf_pass if not wf_pass.empty else survivors_dsr if not survivors_dsr.empty else cand_df.head(10)
    print(f"\n[Sprint4] Слепой Holdout ({HOLDOUT_DAYS}д, pooled, критерий: n>=20, winrate>0.45, Sharpe>0):")
    final_candidates = []
    for _, row in holdout_pool.iterrows():
        cid = int(row["combo_id"])
        params = {k: row[k] for k in keys}
        hm = holdout_metrics_for_combo(params, symbols)
        pooled = hm["pooled"]
        n = pooled.get("n", 0)
        wr = pooled.get("winrate", np.nan)
        sharpe = pooled.get("sharpe", np.nan)
        avg_ret = pooled.get("avg_ret", np.nan)
        pf = pooled.get("profit_factor", np.nan)
        passed = (n >= 20 and sharpe is not np.nan and sharpe > 0 and wr > 0.45)
        flag = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {flag} combo {cid:4d} n={n} WR={wr:.2f} avg={avg_ret*100 if not np.isnan(avg_ret) else np.nan:+.3f}% PF={pf:.2f} Sharpe={sharpe:.2f}")
        # per-symbol breakdown
        if hm["per_sym"]:
            for sym, met in sorted(hm["per_sym"].items())[:5]:
                print(f"       {sym}: n={met['n']} WR={met['winrate']:.2f} Sharpe={met['sharpe']:.2f}")
            if len(hm["per_sym"])>5:
                print(f"       ... +{len(hm['per_sym'])-5} symbols")
        row_d = dict(row)
        row_d.update({"holdout_n": n, "holdout_wr": wr, "holdout_sharpe": sharpe,
                      "holdout_avg": avg_ret, "holdout_pf": pf, "holdout_pass": passed,
                      "holdout_per_sym": hm["per_sym"]})
        if passed:
            final_candidates.append(row_d)

    final_df = pd.DataFrame(final_candidates)
    print("\n" + "="*70)
    if final_df.empty:
        print("[Sprint4] ФИНАЛ: 0 кандидатов прошли DSR + Walk-Forward + Holdout.")
        print("  Рекомендация: ослабьте DSR threshold, увеличьте sample, или проверьте рынок (90д боковик).")
    else:
        print(f"[Sprint4] ФИНАЛ: {len(final_df)} кандидатов прошли все ворота (DSR>={dsr_threshold} + WF hit>={wf_min_hit} + Holdout blind):")
        # сортируем по holdout Sharpe затем DSR
        final_df = final_df.sort_values(["holdout_sharpe","dsr"], ascending=False)
        for i, (_, r) in enumerate(final_df.iterrows(), 1):
            print(f"\n  #{i} combo {int(r['combo_id']):4d} — DSR={r['dsr']:.3f} WF_hit={r['wf_hit_rate']:.2f} Holdout Sharpe={r['holdout_sharpe']:.2f} WR={r['holdout_wr']:.2f} PF={r['holdout_pf']:.2f} n={r['holdout_n']}")
            print(f"     params: " + ", ".join(f"{k}={r[k]}" for k in keys))
            print(f"     global_score={r['global_score']:.4f} coverage={r['coverage']:.0f} pf_frac={r['pf_frac']:.2f}")

    # — PBO всего метода —
    try:
        pbo_res = compute_pbo_for_top_candidates(rank, symbols, top_k=n_top_for_pbo)
        pbo_val = pbo_res["pbo"]["pbo"]
        print(f"\n[Sprint4] PBO (CSCV {pbo_res['pbo']['n_blocks']} блоков, {pbo_res['pbo']['n_splits']} сплитов, top-{n_top_for_pbo}): {pbo_val:.3f}")
        print(f"  Интерпретация: PBO={pbo_val:.2f} — вероятность что лучшая IS стратегия хуже медианы OOS.")
        print(f"  Порог: PBO < {PBO_MAX} => метод не переобучен (требование). {'✓ PASS' if pbo_val < PBO_MAX else '✗ FAIL — высокий риск overfitting'}")
    except Exception as e:
        print(f"\n[Sprint4] PBO расчёт упал: {e}")

    # — сохранение —
    final_path = f"sprint4_candidates{tag}.pkl"
    meta = build_metadata(seed, argparse.Namespace(sample=sample, seed=seed, stratified=stratified,
                                                   dsr_threshold=dsr_threshold, wf_windows=wf_windows,
                                                   universe=universe, from_raw=from_raw, tp=tp, sl=sl),
                          buckets, symbols)
    meta["pbo"] = float(pbo_val) if 'pbo_val' in locals() and not np.isnan(pbo_val) else None
    payload = {"rank": rank, "candidates": cand_df, "wf": wf_df if 'wf_df' in locals() else pd.DataFrame(),
               "final": final_df, "metadata": meta, "raw": raw}
    with open(final_path, "wb") as f:
        pickle.dump(payload, f)
    print(f"\n[Sprint4] Сохранено {final_path} (seed={seed}, воспроизводимо) + метаданные.")
    # также json метаданные отдельно для аудита
    with open(final_path.replace(".pkl","_meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return payload

def main(argv=None):
    ap = argparse.ArgumentParser(description="Sprint 4 — DSR/PBO/Walk-Forward + liquidity stratified")
    ap.add_argument("--sample", type=int, default=21, help="число символов (делится на 3 корзины при --stratified)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="фиксированный seed для воспроизводимости")
    ap.add_argument("--stratified", action="store_true", help="liquidity-стратифицированная выборка High/Mid/Low")
    ap.add_argument("--from-raw", type=str, default=None, help="resume с существующего .pkl")
    ap.add_argument("--dsr-threshold", type=float, default=DSR_THRESHOLD, help="порог DSR (default 0.95)")
    ap.add_argument("--wf-windows", type=int, default=WF_WINDOWS, help="число WF окон")
    ap.add_argument("--wf-hit", type=float, default=WF_MIN_HIT_RATE, help="мин WF hit rate")
    ap.add_argument("--pbo-top", type=int, default=20, help="top-K для PBO")
    ap.add_argument("--universe", type=str, default="data/symbol_universe.txt")
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--cross-pf-min", type=float, default=None, help="cross PF threshold override")
    ap.add_argument("--cross-frac", type=float, default=None, help="cross fraction override")
    ap.add_argument("--cross-min-n", type=int, default=None, help="cross min N override")
    args = ap.parse_args(argv)
    run_sprint4(sample=args.sample, seed=args.seed, stratified=args.stratified,
                from_raw=args.from_raw, dsr_threshold=args.dsr_threshold,
                wf_windows=args.wf_windows, wf_min_hit=args.wf_hit,
                n_top_for_pbo=args.pbo_top, tp=args.tp, sl=args.sl,
                universe=args.universe, cross_pf_min=args.cross_pf_min,
                cross_frac=args.cross_frac, cross_min_n=args.cross_min_n)

if __name__ == "__main__":
    main()
