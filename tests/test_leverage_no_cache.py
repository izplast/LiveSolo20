"""П.4: кэш плеча не используется; в стейт плечо не пишется.

Запуск:  python3 tests/test_leverage_no_cache.py   (код 0 = ок)
"""
import json

from exit_fake import FakeClient, make_bot, check


class LevClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.calls_lev = []

    def set_leverage(self, symbol, leverage):
        self.calls_lev.append((symbol, leverage))
        return {}


client = LevClient()
bot = make_bot(client)
bot.p.leverage = 4.0
bot._apply_leverage("AAAUSDT")
bot._apply_leverage("AAAUSDT")
check("set-leverage на каждый вход, кэша нет",
      client.calls_lev == [("AAAUSDT", 4.0), ("AAAUSDT", 4.0)],
      client.calls_lev)

bot._save_state()
with open(bot.state_path, encoding="utf-8") as f:
    st = json.load(f)
check("в стейте нет ключа leverage", "leverage" not in st, list(st.keys()))
print("LEV_NO_CACHE OK")
