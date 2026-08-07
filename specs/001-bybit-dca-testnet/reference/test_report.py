"""
Проверки сводки: синтетический журнал → ожидаемые агрегаты и вердикты.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_report.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.py")
_spec = importlib.util.spec_from_file_location("report_under_test", _path)
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
sys.modules["report_under_test"] = rp
_spec.loader.exec_module(rp)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


T0 = 1_760_000_000_000  # произвольная точка отсчёта, мс
HOUR = 3_600_000


def write_journal(events: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


# ── процентили ───────────────────────────────────────────────────────────────

print("percentile")
ok("пустой список → None", rp.percentile([], 95) is None)
ok("один элемент → он же", rp.percentile([7.0], 95) == 7.0)
ok("p95 из 100 значений 1..100 → 95", rp.percentile([float(i) for i in range(1, 101)], 95) == 95.0)
ok("медиана из 1..100 → 50", rp.median([float(i) for i in range(1, 101)]) == 50.0)
ok("p95 устойчив к порядку", rp.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 95) == 5.0)

# ── слепые интервалы ─────────────────────────────────────────────────────────

print("\nblind_intervals")
evs = [
    {"kind": "stream_down", "ts": T0 + 1000, "shard": 0},
    {"kind": "stream_up", "ts": T0 + 6000, "shard": 0, "duration_ms": 5000},
    {"kind": "stream_down", "ts": T0 + 9000, "shard": 1},
    {"kind": "stream_up", "ts": T0 + 10000, "shard": 1, "duration_ms": 1000},
]
intervals, unclosed = rp.blind_intervals(evs)
ok("две пары → два интервала", len(intervals) == 2, intervals)
ok("длительности посчитаны", sorted(i["duration_ms"] for i in intervals) == [1000, 5000])
ok("незакрытых нет", unclosed == 0)

evs_unclosed = [
    {"kind": "stream_down", "ts": T0, "shard": 0},
    {"kind": "signal_sent", "ts": T0 + 4000},
]
intervals2, unclosed2 = rp.blind_intervals(evs_unclosed)
ok("обрыв без восстановления закрывается последним событием",
   unclosed2 == 1 and intervals2[0]["duration_ms"] == 4000, intervals2)

evs_shards = [
    {"kind": "stream_down", "ts": T0, "shard": 0},
    {"kind": "stream_down", "ts": T0 + 100, "shard": 1},
    {"kind": "stream_up", "ts": T0 + 500, "shard": 1},
    {"kind": "stream_up", "ts": T0 + 900, "shard": 0},
]
i3, _ = rp.blind_intervals(evs_shards)
ok("шарды считаются независимо, не схлопываются в один интервал", len(i3) == 2, i3)

# ── сводка: успешный прогон ──────────────────────────────────────────────────

print("\nbuild_summary: прогон, укладывающийся в ориентиры")
good: list[dict] = []
for i in range(100):
    ts_signal = T0 + i * 60_000
    sid = f"AAAUSDT:1:{ts_signal}"
    good.append({"kind": "signal_sent", "ts": ts_signal,
                 "signal": {"signal_id": sid, "symbol": "AAAUSDT", "side": "Buy",
                            "price": 100.0, "ts": ts_signal}})
    good.append({"kind": "signal_received", "ts": ts_signal + 50, "signal_id": sid,
                 "symbol": "AAAUSDT", "price_mainnet": 100.0, "price_testnet": 100.2,
                 "ts_signal": ts_signal})
    good.append({"kind": "cycle_opened", "ts": ts_signal + 100, "cycle_id": f"c{i}",
                 "symbol": "AAAUSDT"})
    good.append({"kind": "order_filled", "ts": ts_signal + 800, "signal_id": sid,
                 "cycle_id": f"c{i}", "symbol": "AAAUSDT", "role": "entry", "side": "Buy",
                 "expected_price": 100.0, "avg_fill_price": 100.1,
                 "ts_signal": ts_signal, "ts_confirmed": ts_signal + 800})
    good.append({"kind": "cycle_closed", "ts": ts_signal + 3600_000, "cycle_id": f"c{i}",
                 "symbol": "AAAUSDT", "exit_reason": "take_profit", "pnl": 0.42})
# прогон длиной 73 часа: последнее событие сдвигаем
good.append({"kind": "heartbeat", "ts": T0 + 73 * HOUR})

s = rp.build_summary(sorted(good, key=lambda e: e["ts"]))
ok("длительность 73 ч", abs(s["период"]["длительность_ч"] - 73) < 0.1, s["период"])
ok("сигналов отправлено 100", s["сигналы"]["отправлено_скринером"] == 100)
ok("входов исполнено 100", s["сигналы"]["исполнено_входов"] == 100)
ok("задержка входа: медиана 800 мс",
   s["задержка_сигнал_исполнение_мс"]["entry"]["median"] == 800.0,
   s["задержка_сигнал_исполнение_мс"])
ok("проскальзывание входа: медиана 0.1%",
   abs(s["проскальзывание_абс_pct"]["entry"]["median"] - 0.1) < 1e-9,
   s["проскальзывание_абс_pct"])
ok("базис Testnet к mainnet = 0.2%",
   abs(s["базис_testnet_к_mainnet_pct"]["median"] - 0.2) < 1e-9,
   s["базис_testnet_к_mainnet_pct"])
ok("циклы: 100 открыто, 100 закрыто по тейк-профиту",
   s["циклы"]["открыто"] == 100 and s["циклы"]["по_причине_выхода"]["take_profit"] == 100)
ok("не закрытых к концу нет", s["циклы"]["не_закрыто_к_концу"] == 0)

verd = {v["код"]: v for v in s["критерии"]}
ok("SC-001 выполнен (73 ч)", verd["SC-001"]["вердикт"] == "выполнен", verd["SC-001"])
ok("SC-003 выполнен (p95 задержки 800 мс)", verd["SC-003"]["вердикт"] == "выполнен", verd["SC-003"])
ok("SC-004 выполнен (медиана 0.1%)", verd["SC-004"]["вердикт"] == "выполнен", verd["SC-004"])
ok("SC-005 выполнен (метрики у всех)", verd["SC-005"]["вердикт"] == "выполнен", verd["SC-005"])
ok("SC-002 выполнен (ошибок нет)", verd["SC-002"]["вердикт"] == "выполнен")
ok("SC-008 помечен как требующий ручной проверки, а не выполненным",
   verd["SC-008"]["вердикт"] == "нет данных", verd["SC-008"])

# ── сводка: прогон, не проходящий пороги ─────────────────────────────────────

print("\nbuild_summary: прогон с превышениями")
bad: list[dict] = []
for i in range(20):
    ts_signal = T0 + i * 60_000
    sid = f"BBBUSDT:1:{ts_signal}"
    # каждое пятое исполнение — с задержкой 5 с и проскальзыванием 1.2%
    slow = i % 5 == 0
    bad.append({"kind": "order_filled", "ts": ts_signal + 1, "signal_id": sid,
                "cycle_id": f"d{i}", "symbol": "BBBUSDT", "role": "entry", "side": "Buy",
                "expected_price": 100.0,
                "avg_fill_price": 101.2 if slow else 100.05,
                "ts_signal": ts_signal,
                "ts_confirmed": ts_signal + (5000 if slow else 400)})
bad.append({"kind": "signal_failed", "ts": T0 + 5000, "error": "connection refused",
            "signal": {"signal_id": "x", "symbol": "BBBUSDT"}})
bad.append({"kind": "stream_down", "ts": T0 + 10_000, "shard": 0})
bad.append({"kind": "stream_up", "ts": T0 + 130_000, "shard": 0, "duration_ms": 120_000})

s2 = rp.build_summary(sorted(bad, key=lambda e: e["ts"]))
v2 = {v["код"]: v for v in s2["критерии"]}
ok("SC-001 НЕ выполнен (прогон короткий)", v2["SC-001"]["вердикт"] == "НЕ выполнен")
ok("SC-002 НЕ выполнен (есть недоставленный сигнал)", v2["SC-002"]["вердикт"] == "НЕ выполнен",
   v2["SC-002"])
ok("SC-003 НЕ выполнен (p95 задержки 5000 мс)", v2["SC-003"]["вердикт"] == "НЕ выполнен",
   v2["SC-003"])
ok("SC-004 НЕ выполнен (p95 проскальзывания 1.2%)", v2["SC-004"]["вердикт"] == "НЕ выполнен",
   v2["SC-004"])
ok("SC-007 НЕ выполнен (восстановление 120 с > 30 с)", v2["SC-007"]["вердикт"] == "НЕ выполнен",
   v2["SC-007"])
ok("доля превышений задержки = 0.2",
   s2["превышения_ориентиров"]["задержка_входа_свыше_2с"]["доля"] == 0.2,
   s2["превышения_ориентиров"])

# ── знак проскальзывания для шорта ───────────────────────────────────────────

print("\nзнак проскальзывания")
mixed = [
    # лонг куплен дороже ожидания → невыгодно → знак +
    {"kind": "order_filled", "ts": T0, "role": "entry", "side": "Buy",
     "expected_price": 100.0, "avg_fill_price": 100.5, "ts_signal": T0, "ts_confirmed": T0 + 10},
    # шорт продан дешевле ожидания → тоже невыгодно → знак тоже +
    {"kind": "order_filled", "ts": T0 + 1, "role": "entry", "side": "Sell",
     "expected_price": 100.0, "avg_fill_price": 99.5, "ts_signal": T0, "ts_confirmed": T0 + 10},
]
s3 = rp.build_summary(mixed)
signed = s3["проскальзывание_со_знаком_pct"]["entry"]
ok("невыгодное проскальзывание шорта и лонга имеет один знак",
   signed["min"] > 0 and signed["max"] > 0, signed)
ok("медиана со знаком 0.5%, а не 0 (стороны не схлопнулись)",
   abs(signed["median"] - 0.5) < 1e-9, signed)

# ── устойчивость чтения ──────────────────────────────────────────────────────

print("\nчтение журнала")
path = write_journal([{"kind": "signal_sent", "ts": T0, "signal": {}}])
with open(path, "a", encoding="utf-8") as f:
    f.write('{"kind": "order_filled", "ts": ')          # обрыв питания посреди строки
    f.write("\n")
    f.write('{"no_kind": 1, "ts": 5}\n')
    f.write('{"kind": "ok_but_no_ts"}\n')
events, broken = rp.read_events([path])
ok("битые строки посчитаны, а не уронили разбор", broken == 3, (len(events), broken))
ok("валидная строка прочитана", len(events) == 1)

events2, _ = rp.read_events([path + ".нет-такого"])
ok("отсутствующий файл не роняет сводку", events2 == [])
ok("пустой журнал → понятная ошибка, а не исключение",
   rp.build_summary([]).get("error") is not None)

print("\nтекстовый вывод")
text = rp.render(s)
ok("в выводе есть блок критериев", "Критерии приёмки" in text)
ok("в выводе есть предупреждение про ориентиры", "начальные ориентиры" in text)
ok("базис отделён от проскальзывания", "НЕ входит в порог" in text)
ok("вывод не падает на прогоне с превышениями", isinstance(rp.render(s2), str))
ok("вывод не падает на пустом журнале", "не построена" in rp.render(rp.build_summary([])))

os.unlink(path)
print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
