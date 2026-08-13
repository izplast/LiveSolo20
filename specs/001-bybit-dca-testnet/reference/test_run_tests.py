"""
Проверки единого автораннера (tools/run_tests.py).

Синтетические тестовые файлы и журналы → ожидаемые сводки, парсинг итогов,
валидация config.yml и SC-вердикты. Без сети, без pytest.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_run_tests.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

_repo = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_path = os.path.join(_repo, "tools", "run_tests.py")
_spec = importlib.util.spec_from_file_location("run_tests_under_test", _path)
assert _spec and _spec.loader
rt = importlib.util.module_from_spec(_spec)
sys.modules["run_tests_under_test"] = rt
_spec.loader.exec_module(rt)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def write_lines(rows: list[str]) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(rows))
        f.write("\n")
    return path


def write_jsonl(events: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


# ── фазы тестов ─────────────────────────────────────────────────────────────

print("фазы (сводка по фазам)")
ok("A/B-тест → фаза A/B", rt._phase("test_ab_tools.py") == "A/B")
ok("сквозной пайплайн → интеграционные",
   rt._phase("test_full_pipeline.py") == "интеграционные")
ok("интеграция DCA → интеграционные",
   rt._phase("test_dca_integration.py") == "интеграционные")
ok("обычный юнит → юнит", rt._phase("test_pricing.py") == "юнит")

# ── discover_tests / run_test_file / parse_test_totals ──────────────────────

print("\ndiscover_tests / parse_test_totals")
tmpdir = tempfile.mkdtemp(prefix="rt-test-")
good = os.path.join(tmpdir, "test_dummy_good.py")
with open(good, "w", encoding="utf-8") as f:
    f.write("print('итог: 7 ok, 1 fail')\n"
            "import sys; sys.exit(0)\n")
bad = os.path.join(tmpdir, "test_dummy_bad.py")
with open(bad, "w", encoding="utf-8") as f:
    f.write("print('итог: 2 ok, 3 fail')\n"
            "import sys; sys.exit(1)\n")
crash = os.path.join(tmpdir, "test_dummy_crash.py")
with open(crash, "w", encoding="utf-8") as f:
    f.write("raise RuntimeError('boom')\n")

discovered = rt.discover_tests(tmpdir)
ok("найдены все test_*.py", sorted(os.path.basename(p) for p in discovered)
   == ["test_dummy_bad.py", "test_dummy_crash.py", "test_dummy_good.py"],
   discovered)
ok("не-тесты не попадают",
   all(os.path.basename(p).startswith("test_") for p in discovered))

res_good = rt.run_test_file(good)
ok("rc = 0 у прошедшего теста", res_good["rc"] == 0, res_good)
ok("итог прошедшего распознан", rt.parse_test_totals(res_good["output"]) == (7, 1))

res_bad = rt.run_test_file(bad)
ok("rc = 1 у проваленного теста", res_bad["rc"] == 1, res_bad)
ok("итог проваленного распознан", rt.parse_test_totals(res_bad["output"]) == (2, 3))

res_crash = rt.run_test_file(crash)
ok("упавший тест имеет rc != 0", res_crash["rc"] != 0, res_crash)
ok("упавший тест без итога", rt.parse_test_totals(res_crash["output"]) == (None, None))

ok("пустой вывод → (None, None)", rt.parse_test_totals("") == (None, None))
ok("итог без fail → распознаётся", rt.parse_test_totals("итог: 5 ok, 0 FAIL") == (5, 0))

# ── unit_summary ─────────────────────────────────────────────────────────────

print("\nunit_summary")
results = [
    {"name": "test_ab_tools.py", "rc": 0, "ok": 89, "fail": 0},
    {"name": "test_pricing.py", "rc": 0, "ok": 30, "fail": 0},
    {"name": "test_full_pipeline.py", "rc": 1, "ok": 10, "fail": 2},
]
s = rt.unit_summary(results)
ok("итоги по фазам сгруппированы",
   s["по_фазам"]["A/B"]["ok"] == 89 and s["по_фазам"]["юнит"]["ok"] == 30
   and s["по_фазам"]["интеграционные"]["ok"] == 10, s["по_фазам"])
ok("провал попал в список", s["провалы"] and "test_full_pipeline.py" in s["провалы"][0])
ok("всего ok/fail сходятся",
   s["всего_ok"] == 129 and s["всего_fail"] == 2, (s["всего_ok"], s["всего_fail"]))

# ── validate_config ─────────────────────────────────────────────────────────

print("\nvalidate_config (реальный config.yml)")
REPO_CONFIG = os.path.join(_repo, "config", "config.yml")
checks = rt.validate_config(REPO_CONFIG)
ok("все проверки прошли", all(c["ok"] for c in checks), checks)
ok("проверен leverage dca == screener",
   any("leverage" in c["name"] for c in checks), checks)

print("\nvalidate_config: битый конфиг")
bad_cfg = write_lines([
    "dca:",
    "  steps: -1",
    "  hard_sl_pct: 50.0",
    "screener:",
    "  natr_min: 7.0",
    "  natr_max: 2.0",
    "  required_leverage: 3",
])
checks2 = rt.validate_config(bad_cfg)
ok("битый dca → есть FAIL", any(not c["ok"] for c in checks2), checks2)
ok("DcaParams невалиден отмечен",
   any("DcaParams" in c["name"] and not c["ok"] for c in checks2))
ok("natr_min >= natr_max отмечен",
   any("natr_min" in c["name"] and not c["ok"] for c in checks2))

print("\nvalidate_config: отсутствующий файл")
checks3 = rt.validate_config("/no/such/config.yml")
ok("нет файла → один FAIL, без падения", len(checks3) == 1 and not checks3[0]["ok"],
   checks3)

# ── журналы: SC-вердикты ────────────────────────────────────────────────────

print("\njournal_section: успешный журнал")
T0 = 1_760_000_000_000
HOUR = 3_600_000
MIN = 60_000
good_events = []
for i in range(20):
    ts = T0 + i * MIN
    sid = f"AAAUSDT:{i}"
    good_events.append({"kind": "signal_sent", "ts": ts,
                        "signal": {"signal_id": sid, "symbol": "AAAUSDT",
                                   "side": "Buy", "price": 100.0, "ts": ts}})
    good_events.append({"kind": "signal_received", "ts": ts + 50, "signal_id": sid,
                        "symbol": "AAAUSDT", "price_mainnet": 100.0,
                        "price_testnet": 100.2, "ts_signal": ts})
    good_events.append({"kind": "cycle_opened", "ts": ts + 100, "cycle_id": f"c{i}",
                        "symbol": "AAAUSDT"})
    good_events.append({"kind": "order_filled", "ts": ts + 800, "signal_id": sid,
                        "cycle_id": f"c{i}", "symbol": "AAAUSDT", "role": "entry",
                        "side": "Buy", "expected_price": 100.0,
                        "avg_fill_price": 100.1, "ts_signal": ts,
                        "ts_confirmed": ts + 800})
    good_events.append({"kind": "cycle_closed", "ts": ts + HOUR, "cycle_id": f"c{i}",
                        "symbol": "AAAUSDT", "exit_reason": "take_profit", "pnl": 0.4})
good_events.append({"kind": "heartbeat", "ts": T0 + 73 * HOUR})
j_good = write_jsonl(good_events)

jres = rt.journal_section([j_good])
ok("журнал найден и сводка построена",
   jres["found"] and jres["error"] is None, jres)
ok("SC-001..SC-005 есть в вердиктах",
   {v["код"] for v in jres["verdicts"]} == set(rt.SC_JOURNAL_CODES), jres["verdicts"])
ok("все проверяемые SC выполнен",
   all(v["вердикт"] == "выполнен" for v in jres["verdicts"]), jres["verdicts"])
ok("секция считается прошедшей", rt.journal_ok(jres))

print("\njournal_section: журнал с нарушениями")
bad_events = [{"kind": "stream_down", "ts": T0, "shard": 0},
              {"kind": "stream_up", "ts": T0 + 130_000, "shard": 0,
               "duration_ms": 120_000},
              {"kind": "order_filled", "ts": T0 + 1000, "role": "entry", "side": "Buy",
               "expected_price": 100.0, "avg_fill_price": 101.5,
               "ts_signal": T0, "ts_confirmed": T0 + 5000}]
j_bad = write_jsonl(bad_events)
jres2 = rt.journal_section([j_bad])
ok("сводка построена", jres2["error"] is None)
ok("SC-003 НЕ выполнен (задержка 5 с)",
   any(v["код"] == "SC-003" and v["вердикт"] == "НЕ выполнен"
       for v in jres2["verdicts"]), jres2["verdicts"])
ok("секция считается проваленной", not rt.journal_ok(jres2))

print("\njournal_section: журналов нет")
jres3 = rt.journal_section(["/no/such/journal.jsonl"])
ok("не найдено → found=False", not jres3["found"])
ok("нет журналов — «нет данных», не провал", rt.journal_ok(jres3))

print("\njournal_section: битые строки")
j_broken = write_lines([
    '{"kind": "signal_sent", "ts": ' + str(T0) + ', "signal": {}}',
    '{"kind": "order_filled", "ts": ',  # обрыв
    'not-json',
])
jres4 = rt.journal_section([j_broken])
ok("битые строки посчитаны", jres4["broken"] == 2, jres4)

# ── scan_text_log ────────────────────────────────────────────────────────────

print("\nscan_text_log")
log = write_lines(["2026-08-01 10:00:00 INFO start",
                   "2026-08-01 10:00:01 ERROR something",
                   "2026-08-01 10:00:02 CRITICAL boom",
                   "2026-08-01 10:00:03 INFO stream_down",
                   "2026-08-01 10:00:04 INFO stream_up"])
a = rt.scan_text_log(log)
ok("строки посчитаны", a["строк"] == 5, a)
ok("строки с ошибками посчитаны", a["error_строк"] == 2, a)
ok("простои в тексте видны", a["stream_down"] == 1 and a["stream_up"] == 1, a)
ok("нет файла → found=False", not rt.scan_text_log("/no/such/app.log")["found"])

for p in (good, bad, crash, bad_cfg, j_good, j_bad, j_broken, log):
    try:
        os.unlink(p)
    except OSError:
        pass

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)