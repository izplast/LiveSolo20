"""process_stall и короткий таймаут REST — измерение заморозок процесса.

Запуск:  python3 tests/test_process_stall.py   (код 0 = ок)
"""
import io
import json
import logging
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor as E
import live_screener_midcap as ls
from exit_fake import make_bot, check

TMP = Path("/tmp/opencode/process-stall-test")
TMP.mkdir(parents=True, exist_ok=True)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


def journal_kinds(path):
    return [json.loads(line)["kind"]
            for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_executor_stall():
    jpath = TMP / "bot-stall.jsonl"
    jpath.unlink(missing_ok=True)
    bot = make_bot()
    bot.journal = E.CycleJournal(str(jpath))
    bot._last_monitor_tick = None

    check("executor: первый такт не считается паузой",
          bot._check_process_stall(100.0) is None)
    check("executor: 12с — не пауза",
          bot._check_process_stall(112.0) is None)

    h = Capture()
    E.logger.addHandler(h)
    try:
        gap = bot._check_process_stall(145.0)
    finally:
        E.logger.removeHandler(h)

    check("executor: gap=33с обнаружен", gap == 33.0, gap)
    rows = [json.loads(line)
            for line in jpath.read_text(encoding="utf-8").splitlines()
            if line]
    check("executor: process_stall в журнале",
          [r["kind"] for r in rows] == ["process_stall"], rows)
    check("executor: gap_sec в событии", rows[0]["gap_sec"] == 33.0, rows[0])
    warns = [m for m in h.msgs if m.startswith("process_stall:")]
    check("executor: WARNING в лог", len(warns) == 1, h.msgs)
    bot.journal.close()
    print("test_executor_stall OK")


def test_screener_stall():
    jpath = TMP / "screener-stall.jsonl"
    jpath.unlink(missing_ok=True)
    s = ls.MidcapScreener(dry_run=True)
    s.journal = ls.Journal(str(jpath))
    s._last_monitor_tick = None

    check("screener: первый такт не считается паузой",
          s._check_process_stall(100.0) is None)
    check("screener: 20с — не пауза",
          s._check_process_stall(120.0) is None)

    out = io.StringIO()
    with redirect_stdout(out):
        gap = s._check_process_stall(152.0)

    check("screener: gap=32с обнаружен", gap == 32.0, gap)
    rows = [json.loads(line)
            for line in jpath.read_text(encoding="utf-8").splitlines()
            if line]
    check("screener: process_stall в журнале",
          [r["kind"] for r in rows] == ["process_stall"], rows)
    check("screener: gap_sec в событии", rows[0]["gap_sec"] == 32.0, rows[0])
    check("screener: WARNING в лог", "WARNING process_stall" in out.getvalue(),
          out.getvalue())
    s.journal.close()
    print("test_screener_stall OK")


def test_rest_timeout():
    client = E.BybitClient(E.BybitConfig(api_key="k", api_secret="s"))
    calls = []

    class Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"retCode": 0, "result": {"time": "0"}}

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(timeout)
        return Resp()

    def fake_post(url, data=None, timeout=None, headers=None):
        calls.append(timeout)
        return Resp()

    orig_get, orig_post = E.requests.get, E.requests.post
    E.requests.get, E.requests.post = fake_get, fake_post
    try:
        client._get("/v5/p")
        client._get("/v5/p", signed=True)
        client._post("/v5/p", {})
        client.server_time()
    finally:
        E.requests.get, E.requests.post = orig_get, orig_post

    check("REST: 4 запроса", len(calls) == 4, calls)
    check("REST: timeout=(3,7) у connect/read",
          all(t == (3, 7) for t in calls), calls)
    print("test_rest_timeout OK")


if __name__ == "__main__":
    test_executor_stall()
    test_screener_stall()
    test_rest_timeout()
    print("PROCESS_STALL OK")
