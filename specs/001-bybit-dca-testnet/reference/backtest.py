"""
tools/backtest.py — прогон стратегии DCA-бота на исторических данных.

Повторяет логику связки «скринер → бот» по закрытым свечам:
  * сигналы — через ту же функцию `screener.evaluate` (NATR-14 + UHLO-20),
    те же окна SymbolState, подавление повтора цвета и кулдаун;
  * исполнение — первый вход, докупки по лестнице, тейк-профит от средней
    цены входа, выход по времени (и опциональный стоп по цене), лонг и шорт;
  * реализм — комиссия тейкера и проскальзывание на каждом филле;
  * метрики — итоговый PnL, win-rate, максимальная просадка, число сделок,
    средняя длительность удержания.

Данные берутся с публичного API Bybit /v5/market/kline (постранично, от
новых к старым) либо из CSV-файла, если данных под рукой нет.

Зависимостей нет: только стандартная библиотека, чтобы прогон можно было
запустить прямо на устройстве, где живёт бот. Функции скринера импортируются
из соседнего screener.py; requests/websockets ему не нужны (подменяются
заглушками до импорта, как в reference/test_*.py).

Запуск:
    python3 tools/backtest.py --symbol BTCUSDT --days 30
    python3 tools/backtest.py --symbol BTCUSDT --start 2026-06-01 --end 2026-07-01
    python3 tools/backtest.py --symbols BTCUSDT,ETHUSDT --days 90
    python3 tools/backtest.py --symbol ETHUSDT --days 90 --entry-usdt 50 \
        --dca-step-pct 0.8 --max-docups 3 --tp-pct 1.0 --max-hold-minutes 240 \
        --fee-rate 0.00055 --slippage-pct 0.0005
    python3 tools/backtest.py --csv data/btc-1m.csv --days 30

CSV: одна строка на свечу, заголовок с колонками ts/open/high/low/close
(ts — мс; допустим также epoch-секунды — определяются автоматически).
Разрешение CSV — минута (как основной поток скринера); старший ТФ 15м
агрегируется из минутных свечей.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import tempfile
import time
import types
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Импорт соседних модулей reference-каталога (screener, pricing, bot)
# ---------------------------------------------------------------------------

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    """Загружает модуль из того же каталога, регистрируя его в sys.modules.

    Имена фиксированы: bot.py импортирует pricing как обычный модуль.
    """
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с backtest.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name: str, **attrs):
    """Подменяет внешний модуль заглушкой, если он не установлен."""
    if importlib.util.find_spec(name) is not None:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


# screener.py импортирует requests и websockets на уровне модуля; backtest
# использует только чистые функции, поэтому внешние зависимости не нужны.
_stub("requests")
if importlib.util.find_spec("websockets") is None:
    _ws_root = types.ModuleType("websockets")
    _ws_asy = types.ModuleType("websockets.asyncio")
    _ws_cli = types.ModuleType("websockets.asyncio.client")
    _ws_cli.connect = lambda *a, **k: None
    _ws_asy.client = _ws_cli
    _ws_root.asyncio = _ws_asy
    sys.modules["websockets"] = _ws_root
    sys.modules["websockets.asyncio"] = _ws_asy
    sys.modules["websockets.asyncio.client"] = _ws_cli

# Импорт создаёт logs/ — уводим его во временный каталог, чтобы не сорить в repo.
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="backtest-import-"))
try:
    sc = _load_sibling("screener")
finally:
    os.chdir(_cwd)
pricing = _load_sibling("pricing")
bot = _load_sibling("bot")

quantize_qty = pricing.quantize_qty
tp_price = bot.tp_price
take_profit_pct_at = bot.take_profit_pct_at

# ---------------------------------------------------------------------------
# Параметры DCA
# ---------------------------------------------------------------------------


@dataclass
class DcaParams:
    entry_usdt: float = 50.0        # размер первого входа в USDT
    dca_step_pct: float = 0.8       # шаг усреднения в %
    max_docups: int = 3             # максимум докупок
    tp_pct: float = 1.0             # тейк-профит от средней цены входа, %
    max_hold_minutes: int = 240     # выход по времени
    stop_pct: float = 0.0           # ценовой стоп, %; 0 = выключен (спека: стоп не применяется)
    leverage: float = 3.0           # фиксированное плечо
    fee_rate: float = 0.00055       # тейкер Bybit USDT-perp (0.055%)
    slippage_pct: float = 0.0005    # проскальзывание на филл, %
    max_concurrent: int = 3         # лимит одновременных циклов (FR-015)
    multiplier: float = 2.0         # множитель объёма Мартингейла: докупка k = entry * multiplier^k
    tp_escalation: tuple[float, ...] = ()  # поуровневый TP: элемент по числу докупок
    # Адаптивный шаг сетки от NATR-14 (сигнала скринера):
    # эффективный шаг = clamp(NATR * step_atr_mult, step_min_pct, step_max_pct);
    # step_atr_mult: 0 — фиксированный dca_step_pct (как в базовом боте).
    step_atr_mult: float = 0.0
    step_min_pct: float = 0.0
    step_max_pct: float = 0.0
    # Адаптивный аварийный стоп от NATR-14:
    # эффективный стоп = clamp(NATR * sl_atr_mult, sl_min_pct, sl_max_pct);
    # sl_atr_mult: 0 — фиксированный stop_pct; при шумных проколах стоп
    # отодвигается от входа, в спокойном рынке — подтягивается.
    sl_atr_mult: float = 0.0
    sl_min_pct: float = 0.0
    sl_max_pct: float = 0.0
    # Жёсткий лимит убытка цикла в USDT (независимо от ценового стопа):
    # при unrealized-убытке >= значения цикл закрывается рыночно с причиной
    # hard_loss_limit. 0 — выключен. Цена срабатывания = цена, при которой
    # убыток достигает лимита; если она ближе к входу, чем стоп, лимит
    # срабатывает раньше.
    max_cycle_loss_usdt: float = 0.0

    def validate(self) -> None:
        problems = []
        if not self.entry_usdt > 0:
            problems.append("entry_usdt > 0")
        if not self.dca_step_pct > 0:
            problems.append("dca_step_pct > 0")
        if not self.max_docups >= 0:
            problems.append("max_docups >= 0")
        if not self.tp_pct > 0:
            problems.append("tp_pct > 0")
        if not self.max_hold_minutes > 0:
            problems.append("max_hold_minutes > 0")
        if not (0 <= self.stop_pct < 50):
            problems.append("stop_pct в [0, 50)")
        if not self.fee_rate >= 0:
            problems.append("fee_rate >= 0")
        if not 0 <= self.slippage_pct <= 0.05:
            problems.append("slippage_pct в [0, 5%]")
        if self.max_concurrent < 1:
            problems.append("max_concurrent >= 1")
        if not self.multiplier > 0:
            problems.append("multiplier > 0")
        if any(pct <= 0 for pct in self.tp_escalation):
            problems.append("tp_escalation: все элементы > 0")
        if not self.step_atr_mult >= 0:
            problems.append("step_atr_mult >= 0")
        if self.step_atr_mult and not (0 < self.step_min_pct <= self.step_max_pct):
            problems.append("шаг: 0 < step_min_pct <= step_max_pct при адаптиве")
        if not self.sl_atr_mult >= 0:
            problems.append("sl_atr_mult >= 0")
        if self.sl_atr_mult and not (0 < self.sl_min_pct <= self.sl_max_pct):
            problems.append("стоп: 0 < sl_min_pct <= sl_max_pct при адаптиве")
        if not self.max_cycle_loss_usdt >= 0:
            problems.append("max_cycle_loss_usdt >= 0")
        if problems:
            raise ValueError("некорректные параметры DCA:\n- " + "\n- ".join(problems))


def clamp(x: float, lo: float, hi: float) -> float:
    """Зажим значения в диапазон [lo, hi]."""
    return max(lo, min(hi, x))


def effective_step_pct(p: DcaParams, natr: float | None) -> float:
    """Эффективный шаг докупки, %: адаптив от NATR-14 или фиксированный.

    NATR из сигнала скринера — уже в процентах (compute_natr: ATR/close*100);
    при выключенном адаптиве (step_atr_mult=0) или отсутствии NATR в сигнале
    (восстановленные циклы) возвращается dca_step_pct.
    """
    if p.step_atr_mult > 0 and natr is not None:
        return clamp(natr * p.step_atr_mult, p.step_min_pct, p.step_max_pct)
    return p.dca_step_pct


def effective_stop_pct(p: DcaParams, natr: float | None) -> float:
    """Эффективный аварийный стоп, %: адаптив от NATR-14 или фиксированный."""
    if p.sl_atr_mult > 0 and natr is not None:
        return clamp(natr * p.sl_atr_mult, p.sl_min_pct, p.sl_max_pct)
    return p.stop_pct


# ---------------------------------------------------------------------------
# Загрузка данных
# ---------------------------------------------------------------------------

BYBIT_REST = "https://api.bybit.com"
KLINE_LIMIT = 1000


def _http_get(url: str, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt))
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body.get("retCode") != 0:
                raise RuntimeError(f"retCode={body.get('retCode')} {body.get('retMsg')}")
            return body["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"GET {url} не удался после {retries + 1} попыток: {last}")


def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "1",
                 pause_sec: float = 0.12, cache_dir: str | None = None) -> list[list]:
    """Закрытые свечи 1m от старых к новым, постранично назад от end_ms.

    Bybit отдаёт не более limit свечей из окна (новейшие); продолжение берём
    следующим запросом с end = самая старая свеча страницы − 1 мс.
    """
    cache_path: str | None = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir,
                                  f"{symbol}-{start_ms}-{end_ms}-{interval}.json")
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)

    rows: list[list] = []
    end = end_ms
    while True:
        query = urllib.parse.urlencode({
            "category": "linear", "symbol": symbol, "interval": interval,
            "limit": KLINE_LIMIT, "start": start_ms, "end": end,
        })
        res = _http_get(f"{BYBIT_REST}/v5/market/kline?{query}")
        page = res.get("list", [])
        if not page:
            break
        parsed = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                   float(r[5])] for r in page]
        rows.extend(parsed)
        oldest = min(r[0] for r in parsed)
        if oldest <= start_ms:
            break
        end = oldest - 1
        time.sleep(pause_sec)

    # дедупликация и сортировка от старых к новым
    seen: set[int] = set()
    uniq: list[list] = []
    for r in sorted(rows, key=lambda x: x[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        uniq.append(r)
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(uniq, f)
    return uniq


def fetch_instrument(symbol: str) -> dict:
    """Ограничения инструмента: qtyStep, minOrderQty, tickSize."""
    query = urllib.parse.urlencode({"category": "linear", "symbol": symbol})
    res = _http_get(f"{BYBIT_REST}/v5/market/instruments-info?{query}")
    for it in res.get("list", []):
        if it.get("contractType") == "LinearPerpetual":
            return {
                "symbol": symbol,
                "qty_step": float(it["lotSizeFilter"]["qtyStep"]),
                "min_qty": float(it["lotSizeFilter"]["minOrderQty"]),
                "tick_size": float(it["priceFilter"]["tickSize"]),
            }
    return {"symbol": symbol, "qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01}


def fetch_universe(top_n: int, required_leverage: float,
                   skip_top_volume: int = 0,
                   base_coin_blacklist: Sequence[str] = ()) -> list[str]:
    """Топ-N по turnover24h, как build_universe скринера (screener.py:521):
    торгуемые на mainnet и Testnet, с max_leverage >= required_leverage.

    skip_top_volume — пропустить первые N символов по обороту (низковолатильные
    гиганты: BTC, ETH...), как skip_top_volume скринера.

    base_coin_blacklist — исключить инструменты по базовой монете (INXUSDT →
    INX), как base_coin_blacklist скринера.
    """
    def _instruments(base: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cursor: str | None = None
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = _http_get(f"{base}/v5/market/instruments-info?"
                            + urllib.parse.urlencode(params))
            for it in res.get("list", []):
                if it.get("contractType") != "LinearPerpetual" or it.get("quoteCoin") != "USDT":
                    continue
                out[it["symbol"]] = {
                    "status": it.get("status"),
                    "max_leverage": float(it["leverageFilter"]["maxLeverage"]),
                }
            cursor = res.get("nextPageCursor") or None
            if not cursor:
                return out

    mainnet = _instruments(BYBIT_REST)
    testnet = _instruments("https://api-testnet.bybit.com")
    turn_res = _http_get(f"{BYBIT_REST}/v5/market/tickers?category=linear")
    turnover = {t["symbol"]: float(t.get("turnover24h") or 0)
                for t in turn_res.get("list", [])}

    ranked = sorted(turnover.items(), key=lambda kv: kv[1], reverse=True)
    selected: list[str] = []
    for i, (symbol, turn) in enumerate(ranked):
        if i < skip_top_volume:
            continue
        info = mainnet.get(symbol)
        if info is None or info["status"] != "Trading":
            continue
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base in base_coin_blacklist:
            continue
        if symbol not in testnet or testnet[symbol]["status"] != "Trading":
            continue
        if info["max_leverage"] < required_leverage:
            continue
        selected.append(symbol)
        if len(selected) >= top_n:
            break
    if not selected:
        raise RuntimeError("вселенная пуста — проверьте фильтры конфигурации")
    return selected


def read_csv(path: str, start_ms: int | None, end_ms: int | None) -> list[list]:
    """CSV с заголовком ts/open/high/low/close[/volume]."""
    import csv

    rows: list[list] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        ts_col = next((c for c in ("ts", "start", "time") if c in cols), None)
        if ts_col is None:
            raise ValueError(f"CSV должен содержать колонку ts/start/time, есть: {sorted(cols)}")
        for line in cols:  # нормализация открытых имён
            pass
        for raw in reader:
            ts = int(float(raw[ts_col].strip()))
            if ts < 1_000_000_000_000:  # epoch-секунды → мс
                ts *= 1000
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            o, h, l, c = (float(raw[cols[k]].strip()) for k in ("open", "high", "low", "close"))
            v = float(raw[cols["volume"]].strip()) if "volume" in cols else 0.0
            rows.append([ts, o, h, l, c, v])
    rows.sort(key=lambda x: x[0])
    return rows


def aggregate_minutes(rows: list[list], tf_minutes: int) -> list[list]:
    """Группирует свечи произвольного разрешения в свечи tf_minutes."""
    bucket = tf_minutes * 60_000
    out: list[list] = []
    for r in rows:
        b = (r[0] // bucket) * bucket
        if out and out[-1][0] == b:
            prev = out[-1]
            prev[2] = max(prev[2], r[2])
            prev[3] = min(prev[3], r[3])
            prev[4] = r[4]
            prev[5] += r[5]
        else:
            out.append([b, r[1], r[2], r[3], r[4], r[5]])
    return out


# ---------------------------------------------------------------------------
# Симуляция DCA-цикла
# ---------------------------------------------------------------------------


@dataclass
class Cycle:
    symbol: str
    side: str
    open_ts: int
    qty_step: float
    min_qty: float
    tick_size: float
    qty: float = 0.0
    avg_entry: float = 0.0
    docups: int = 0
    tp_level: float = 0.0
    stop_level: float = 0.0
    next_level: float = 0.0
    fee: float = 0.0
    fills: list = field(default_factory=list)
    closed: bool = False
    exit_ts: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    natr: float | None = None      # NATR-14 сигнала, открывшего цикл (для адаптива)

    @property
    def duration_minutes(self) -> float:
        return (self.exit_ts - self.open_ts) / 60_000


class Backtest:
    """Прогон одного символа по минутным свечам с логикой скринера и бота."""

    def __init__(self, cfg: sc.Config, params: DcaParams, symbol: str,
                 instrument: dict | None = None):
        self.cfg = cfg
        self.p = params
        self.symbol = symbol
        inst = instrument or fetch_instrument(symbol)
        self.instrument = inst
        self.state = sc.SymbolState(
            max(cfg.natr_period + 2, cfg.uhlo_length * 2 + 2),
            cfg.uhlo_length * 2 + 2,
        )
        self.open: list[Cycle] = []
        self.closed: list[Cycle] = []
        self.signals = 0
        self.rejected_limit = 0
        self._pending_side: str | None = None
        self._pending_natr: float | None = None

    # ── основной цикл ─────────────────────────────────────────────────────

    def run(self, rows: list[list], slow_tf_minutes: int = 15) -> "RunResult":
        slow = aggregate_minutes(rows, slow_tf_minutes)
        slow_bucket = slow_tf_minutes * 60_000
        slow_ptr = 0
        for row in rows:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            # старшие свечи, закрывшиеся к этому моменту: свеча [b, b+bucket)
            # закрыта только когда текущая минута >= b + bucket
            while slow_ptr < len(slow) and slow[slow_ptr][0] + slow_bucket <= ts:
                self.state.push("slow", slow[slow_ptr][:5])
                slow_ptr += 1
            # сигнал предыдущей свечи исполняется по открытию текущей
            if self._pending_side is not None:
                self._open_cycle(self._pending_side, ts, o, self._pending_natr)
                self._pending_side = None
                self._pending_natr = None
            is_new = self.state.push("fast", row[:5])
            if is_new:
                d = sc.evaluate(list(self.state.fast), list(self.state.slow), self.cfg)
                self._on_decision(d, ts, row)
            self._update_cycles(row, ts)

        # циклы, оставшиеся открытыми к концу прогона, не дают реализованного PnL
        return RunResult(
            symbol=self.symbol,
            instrument=self.instrument,
            first_ts=rows[0][0] if rows else 0,
            last_ts=rows[-1][0] if rows else 0,
            candles=len(rows),
            signals=self.signals,
            rejected_limit=self.rejected_limit,
            open=self.open,
            closed=self.closed,
        )

    # ── сигналы ────────────────────────────────────────────────────────────

    def _on_decision(self, d: "sc.Decision", ts: int, row: list) -> None:
        st = self.state
        if not d.passed:
            # Цвет сбрасывается при уходе в неопределённое состояние — как в
            # скринере: без этого монета, побывавшая в none, больше не сигналит.
            if d.reason in ("uhlo_no_color", "uhlo_slow_missing"):
                st.last_color = "none"
            return
        if d.color == st.last_color:  # подавление повтора на том же цвете
            return
        if ts - st.last_signal_ms < self.cfg.cooldown_sec * 1000:
            return
        st.last_color = d.color
        st.last_signal_ms = ts
        self.signals += 1
        if len(self.open) >= self.p.max_concurrent:  # FR-015: лимит циклов
            self.rejected_limit += 1
            return
        self._pending_side = sc.color_to_side(d.color)
        self._pending_natr = d.natr

    # ── исполнение ─────────────────────────────────────────────────────────

    def _open_cycle(self, side: str, ts: int, open_price: float,
                    natr: float | None = None) -> None:
        p = self.p
        fill = open_price * (1 + p.slippage_pct if side == "Buy" else 1 - p.slippage_pct)
        cyc = Cycle(
            symbol=self.symbol, side=side, open_ts=ts,
            qty_step=self.instrument["qty_step"],
            min_qty=self.instrument["min_qty"],
            tick_size=self.instrument["tick_size"],
            natr=natr,
        )
        self._fill(cyc, fill, "entry", ts)
        self.open.append(cyc)

    def _fill(self, cyc: Cycle, price: float, role: str, ts: int) -> None:
        p = self.p
        if role == "close":
            qty = cyc.qty
        else:
            qty = quantize_qty(p.entry_usdt / price, cyc.qty_step)
            if qty < cyc.min_qty:
                qty = quantize_qty(cyc.min_qty, cyc.qty_step)
            if qty <= 0:
                qty = cyc.min_qty
            if role == "dca":
                cyc.docups += 1
        side = cyc.side
        new_qty = cyc.qty + qty
        cyc.avg_entry = (cyc.avg_entry * cyc.qty + price * qty) / new_qty
        cyc.qty = new_qty
        cyc.fee += qty * price * p.fee_rate
        cyc.fills.append({"ts": ts, "role": role, "side": side, "qty": qty, "price": price})
        # уровни пересчитываются после каждого филла (FR-011): поуровневый TP
        # от средней (эскалация по числу докупок), адаптивный шаг и стоп от NATR
        cyc.tp_level = tp_price(cyc.avg_entry, side,
                                take_profit_pct_at(p, cyc.docups), cyc.tick_size)
        stop_pct = effective_stop_pct(p, cyc.natr)
        if stop_pct:
            cyc.stop_level = cyc.avg_entry * (
                1 - stop_pct / 100 if side == "Buy" else 1 + stop_pct / 100)
        cyc.next_level = price * (
            1 - effective_step_pct(p, cyc.natr) / 100 if side == "Buy"
            else 1 + effective_step_pct(p, cyc.natr) / 100)

    def _close(self, cyc: Cycle, price: float, reason: str, ts: int) -> None:
        p = self.p
        cyc.exit_price = price
        cyc.exit_ts = ts
        cyc.exit_reason = reason
        cyc.fee += cyc.qty * price * p.fee_rate
        if cyc.side == "Buy":
            cyc.pnl = (price - cyc.avg_entry) * cyc.qty
        else:
            cyc.pnl = (cyc.avg_entry - price) * cyc.qty
        cyc.pnl -= cyc.fee
        cyc.fills.append({"ts": ts, "role": "close", "side": cyc.side,
                          "qty": cyc.qty, "price": price})
        cyc.closed = True

    def _hard_loss_price(self, cyc: Cycle, p: DcaParams) -> float | None:
        """Цена, при которой unrealized-убыток цикла достигает
        max_cycle_loss_usdt. None — лимит выключен."""
        if not p.max_cycle_loss_usdt or cyc.qty <= 0:
            return None
        per_qty = p.max_cycle_loss_usdt / cyc.qty
        return (cyc.avg_entry - per_qty if cyc.side == "Buy"
                else cyc.avg_entry + per_qty)

    def _update_cycles(self, row: list, ts: int) -> None:
        o, h, l, c = row[1], row[2], row[3], row[4]
        eps = 1e-9
        for cyc in list(self.open):
            p = self.p
            if cyc.closed:
                continue
            if cyc.side == "Buy":
                # докупки по лестнице: уровень уходит вниз от последнего филла
                while cyc.docups < p.max_docups and l <= cyc.next_level + eps:
                    fill = cyc.next_level * (1 + p.slippage_pct)
                    self._fill(cyc, fill, "dca", ts)
                # жёсткий лимит убытка в USDT считается после докупок (по новой
                # средней); из стопа и лимита срабатывает тот, кто ближе к входу.
                hard = self._hard_loss_price(cyc, p)
                stop_hit = bool(cyc.stop_level and l <= cyc.stop_level)
                hard_hit = hard is not None and l <= hard
                if hard_hit and (not stop_hit or hard > cyc.stop_level):
                    self._close(cyc, hard * (1 - p.slippage_pct), "hard_loss_limit", ts)
                elif stop_hit:
                    self._close(cyc, cyc.stop_level * (1 - p.slippage_pct), "stop", ts)
                elif h >= cyc.tp_level:
                    self._close(cyc, cyc.tp_level * (1 - p.slippage_pct), "take_profit", ts)
                else:
                    self._maybe_time_exit(cyc, c, ts)
            else:
                while cyc.docups < p.max_docups and h >= cyc.next_level - eps:
                    fill = cyc.next_level * (1 - p.slippage_pct)
                    self._fill(cyc, fill, "dca", ts)
                hard = self._hard_loss_price(cyc, p)
                stop_hit = bool(cyc.stop_level and h >= cyc.stop_level)
                hard_hit = hard is not None and h >= hard
                if hard_hit and (not stop_hit or hard < cyc.stop_level):
                    self._close(cyc, hard * (1 + p.slippage_pct), "hard_loss_limit", ts)
                elif stop_hit:
                    self._close(cyc, cyc.stop_level * (1 + p.slippage_pct), "stop", ts)
                elif l <= cyc.tp_level:
                    self._close(cyc, cyc.tp_level * (1 + p.slippage_pct), "take_profit", ts)
                else:
                    self._maybe_time_exit(cyc, c, ts)

    def _maybe_time_exit(self, cyc: Cycle, close_price: float, ts: int) -> None:
        p = self.p
        if (ts - cyc.open_ts) / 60_000 < p.max_hold_minutes:
            return
        fill = close_price * (1 - p.slippage_pct if cyc.side == "Buy" else 1 + p.slippage_pct)
        self._close(cyc, fill, "time_exit", ts)

    def remove_closed(self) -> None:
        """Переносит закрытые циклы из open в closed (вызывается вне run)."""
        self.closed.extend(c for c in self.open if c.closed)
        self.open = [c for c in self.open if not c.closed]


@dataclass
class RunResult:
    symbol: str
    instrument: dict
    first_ts: int
    last_ts: int
    candles: int
    signals: int
    rejected_limit: int
    open: list
    closed: list


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------


def max_drawdown(pnls: list[float]) -> tuple[float, float]:
    """Максимальная просадка кривой реализованного PnL: абсолютная и % от пика."""
    peak = 0.0
    equity = 0.0
    worst_abs = 0.0
    worst_pct = 0.0
    for v in pnls:
        equity += v
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > worst_abs:
            worst_abs = dd
        if peak > 0:
            worst_pct = max(worst_pct, dd / peak)
    return worst_abs, worst_pct * 100


def summarize(result: RunResult) -> dict:
    closed = result.closed
    pnls = [c.pnl for c in closed]
    wins = [c for c in closed if c.pnl > 0]
    losses = [c for c in closed if c.pnl <= 0]
    total_fee = sum(c.fee for c in closed)
    gross = sum(c.pnl for c in closed) + total_fee
    mdd_abs, mdd_pct = max_drawdown(pnls)
    reasons = {}
    for c in closed:
        reasons[c.exit_reason] = reasons.get(c.exit_reason, 0) + 1
    avg_dur = (sum(c.duration_minutes for c in closed) / len(closed)) if closed else 0.0
    return {
        "symbol": result.symbol,
        "range": (result.first_ts, result.last_ts),
        "candles": result.candles,
        "signals": result.signals,
        "rejected_limit": result.rejected_limit,
        "n_cycles": len(closed) + len(result.open),
        "closed": len(closed),
        "open_at_end": len(result.open),
        "exit_reasons": reasons,
        "total_pnl": sum(pnls),
        "gross_pnl": gross,
        "total_fees": total_fee,
        "win_rate": (len(wins) / len(closed)) * 100 if closed else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "max_drawdown_abs": mdd_abs,
        "max_drawdown_pct": mdd_pct,
        "avg_duration_min": avg_dur,
        "avg_fill_count": (sum(len(c.fills) for c in closed) / len(closed)) if closed else 0.0,
    }


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------


def _fmt_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_dur(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} мин"
    return f"{minutes / 60:.1f} ч"


def render(m: dict, params: DcaParams, verbose: bool = False) -> str:
    lines: list[str] = []
    a, b = m["range"]
    lines.append(f"=== Backtest: {m['symbol']} ===")
    lines.append(f"Период: {_fmt_dt(a)} — {_fmt_dt(b)} ({m['candles']} свечей)")
    lines.append(f"Параметры: вход {params.entry_usdt:g} USDT, шаг {params.dca_step_pct}%, "
                 f"докупок {params.max_docups}, TP {params.tp_pct}%, "
                 f"удержание {_fmt_dur(params.max_hold_minutes)}, "
                 f"комиссия {params.fee_rate * 100:.3f}%, "
                 f"слиппедж {params.slippage_pct * 100:.3f}%")
    if params.step_atr_mult > 0:
        lines.append(f"Адаптивный шаг: NATR × {params.step_atr_mult:g} в "
                     f"[{params.step_min_pct:g}%, {params.step_max_pct:g}%]")
    if params.sl_atr_mult > 0:
        lines.append(f"Адаптивный стоп: NATR × {params.sl_atr_mult:g} в "
                     f"[{params.sl_min_pct:g}%, {params.sl_max_pct:g}%] "
                     f"(фолбэк {params.stop_pct:g}%)")
    if params.max_cycle_loss_usdt > 0:
        lines.append(f"Жёсткий лимит убытка цикла: {params.max_cycle_loss_usdt:g} USDT")
    if params.tp_escalation:
        lines.append("Поуровневый TP: " + "/".join(f"{x:g}%" for x in params.tp_escalation))
    lines.append(f"Сигналы: {m['signals']} (отклонено по лимиту циклов: {m['rejected_limit']})")
    lines.append(f"Циклы: закрыто {m['closed']} из {m['n_cycles']} "
                 f"(открыто к концу: {m['open_at_end']})")
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(m["exit_reasons"].items()))
    lines.append(f"Причины выхода: {reasons}")
    lines.append(f"Итоговый PnL: {m['total_pnl']:+.2f} USDT "
                 f"(гросс {m['gross_pnl']:+.2f}, комиссии {m['total_fees']:.2f})")
    lines.append(f"Win-rate: {m['win_rate']:.1f}% ({m['wins']} из {m['closed']})")
    lines.append(f"Макс. просадка: {m['max_drawdown_abs']:.2f} USDT "
                 f"({m['max_drawdown_pct']:.1f}% от пика)")
    lines.append(f"Средняя длительность удержания: {_fmt_dur(m['avg_duration_min'])}")
    lines.append(f"Среднее число филлов на сделку: {m['avg_fill_count']:.1f}")
    if verbose:
        lines.append("\nСделки (роль, сторона, вход, выход, причина, PnL):")
        for c in m["_cycles"]:
            lines.append(f"  {_fmt_dt(c.open_ts)} {c.side:4s} вх={c.avg_entry:.4f} "
                         f"вых={c.exit_price:.4f} {c.exit_reason:11s} {c.pnl:+.2f} USDT")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default=None,
                    help="пара (или несколько через запятую); без --universe и без "
                         "--symbol — весь топ из конфига (top_n_turnover, 600)")
    ap.add_argument("--universe", type=int, default=None,
                    help="топ-N по turnover24h, как build_universe скринера; "
                         "по умолчанию — весь топ из конфига (top_n_turnover, 600)")
    ap.add_argument("--cache-dir", default=None,
                    help="каталог для кэша загруженных klines (ускоряет повторные прогоны)")
    ap.add_argument("--days", type=int, default=None, help="глубина истории в днях")
    ap.add_argument("--start", type=_parse_date, default=None, help="начало, YYYY-MM-DD")
    ap.add_argument("--end", type=_parse_date, default=None, help="конец, YYYY-MM-DD")
    ap.add_argument("--csv", default=None, help="CSV-файл вместо загрузки с биржи")
    ap.add_argument("--tf", default="1", help="разрешение данных в минутах (по умолчанию 1)")
    ap.add_argument("--entry-usdt", type=float, default=50.0)
    ap.add_argument("--dca-step-pct", type=float, default=0.8)
    ap.add_argument("--max-docups", type=int, default=3)
    ap.add_argument("--tp-pct", type=float, default=1.0)
    ap.add_argument("--max-hold-minutes", type=int, default=240)
    ap.add_argument("--stop-pct", type=float, default=0.0,
                    help="ценовой стоп в процентах (0 = выключен)")
    ap.add_argument("--leverage", type=float, default=3.0)
    ap.add_argument("--fee-rate", type=float, default=0.00055)
    ap.add_argument("--slippage-pct", type=float, default=0.0005)
    ap.add_argument("--max-concurrent", type=int, default=3)
    ap.add_argument("--natr-min", type=float, default=None,
                    help="нижняя граница NATR (по умолчанию — как у скринера: 0.9)")
    ap.add_argument("--natr-max", type=float, default=None,
                    help="верхняя граница NATR (по умолчанию — как у скринера: 2.5)")
    ap.add_argument("--verbose", action="store_true", help="детальный список сделок")
    ap.add_argument("--json", action="store_true", dest="as_json")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = DcaParams(
        entry_usdt=args.entry_usdt, dca_step_pct=args.dca_step_pct,
        max_docups=args.max_docups, tp_pct=args.tp_pct,
        max_hold_minutes=args.max_hold_minutes, stop_pct=args.stop_pct,
        leverage=args.leverage, fee_rate=args.fee_rate,
        slippage_pct=args.slippage_pct, max_concurrent=args.max_concurrent,
    )
    params.validate()

    end_ms = args.end
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if args.days:
        start_ms = end_ms - args.days * 86_400_000
    else:
        start_ms = args.start if args.start is not None else end_ms - 30 * 86_400_000

    cfg = sc.Config()  # границы NATR, кулдаун — как у скринера по умолчанию
    if args.natr_min is not None:
        cfg.natr_min = args.natr_min
    if args.natr_max is not None:
        cfg.natr_max = args.natr_max
    sc.validate_config(cfg)

    if args.csv:
        symbols = [args.symbol or "BTCUSDT"]  # имя в отчёте; данные из CSV
    elif args.universe is not None:
        symbols = fetch_universe(args.universe, cfg.required_leverage)
    elif args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]
    else:
        # без явного --symbol/--universe — весь топ из конфига (top_n_turnover, 600)
        symbols = fetch_universe(cfg.top_n_turnover, cfg.required_leverage)
    sys.stderr.write(f"[backtest] вселенная: {len(symbols)} символов, "
                     f"первые: {', '.join(symbols[:5])}…\n")
    summaries = []
    for symbol in symbols:
        sys.stderr.write(f"[backtest] {symbol}: загрузка данных…\n")
        if args.csv:
            rows = read_csv(args.csv, start_ms, end_ms)
            instrument = None
        else:
            rows = fetch_klines(symbol, start_ms, end_ms, args.tf, cache_dir=args.cache_dir)
            instrument = fetch_instrument(symbol)
        if not rows:
            sys.stderr.write(f"[backtest] {symbol}: нет данных в периоде\n")
            continue

        bt = Backtest(cfg, params, symbol, instrument)
        result = bt.run(rows, slow_tf_minutes=15)
        bt.remove_closed()
        # remove_closed перепривязывает bt.open, а result.open ещё держит старый
        # список со «спящими» закрытыми циклами — иначе сводка считает их открытыми.
        result.open = bt.open
        result.closed.sort(key=lambda c: c.exit_ts)
        m = summarize(result)
        m["_cycles"] = result.closed
        summaries.append(m)

        sys.stderr.write(f"[backtest] {symbol}: сигналов {m['signals']}, "
                         f"закрыто циклов {m['closed']}, PnL {m['total_pnl']:+.2f}\n")
        if args.as_json:
            print(json.dumps({k: v for k, v in m.items() if k != "_cycles"},
                             ensure_ascii=False, default=str))
        else:
            print(render(m, params, verbose=args.verbose))
            print()

    if len(symbols) > 1 and len(summaries) > 1 and not args.as_json:
        total = sum(m["total_pnl"] for m in summaries)
        closed = sum(m["closed"] for m in summaries)
        wins = sum(m["wins"] for m in summaries)
        fees = sum(m["total_fees"] for m in summaries)
        mdd = max(m["max_drawdown_abs"] for m in summaries)
        print("=== Сводно по символам ===")
        print(f"Закрыто сделок: {closed}, win-rate: {wins / closed * 100:.1f}%"
              if closed else "Закрытых сделок нет")
        print(f"Суммарный PnL: {total:+.2f} USDT (комиссии {fees:.2f})")
        print(f"Макс. просадка по символам: {mdd:.2f} USDT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
