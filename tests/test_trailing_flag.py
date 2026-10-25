"""Трейлинг выключен флагом config.yml, код сохранён.

Запуск:  python3 tests/test_trailing_flag.py   (код 0 = ок)
"""
import os
import tempfile
from types import SimpleNamespace

from exit_fake import FakeClient, check

import executor as E


def params():
    return E.DcaParams(entry_usdt=20.0, max_docups=2, multiplier=2.0,
                       dca_step_pct=1.2, stop_pct=2.5, tp_pct=1.2,
                       max_hold_minutes=90, leverage=4.0)


def make_real_bot(enabled: bool):
    fd, journal = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    bot = E.DcaBot(
        p=params(), bp=SimpleNamespace(), client=FakeClient(),
        mode="paper", journal_path=journal,
        state_path=journal + ".state", screener_path=journal,
        trail_trigger_pct=1.5, trail_step_pct=0.5,
        trailing_enabled=enabled)
    return bot


bot = make_real_bot(False)
check("trailing_enabled=False", bot.trailing_enabled is False)
check("trail_trigger обнулён", bot.trail_trigger == 0.0, bot.trail_trigger)
check("trail_step обнулён", bot.trail_step == 0.0, bot.trail_step)

bot2 = make_real_bot(True)
check("trailing_enabled=True сохраняет trigger", bot2.trail_trigger == 1.5,
      bot2.trail_trigger)
check("trailing_enabled=True сохраняет step", bot2.trail_step == 0.5,
      bot2.trail_step)

cfg = E.bot_config.read_dca_section("config/config.yml")
check("config: trailing_enabled=false", cfg.get("trailing_enabled") is False,
      cfg.get("trailing_enabled"))

print("TRAILING_FLAG OK")
