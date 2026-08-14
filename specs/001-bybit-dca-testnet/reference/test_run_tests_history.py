"""
Автономные проверки истории прогонов автораннера (T025).

Покрывает запись/чтение logs/test_runs.jsonl (tools/run_tests.py):
build_history_record, write_history, read_history, summarize_history,
git_version. Без сети, без pytest.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_run_tests_history.py
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
_spec = importlib.util.spec_from_file_location("run_tests_history_under_test", _path)
assert _spec and _spec.loader
rt = importlib.util.module_from_spec(_spec)
sys.modules["run_tests_history_under_test"] = rt
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
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(rows))
        f.write("\n")
    return path


# ── git_version ─────────────────────────────────────────────────────────────

print("git_version")
v = rt.git_version()
ok("git_version возвращает непустую строку", isinstance(v, str) and len(v) > 0, v)
ok("git_version — короткий хэш (hex) или unknown",
   (len(v) <= 10 and all(c in "0123456789abcdef" for c in v)) or v == "unknown", v)

# ── section_record / build_history_record ────────────────────────────────────

print("\nsection_record / build_history_record")
results = {
    "unit": [{"name": "a.py", "rc": 0, "ok": 10, "fail": 0},
             {"name": "b.py", "rc": 1, "ok": 5, "fail": 2}],
    "config": [{"ok": True, "name": "c1"}, {"ok": False, "name": "c2"}],
    "journal": {"found": True, "error": None,
                "verdicts": [{"код": "SC-001", "вердикт": "выполнен"},
                             {"код": "SC-003", "вердикт": "НЕ выполнен"}]},
    "journal_sim": {"stable": {"ok": True}, "downtime": {"ok": False}},
}
rec = rt.build_history_record(results, rc=1, version="abc123")
ok("запись содержит ts, version, rc",
   "ts" in rec and rec["version"] == "abc123" and rec["rc"] == 1, rec)
ok("unit: ok/fail по сумме файлов",
   rec["sections"]["unit"] == {"ok": 15, "fail": 2, "files": 2},
   rec["sections"]["unit"])
ok("config: ok/fail посчитаны", rec["sections"]["config"] == {"ok": 1, "fail": 1},
   rec["sections"]["config"])
ok("journal: вердикты в ok/fail",
   rec["sections"]["journal"] == {"ok": 1, "fail": 1}, rec["sections"]["journal"])
ok("journal_sim: сценарии в ok/fail/files",
   rec["sections"]["journal_sim"] == {"ok": 1, "fail": 1, "files": 2},
   rec["sections"]["journal_sim"])
ok("ok_total/fail_total сходятся",
   rec["ok_total"] == 18 and rec["fail_total"] == 5,
   (rec["ok_total"], rec["fail_total"]))

rec_nd = rt.build_history_record(
    {"journal": {"found": False, "error": None, "verdicts": []}},
    rc=0, version="x")
ok("journal без данных → нет_данных, без провала",
   rec_nd["sections"]["journal"].get("нет_данных") is True
   and rec_nd["fail_total"] == 0, rec_nd["sections"]["journal"])

# ── write_history / read_history ─────────────────────────────────────────────

print("\nwrite_history / read_history")
tmpdir = tempfile.mkdtemp(prefix="rt-hist-")
hist_path = os.path.join(tmpdir, "nested", "test_runs.jsonl")
rt.write_history(hist_path, rec)
rt.write_history(hist_path, rec_nd)
ok("файл создан (включая каталог)", os.path.exists(hist_path))
raw = open(hist_path, encoding="utf-8").read().strip().splitlines()
ok("две записи в JSONL", len(raw) == 2, raw)

records = rt.read_history(hist_path)
ok("read_history возвращает обе записи", len(records) == 2, records)
ok("порядок записей сохранён",
   records[0]["version"] == "abc123" and records[1]["version"] == "x")

print("\nread_history: битые строки и пустой файл")
broken = write_lines([
    '{"ts": "2026-08-14T00:00:00+0000", "version": "a", "rc": 0}',
    'not-json{',
    '{"ts": "2026-08-14T01:00:00+0000", "version": "b", "rc": 1}',
    '',
])
r2 = rt.read_history(broken)
ok("битые/пустые строки пропущены", len(r2) == 2, r2)
ok("нет файла → пустой список", rt.read_history("/no/such/history.jsonl") == [])

# ── summarize_history ────────────────────────────────────────────────────────

print("\nsummarize_history")
hist = rt.summarize_history(records)
ok("прогонов и провалов посчитаны",
   hist["прогонов"] == 2 and hist["прогонов_с_провалом"] == 1,
   hist)
ok("суммарно ok/fail по всем записям",
   hist["ok_total"] == 18 and hist["fail_total"] == 5,
   (hist["ok_total"], hist["fail_total"]))
ok("последний прогон — последняя запись",
   hist["последний"]["version"] == "x", hist["последний"])
sec = hist["секции"]["unit"]
ok("проходимость секции unit сгруппирована",
   sec["прогонов"] == 1 and sec["ok"] == 15 and sec["fail"] == 2, sec)

h_empty = rt.summarize_history([])
ok("пустая история → прогонов 0",
   h_empty["прогонов"] == 0 and h_empty["последний"] is None, h_empty)

for p in (broken, hist_path):
    try:
        os.unlink(p)
    except OSError:
        pass

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)