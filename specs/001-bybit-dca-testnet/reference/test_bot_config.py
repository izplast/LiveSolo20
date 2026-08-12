"""
Автономные проверки маппера конфига бота (reference/bot_config.py).

Без сети: разбор реального config/config.yml, маппинг секции dca → DcaParams,
обобщённый read_section для произвольной секции (в т.ч. screener).

Без зависимостей (yaml не нужен): парсер секции dca из config/config.yml,
маппинг имён бота → DcaParams, дефолты и валидация.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_bot_config.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

_ref = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, _ref)


def _load(name: str):
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bc = _load("bot_config")  # подтягивает backtest

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


REPO_CONFIG = os.path.abspath(os.path.join(_ref, "..", "..", "..",
                                           "config", "config.yml"))

# ── парсер скаляров ───────────────────────────────────────────────────────────

print("parse_scalar")
ok("целое число", bc._parse_scalar("2") == 2)
ok("дробное число", bc._parse_scalar("1.5") == 1.5)
ok("список чисел", bc._parse_scalar("[1.2, 1.5, 2.0]") == [1.2, 1.5, 2.0])
ok("пустой список", bc._parse_scalar("[]") == [])
ok("строковый булев", bc._parse_scalar("true") is True)
ok("строка в кавычках", bc._parse_scalar('"1"') == "1")

# ── парсер секции dca из реального config.yml ────────────────────────────────

print("\nread_dca_section(config/config.yml)")
ok("конфиг существует в зеркале", os.path.exists(REPO_CONFIG), REPO_CONFIG)
_dca = bc.read_dca_section(REPO_CONFIG)
ok("секция dca разобрана", isinstance(_dca, dict) and len(_dca) > 5, _dca)
ok("числовые значения — числа (не строки)",
   _dca.get("entry_usdt") == 20 and _dca.get("steps") == 2
   and _dca.get("max_hold_hours") == 1.5, _dca)
ok("tp_escalation из конфига — список чисел",
   _dca.get("tp_escalation") == [1.2, 1.5, 2.0], _dca.get("tp_escalation"))
ok("ключи других секций (screener/bot) не попадают в dca",
   "bot_api_url" not in _dca and "monitor_interval_sec" not in _dca)
ok("отсутствующий файл → пустой словарь (не падение)",
   bc.read_dca_section("/no/such/file.yml") == {})

# ── обобщённый read_section: произвольная секция (screener) ──────────────────

print("\nread_section: секция screener из config.yml")
_screener = bc.read_section(REPO_CONFIG, "screener")
ok("секция screener разобрана", isinstance(_screener, dict) and len(_screener) > 5,
   _screener)
ok("числовые пороги — числа", _screener.get("natr_min") == 0.95
   and _screener.get("natr_max") == 5.0, _screener)
ok("tf остаются строками", _screener.get("tf_fast") == "1"
   and _screener.get("tf_slow") == "15", _screener)
ok("ключи секции dca не попадают в screener",
   "entry_usdt" not in _screener and "tp_escalation" not in _screener)
ok("отсутствующий файл → пустой словарь", bc.read_section("/no/such/file.yml",
                                                            "screener") == {})

# ── маппинг имён бота → DcaParams ─────────────────────────────────────────────

print("\ndca_params_from_config: маппинг")
p = bc.dca_params_from_config(_dca)
ok("конфиг бота → валидный DcaParams", p.validate() is None)
ok("entry_usdt → entry_usdt", p.entry_usdt == 20)
ok("step_pct → dca_step_pct", p.dca_step_pct == 1.2)
ok("steps → max_docups", p.max_docups == 2)
ok("multiplier → multiplier", p.multiplier == 2.0)
ok("take_profit_pct → tp_pct", p.tp_pct == 1.2)
ok("tp_escalation → кортеж", p.tp_escalation == (1.2, 1.5, 2.0))
ok("hard_sl_pct → stop_pct", p.stop_pct == 5.0)
ok("max_hold_hours → max_hold_minutes (×60)", p.max_hold_minutes == 90)
ok("leverage → leverage", p.leverage == 4)

print("\ndca_params_from_config: дефолты и валидация")
pd = bc.dca_params_from_config({})
ok("пустой конфиг → валидные дефолты",
   pd.max_docups == 3 and pd.stop_pct == 0.0
   and pd.tp_escalation == () and pd.max_hold_minutes == 240, pd)
ok("отсутствие эскалации → фиксированный TP (фолбэк)",
   bc.dca_params_from_config({"steps": 0}).tp_escalation == ())
ok("негативные steps отвергаются валидацией",
   raises(lambda: bc.dca_params_from_config({"steps": -1})))
ok("стоп >= 50% отвергается валидацией",
   raises(lambda: bc.dca_params_from_config({"hard_sl_pct": 50.0})))

print("\nитог: {} ok, {} fail".format(PASS, FAIL))
sys.exit(1 if FAIL else 0)
