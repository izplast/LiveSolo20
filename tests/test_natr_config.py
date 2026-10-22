"""NATR-пороги: config.yml → CLI override → дефолт; фактические значения старта.

Скринер читает natr_min/natr_max из секции screener config.yml. Явные
--natr-min/--natr-max имеют приоритет; без файла/ключей берутся DEFAULT_NATR_*.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_screener_midcap as ls

TMP = Path("/tmp/opencode/natr-config-test")
TMP.mkdir(parents=True, exist_ok=True)


def _write_cfg(name, min_v, max_v):
    p = TMP / name
    p.write_text(
        "screener:\n"
        f"  natr_min: {min_v}\n"
        f"  natr_max: {max_v}\n",
        encoding="utf-8",
    )
    return p


def test_config_values_used():
    p = _write_cfg("config.yml", 0.9, 1.5)
    args = ls.parse_args(["--config", str(p)])
    assert args.natr_min is None and args.natr_max is None
    cfg = ls.load_screener_config(str(p))
    assert ls.resolve_natr_thresholds(args, cfg) == (0.9, 1.5)
    assert ls.natr_source(args, cfg, "natr_min") == "config"
    assert ls.natr_source(args, cfg, "natr_max") == "config"
    print("test_config_values_used OK")


def test_cli_overrides_config():
    p = _write_cfg("override.yml", 0.9, 1.5)
    args = ls.parse_args(["--config", str(p), "--natr-min", "1.1", "--natr-max", "2.2"])
    cfg = ls.load_screener_config(str(p))
    assert ls.resolve_natr_thresholds(args, cfg) == (1.1, 2.2)
    assert ls.natr_source(args, cfg, "natr_min") == "CLI"
    assert ls.natr_source(args, cfg, "natr_max") == "CLI"
    print("test_cli_overrides_config OK")


def test_defaults_without_config():
    missing = TMP / "missing.yml"
    args = ls.parse_args(["--config", str(missing)])
    cfg = ls.load_screener_config(str(missing))
    assert cfg == {}
    assert ls.resolve_natr_thresholds(args, cfg) == (ls.DEFAULT_NATR_MIN, ls.DEFAULT_NATR_MAX)
    print("test_defaults_without_config OK")


def test_repo_config_has_working_values():
    cfg = ls.load_screener_config("config/config.yml")
    assert cfg.get("natr_min") == 0.9, cfg.get("natr_min")
    assert cfg.get("natr_max") == 1.5, cfg.get("natr_max")
    print("test_repo_config_has_working_values OK")


if __name__ == "__main__":
    test_config_values_used()
    test_cli_overrides_config()
    test_defaults_without_config()
    test_repo_config_has_working_values()
    print("NATR CONFIG OK")
