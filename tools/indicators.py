"""
tools/indicators.py — индикаторы для ботов.
ACHOP: Adaptive Choppiness Index с ALMA-сглаживанием.
Минутные фильтры F1/F4/F6/F7 (Alpha Zoo → 1m адаптация, IC-проверка
на 30 монетах x 14 дней, форвард 15 мин; композит IC +0.028):
  F6 vwap_revert    -(close-vwap20)/std20   — перегрев vs VWAP
  F4 highvol_corr    -corr(high,volume,10)  — выдохшийся памп
  F1 vol_reversal    -sign(dVol1)*dClose1    — объёмное подтверждение
  F7 body_range      (close-open)/(high-low)— убеждённость свечи
Формат свечей везде: [ts_ms, open, high, low, close, volume].
"""
from __future__ import annotations

import math
from typing import List, Any


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_natr(candles: List[List[Any]], period: int = 14) -> float | None:
    """NATR для адаптации length (копия из live_screener_midcap)."""
    if len(candles) < period + 1:
        return None
    highs = [float(k[2]) for k in candles]
    lows = [float(k[3]) for k in candles]
    closes = [float(k[4]) for k in candles]
    trs = []
    for i in range(1, len(candles)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    last_close = closes[-1]
    if not last_close:
        return None
    return (atr / last_close) * 100


def _alma(series: List[float], window: int = 5, offset: float = 0.85, sigma: float = 6.0) -> float:
    """ALMA (Arnaud Legoux Moving Average)."""
    if not series:
        return 0.0
    n = min(window, len(series))
    # берём последние n значений
    vals = series[-n:]
    m = offset * (n - 1)
    s = n / sigma
    weights = []
    for i in range(n):
        w = math.exp(-((i - m) ** 2) / (2 * s * s))
        weights.append(w)
    w_sum = sum(weights)
    if w_sum == 0:
        return vals[-1]
    # weights соответствуют индексу 0..n-1, 0 — самый старый
    alma = sum(v * w for v, w in zip(vals, weights)) / w_sum
    return alma


def compute_achop(candles: List[List[Any]], cycle_part: float = 0.15) -> float | None:
    """
    Adaptive Choppiness Index с ALMA.
    - length адаптивный через NATR: length = clamp(int(14 * NATR/0.9), 10, 30) * (0.75 + cycle_part) -> фактически 0.9 при 0.15
    - CHOP = 100 * log10(sum ATR(1) / (Highest-Lowest)) / log10(length)
    - ALMA window=5 offset=0.85 sigma=6 по серии CHOP
    Возвращает ACHOP в диапазоне ~0-100, или None если данных мало.
    """
    if len(candles) < 15:
        return None
    # 1. адаптивный length через NATR
    natr = compute_natr(candles, period=14)
    if natr is None or natr <= 0:
        natr = 0.9
    # cycle_part влияет слабо (0.75 + cycle_part) = 0.9 при 0.15
    factor = 0.75 + float(cycle_part)
    base_len = int(14 * (natr / 0.9) * factor)
    # альтернатива если NATR <0.9 — не уменьшать ниже 10
    length = max(10, min(30, base_len))
    if len(candles) < length + 1:
        return None

    # 2. серия CHOP для последних 5 окон для ALMA
    chops: List[float] = []
    # считаем CHOP для 5 сдвинутых окон
    for shift in range(5):
        if len(candles) < length + 1 + shift:
            break
        # окно: последние length свечей со сдвигом shift
        if shift == 0:
            window = candles[-length:]
            prev_window = candles[-(length+1):-1] if len(candles) >= length+1 else []
        else:
            window = candles[-(length+shift):-shift]
            prev_window = candles[-(length+1+shift):-(1+shift)]
        # ATR(1) сумма за length
        # собираем highs/lows/closes для TR
        highs = [float(k[2]) for k in window]
        lows = [float(k[3]) for k in window]
        closes = [float(k[4]) for k in window]
        # для TR нужен prev_close — берём из предыдущего окна
        tr_sum = 0.0
        # first TR in window needs prev close from before window
        all_candles = prev_window + window if prev_window else window
        # если prev_window пусто, используем первый close окна как prev
        for i in range(len(window)):
            idx = len(all_candles) - len(window) + i
            # all_candles[idx] is current window[i]
            h = float(all_candles[idx][2])
            l = float(all_candles[idx][3])
            if idx == 0:
                pc = float(all_candles[idx][4])
            else:
                pc = float(all_candles[idx-1][4])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_sum += tr
        highest = max(highs) if highs else 0
        lowest = min(lows) if lows else 0
        price_range = highest - lowest
        if price_range <= 1e-12 or tr_sum <= 1e-12 or length <= 1:
            chop = 50.0  # нейтрально при вырожденном диапазоне
        else:
            try:
                chop = 100.0 * math.log10(tr_sum / price_range) / math.log10(length)
                chop = max(0.0, min(100.0, chop))
            except (ValueError, ZeroDivisionError):
                chop = 50.0
        chops.append(chop)
    if not chops:
        return None
    # chops собраны от нового к старому? Мы append в порядке shift 0..4 (0 — самый свежий)
    # Для ALMA нужен chronological oldest -> newest, реверсим
    chops = list(reversed(chops))
    if len(chops) == 1:
        return chops[0]
    # ALMA сглаживание
    achop = _alma(chops, window=5, offset=0.85, sigma=6.0)
    return max(0.0, min(100.0, achop))


# ---------------------------------------------------------------------------
# Минутные фильтры F1/F4/F6/F7 (Zoo: alpha101 #12/#44/#5/#101, gtja191_002)
# ---------------------------------------------------------------------------

def compute_vwap_dev(candles: List[List[Any]], length: int = 20) -> float | None:
    """F6: отклонение close от VWAP в единицах std(length).

    +2.0 = цена на 2 сигмы выше VWAP (перегрев для LONG).
    None если свечей < length или объём нулевой.
    """
    if len(candles) < length:
        return None
    win = candles[-length:]
    try:
        tp = [(float(k[2]) + float(k[3]) + float(k[4])) / 3.0 for k in win]
        vol = [float(k[5]) for k in win]
        closes = [float(k[4]) for k in win]
    except (ValueError, TypeError, IndexError):
        return None
    vsum = sum(vol)
    if vsum <= 1e-12:
        return None
    vwap = sum(t * v for t, v in zip(tp, vol)) / vsum
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    if var <= 1e-18:
        return None
    return (closes[-1] - vwap) / math.sqrt(var)


def compute_highvol_corr(candles: List[List[Any]], window: int = 10) -> float | None:
    """F4: корреляция Пирсона high vs volume за window.

    Высокое +значение = рост на растущем объёме (памп/распределение).
    None если свечей < window или дисперсия нулевая.
    """
    if len(candles) < window:
        return None
    win = candles[-window:]
    try:
        hs = [float(k[2]) for k in win]
        vs = [float(k[5]) for k in win]
    except (ValueError, TypeError, IndexError):
        return None
    mh = sum(hs) / window
    mv = sum(vs) / window
    cov = sum((h - mh) * (v - mv) for h, v in zip(hs, vs))
    sh = math.sqrt(sum((h - mh) ** 2 for h in hs))
    sv = math.sqrt(sum((v - mv) ** 2 for v in vs))
    if sh <= 1e-12 or sv <= 1e-12:
        return None
    corr = cov / (sh * sv)
    return max(-1.0, min(1.0, corr))


def compute_vol_reversal(candles: List[List[Any]]) -> float | None:
    """F1 (alpha101 #12): -sign(dVol1) * dClose1, в единицах цены.

    > 0 согласуется с лонгом (просадка на растущем объёме = выкуп),
    < 0 — с шортом. Нужны минимум 2 свечи.
    """
    if len(candles) < 2:
        return None
    try:
        dvol = float(candles[-1][5]) - float(candles[-2][5])
        dclose = float(candles[-1][4]) - float(candles[-2][4])
    except (ValueError, TypeError, IndexError):
        return None
    if dvol > 0:
        return -dclose
    if dvol < 0:
        return dclose
    return 0.0


def compute_body_range(candles: List[List[Any]]) -> float | None:
    """F7 (alpha101 #101): тело/диапазон последней свечи, [-1, 1].

    +1 = закрытие на хае полным телом (перегрев для LONG).
    None если диапазон нулевой.
    """
    if not candles:
        return None
    try:
        o = float(candles[-1][1])
        h = float(candles[-1][2])
        l = float(candles[-1][3])
        c = float(candles[-1][4])
    except (ValueError, TypeError, IndexError):
        return None
    rng = h - l
    if rng <= 1e-12:
        return None
    return (c - o) / rng


def minute_factor_gates(
    candles: List[List[Any]],
    side: str,
    vwap_max_std: float = 1.5,
    hv_corr_max: float = 0.6,
    body_range_max: float = 0.7,
    f1_enabled: bool = True,
) -> tuple[bool, str | None, dict]:
    """Каскад минутных фильтров F6/F4/F7/F1 для стороны Buy/Sell.

    Возвращает (passed, reject_reason|None, values dict).
    Порядок вето: vwap_overext → highvol_corr_pump → body_overext →
    vol_reversal_against. Недостаток данных = пропуск фильтра (pass),
    строгий в этом каскаде только явный перегрев.
    """
    values: dict = {}
    is_buy = side == "Buy"

    vwap_dev = compute_vwap_dev(candles)
    values["vwap_dev"] = vwap_dev
    if vwap_dev is not None:
        if is_buy and vwap_dev > vwap_max_std:
            return (False, "vwap_overext", values)
        if not is_buy and vwap_dev < -vwap_max_std:
            return (False, "vwap_overext", values)

    hv_corr = compute_highvol_corr(candles)
    values["hv_corr"] = hv_corr
    if hv_corr is not None and hv_corr > hv_corr_max:
        return (False, "highvol_corr_pump", values)

    body = compute_body_range(candles)
    values["body_range"] = body
    if body is not None:
        if is_buy and body > body_range_max:
            return (False, "body_overext", values)
        if not is_buy and body < -body_range_max:
            return (False, "body_overext", values)

    if f1_enabled:
        f1 = compute_vol_reversal(candles)
        values["vol_reversal"] = f1
        if f1 is not None:
            if is_buy and f1 < 0:
                return (False, "vol_reversal_against", values)
            if not is_buy and f1 > 0:
                return (False, "vol_reversal_against", values)
    else:
        values["vol_reversal"] = None

    return (True, None, values)
