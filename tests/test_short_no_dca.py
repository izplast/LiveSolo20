"""Вариант B: лестница 20/40 для лонгов и один вход для шортов.

Проверяем конфиг (steps=1 → 2 уровня, short_dca_enabled=false), стоп-план
от одного шортового входа и отсутствие DCA-ордера после шортового филла.

Запуск:  python3 tests/test_short_no_dca.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import bot_config
import executor as E


dca = bot_config.read_dca_section("config/config.yml")
p = bot_config.dca_params_from_config(dca)
check("config: steps=1 (два уровня B)", p.max_docups == 1, p.max_docups)
check("config: short_dca_enabled=false",
      dca.get("short_dca_enabled") is False, dca)
check("config: лестница B = 60 USDT",
      abs(E.planned_ladder_notional(p) - 60.0) < 1e-9,
      E.planned_ladder_notional(p))

now = int(time.time() * 1000)
bot = make_bot(FakeClient(), p=p)
bot.short_dca_enabled = False
short = cyc(side="Sell", qty=1.0, avg_entry=100.0, natr=1.0)
plan = bot._build_stop_plan(short, 100.0, 1.0)
check("short: нет уровней DCA", plan and plan["levels"] == [], plan)
stop_pct = E.effective_stop_pct(p, 1.0)
check("short: стоп от одного входа",
      plan and abs(plan["stop_level"] - 100.0 * (1 + stop_pct / 100)) < 1e-9,
      plan)
check("short: стоп выше входа", plan and plan["stop_level"] > 100.0, plan)

short["stop_level"] = plan["stop_level"]
short["tp_link"] = None
short["so_link"] = None
short["so_seq"] = 0
bot._after_position_change(short, now)
check("short: после входа нет DCA-ордера",
      not [k for k in short["links"] if "_so" in k], short["links"])

long = cyc(side="Buy", qty=1.0, avg_entry=100.0, natr=1.0)
plan_long = bot._build_stop_plan(long, 100.0, 1.0)
check("long: один уровень DCA", plan_long and len(plan_long["levels"]) == 1,
      plan_long)
long["stop_level"] = plan_long["stop_level"]
long["tp_link"] = None
long["so_link"] = None
long["so_seq"] = 0
bot._after_position_change(long, now)
check("long: DCA-ордер выставлен",
      bool([k for k in long["links"] if "_so1" in k]), long["links"])

print("SHORT_NO_DCA OK")
