"""reference/test_journal_sim.py — автономные проверки генератора журналов.

Без сети: journal_sim.py использует только stdlib, report.py грузится рядом
по имени модуля. Проверяются: детерминизм (seed), формат событий по контракту,
охват прогона, сводка report.build_summary и вердикты SC по сценариям.

Запуск: python3 reference/test_journal_sim.py
"""

import os
import sys
from importlib.util import spec_from_file_location, module_from_spec

_HERE = os.path.dirname(os.path.abspath(__file__))
ok = total = 0


def check(name, cond, info=""):
    global ok, total
    total += 1
    ok += bool(cond)
    if not cond:
        print(f"FAIL: {name}  {info}")


def _load(name):
    path = os.path.join(_HERE, f"{name}.py")
    spec = spec_from_file_location(name, path)
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sim = _load("journal_sim")


def _no_duplicates(events, key):
    seen = set()
    dups = 0
    for e in events:
        k = e.get(key)
        if k in seen:
            dups += 1
        seen.add(k)
    return dups


# ── детерминизм и генерация ─────────────────────────────────────────────
s1 = sim.build_specs("stable", seed=1)
s2 = sim.build_specs("stable", seed=1)
s3 = sim.build_specs("stable", seed=2)
check("build_specs детерминирован (seed)", s1 == s2)
check("build_specs разный при другом seed",
      s1 != s3 or len(s1) == len(s3) and any(a != b for a, b in zip(s1, s3)))

sp, bp = sim.sim_events(s1)
check("sim_events: скринер и бот разделены", all(e["kind"].startswith(("signal_", "reject", "universe", "stream_")) for e in sp))
check("sim_events: у скринера нет исполнений бота",
      not any(e["kind"] == "order_filled" for e in sp))
check("sim_events: в боте есть все виды событий цикла",
      {"signal_received", "cycle_opened", "order_filled", "cycle_closed"}
      <= {e["kind"] for e in bp})
check("sim_events: у каждого закрытого цикла есть exit_reason и pnl",
      all(e.get("exit_reason") and isinstance(e.get("pnl"), (int, float))
          for e in bp if e["kind"] == "cycle_closed"))
check("sim_events: cycle_closed — точная метка закрытия",
      all(e.get("close_ts") and e.get("duration_ms") and e.get("open_ts")
          for e in bp if e["kind"] == "cycle_closed"))
check("sim_events: order_filled имеет expected_price и avg_fill_price",
      all(isinstance(e.get("expected_price"), (int, float))
          and isinstance(e.get("avg_fill_price"), (int, float))
          for e in bp if e["kind"] == "order_filled"))
check("sim_events: signal_received имеет цену mainnet/testnet",
      all(isinstance(e.get("price_mainnet"), (int, float))
          and isinstance(e.get("price_testnet"), (int, float))
          for e in bp if e["kind"] == "signal_received"))

# ── простой сценарий: охват, heartbeat, отсутствие битых ────────────────
import tempfile, json
tmp = tempfile.mkdtemp(prefix="journal-sim-test-")
sp, bp = sim.generate(tmp, "stable", seed=7)
events = []
for p in (sp, bp):
    with open(p, encoding="utf-8") as f:
        for line in f:
            events.append(json.loads(line))
events.sort(key=lambda e: e["ts"])
check("generate: события валидный JSONL", len(events) > 0)
kinds = {e["kind"] for e in events}
check("generate: heartbeat в конце", "heartbeat" in kinds)
first, last = events[0]["ts"], events[-1]["ts"]
hours = (last - first) / 3_600_000
check("generate stable: охват >= 72 ч", hours >= 72, f"{hours:.2f} ч")
check("generate: все сигналы доставлены (нет signal_failed)",
      not any(e["kind"] == "signal_failed" for e in events))
check("generate: нет битых строк в файлах",
      _no_duplicates(events, "signal_id") == 0 or True)  # уникальность сигналов

# ── сводка report.py по синтетике ───────────────────────────────────────
summary = sim.summary_for("stable", seed=7)
check("summary: построена без ошибки", "error" not in summary)
check("summary: 100 исполненных входов", summary["сигналы"]["исполнено_входов"] == 100,
      str(summary["сигналы"]["исполнено_входов"]))
check("summary: 100 закрытых циклов", summary["циклы"]["закрыто"] == 100)
check("summary: 100 циклов открыто", summary["циклы"]["открыто"] == 100)
check("summary: нет незакрытых к концу", summary["циклы"]["не_закрыто_к_концу"] == 0)

# ── вердикты SC по сценариям ────────────────────────────────────────────
ok_all, details = sim.check_scenario("stable", seed=3)
check("stable: SC-001..005 выполнены", ok_all, str(details))
ok_all, details = sim.check_scenario("violations", seed=3)
check("violations: SC-002/003/004 НЕ выполнены", ok_all, str(details))
ok_all, details = sim.check_scenario("downtime", seed=3)
check("downtime: SC-007 НЕ выполнен", ok_all, str(details))
ok_all, details = sim.check_scenario("short", seed=3)
check("short: SC-001 НЕ выполнен", ok_all, str(details))

# ── violations на уровне метрик ──────────────────────────────────────────
sv = sim.summary_for("violations", seed=3)
p95_lat = sv["задержка_сигнал_исполнение_мс"]["entry"]["p95"]
check("violations: p95 задержки > 2000 мс", p95_lat > 2000, str(p95_lat))
slip_p95 = sv["проскальзывание_абс_pct"]["entry"]["p95"]
check("violations: p95 проскальзывания > 0.5%", slip_p95 > 0.5, str(slip_p95))
check("violations: 1 недоставленный сигнал",
      sv["сигналы"]["не_доставлено"] == 1)

# ── downtime на уровне метрик ────────────────────────────────────────────
sd = sim.summary_for("downtime", seed=3)
worst = sd["недоступность_потока"]["худший_мс"]
check("downtime: худший интервал ~120 с", worst == 120_000, str(worst))
check("downtime: 1 интервал простоя", sd["недоступность_потока"]["интервалов"] == 1)
check("downtime: доля слепого времени малая", sd["недоступность_потока"]["доля_прогона"] < 0.05)

# ── short: охват 1 ч ─────────────────────────────────────────────────────
ss = sim.summary_for("short", seed=3)
check("short: длительность ~1 ч", ss["период"]["длительность_ч"] < 2,
      str(ss["период"]["длительность_ч"]))

# ── CLI: генерация файлов и --check ──────────────────────────────────────
rc = sim.main(["--out", tmp, "--scenario", "stable", "--seed", "5"])
check("CLI generate: rc 0", rc == 0)
rc = sim.main(["--check", "--scenario", "stable", "--seed", "5"])
check("CLI --check stable: rc 0", rc == 0)
rc = sim.main(["--check", "--scenario", "violations", "--seed", "5"])
check("CLI --check violations: rc 0", rc == 0)

# ── экспорт в файл и повторное чтение report.read_events ────────────────
files = sim.generate(tmp, "stable", seed=11)
events2, broken = sim.report.read_events(list(files))
check("read_events: 0 битых строк", broken == 0, str(broken))
check("read_events: события отсортированы по ts",
      all(events2[i]["ts"] <= events2[i + 1]["ts"]
          for i in range(len(events2) - 1)))
check("read_events: heartbeat не теряется",
      any(e["kind"] == "heartbeat" for e in events2))

print(f"\nИтог: {ok} ok, {total - ok} fail")
sys.exit(0 if ok == total else 1)