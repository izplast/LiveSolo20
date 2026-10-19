"""Журнал: вход + 2 докупки → три события stop_armed (перевыставление стопа).

Запуск:  python3 tests/test_stop_armed_3.py   (код 0 = ок)
"""
import time

from exit_fake import FakeClient, make_bot, cyc, check

import executor as E


class RespClient(FakeClient):
    def set_trading_stop(self, symbol, stop_loss, trigger="MarkPrice"):
        self.calls.append({"trading_stop": stop_loss, "trigger": trigger})
        return {"retCode": 0, "result": {"stopLoss": stop_loss}}


client = RespClient()
p = E.DcaParams(entry_usdt=20.0, max_docups=2, stop_pct=1.0,
                dca_step_pct=0.8, multiplier=2.0)
bot = make_bot(client, p=p)
c = cyc()
c["price_testnet"] = 100.0
c["tp_link"] = None
c["so_seq"] = 0
bot.cycles[c["cycle_id"]] = c

now = int(time.time() * 1000)
bot._on_fill(c, "t1", "entry", 0.2, 100.0, 0.01, now, {})
bot._on_fill(c, "t1_so1", "dca", 0.4, 99.2, 0.01, now, {"price": "99.2"})
bot._on_fill(c, "t1_so2", "dca", 0.8, 98.4064, 0.01, now,
             {"price": "98.4064"})

armed = [kw for n, a, kw in bot.journal.rows
         if n == "write" and a and a[0] == "stop_armed"]
check("вход + 2 докупки → 3 stop_armed", len(armed) == 3, len(armed))
check("qty в stop_armed растёт по лестнице",
      [round(kw.get("qty", 0), 6) for kw in armed] == [0.2, 0.6, 1.4],
      [kw.get("qty") for kw in armed])
check("stop_level есть в каждом stop_armed",
      all(kw.get("stop_level") for kw in armed), armed)
check("ответ Bybit есть в каждом stop_armed",
      all(kw.get("bybit_response") for kw in armed), armed)
check("stop_armed содержит side/notional/avg_price",
      all(kw.get("side") == "Buy" and kw.get("notional", 0) > 0
          and kw.get("avg_price", 0) > 0 for kw in armed), armed)
print("STOP_ARMED OK")
