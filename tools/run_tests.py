"""
tools/run_tests.py — единый автораннер проверок проекта.

Одна команда вместо ручного запуска каждого скрипта. Три секции (по умолчанию
все включены, каждая отключается флагом):

  --unit      все автотесты reference/: юнит-, интеграционные и A/B
              (reference/test_*.py), без сети, без pytest; сводка по фазам;
  --config    валидация config/config.yml на совпадение параметров с логикой
              бэктеста/бота: секция dca → DcaParams (build_params), секция
              screener → Config скринера (load_screener_cfg), согласованность
              leverage и границ NATR;
  --journal   разбор журналов Testnet (logs/bot-events.jsonl,
              logs/screener-events.jsonl и опционально logs/app.log):
              сводка reference/report.py, вердикты SC-001..SC-005, отчёт об
              ошибках (битые строки) и простоях (слепые интервалы).

Запуск:

    python3 tools/run_tests.py                          # всё
    python3 tools/run_tests.py --unit                   # только тесты
    python3 tools/run_tests.py --config                 # только config
    python3 tools/run_tests.py --journal                # только журналы
    python3 tools/run_tests.py --json                   # машиночитаемый вывод
    python3 tools/run_tests.py --ref-dir specs/001-bybit-dca-testnet/reference
    python3 tools/run_tests.py --config-path config/config.yml
    python3 tools/run_tests.py --journal logs/bot-events.jsonl logs/screener-events.jsonl
    python3 tools/run_tests.py --app-log logs/app.log

Код выхода: 0 — все включённые секции прошли (или «нет данных»), 1 — есть
провалы или ошибки исполнения.

Зависимостей нет: только стандартная библиотека. Модули reference-каталога
(ab_common, report) импортируются через importlib по пути, как в test_*.py,
и подменяют внешние зависимости заглушками (см. reference/backtest.py).
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
from typing import Any, Sequence

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
DEFAULT_REF_DIR = os.path.join(_REPO_ROOT, "specs", "001-bybit-dca-testnet",
                               "reference")
DEFAULT_CONFIG = os.path.join(_REPO_ROOT, "config", "config.yml")
DEFAULT_JOURNALS = [
    os.path.join(_REPO_ROOT, "logs", "bot-events.jsonl"),
    os.path.join(_REPO_ROOT, "logs", "screener-events.jsonl"),
]
DEFAULT_APP_LOG = os.path.join(_REPO_ROOT, "logs", "app.log")

# Критерии приёмки, проверяемые по журналам (остальные SC-* требуют ручной
# проверки или истории счёта и в автораннере только перечисляются).
SC_JOURNAL_CODES = ("SC-001", "SC-002", "SC-003", "SC-004", "SC-005")

# Фазы для сводки: тест → категория по имени файла.
def _phase(name: str) -> str:
    low = name.lower()
    if "ab" in low:
        return "A/B"
    if any(k in low for k in ("pipeline", "integration", "run_sim", "dca_integration")):
        return "интеграционные"
    return "юнит"


# ---------------------------------------------------------------------------
# Загрузка reference-модулей
# ---------------------------------------------------------------------------

def load_reference(name: str):
    """Загружает модуль reference-каталога по имени (без установки в sys.path)."""
    path = os.path.join(_REF_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден в reference-каталоге"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_REF_DIR = DEFAULT_REF_DIR


# ---------------------------------------------------------------------------
# Секция unit: прогон reference/test_*.py
# ---------------------------------------------------------------------------

def discover_tests(ref_dir: str) -> list[str]:
    """Список автотестов reference/ в алфавитном порядке."""
    return sorted(glob.glob(os.path.join(ref_dir, "test_*.py")))


def run_test_file(path: str, timeout: int = 600) -> dict[str, Any]:
    """Запускает один автотест subprocess-ом, возвращает результат."""
    try:
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=timeout,
        )
        return {"name": os.path.basename(path), "path": path,
                "rc": proc.returncode, "output": proc.stdout + proc.stderr}
    except subprocess.TimeoutExpired:
        return {"name": os.path.basename(path), "path": path,
                "rc": -1, "output": f"TIMEOUT (> {timeout} c)"}


def parse_test_totals(output: str) -> tuple[int | None, int | None]:
    """Итог теста из вывода: 'итог: N ok, M fail' → (N, M)."""
    m = re.search(r"итог:\s*(\d+)\s+ok,\s*(\d+)\s+fail", output, re.IGNORECASE)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def unit_section(ref_dir: str, timeout: int = 600) -> list[dict[str, Any]]:
    """Прогоняет все reference/test_*.py и возвращает результаты."""
    results = []
    for path in discover_tests(ref_dir):
        res = run_test_file(path, timeout)
        ok, fail = parse_test_totals(res["output"])
        res["ok"], res["fail"] = ok, fail
        res["passed"] = res["rc"] == 0
        results.append(res)
    return results


def unit_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Сводка по прогону: счётчики по фазам, всего ok/fail, провалы."""
    phases: dict[str, dict[str, int]] = {}
    total_ok = total_fail = 0
    failed: list[str] = []
    for r in results:
        phase = _phase(r["name"])
        ph = phases.setdefault(phase, {"files": 0, "ok": 0, "fail": 0})
        ph["files"] += 1
        if r.get("ok") is not None:
            ph["ok"] += r["ok"]
            ph["fail"] += r["fail"]
            total_ok += r["ok"]
            total_fail += r["fail"]
        if r.get("rc") != 0:
            out = (r.get("output") or "").strip()
            failed.append(f"{r['name']} (rc={r.get('rc')}, "
                          f"{out.splitlines()[-1] if out else ''})")
    return {"по_фазам": phases, "всего_ok": total_ok, "всего_fail": total_fail,
            "провалы": failed}


