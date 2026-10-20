"""П.1: retCode 110043 (leverage not modified) = успех, сигнал не реджектится.

Кэша плеча нет: set-leverage вызывается перед каждым входом, поэтому
повторный вызов снова идёт на биржу (иначе позиция может остаться с чужим
плечом после смены venue/ручной правки).
"""
from exit_fake import FakeClient, make_bot, check
import executor as E


class LevClient(FakeClient):
    def __init__(self, err=None):
        super().__init__()
        self.err = err
        self.calls_lev = []

    def set_leverage(self, symbol, leverage):
        self.calls_lev.append((symbol, leverage))
        if self.err is not None:
            raise self.err
        return {}


# --- 110043 -> успех + leverage_already_set, без исключения ---
client = LevClient(err=E.BybitApiError(110043, "leverage not modified"))
bot = make_bot(client)
bot.p.leverage = 4.0
bot._apply_leverage("TSTUSDT")
check("без исключения (сигнал не реджектится)", True)
got = [kw for n, a, kw in bot.journal.rows
       if n == "write" and a and a[0] == "leverage_already_set"]
check("journal leverage_already_set", len(got) == 1, got)
# повторный вызов — снова биржа, кэша нет
bot._apply_leverage("TSTUSDT")
check("повторно биржу дёргаем (кэша нет)", len(client.calls_lev) == 2,
      client.calls_lev)

# --- прочая ошибка -> пробрасывается (сигнал реджектится как раньше) ---
client2 = LevClient(err=RuntimeError("Bybit retCode 10002: timeout"))
bot2 = make_bot(client2)
bot2.p.leverage = 4.0
try:
    bot2._apply_leverage("TSTUSDT")
    check("чужая ошибка проброшена", False)
except RuntimeError:
    check("чужая ошибка проброшена", True)

# --- обычный успех без изменений ---
client3 = LevClient()
bot3 = make_bot(client3)
bot3.p.leverage = 4.0
bot3._apply_leverage("TSTUSDT")
check("успех отправляет плечо", client3.calls_lev == [("TSTUSDT", 4.0)],
      client3.calls_lev)
print("LEV110043 OK")
