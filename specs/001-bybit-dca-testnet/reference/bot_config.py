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
  step_atr_mult     → step_atr_mult (адаптивный шаг от NATR, 0 — фиксированный step_pct)
  step_min_pct      → step_min_pct  (нижний зажим адаптивного шага, %)
  step_max_pct      → step_max_pct  (верхний зажим адаптивного шага, %)
  sl_atr_mult       → sl_atr_mult   (адаптивный стоп от NATR, 0 — фиксированный hard_sl_pct)
  sl_min_pct        → sl_min_pct    (нижний зажим адаптивного стопа, %)
  sl_max_pct        → sl_max_pct    (верхний зажим адаптивного стопа, %)
  max_cycle_loss_usdt → max_cycle_loss_usdt (жёсткий лимит убытка цикла в USDT)

Запуск проверок:
    python3 specs/001-bybit-dca-testnet/reference/test_bot_config.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field

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

resilience = _load_sibling("resilience")
notifier = _load_sibling("notifier")
TelegramParams = notifier.TelegramParams

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


@dataclass
class BotParams:
    """Секция bot конфига: сопровождение циклов и старт процесса (FR-015, FR-032)."""

    max_cycles: int = 3            # одновременно открытых циклов
    monitor_interval_sec: int = 3  # такт сопровождения циклов
    fill_timeout_ms: int = 5000    # сколько ждать подтверждения исполнения
    max_clock_skew_ms: int = 3000  # больше — старт запрещён (FR-032)
    heartbeat_sec: int = 60        # периодичность журнала/файла инбокса
    autostart: bool = True         # принимать сигналы сразу после старта
    journal_path: str = "logs/bot-events.jsonl"
    # Частота вычитывания журнала сигналов, с. Малое значение режет задержку
    # сигнал→вход (SC-003): такт монитора добавлял к ней в среднем полтакта.
    signal_poll_sec: float = 0.5

    def validate(self) -> None:
        problems = []
        if self.max_cycles < 1:
            problems.append("max_cycles >= 1")
        if self.monitor_interval_sec < 1:
            problems.append("monitor_interval_sec >= 1")
        if self.fill_timeout_ms < 0:
            problems.append("fill_timeout_ms >= 0")
        if self.max_clock_skew_ms < 0:
            problems.append("max_clock_skew_ms >= 0")
        if self.heartbeat_sec < 1:
            problems.append("heartbeat_sec >= 1")
        if not self.journal_path:
            problems.append("journal_path не пуст")
        if not 0.05 <= self.signal_poll_sec <= self.monitor_interval_sec:
            problems.append("signal_poll_sec в пределах "
                            "0.05..monitor_interval_sec")
        if problems:
            raise ValueError("некорректная секция bot:\n- " + "\n- ".join(problems))


# Значения по умолчанию секции bot (применяются только при отсутствии файла).
BOT_DEFAULTS = {
    "max_cycles": 3,
    "monitor_interval_sec": 3,
    "fill_timeout_ms": 5000,
    "max_clock_skew_ms": 3000,
    "heartbeat_sec": 60,
    "autostart": True,
    "journal_path": "logs/bot-events.jsonl",
    "signal_poll_sec": 0.5,
}

# Обязательные ключи при существующем файле: их отсутствие — расхождение
# «конфиг vs логика», а не повод для фолбэка на дефолт.
REQUIRED_DCA_KEYS = ("entry_usdt", "step_pct", "steps", "take_profit_pct",
                     "max_hold_hours", "leverage")
REQUIRED_BOT_KEYS = ("max_cycles", "monitor_interval_sec",
                     "max_clock_skew_ms", "heartbeat_sec")
REQUIRED_SCREENER_KEYS = ("natr_min", "natr_max", "required_leverage")


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
    step_atr_mult=float(dca.get("step_atr_mult", 0.0)),
    step_min_pct=float(dca.get("step_min_pct", 0.0)),
    step_max_pct=float(dca.get("step_max_pct", 0.0)),
    sl_atr_mult=float(dca.get("sl_atr_mult", 0.0)),
    sl_min_pct=float(dca.get("sl_min_pct", 0.0)),
    sl_max_pct=float(dca.get("sl_max_pct", 0.0)),
    max_cycle_loss_usdt=float(dca.get("max_cycle_loss_usdt", 0.0)),
)
    p.validate()
    return p


def bot_params_from_config(bot: dict) -> BotParams:
    """Словарь секции bot → валидный BotParams. Дефолты — только для ключей,
    которых нет в конфиге (при наличии файла полнота проверяется отдельно)."""
    p = BotParams(
        max_cycles=int(bot.get("max_cycles", BOT_DEFAULTS["max_cycles"])),
        monitor_interval_sec=int(bot.get(
            "monitor_interval_sec", BOT_DEFAULTS["monitor_interval_sec"])),
        fill_timeout_ms=int(bot.get(
            "fill_timeout_ms", BOT_DEFAULTS["fill_timeout_ms"])),
        max_clock_skew_ms=int(bot.get(
            "max_clock_skew_ms", BOT_DEFAULTS["max_clock_skew_ms"])),
        heartbeat_sec=int(bot.get("heartbeat_sec", BOT_DEFAULTS["heartbeat_sec"])),
        autostart=bool(bot.get("autostart", BOT_DEFAULTS["autostart"])),
        journal_path=str(bot.get("journal_path", BOT_DEFAULTS["journal_path"])),
        signal_poll_sec=float(bot.get(
            "signal_poll_sec", BOT_DEFAULTS["signal_poll_sec"])),
    )
    p.validate()
    return p