# ---------------------------------------------------------------------------
# Секция config: валидация config.yml против логики бэктеста/бота
# ---------------------------------------------------------------------------

def validate_config(config_path: str) -> list[dict[str, Any]]:
    """Проверки config.yml: парсинг секций, DcaParams, Config скринера,
    согласованность. Возвращает список {name, ok, detail}."""
    ab = load_reference("ab_common")
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    exists = os.path.exists(config_path)
    add("config.yml существует", exists, config_path)
    if not exists:
        return checks

    dca = ab.bc.read_dca_section(config_path)
    screener = ab.bc.read_section(config_path, "screener")
    add("секция dca разобрана", len(dca) > 0, f"{len(dca)} ключей")
    add("секция screener разобрана", len(screener) > 0, f"{len(screener)} ключей")

    try:
        params = ab.build_params(config_path)
        detail = (f"entry={params.entry_usdt} docups(steps)={params.max_docups} "
                  f"tp={params.tp_pct} sl={params.stop_pct} "
                  f"hold={params.max_hold_minutes}м lev={params.leverage}")
        add("dca → DcaParams валиден (логика бэктеста/бота)", True, detail)
    except Exception as e:
        add("dca → DcaParams валиден (логика бэктеста/бота)", False, str(e))

    try:
        cfg = ab.load_screener_cfg(config_path)
        detail = (f"natr {cfg.natr_min}–{cfg.natr_max}% "
                  f"tf {cfg.tf_fast}/{cfg.tf_slow} lev {cfg.required_leverage}")
        add("screener → Config валиден", True, detail)
    except Exception as e:
        add("screener → Config валиден", False, str(e))

    lev = dca.get("leverage")
    req = screener.get("required_leverage")
    add("dca.leverage == screener.required_leverage", lev == req, f"{lev} vs {req}")

    nmin, nmax = screener.get("natr_min"), screener.get("natr_max")
    ok_bounds = (isinstance(nmin, (int, float)) and isinstance(nmax, (int, float))
                 and nmin < nmax)
    add("screener.natr_min < natr_max", ok_bounds, f"{nmin} vs {nmax}")
    return checks


# ---------------------------------------------------------------------------
# Секция journal: разбор журналов Testnet и SC-вердикты
# ---------------------------------------------------------------------------

