"""
statistical_validation.py
=========================
Спринт 4: статистическая защита от overfitting.

Реализует:
 - DSR (Deflated Sharpe Ratio) — Bailey & López de Prado 2014
 - PSR (Probabilistic Sharpe Ratio)
 - PBO (Probability of Backtest Overfitting) через CSCV
 - Walk-Forward сплиты и метрики
 - Liquidity-stratified сэмплинг

Без зависимости от scipy — чистый numpy/pandas + math.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Normal CDF / PPF без scipy
# ---------------------------------------------------------------------------

def _norm_cdf(x: float | np.ndarray) -> float | np.ndarray:
    """Φ(x) — через erf."""
    if isinstance(x, np.ndarray):
        return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_ppf(p: float) -> float:
    """Обратная Φ — аппроксимация Acklam (высокая точность).

    https://web.archive.org/web/20150910085709/http://home.online.no/~pjacklam/notes/invnorm/
    Ошибка < 1.15e-9.
    """
    if not 0 < p < 1:
        raise ValueError(f"p must be in (0,1), got {p}")
    # коэффициенты Acklam
    a1 = -3.969683028665376e1
    a2 = 2.209460984245205e2
    a3 = -2.759285104469687e2
    a4 = 1.383577518672690e2
    a5 = -3.066479806614716e1
    a6 = 2.506628277459239

    b1 = -5.447609879822406e1
    b2 = 1.615858368580409e2
    b3 = -1.556989798598866e2
    b4 = 6.680131188771972e1
    b5 = -1.328068155288572e1

    c1 = -7.784894002430293e-3
    c2 = -3.223964580411365e-1
    c3 = -2.400758277161838
    c4 = -2.549732539343734
    c5 = 4.374664141464968
    c6 = 2.938163982698783

    d1 = 7.784695709041462e-3
    d2 = 3.224671290700398e-1
    d3 = 2.445134137142996
    d4 = 3.754408661907416

    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6) / \
               ((((d1 * q + d2) * q + d3) * q + d4) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a1 * r + a2) * r + a3) * r + a4) * r + a5) * r + a6) * q / \
               (((((b1 * r + b2) * r + b3) * r + b4) * r + b5) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6) / \
              ((((d1 * q + d2) * q + d3) * q + d4) * q + 1)

# ---------------------------------------------------------------------------
# PSR / DSR
# ---------------------------------------------------------------------------

def _sharpe_stats(returns: pd.Series | np.ndarray) -> dict:
    r = pd.Series(returns).dropna().astype(float)
    n = len(r)
    if n < 20:
        return {"n": n, "mean": np.nan, "std": np.nan, "sr": np.nan,
                "skew": np.nan, "kurt": np.nan}
    mean = float(r.mean())
    std = float(r.std(ddof=1))
    sr = mean / std if std > 0 else np.nan
    # pandas skew/kurt — bias-corrected; для DSR это ок, используем их
    skew = float(r.skew())
    kurt_excess = float(r.kurtosis())  # excess (Fisher)
    kurt = kurt_excess + 3.0
    # NaN guard
    if np.isnan(skew):
        skew = 0.0
    if np.isnan(kurt) or kurt < 1:
        kurt = 3.0
    return {"n": n, "mean": mean, "std": std, "sr": sr, "skew": skew, "kurt": kurt}

def probabilistic_sharpe_ratio(returns: pd.Series | np.ndarray,
                              sr_threshold: float = 0.0) -> float:
    """PSR — вероятность что истинный SR > threshold.

    Bailey & López de Prado (2012): PSR = Φ( (SR̂ - SR*) / σ_SR )
    где σ_SR = sqrt( (1 - γ3 SR̂ + (γ4-1)/4 SR̂²) / (T-1) )  — Lo 2002.
    """
    st = _sharpe_stats(returns)
    n, sr, skew, kurt = st["n"], st["sr"], st["skew"], st["kurt"]
    if n < 20 or np.isnan(sr):
        return np.nan
    var = (1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2) / (n - 1)
    if var <= 0:
        return np.nan
    se = math.sqrt(var)
    z = (sr - sr_threshold) / se
    return float(_norm_cdf(z))

def deflated_sharpe_ratio(returns: pd.Series | np.ndarray,
                          n_trials: int,
                          sr_threshold: float = 0.0) -> dict:
    """DSR с поправкой на множественность тестов.

    Returns dict с полями:
      dsr, psr, sr, sr0, se, n, n_trials, expected_max_sr
    DSR = PSR(SR0), где SR0 = E[max SR под H0].

    Формула SR0 (Bailey & López de Prado 2014, eq. 7):
      SR0 = sqrt(V) * [ (1-γ) Φ⁻¹(1-1/N) + γ Φ⁻¹(1-1/(N e)) ]
      V = Var[SR̂] при SR=0? В DSR есть нюанс: V зависит от SR̂.
      Используем Var как в PSR (с наблюдёнными skew/kurt/SR).
      γ = Euler-Mascheroni ~0.5772.

    n_trials — число независимых попыток (N). Для коррелированных
    стратегий N_eff < N; рекомендуем передавать уже скорректированное.
    """
    st = _sharpe_stats(returns)
    n, sr, skew, kurt = st["n"], st["sr"], st["skew"], st["kurt"]
    if n < 20 or np.isnan(sr) or n_trials < 1:
        return {"dsr": np.nan, "psr": np.nan, "sr": sr, "sr0": np.nan,
                "se": np.nan, "n": n, "n_trials": n_trials,
                "expected_max_sr": np.nan, "skew": skew, "kurt": kurt}
    var = (1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2) / (n - 1)
    # альтернатива классической: (1 +0.5 sr² - skew sr + (kurt-3)/4 sr²)/(n-1)
    # обе дают близкое; оставляем Bailey PSR версию (строже для больших SR)
    if var <= 0:
        return {"dsr": np.nan, "psr": np.nan, "sr": sr, "sr0": np.nan,
                "se": np.nan, "n": n, "n_trials": n_trials,
                "expected_max_sr": np.nan, "skew": skew, "kurt": kurt}
    se = math.sqrt(var)
    psr = float(_norm_cdf((sr - sr_threshold) / se)) if se > 0 else np.nan

    if n_trials == 1:
        sr0 = sr_threshold
    else:
        gamma_e = 0.5772156649015329
        try:
            q1 = _norm_ppf(1 - 1.0 / n_trials)
            q2 = _norm_ppf(1 - 1.0 / (n_trials * math.e))
        except ValueError:
            q1 = q2 = 0.0
        em = (1 - gamma_e) * q1 + gamma_e * q2
        sr0 = se * em  # ожидаемый максимум под H0
        # если задан внешний threshold (например 0), берём max
        if sr_threshold != 0:
            sr0 = max(sr0, sr_threshold)

    dsr = float(_norm_cdf((sr - sr0) / se)) if se > 0 else np.nan
    return {"dsr": dsr, "psr": psr, "sr": sr, "sr0": float(sr0),
            "se": float(se), "n": n, "n_trials": int(n_trials),
            "expected_max_sr": float(sr0), "skew": skew, "kurt": kurt}

def estimate_n_trials_independent(n_total: int, avg_corr: float | None = None) -> int:
    """Оценка эффективного числа независимых испытаний.

    Если стратегии коррелированы, N_eff < N_total.
    Простая модель: N_eff = N_total * (1 - avg_corr)  — грубая эвристика,
    но лучше чем игнорировать корреляцию.
    Если avg_corr не задан, возвращаем N_total (консервативно — DSR строже).
    """
    if avg_corr is None or np.isnan(avg_corr):
        return int(n_total)
    # clamp
    avg_corr = float(np.clip(avg_corr, 0, 0.95))
    n_eff = max(1, int(round(n_total * (1 - avg_corr))))
    return n_eff

# ---------------------------------------------------------------------------
# PBO — CSCV
# ---------------------------------------------------------------------------

def pbo_cscv(perf_matrix: np.ndarray | pd.DataFrame,
             n_samples: int | None = None,
             seed: int = 42) -> dict:
    """PBO через Combinatorial Symmetric Cross-Validation.

    perf_matrix: shape (n_strategies, n_blocks) — метрика (SR / avg_ret) стратегии
                 в каждом блоке. Либо DataFrame с теми же осями.
    Каждая колонка — временной блок (S блоков). Разбиваем блоки пополам на
    IS (in-sample) и OOS. Для каждого разбиения:
      - находим стратегию с max IS mean
      - её OOS mean ранжируем среди всех OOS means
      - считаем logit ранга; degraded если ранг < 0.5

    PBO = доля разбиений, где лучшая IS стратегия — ниже медианы OOS.

    n_samples: если число комбинаций C(S, S/2) велико, сэмплируем Monte-Carlo.
               None — полный перебор до разумного лимита (S<=12 полный).
    """
    if isinstance(perf_matrix, pd.DataFrame):
        mat = perf_matrix.values.astype(float)
    else:
        mat = np.asarray(perf_matrix, dtype=float)
    n_strat, s = mat.shape
    if s < 2 or n_strat < 2:
        return {"pbo": np.nan, "n_splits": 0, "n_blocks": s, "n_strategies": n_strat,
                "logits": np.array([]), "degraded": np.array([])}

    half = s // 2
    all_combos = list(itertools.combinations(range(s), half))
    total = len(all_combos) // 2  # симметрия: каждая пара (IS, OOS) дублируется
    # deduplicate симметрии: берём только половину
    # itertools даёт все сочетания; пары A и complement дают дубль — оставим уникальные по frozenset
    uniq = []
    seen = set()
    for c in all_combos:
        key = frozenset(c)
        comp = frozenset(set(range(s)) - set(c))
        if key in seen or comp in seen:
            continue
        seen.add(key)
        uniq.append(c)
    combos = uniq

    if n_samples is not None and len(combos) > n_samples:
        rng = random.Random(seed)
        combos = rng.sample(combos, n_samples)

    logits = []
    degraded = []
    oos_ranks = []
    for is_blocks in combos:
        is_blocks = list(is_blocks)
        oos_blocks = [b for b in range(s) if b not in is_blocks]
        is_mean = np.nanmean(mat[:, is_blocks], axis=1)
        oos_mean = np.nanmean(mat[:, oos_blocks], axis=1)
        if np.all(np.isnan(is_mean)) or np.all(np.isnan(oos_mean)):
            continue
        # заменить NaN на -inf чтобы не выбрать NaN как лучшего
        is_mean_f = np.where(np.isnan(is_mean), -np.inf, is_mean)
        best = int(np.argmax(is_mean_f))
        best_oos = oos_mean[best]
        if np.isnan(best_oos):
            continue
        # ранг среди OOS: доля стратегий с OOS < best_oos
        valid = ~np.isnan(oos_mean)
        if valid.sum() == 0:
            continue
        rank = (oos_mean[valid] < best_oos).mean()  # 0..1, 0.5 = медиана
        # обработка ties: если есть равные, rank немного сдвинут, но ок
        # logit для диагностики
        eps = 1e-9
        rank_clipped = np.clip(rank, eps, 1 - eps)
        logit = float(math.log(rank_clipped / (1 - rank_clipped)))
        logits.append(logit)
        oos_ranks.append(float(rank))
        degraded.append(1 if rank < 0.5 else 0)

    if not degraded:
        return {"pbo": np.nan, "n_splits": 0, "n_blocks": s, "n_strategies": n_strat,
                "logits": np.array([]), "degraded": np.array([])}
    pbo = float(np.mean(degraded))
    return {"pbo": pbo, "n_splits": len(degraded), "n_blocks": s,
            "n_strategies": n_strat,
            "logits": np.array(logits), "ranks": np.array(oos_ranks),
            "degraded": np.array(degraded)}

def build_perf_matrix_by_blocks(trades_by_strategy: dict[int, pd.DataFrame],
                                ts_col: str = "ts",
                                ret_col: str = "net_ret",
                                n_blocks: int = 8) -> pd.DataFrame:
    """Построить матрицу (стратегия x блок) из трейдов.

    trades_by_strategy: {combo_id: DataFrame с колонками ts, net_ret}
    Блоки — равные по времени, по ts диапазона всего периода.
    Метрика блока = Sharpe блока (или avg_ret если n < 20).
    """
    if not trades_by_strategy:
        return pd.DataFrame()
    # общий временной диапазон
    all_ts = pd.concat([df[ts_col] for df in trades_by_strategy.values() if not df.empty], ignore_index=True)
    if all_ts.empty:
        return pd.DataFrame()
    t_min, t_max = all_ts.min(), all_ts.max()
    # границы блоков
    bounds = pd.date_range(start=t_min, end=t_max, periods=n_blocks + 1)
    # для numpy datetime64 тоже работает через pd
    matrix = {}
    for cid, df in trades_by_strategy.items():
        row = []
        for b in range(n_blocks):
            lo, hi = bounds[b], bounds[b + 1]
            mask = (df[ts_col] >= lo) & (df[ts_col] < hi) if b < n_blocks - 1 else (df[ts_col] >= lo) & (df[ts_col] <= hi)
            sub = df.loc[mask, ret_col]
            if len(sub) < 5:
                row.append(np.nan)
            else:
                # Sharpe блока
                m, s = sub.mean(), sub.std(ddof=1)
                sr = m / s if s and s > 0 else np.nan
                row.append(sr)
        matrix[cid] = row
    perf = pd.DataFrame.from_dict(matrix, orient="index",
                                  columns=[f"block_{i}" for i in range(n_blocks)])
    return perf

# ---------------------------------------------------------------------------
# Walk-Forward
# ---------------------------------------------------------------------------

@dataclass
class WFSplit:
    train_start: np.datetime64
    train_end: np.datetime64
    val_start: np.datetime64
    val_end: np.datetime64

def walk_forward_splits(ts: np.ndarray | pd.Series,
                        n_windows: int = 4,
                        holdout_days: int = 30) -> list[WFSplit]:
    """Анкорные WF сплиты: расширяющийся train, скользящий val.

    Весь период [t0, t_max]; последний holdout_days — слепой holdout (не участвует).
    Оставшееся [t0, holdout_start) делим на n_windows равных окон.
    Для шага k (0..n_windows-2):
      train = [t0, window_k_end)  — expanding
      val   = [window_k_end, window_{k+1}_end)
    """
    if isinstance(ts, pd.Series):
        arr = ts.values
    else:
        arr = np.asarray(ts)
    # нормализуем к datetime64[ns] naive
    try:
        arr = arr.astype("datetime64[ns]")
    except Exception:
        pass
    t0 = np.min(arr)
    t_max = np.max(arr)
    holdout_start = t_max - np.timedelta64(holdout_days, "D")
    if holdout_start <= t0:
        # данных меньше holdout — делим всё
        holdout_start = t_max
    usable_end = holdout_start
    total_ns = (usable_end - t0).astype("timedelta64[ns]").astype(np.int64)
    if total_ns <= 0 or n_windows < 2:
        return []
    window_ns = total_ns // n_windows
    splits: list[WFSplit] = []
    for k in range(n_windows - 1):
        train_end = t0 + np.timedelta64((k + 1) * window_ns, "ns")
        val_end = train_end + np.timedelta64(window_ns, "ns")
        if val_end > usable_end:
            val_end = usable_end
        train_start = t0
        val_start = train_end
        splits.append(WFSplit(train_start=train_start, train_end=train_end,
                              val_start=val_start, val_end=val_end))
    return splits

def walk_forward_metrics_for_combo(trades: pd.DataFrame,
                                   splits: list[WFSplit],
                                   ts_col: str = "ts",
                                   ret_col: str = "net_ret") -> dict:
    """WF-метрики одной стратегии по её трейдам.

    Возвращает:
      wf_hit_rate, wf_avg_oos_sr, wf_efficiency, n_windows, per_window_sr, ...
    """
    if trades.empty or not splits:
        return {"wf_hit_rate": np.nan, "wf_avg_oos_sr": np.nan,
                "wf_efficiency": np.nan, "n_windows": 0,
                "per_window_sr": [], "per_window_n": []}
    per_sr = []
    per_n = []
    is_sr_list = []
    oos_sr_list = []
    for sp in splits:
        # IS = train, OOS = val — для диагностики считаем SR train vs val
        is_mask = (trades[ts_col] >= sp.train_start) & (trades[ts_col] < sp.train_end)
        oos_mask = (trades[ts_col] >= sp.val_start) & (trades[ts_col] < sp.val_end)
        is_rets = trades.loc[is_mask, ret_col]
        oos_rets = trades.loc[oos_mask, ret_col]
        # Sharpe per window (OOS)
        if len(oos_rets) >= 5:
            sr_oos = oos_rets.mean() / oos_rets.std(ddof=1) if oos_rets.std(ddof=1) > 0 else np.nan
        else:
            sr_oos = np.nan
        if len(is_rets) >= 5:
            sr_is = is_rets.mean() / is_rets.std(ddof=1) if is_rets.std(ddof=1) > 0 else np.nan
        else:
            sr_is = np.nan
        per_sr.append(sr_oos)
        per_n.append(len(oos_rets))
        is_sr_list.append(sr_is)
        oos_sr_list.append(sr_oos)

    arr = np.array(per_sr, dtype=float)
    valid = ~np.isnan(arr)
    n_valid = int(valid.sum())
    if n_valid == 0:
        return {"wf_hit_rate": np.nan, "wf_avg_oos_sr": np.nan,
                "wf_efficiency": np.nan, "n_windows": len(splits),
                "per_window_sr": per_sr, "per_window_n": per_n,
                "is_sr": is_sr_list, "oos_sr": oos_sr_list}
    hit_rate = float(np.mean(arr[valid] > 0)) if n_valid else np.nan
    avg_oos = float(np.nanmean(arr))
    # efficiency = mean(OOS / IS) где IS>0
    eff_vals = []
    for a, b in zip(is_sr_list, oos_sr_list):
        if not np.isnan(a) and not np.isnan(b) and a > 0:
            eff_vals.append(b / a)
    eff = float(np.nanmean(eff_vals)) if eff_vals else np.nan
    return {"wf_hit_rate": hit_rate, "wf_avg_oos_sr": avg_oos,
            "wf_efficiency": eff, "n_windows": len(splits),
            "n_valid_windows": n_valid,
            "per_window_sr": per_sr, "per_window_n": per_n,
            "is_sr": is_sr_list, "oos_sr": oos_sr_list}

# ---------------------------------------------------------------------------
# Liquidity stratification
# ---------------------------------------------------------------------------

def compute_turnover(symbol: str, data_dir: Path | str = "./data") -> float:
    """Среднесуточный оборот (volume * close) по 1m данным."""
    data_dir = Path(data_dir)
    path = data_dir / f"{symbol}_1m.parquet"
    if not path.exists():
        return np.nan
    df = pd.read_parquet(path, columns=["volume", "close", "ts"])
    if df.empty:
        return np.nan
    # дневной оборот
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["date"] = df["ts"].dt.date
    daily = df.groupby("date").apply(lambda g: (g["volume"] * g["close"]).sum(), include_groups=False)
    return float(daily.mean()) if len(daily) else float((df["volume"] * df["close"]).sum())

def assign_liquidity_buckets(turnovers: dict[str, float],
                             n_buckets: int = 3,
                             labels: Sequence[str] | None = None) -> dict[str, str]:
    """Разделить символы на корзины по обороту.

    По умолчанию 3 корзины: Low / Mid / High (терцили).
    """
    if labels is None:
        if n_buckets == 3:
            labels = ["Low-vol", "Mid-vol", "High-vol"]
        else:
            labels = [f"bucket_{i}" for i in range(n_buckets)]
    vals = np.array([v for v in turnovers.values() if not np.isnan(v)], dtype=float)
    if len(vals) == 0:
        return {k: labels[0] for k in turnovers}
    # квантильные границы
    qs = np.linspace(0, 1, n_buckets + 1)
    bounds = np.quantile(vals, qs)
    out: dict[str, str] = {}
    for sym, tv in turnovers.items():
        if np.isnan(tv):
            out[sym] = labels[0]
            continue
        # найти корзину
        idx = np.searchsorted(bounds[1:-1], tv, side="right") if n_buckets > 1 else 0
        # searchsorted на bounds[1:-1] даёт 0..n_buckets-1
        # пример: bounds = [0, 33%, 66%, 100%], bounds[1:-1]=[33%,66%]
        # tv <33 -> idx0, 33<=tv<66 -> idx1, >=66 -> idx2
        idx = int(np.clip(idx, 0, n_buckets - 1))
        out[sym] = labels[idx]
    return out

def stratified_sample(symbols: list[str],
                      buckets: dict[str, str],
                      n_per_bucket: int | dict[str, int] | None = None,
                      total: int | None = None,
                      seed: int = 42) -> list[str]:
    """Стратифицированная выборка: равное покрытие корзин.

    buckets: {symbol: bucket_label}
    n_per_bucket: int или {label: n}
    total: если задан, делит поровну по корзинам (остаток в High-vol).
    seed — фиксируем для воспроизводимости.
    """
    rng = np.random.default_rng(seed)
    # группируем
    by_bucket: dict[str, list[str]] = {}
    for s in symbols:
        b = buckets.get(s, "unknown")
        by_bucket.setdefault(b, []).append(s)

    if total is not None:
        # равномерное деление
        labels = sorted(by_bucket.keys())
        k = len(labels)
        base = total // k
        rem = total % k
        n_per_bucket = {lbl: base for lbl in labels}
        # остаток отдаём корзине с большим оборотом (High-vol если есть)
        if rem:
            pref = "High-vol" if "High-vol" in n_per_bucket else labels[-1]
            n_per_bucket[pref] += rem
    if isinstance(n_per_bucket, int):
        n_per_bucket = {lbl: n_per_bucket for lbl in by_bucket}

    out: list[str] = []
    for lbl, syms in by_bucket.items():
        n_need = (n_per_bucket.get(lbl, len(syms)) if isinstance(n_per_bucket, dict) else len(syms))
        n_need = min(n_need, len(syms))
        if n_need <= 0:
            continue
        chosen = rng.choice(syms, size=n_need, replace=False)
        out.extend(list(chosen))
    # перемешать финальный список детерминированно
    rng.shuffle(out)
    return out