def telegram_params_from_config(telegram: dict) -> TelegramParams:
    """Словарь секции telegram → валидный TelegramParams.

    enabled=False (по умолчанию) не требует token/chat_id — секция может быть
    пустой. При enabled=True отсутствие bot_token/chat_id — расхождение.
    """
    p = TelegramParams(
        bot_token=str(telegram.get("bot_token", "")),
        chat_id=str(telegram.get("chat_id", "")),
        enabled=bool(telegram.get("enabled", False)),
        parse_mode=str(telegram.get("parse_mode", "HTML")),
    )
    p.validate()
    return p


def load_screener_cfg(config_path: str):
    """Конфиг скринера из config.yml (секция screener) → Config.

    Единая точка загрузки для бота, симулятора и бэктеста (ранее — дубли
    в ab_common/run_sim). Незнакомые ключи (блэклисты, pump-фильтры) игнорируются:
    их знает только живой скринер.
    """
    sc = _load_sibling("screener")
    raw = read_section(config_path, "screener")
    cfg = sc.Config()
    known = {f.name for f in sc.Config.__dataclass_fields__.values()}
    for k, v in raw.items():
        if k in known:
            setattr(cfg, k, v)
    sc.validate_config(cfg)
    return cfg


def validate_config(config_path: str) -> list[dict]:
    """Полная проверка config.yml на старте бота (T027): секции dca/bot/screener,
    полнота обязательных ключей, маппинг в DcaParams/BotParams/Config скринера,
    консистентность leverage и NATR.

    Возвращает список {name, ok, detail}. Фолбэки на дефолты допустимы только
    при отсутствии файла: если файл есть, но обязательный ключ не задан — FAIL,
    чтобы расхождение «конфиг vs логика» не оставалось незамеченным.
    """
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    exists = os.path.exists(config_path)
    add("config.yml существует", exists, config_path)
    if not exists:
        return checks  # фолбэки на дефолты — допустимы

    dca = read_dca_section(config_path)
    bot = read_section(config_path, "bot")
    screener = read_section(config_path, "screener")
    telegram = read_section(config_path, "telegram")

    add("секция dca разобрана", len(dca) > 0, f"{len(dca)} ключей")
    add("секция bot разобрана", len(bot) > 0, f"{len(bot)} ключей")
    add("секция screener разобрана", len(screener) > 0, f"{len(screener)} ключей")

    if "enabled" not in telegram:
        add("секция telegram разобрана", False, "нет ключа enabled")
    else:
        add("секция telegram разобрана", True, f"{len(telegram)} ключей")

    missing_dca = [k for k in REQUIRED_DCA_KEYS if k not in dca]
    add("dca: обязательные ключи заданы", not missing_dca,
        ", ".join(missing_dca) if missing_dca else f"{len(REQUIRED_DCA_KEYS)} ключей")
    missing_bot = [k for k in REQUIRED_BOT_KEYS if k not in bot]
    add("bot: обязательные ключи заданы", not missing_bot,
        ", ".join(missing_bot) if missing_bot else f"{len(REQUIRED_BOT_KEYS)} ключей")
    missing_sc = [k for k in REQUIRED_SCREENER_KEYS if k not in screener]
    add("screener: обязательные ключи заданы", not missing_sc,
        ", ".join(missing_sc) if missing_sc else f"{len(REQUIRED_SCREENER_KEYS)} ключей")

    try:
        p = dca_params_from_config(dca)
        detail = (f"entry={p.entry_usdt} docups(steps)={p.max_docups} "
                  f"tp={p.tp_pct} sl={p.stop_pct} hold={p.max_hold_minutes}м "
                  f"lev={p.leverage}")
        add("dca → DcaParams валиден (логика бэктеста/бота)", True, detail)
    except Exception as e:
        add("dca → DcaParams валиден (логика бэктеста/бота)", False, str(e))

    try:
        bp = bot_params_from_config(bot)
        detail = (f"max_cycles={bp.max_cycles} monitor={bp.monitor_interval_sec}с "
                  f"skew={bp.max_clock_skew_ms}мс heartbeat={bp.heartbeat_sec}с")
        add("bot → BotParams валиден", True, detail)
    except Exception as e:
        add("bot → BotParams валиден", False, str(e))

    try:
        cfg = load_screener_cfg(config_path)
        detail = (f"natr {cfg.natr_min}–{cfg.natr_max}% tf {cfg.tf_fast}/{cfg.tf_slow} "
                  f"lev {cfg.required_leverage}")
        add("screener → Config валиден", True, detail)
    except Exception as e:
        add("screener → Config валиден", False, str(e))

    try:
        tp_ = telegram_params_from_config(telegram)
        detail = (f"enabled={tp_.enabled} mode={tp_.parse_mode} "
                  f"chat={tp_.chat_id[:12] or '—'}…")
        add("telegram → TelegramParams валиден", True, detail)
    except Exception as e:
        add("telegram → TelegramParams валиден", False, str(e))

    lev = dca.get("leverage")
    req = screener.get("required_leverage")
    add("dca.leverage == screener.required_leverage", lev == req, f"{lev} vs {req}")

    nmin, nmax = screener.get("natr_min"), screener.get("natr_max")
    ok_bounds = (isinstance(nmin, (int, float)) and isinstance(nmax, (int, float))
                 and nmin < nmax)
    add("screener.natr_min < natr_max", ok_bounds, f"{nmin} vs {nmax}")
    return checks