def scan_text_log(path: str) -> dict[str, Any]:
    """Грубая проверка текстового лога: ошибки и упоминания простоев."""
    if not os.path.exists(path):
        return {"file": path, "found": False}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {"file": path, "found": False}
    text = "\n".join(lines)
    return {
        "file": path, "found": True, "строк": len(lines),
        "error_строк": sum(1 for l in lines if any(
            k in l for k in ("ERROR", "CRITICAL", "Traceback"))),
        "stream_down": text.count("stream_down"),
        "stream_up": text.count("stream_up"),
    }


def journal_section(journal_paths: Sequence[str],
                    app_log: str | None = None) -> dict[str, Any]:
    """Сводка по журналам Testnet: SC-вердикты, ошибки, простои."""
    rp = load_reference("report")
    scanned = scan_text_log(app_log) if app_log else None
    existing = [p for p in journal_paths if os.path.exists(p)]
    if not existing:
        return {"journals": list(journal_paths), "found": False, "error": None,
                "verdicts": [], "broken": 0, "app_log": scanned, "summary": None}
    events, broken = rp.read_events(existing)
    summary = rp.build_summary(events)
    verdicts = [v for v in summary.get("критерии", [])
                if v["код"] in SC_JOURNAL_CODES]
    return {
        "journals": existing, "found": True, "error": summary.get("error"),
        "verdicts": verdicts, "broken": broken,
        "app_log": scanned,
        "summary": summary,
    }


def journal_ok(res: dict[str, Any]) -> bool:
    """Секция journal прошла: журналы найдены, сводка построена, ни один
    проверяемый SC-критерий не «НЕ выполнен»."""
    if not res["found"]:
        return True  # нет журналов — «нет данных», а не провал
    if res["error"]:
        return False
    return all(v["вердикт"] != "НЕ выполнен" for v in res["verdicts"])


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def _mark(ok: bool) -> str:
    return "ok" if ok else "FAIL"


def render_unit(results: Sequence[dict[str, Any]]) -> str:
    s = unit_summary(results)
    lines = ["Юнит / интеграционные / A-B тесты (reference/test_*.py)"]
    for r in results:
        counts = f"{r['ok']} ok, {r['fail']} fail" if r["ok"] is not None else "нет итога"
        lines.append(f"  [{_mark(r['passed'])}] {r['name']:<40} {counts}")
    lines.append("")
    for phase, ph in sorted(s["по_фазам"].items()):
        lines.append(f"  {phase:<18} файлов {ph['files']:>2}, "
                     f"{ph['ok']} ok, {ph['fail']} fail")
    lines.append(f"  Итого: {s['всего_ok']} ok, {s['всего_fail']} fail")
    if s["провалы"]:
        lines.append("  Провалы:")
        for f in s["провалы"]:
            lines.append(f"    {f}")
    return "\n".join(lines)


def render_config(checks: Sequence[dict[str, Any]]) -> str:
    lines = ["Валидация config/config.yml (параметры vs логика бэктеста/бота)"]
    for c in checks:
        tail = f"  [{c['detail']}]" if c["detail"] else ""
        lines.append(f"  [{_mark(c['ok'])}] {c['name']}{tail}")
    return "\n".join(lines)


