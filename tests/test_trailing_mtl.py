"""Трейлинг: лимитка ниже рынка не активирует его мгновенно.

Buy-лимитка исполнилась по 100, а mark на филле уже 102 (+2% > trigger 1.5%).
Пик должен стать 102, но активация — только после нового экстремума.
Также проверяем шаг max(0.5%, 0.5×NATR).

Запуск:  python3 tests/test_trailing_mtl.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


def params():
    return E.DcaParams(entry_usdt=20.0, max_docups=2, multiplier=2.0,
                       dca_step_pct=1.2, step_atr_mult=0.0,
                       stop_pct=2.5, sl_atr_mult=0.0, tp_pct=1.2,
                       max_hold_minutes=90, leverage=4.0)


now = int(time.time() * 1000)
client = FakeClient()
client.mark = 102.0
bot = make_bot(client, p=params())
bot.trail_trigger, bot.trail_step = 1.5, 0.5

c = cyc(side="Buy", qty=1.0, avg_entry=100.0, natr=1.0,
        stop_level=97.5, trail_active=False, trail_peak=None)
c.update({"qty_step": 0.1, "min_qty": 0.1, "tick_size": 0.01,
          "price_testnet": 100.0, "links": {}, "fills": {},
          "tp_link": None, "so_link": None, "so_seq": 0})
bot.cycles[c["cycle_id"]] = c

bot._on_fill(c, "t1", "entry", 1.0, 100.0, 0.0, now,
             {"price": "100", "orderId": "e1"})
check("MTL: fill_price сохранён", c["trail_fill_price"] == 100.0,
      c["trail_fill_price"])
check("MTL: mark на филле сохранён", c["trail_mark_at_fill"] == 102.0,
      c["trail_mark_at_fill"])
check("MTL: peak = max(fill, mark)", c["trail_peak"] == 102.0,
      c["trail_peak"])
check("MTL: нет мгновенной активации", c["trail_active"] is False,
      c["trail_active"])

bot._check_exits(c, now + 1)
check("MTL: mark на месте не активирует", c["trail_active"] is False,
      c["trail_active"])

client.mark = 102.05
bot._check_exits(c, now + 2)
check("MTL: новый экстремум активирует", c["trail_active"] is True,
      c["trail_active"])
check("MTL: шаг = 0.5% при NATR 1%", c["trail_step_pct"] == 0.5,
      c["trail_step_pct"])

check("step: NATR 4% → 2%", bot._trail_step_pct(cyc(natr=4.0)) == 2.0,
      bot._trail_step_pct(cyc(natr=4.0)))
check("step: NATR 0.2% → пол 0.5%",
      bot._trail_step_pct(cyc(natr=0.2)) == 0.5,
      bot._trail_step_pct(cyc(natr=0.2)))

print("TRAILING_MTL OK")
