"""Регресс: X-BAPI-TIMESTAMP пересчитывается на каждой попытке запроса.

Было: ts считался до цикла ретраев, поэтому повтор через ~15 с уходил с
протухшим timestamp и Bybit отвечал retCode 10002.
Запуск:  python3 tests/test_retry_fresh_timestamp.py   (код 0 = ок)
"""
from exit_fake import check
import executor as E


class Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def run_with_recorder(method, call):
    calls = []
    orig = getattr(E.requests, method)

    def fake(url, **kw):
        now = E.time.time() * 1000
        ts = int(kw["headers"]["X-BAPI-TIMESTAMP"])
        calls.append({"ts": ts, "now": now})
        if len(calls) == 1:
            raise E.BybitApiError(10002, "invalid request, please check your "
                                        "server timestamp or recv_window param",
                                  retryable=True)
        return Resp({"retCode": 0, "result": {}})

    setattr(E.requests, method, fake)
    try:
        call()
    finally:
        setattr(E.requests, method, orig)
    return calls


def check_calls(name, calls):
    check(f"{name}: ровно 2 попытки (ошибка + успех)", len(calls) == 2, calls)
    fresh = [abs(c["now"] - c["ts"]) for c in calls]
    check(f"{name}: ts свежий в момент каждой отправки (<250 мс)",
          all(f < 250 for f in fresh), [round(f) for f in fresh])
    delta = calls[1]["ts"] - calls[0]["ts"]
    check(f"{name}: повтор ушёл с новым ts (сдвиг >= 300 мс)", delta >= 300, delta)


def new_client():
    return E.BybitClient(E.BybitConfig(api_key="k", api_secret="s", demo=True))


calls = run_with_recorder(
    "post", lambda: new_client()._post("/v5/order/create", {"symbol": "TSTUSDT"}))
check_calls("_post", calls)

calls = run_with_recorder(
    "get", lambda: new_client()._get("/v5/position/list",
                                     {"category": "linear"}, signed=True))
check_calls("_get signed", calls)

print("OK: подпись пересчитывается на каждой попытке")
