"""
tools/indicators.py — индикаторы для ботов.
ACHOP: Adaptive Choppiness Index с ALMA-сглаживанием.
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
