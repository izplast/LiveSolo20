"""Стоп: от worst-case средней всей лестницы DCA, один раз на входе.

Процент берётся из effective_stop_pct: при sl_atr_mult > 0 это
clamp(NATR * sl_atr_mult, sl_min_pct, sl_max_pct), иначе фиксированный
stop_pct (config hard_sl_pct). Средняя для стопа считается при условии,
что заполнятся вход и все плановые докупки; после DCA стоп не двигается.

Запуск:  python3 tests/test_stop_level_formula.py   (код 0 = ок)
"""
from exit_fake import make_bot, cyc, check

import executor as E


def bot_with(**pkw):
    p = E.DcaParams(**pkw)
    return make_bot(p=p)


# 1) фиксированный стоп (sl_atr_mult=0): 2.5% от средней, Buy/Sell
bot = bot_with(stop_pct=2.5, sl_atr_mult=0.0)
buy = cyc(side="Buy", avg_entry=0.004726941, natr=0.9)
sell = cyc(side="Sell", avg_entry=0.004726941, natr=0.9)
check("Buy: avg*(1-2.5%)",
      abs(bot._stop_level_for(buy) - 0.004726941 * 0.975) < 1e-15,
      bot._stop_level_for(buy))
check("Sell: avg*(1+2.5%)",
      abs(bot._stop_level_for(sell) - 0.004726941 * 1.025) < 1e-15,
      bot._stop_level_for(sell))

# 2) адаптивный стоп: KATUSDT natr=0.91834, mult=1.5 -> 1.37751%
bot2 = bot_with(entry_usdt=20.0, max_docups=2, multiplier=2.0,
                stop_pct=2.5, sl_atr_mult=1.5, sl_min_pct=1.0,
                sl_max_pct=8.0, step_atr_mult=1.0, step_min_pct=0.9,
                step_max_pct=3.0)
katu = cyc(side="Buy", avg_entry=0.00472694125590817, natr=0.9183425323500835)
pct = min(8.0, max(1.0, katu["natr"] * 1.5))
check("NATR-стоп: clamp(0.91834*1.5, 1, 8)=1.37751%",
      abs(pct - 1.3775137985251252) < 1e-12, pct)
check("stop_level = avg*(1-1.37751%) ≈ 0.0046618",
      abs(bot2._stop_level_for(katu) - 0.004661826987859858) < 1e-15,
      bot2._stop_level_for(katu))
check("это не цена последнего филла (0.004702)",
      abs(bot2._stop_level_for(katu) - 0.004702) > 1e-6,
      bot2._stop_level_for(katu))

# 3) worst-case план всей лестницы: стоп дальше последнего уровня
katu["qty"] = 4180.0
katu["qty_step"] = 1.0
katu["min_qty"] = 1.0
plan = bot2._build_stop_plan(katu, 0.00479, 4180.0)
check("план: 2 уровня DCA", len(plan["levels"]) == 2, plan["levels"])
check("план: стоп дальше последнего уровня",
      plan["stop_beyond_last"] and plan["stop_level"] < plan["last_level"],
      plan)
check("план: worst_avg ниже входа (Buy)",
      plan["worst_avg"] < 0.00479, plan["worst_avg"])
check("план: stop ≈ worst_avg*(1-1.37751%)",
      abs(plan["stop_level"] - plan["worst_avg"] * (1 - pct / 100)) < 1e-15,
      plan["stop_level"])

# 4) после докупки средняя меняется, но стоп уже зафиксирован
katu2 = dict(katu)
katu2["avg_entry"] = 0.004760585249801745
katu2["stop_avg_entry"] = plan["worst_avg"]
katu2["stop_level"] = plan["stop_level"]
check("после докупки stop_level не пересчитан",
      bot2._stop_level_for(katu2) == plan["stop_level"],
      bot2._stop_level_for(katu2))

# 5) stop_pct=0 и sl_atr_mult=0 -> стопа нет
bot3 = bot_with(stop_pct=0.0, sl_atr_mult=0.0)
check("стоп выключен -> None", bot3._stop_level_for(buy) is None,
      bot3._stop_level_for(buy))

print("STOP_LEVEL_FORMULA OK")
