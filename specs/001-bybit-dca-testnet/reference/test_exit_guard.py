"""
Автономные проверки защиты выходов от проскальзывания и ленты котировок.

Покрывает executor.py без сети:
  * maker-выход (PostOnly) закрывает позицию без маркет-ордера;
  * Max Slippage Filter откладывает маркет при широком спреде (exit_deferred)
    и отпускает его после дедлайна exit_force_after_sec;
  * частичный филл maker-лимитки списывается с позиции, остаток добивает
    маркет;
  * MainnetQuoteFeed: TTL-кэш, разбор tickers-сообщений, приоритет ленты
    над REST в PaperClient.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_exit_guard.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_DIR)))

_tmp = tempfile.mkdtemp(prefix="exit-guard-test-")
os.chdir(_tmp)

# executor.py сам добавит reference-каталог в sys.path; здесь — корень проекта.
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "executor_under_test", os.path.join(_ROOT, "executor.py"))
assert _spec and _spec.loader
ex = importlib.util.module_from_spec(_spec)
sys.modules["executor_under_test"] = ex
_spec.loader.exec_module(ex)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


# ── фейковый клиент биржи ─────────────────────────────────────────────────────

TAKER_FEE = 0.00055


class FakeClient:
    """Стакан по сценарию + мгновенное исполнение ордеров по правилам стакана."""

    def __init__(self, bids, asks, maker_fill=True, maker_partial=False):
        self.book = {"bids": list(bids), "asks": list(asks)}
        self.maker_fill = maker_fill
        self.maker_partial = maker_partial
        self.orders: dict[str, dict] = {}
        self.market_fills: list[dict] = []
        self.cancelled: list[str] = []

    def orderbook(self, symbol):
        return {"bids": list(self.book["bids"]), "asks": list(self.book["asks"])}

    def create_order(self, *, symbol, side, order_type, qty, price=None,
                     reduce_only=False, time_in_force="GTC",
                     order_link_id=""):
        o = {"orderId": order_link_id, "orderLinkId": order_link_id,
             "symbol": symbol, "side": side, "orderType": order_type,
             "qty": str(qty), "price": str(price or 0),
             "reduceOnly": reduce_only, "timeInForce": time_in_force,
             "orderStatus": "New", "cumExecQty": "0", "cumExecFee": "0",
             "avgPrice": "0"}
        if order_type == "Market":
            best = self.book["bids"][0][0] if side == "Sell" \
                else self.book["asks"][0][0]
            o.update(orderStatus="Filled", cumExecQty=str(qty),
                     avgPrice=str(best), cumExecFee=str(qty * best * TAKER_FEE))
            self.market_fills.append(o)
        else:
            fill_px = price
            if self.maker_fill or (self.maker_partial and False):
                o.update(orderStatus="Filled", cumExecQty=str(qty),
                         avgPrice=str(fill_px),
                         cumExecFee=str(qty * fill_px * 0.0002))
            elif self.maker_partial:
                half = round(qty / 2, 12)
                o.update(orderStatus="PartiallyFilled", cumExecQty=str(half),
                         avgPrice=str(fill_px),
                         cumExecFee=str(half * fill_px * 0.0002))
            # иначе остаётся New — таймаут maker-фазы
        self.orders[order_link_id] = o
        return o

    def get_order(self, symbol, link):
        return self.orders.get(link)

    def open_orders(self, symbol):
        return [o for o in self.orders.values()
                if o["orderStatus"] in ("New", "PartiallyFilled")]

    def cancel_order(self, symbol, link):
        o = self.orders.get(link)
        if o and o["orderStatus"] in ("New", "PartiallyFilled"):
            o["orderStatus"] = "Cancelled"
            self.cancelled.append(link)
            return {"status": "success"}
        return {"status": "error"}

    def ticker(self, symbol):
        mid = (self.book["bids"][0][0] + self.book["asks"][0][0]) / 2
        return {"lastPrice": str(mid), "markPrice": str(mid)}

    def set_leverage(self, symbol, leverage):
        return {}


_journal_seq = [0]


def make_bot(client, **over):
    dca = ex.bot_config.read_dca_section(
        os.path.join(_ROOT, "config", "config.yml"))
    p = ex.bot_config.dca_params_from_config(dca)
    bp = ex.bot_config.BotParams()
    kwargs = dict(
        exit_maker_enabled=over.pop("exit_maker_enabled", True),
        exit_maker_timeout_sec=over.pop("exit_maker_timeout_sec", 0.2),
        max_exit_slippage_pct=over.pop("max_exit_slippage_pct", 0.5),
        exit_force_after_sec=over.pop("exit_force_after_sec", 2.5),
    )
    _journal_seq[0] += 1
    jp = os.path.join(_tmp, f"bot-events-{_journal_seq[0]}.jsonl")
    bot = ex.DcaBot(p=p, bp=bp, client=client, mode="paper",
                    journal_path=jp,
                    state_path=os.path.join(_tmp, "bot-state.json"),
                    screener_path=os.path.join(_tmp, "screener.jsonl"),
                    **kwargs, **over)
    bot._test_journal_path = jp
    return bot


def make_cycle(bot, qty=2.0, entry=100.0):
    c = {
        "cycle_id": "TESTUSDT:1:1", "signal_id": "TESTUSDT:1:1",
        "symbol": "TESTUSDT", "side": "Buy", "natr": 1.0,
        "open_ts": int(time.time() * 1000), "signal_ts": int(time.time() * 1000),
        "signal_price": entry, "price_testnet": entry,
        "qty": qty, "avg_entry": entry, "docups": 0, "fee": 1.0,
        "last_fill_price": entry, "qty_step": 0.001, "tick_size": 0.01,
        "min_qty": 0.001, "base_link": "X", "links": {}, "fills": {},
        "tp_seq": 0, "tp_link": None, "tp_price": None,
        "so_seq": 0, "so_link": None, "so_price": None, "so_qty": None,
        "stop_level": None, "trail_active": False, "trail_peak": None,
        "closed": False,
    }
    bot.cycles[c["cycle_id"]] = c
    return c


def journal_lines(bot=None):
    path = bot._test_journal_path if bot is not None \
        else os.path.join(_tmp, "bot-events-1.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# ── 1. maker-выход полностью закрывает позицию ───────────────────────────────

print("maker-выход")

client = FakeClient(bids=[[99.99, 500]], asks=[[100.01, 500]], maker_fill=True)
bot = make_bot(client)
c = make_cycle(bot)
t0 = time.time()
bot._market_close(c, "hard_sl")
ok("маркет-ордер не потребовался", len(client.market_fills) == 0,
   client.market_fills)
ok("цикл закрыт", "TESTUSDT:1:1" not in bot.cycles)
events = journal_lines(bot)
ok("причина выхода сохранена", events[-1].get("exit_reason") == "hard_sl",
   events[-1])
ok("pnl посчитан по maker-цене 99.99",
   abs(events[-1]["pnl"]
       - round((99.99 - 100.0) * 2.0 - 1.0 - 2.0 * 99.99 * 0.0002, 4)) < 1e-9,
   events[-1])
ok("maker-выход быстрее дедлайна", time.time() - t0 < 2.0)

# ── 2. широкий спред: маркет откладывается до дедлайна ───────────────────────

print("\nMax Slippage Filter")

client = FakeClient(bids=[[99.0, 500]], asks=[[101.0, 500]],
                    maker_fill=False)
bot = make_bot(client)   # полуспред ~1% > лимита 0.5%, дедлайн 2.5 с
c = make_cycle(bot)
bot._market_close(c, "hard_sl")
events = journal_lines(bot)
kinds = [e["kind"] for e in events]
ok("маркет ушёл только после дедлайна", len(client.market_fills) == 1,
   client.market_fills)
ok("выход фиксировался как exit_deferred", "exit_deferred" in kinds, kinds[:20])
deferred = [e for e in events if e["kind"] == "exit_deferred"]
ok("в deferred записаны спред и лимит",
   all(abs(e["half_spread_pct"] - 1.0) < 1e-6 and e["limit_pct"] == 0.5
       for e in deferred), deferred[:2])
ok("цикл закрыт по best bid 99.0",
   not bot.cycles and events[-1]["exit_reason"] == "hard_sl"
   and abs(events[-1]["pnl"] - ((99.0 - 100.0) * 2.0 - 1.0 - 2.0 * 99.0 * TAKER_FEE)) < 1e-6,
   events[-1])

# ── 3. узкий спред: маркет разрешён сразу после неудачного maker ─────────────

print("\nузкий спред")

client = FakeClient(bids=[[99.98, 500]], asks=[[100.02, 500]],
                    maker_fill=False)
bot = make_bot(client)   # полуспред 0.02% <= 0.5%
c = make_cycle(bot)
t0 = time.time()
bot._market_close(c, "trailing")
events = journal_lines(bot)
ok("маркет не ждал дедлайна", time.time() - t0 < 2.0, time.time() - t0)
ok("один маркет-ордер", len(client.market_fills) == 1)
ok("нет exit_deferred при допустимом спреде",
   all(e["kind"] != "exit_deferred" for e in events), kinds if False else [e["kind"] for e in events])

# ── 4. частичный филл maker + маркет на остаток ───────────────────────────────

print("\nчастичный филл")

client = FakeClient(bids=[[99.98, 500]], asks=[[100.02, 500]],
                    maker_fill=False, maker_partial=True)
bot = make_bot(client)
c = make_cycle(bot)
bot._market_close(c, "time_exit")
events = journal_lines(bot)
partials = [e for e in events if e.get("role") == "close_maker"]
markets = [e for e in events if e.get("role") == "close"]
ok("частичный филл списал половину позиции",
   partials and abs(partials[0]["qty_closed"] - 1.0) < 1e-9, partials)
ok("маркет добил остаток 1.0",
   markets and abs(markets[-1]["qty_closed"] - 1.0) < 1e-9, markets)
ok("позиция обнулилась, цикл закрыт", "TESTUSDT:1:1" not in bot.cycles)
expected_pnl = round(((99.98 - 100.0) * 1.0 - 1.0 * 99.98 * 0.0002)
                     + ((99.98 - 100.0) * 1.0 - 1.0 * 99.98 * TAKER_FEE) - 1.0, 4)
ok("pnl суммирован по двум филлам",
   abs(events[-1]["pnl"] - expected_pnl) < 1e-9,
   (events[-1]["pnl"], expected_pnl))
ok("зависшая лимитка снята", len(client.cancelled) >= 1, client.cancelled)

# ── 5. выключенный maker → прежнее поведение ─────────────────────────────────

print("\nвыключенный maker")

client = FakeClient(bids=[[99.98, 500]], asks=[[100.02, 500]], maker_fill=False)
bot = make_bot(client, exit_maker_enabled=False)
c = make_cycle(bot)
bot._market_close(c, "hard_sl")
ok("сразу один маркет-ордер", len(client.market_fills) == 1)
ok("лимитки не выставлялись",
   not [l for l in client.orders if "_mk" in l], client.orders.keys())

# ── 6. MainnetQuoteFeed: кэш, TTL, разбор тикеров ────────────────────────────

print("\nлента котировок Mainnet")

feed = ex.MainnetQuoteFeed(ttl_ms=10_000)
feed.touch("AAAUSDT")
now_ms = int(time.time() * 1000)
feed._prices["AAAUSDT"] = (now_ms, 123.45)
ok("свежая цена из кэша", feed.last_price("AAAUSDT") == 123.45)
feed._prices["AAAUSDT"] = (now_ms - 60_000, 1.0)
ok("устаревшая цена отбрасывается по TTL", feed.last_price("AAAUSDT") is None)

feed._on_ticker(json.dumps({"topic": "tickers.BBBUSDT",
                            "data": {"lastPrice": "9.5"}}))
ok("тикер разобран в кэш", abs(feed._prices["BBBUSDT"][1] - 9.5) < 1e-12)
feed._on_ticker(json.dumps({"topic": "tickers.BBBUSDT", "data": {}}))
ok("тикер без lastPrice не портит кэш",
   abs(feed._prices["BBBUSDT"][1] - 9.5) < 1e-12)
feed._on_ticker("{битый json")
ok("битый json не роняет обработку", True)
feed._on_ticker(json.dumps({"topic": "kline.1.X", "data": {}}))
ok("не-tickers топик игнорируется", "kline" not in str(feed._prices))

# ── 7. PaperClient берёт цену из ленты, REST — только фолбэк ─────────────────

print("\nPaperClient поверх ленты")


class FeedOnly:
    def __init__(self):
        self.touched = []

    def last_price(self, symbol):
        self.touched.append(symbol)
        return 777.0


pc = ex.PaperClient.__new__(ex.PaperClient)
pc.bybit = None
pc.ref = {}
px_feed = FeedOnly()
pc.mainnet = px_feed
t = pc.ticker("QQQUSDT")
ok("цена взята из ленты", t["lastPrice"] == "777.0", t)
ok("REST не вызван при живой ленте", px_feed.touched == ["QQQUSDT"])

# Фолбэк: у пустой ленты MainnetPublic ходит в REST, но не чаще TTL.
class FakeResp:
    status_code = 200

    def json(self):
        return {"result": {"list": [{"symbol": "WWWUSDT",
                                     "lastPrice": "55"}]}}


rest_calls = [0]
_orig_get = ex.requests.get


def _fake_get(url, params=None, timeout=None):
    rest_calls[0] += 1
    return FakeResp()


ex.requests.get = _fake_get
try:
    mp = ex.MainnetPublic(ttl_ms=60_000)
    ok("REST-фолбэк отдаёт цену", mp.last_price("WWWUSDT") == 55.0)
    pc.mainnet = mp
    t2 = pc.ticker("WWWUSDT")
    ok("PaperClient получил фолбэк-цену", t2["lastPrice"] == "55.0", t2)
    ok("повторный запрос из TTL-кэша",
       mp.last_price("WWWUSDT") == 55.0 and rest_calls[0] == 1, rest_calls[0])
finally:
    ex.requests.get = _orig_get

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
