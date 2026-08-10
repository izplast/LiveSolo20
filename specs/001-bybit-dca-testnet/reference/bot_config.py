"""
reference/bot_config.py — конфиг бота (config/config.yml, секция dca) → DcaParams.

Живой бот хранит параметры в своих именах (steps, hard_sl_pct, max_hold_hours),
а симулятор/бэктест — в backtest.DcaParams (max_docups, stop_pct,
max_hold_minutes). Этот модуль — маппер между ними, плюс минимальный парсер
секции dca без yaml (плоский блок «ключ: значение» с inline-комментариями и
списками), чтобы симулятор гонял ровно те настройки, что и живой бот.

Маппинг:
  entry_usdt        → entry_usdt
  step_pct          → dca_step_pct
  steps             → max_docups
  multiplier        → multiplier
  take_profit_pct   → tp_pct
  tp_escalation     → tp_escalation
  hard_sl_pct       → stop_pct
  max_hold_hours    → max_hold_minutes (×60)
  leverage          → leverage
  fee_rate          → fee_rate

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_bot_config.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с bot_config.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


backtest = _load_sibling("backtest")
DcaParams = backtest.DcaParams

# Значения по умолчанию DcaParams для ключей, которых нет в конфиге бота.
DEFAULTS = {
    "entry_usdt": 50.0,
    "step_pct": 0.8,
    "steps": 3,
    "multiplier": 2.0,
    "take_profit_pct": 1.0,
    "tp_escalation": (),
    "hard_sl_pct": 0.0,
    "max_hold_hours": 4.0,
    "leverage": 3.0,
    "fee_rate": 0.00055,
}


def _parse_scalar(value: str):
    """Скаляр из «ключ: значение»: int, float, bool, список, строка."""
    value = value.strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_parse_scalar(x) for x in inner.split(",")] if inner else []
    if value[0] in "'\"":
        return value[1:-1]
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def read_section(path: str, section: str) -> dict:
    """Плоская секция из YAML-подобного файла (без yaml): {ключ: значение}.

    Понимает только блоки вида «ключ: значение» с инлайн-комментариями и
    списками — ровно то, что нужно симулятору, чтобы гонять те же настройки,
    что и живой бот, не таская за собой pyyaml.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {}

    out: dict = {}
    in_section = False
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and stripped.endswith(":"):
            in_section = stripped == f"{section}:"
            continue
        if not in_section:
            continue
        if "#" in line:
            line = line.split("#", 1)[0].rstrip()
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = _parse_scalar(value)
    return out


def read_dca_section(path: str) -> dict:
    """Секция dca из плоского YAML-файла. Отсутствие файла — пустой словарь."""
    return read_section(path, "dca")


def dca_params_from_config(dca: dict) -> DcaParams:
    """Словарь секции dca (из read_dca_section или теста) → валидный DcaParams."""
    p = DcaParams(
        entry_usdt=dca.get("entry_usdt", DEFAULTS["entry_usdt"]),
        dca_step_pct=dca.get("step_pct", DEFAULTS["step_pct"]),
        max_docups=int(dca.get("steps", DEFAULTS["steps"])),
        multiplier=float(dca.get("multiplier", DEFAULTS["multiplier"])),
        tp_pct=float(dca.get("take_profit_pct", DEFAULTS["take_profit_pct"])),
        tp_escalation=tuple(
            float(x) for x in dca.get("tp_escalation", DEFAULTS["tp_escalation"])),
        stop_pct=float(dca.get("hard_sl_pct", DEFAULTS["hard_sl_pct"])),
        max_hold_minutes=int(float(dca.get("max_hold_hours",
                                           DEFAULTS["max_hold_hours"])) * 60),
        leverage=float(dca.get("leverage", DEFAULTS["leverage"])),
        fee_rate=float(dca.get("fee_rate", DEFAULTS["fee_rate"])),
    )
    p.validate()
    return p
