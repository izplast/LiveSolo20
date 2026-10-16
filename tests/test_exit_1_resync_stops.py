"""Item 1: рестарт с открытой позицией без стопа -> stop_armed + stopLoss == stop_level."""
from exit_fake import FakeClient, FakeJournal, make_bot, cyc, check

client = FakeClient()
bot = make_bot(client)
c = cyc()                       # stop_level=97.5, stop_armed отсутствует
assert not c.get("stop_armed")
bot.cycles["t1"] = c

bot._resync_all_stops()

stops = [k for k in client.calls if "trading_stop" in k]
check("стоп выставлен один раз", len(stops) == 1, stops)
check("stopLoss == stop_level", stops[0]["trading_stop"] == "97.5", stops)
check("stop_armed=True", c.get("stop_armed") is True)
check("slTriggerBy=MarkPrice (дефолт)",
      stops[0].get("trigger", "MarkPrice") == "MarkPrice", stops)

# Повторный вызов — уже armed, нового запроса нет
bot._resync_all_stops()
check("повторно не выставляется",
      len([k for k in client.calls if "trading_stop" in k]) == 1)
print("ITEM1 OK")
