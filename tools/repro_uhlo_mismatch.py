#!/usr/bin/env python3
"""Phase 1 loop: UHLO TV mismatch reproduction.

Тянет закрытые kline Bybit linear для 3 SL-сигналов (HNT/MAGMA/UAI) и
сравнивает цвет classify_color vs ожидаемый side из signal_sent.
Скрипт идёт RED когда хотя бы один сигнал не совпадает с TV/ожидаемым цветом.

Запуск: python3 tools/repro_uhlo_mismatch.py

Ожидаем: HNT red OK, MAGMA green OK, UAI green FAIL -> RED -> bug reproduced.
"""
import requests, time
from datetime import datetime, timezone

def compute_natr(klines, period=14):
    if len(klines) < period+1: return None
    highs=[float(k[2]) for k in klines]; lows=[float(k[3]) for k in klines]; closes=[float(k[4]) for k in klines]
    trs=[]
    for i in range(1,len(klines)):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    atr=sum(trs[:period])/period
    for i in range(period,len(trs)): atr=(atr*(period-1)+trs[i])/period
    return (atr/closes[-1])*100 if closes[-1] else None

def compute_uhlo(klines, length=15):
    if len(klines) < length: return {"highs":0,"lows":0}
    unreached_highs, unreached_lows=[], []
    u_highs=u_lows=0.0
    for k in klines:
        h=float(k[2]); l=float(k[3])
        unreached_highs=[x for x in unreached_highs if h <= x]
        unreached_lows=[x for x in unreached_lows if l >= x]
        if len(unreached_highs)>length: unreached_highs.pop()
        if len(unreached_lows)>length: unreached_lows.pop()
        u_highs=100*len(unreached_highs)/length
        u_lows=100*len(unreached_lows)/length
        unreached_highs.insert(0,h)
        unreached_lows.insert(0,l)
    return {"highs":100-u_highs,"lows":100-u_lows}

def classify_color(a,b):
    FAST_MIN,FAST_MAX,SLOW_MIN,SLOW_MAX=80.,20.,80.,20.
    if not a or not b: return "none"
    # FIX H1: inclusive >= / <= to match reference/screener.py:432 and TV LuxAlgo
    green=(a["highs"]>=FAST_MIN and a["lows"]<=FAST_MAX and b["highs"]>=SLOW_MIN and b["lows"]<=SLOW_MAX)
    red=(a["lows"]>=FAST_MIN and a["highs"]<=FAST_MAX and b["lows"]>=SLOW_MIN and b["highs"]<=SLOW_MAX)
    return "green" if green else "red" if red else "none"

def fetch_klines_at(symbol, interval, signal_ts):
    start=signal_ts-80*int(interval)*60_000
    end=signal_ts+int(interval)*60_000
    params={'category':'linear','symbol':symbol,'interval':interval,'limit':200,'start':start,'end':end}
    r=requests.get('https://api.bybit.com/v5/market/kline', params=params, timeout=10)
    j=r.json()
    rows=[]
    for rr in j['result']['list']:
        rows.append([int(rr[0]), float(rr[1]), float(rr[2]), float(rr[3]), float(rr[4]), float(rr[5])])
    rows.sort(key=lambda x: x[0])
    closed=[]
    for x in rows:
        if interval=='15':
            # FIX H2: include forming 15m candle to match live WS deque (live_screener_midcap.py:733 includes forming)
            # At signal ts=00:17, the 00:15-00:30 forming candle is in deque and must be counted.
            if x[0] <= signal_ts:
                closed.append(x)
        else:
            if x[0] <= signal_ts:
                closed.append(x)
    return closed[-50:]

targets=[
    ('HNTUSDT',1788289320000,'Sell', 'red'),
    ('MAGMAUSDT',1788293640000,'Buy', 'green'),
    ('UAIUSDT',1788308220000,'Buy', 'green'),
]

red=False
for sym, ts, side, expected_color in targets:
    k1=fetch_klines_at(sym,'1',ts)
    k15=fetch_klines_at(sym,'15',ts)
    uhlo1=compute_uhlo(k1,15)
    uhlo15=compute_uhlo(k15,15)
    color=classify_color(uhlo1, uhlo15)
    exp = expected_color
    ok = (color == exp)
    status = "OK" if ok else "MISMATCH"
    if not ok:
        red=True
    dt=datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"[DEBUG-uhlo] {sym} {dt} side={side} expected={exp} got={color} -> {status}")
    print(f"  1m highs={uhlo1['highs']:.1f} lows={uhlo1['lows']:.1f} raw UH={100-uhlo1['highs']:.1f} UL={100-uhlo1['lows']:.1f}")
    print(f"  15m highs={uhlo15['highs']:.1f} lows={uhlo15['lows']:.1f} raw UH={100-uhlo15['highs']:.1f} UL={100-uhlo15['lows']:.1f}")
    # also print last prices for TV cross-check
    time.sleep(0.2)

if red:
    print("\n[RESULT] RED — mismatch reproduced (UAI green expected but got none)")
    exit(1)
else:
    print("\n[RESULT] GREEN — all colors match")
    exit(0)