def render_journal(res: dict[str, Any]) -> str:
    lines = ["Журналы Testnet (SC-001..SC-005)"]
    if not res["found"]:
        lines.append("  журналы не найдены — «нет данных» по прогону")
        for p in res["journals"]:
            lines.append(f"    не найден: {p}")
        if res["app_log"] and res["app_log"]["found"]:
            a = res["app_log"]
            lines.append(f"  app.log: {a['строк']} строк, ошибок {a['error_строк']}, "
                         f"stream_down/up {a['stream_down']}/{a['stream_up']}")
        return "\n".join(lines)
    if res["error"]:
        lines.append(f"  [FAIL] сводка не построена: {res['error']}")
        return "\n".join(lines)
    if res["broken"]:
        lines.append(f"  [FAIL] битых строк в журналах: {res['broken']}")
    for v in res["verdicts"]:
        mark = {"выполнен": "ok", "НЕ выполнен": "FAIL",
                "нет данных": "нет данных"}.get(v["вердикт"], "?")
        lines.append(f"  [{mark}] {v['код']}  {v['порог']}")
        lines.append(f"           измерено: {v['измерено']}")
    d = (res["summary"] or {}).get("недоступность_потока")
    if d:
        lines.append(f"  Простои: интервалов {d['интервалов']} "
                     f"(незакрытых {d['незакрытых']}), суммарно {d['суммарно_мс']} мс "
                     f"= {d['доля_прогона'] * 100:.3f}% прогона")
    if res["app_log"] and res["app_log"]["found"]:
        a = res["app_log"]
        lines.append(f"  app.log: {a['строк']} строк, ошибок {a['error_строк']}, "
                     f"stream_down/up {a['stream_down']}/{a['stream_up']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_sections(args: argparse.Namespace) -> dict[str, Any]:
    global _REF_DIR
    _REF_DIR = args.ref_dir
    out: dict[str, Any] = {}
    if args.unit:
        out["unit"] = unit_section(args.ref_dir, timeout=args.timeout)
    if args.config:
        out["config"] = validate_config(args.config_path)
    if args.journal:
        out["journal"] = journal_section(args.journals, app_log=args.app_log)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Единый автораннер проверок проекта")
    ap.add_argument("--unit", action="store_true",
                    help="прогнать все reference/test_*.py")
    ap.add_argument("--config", action="store_true",
                    help="валидировать config.yml против логики бэктеста/бота")
    ap.add_argument("--journal", action="store_true",
                    help="разобрать журналы Testnet, вердикты SC-001..SC-005")
    ap.add_argument("--ref-dir", default=DEFAULT_REF_DIR,
                    help=f"reference-каталог (по умолчанию {DEFAULT_REF_DIR})")
    ap.add_argument("--config-path", default=DEFAULT_CONFIG,
                    help=f"путь к config.yml (по умолчанию {DEFAULT_CONFIG})")
    ap.add_argument("--journal-files", nargs="+", default=None,
                    help="файлы журналов JSONL (по умолчанию из config)")
    ap.add_argument("--app-log", default=None,
                    help="текстовый лог процесса (например logs/app.log)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="таймаут одного теста, секунд (по умолчанию 600)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args(argv)

    # Если секции не указаны — запускаем все.
    if not (args.unit or args.config or args.journal):
        args.unit = args.config = args.journal = True

    # Журналы по умолчанию: из config.yml, иначе стандартные пути.
    if args.journal_files is None:
        args.journals = list(DEFAULT_JOURNALS)
        if os.path.exists(args.config_path):
            bc = load_reference("bot_config")
            bot = bc.read_section(args.config_path, "bot")
            sc = bc.read_section(args.config_path, "screener")
            args.journals = []
            for raw in (bot.get("journal_path"), sc.get("journal_path")):
                if isinstance(raw, str):
                    args.journals.append(os.path.join(_REPO_ROOT, raw))
            if not args.journals:
                args.journals = list(DEFAULT_JOURNALS)
    else:
        args.journals = list(args.journal_files)

    results = run_sections(args)

    if args.json:
        payload: dict[str, Any] = {}
        if "unit" in results:
            payload["unit"] = unit_summary(results["unit"])
        if "config" in results:
            payload["config"] = results["config"]
        if "journal" in results:
            j = results["journal"]
            payload["journal"] = {
                "found": j["found"], "error": j["error"],
                "broken": j["broken"], "verdicts": j["verdicts"],
                "app_log": j["app_log"],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        sections = [("unit", render_unit), ("config", render_config),
                    ("journal", render_journal)]
        for key, render in sections:
            if key in results:
                print(render(results[key]))
                print()

    # Итоговый код выхода.
    failed = False
    if "unit" in results:
        failed |= any(not r["passed"] for r in results["unit"])
    if "config" in results:
        failed |= any(not c["ok"] for c in results["config"])
    if "journal" in results:
        failed |= not journal_ok(results["journal"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())