"""
reference/run_sim.py — прогон DcaCycle на стакане Bybit: live, запись, replay.

Мост «скринер/стакан → симулятор» без внешних зависимостей (urllib):
  record  — опросить стакан N секунд, записать JSONL (для оффлайн-реплея);
  replay  — прогнать записанный стакан через DcaCycle (без сети);
  live    — опросить стакан и прогонять цикл по ходу (нужна сеть).

Направление сделки выбирает СКРИНЕР (screener.py: SymbolState + evaluate —
NATR-14 + UHLO-20 на 1м и 15м), как в связке «скринер → бот»:
  * replay — по свечам, восстановленным из записанного стакана (mid-цена
    снимков, одна минута = один бар), либо по истории из --candles CSV;
  * live   — по загруженным с биржи klines (прогрев скринера, --seed-hours)
    и, при --universe N, скринер сам выбирает монету из топ-N по обороту.
Флаг --side {auto|Buy|Sell} позволяет принудительно задать сторону вместо
решения скринера.

Параметры сетки берутся из config/config.yml бота (через bot_config) и могут
переопределяться флагами: --entry-usdt, --step-pct, --steps, --multiplier,
--tp-pct, --tp-escalation, --stop-pct, --max-hold-minutes, --fee-rate.

Примеры:
  python3 specs/001-bybit-dca-testnet/reference/run_sim.py record \
      --seconds 75 --out /tmp/btc-book.jsonl
  python3 specs/001-bybit-dca-testnet/reference/run_sim.py replay /tmp/btc-book.jsonl
  python3 specs/001-bybit-dca-testnet/reference/run_sim.py replay \
      /tmp/btc-book.jsonl --candles /tmp/btc.csv
  python3 specs/001-bybit-dca-testnet/reference/run_sim.py live --seconds 120
  python3 specs/001-bybit-dca-testnet/reference/run_sim.py live \
      --universe 10 --seconds 300
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    path = os.path.join(_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{name}.py не найден рядом с run_sim.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dc = _load_sibling("dca_cycle")     # подтягивает backtest, mock_execution, bot
bs = _load_sibling("book_streamer")
bc = _load_sibling("bot_config")
m = dc.mock_execution
bt = dc.backtest
sc = sys.modules["screener"]        # скринер зарегистрирован backtest'ом

DcaCycle = dc.DcaCycle
DcaParams = dc.DcaParams

# Конфиг бота по умолчанию: config/config.yml в корне репозитория.
DEFAULT_CONFIG = os.path.abspath(os.path.join(_DIR, "..", "..", "..",
                                              "config", "config.yml"))


def resolve_params(args) -> DcaParams:
    """Параметры сетки: конфиг бота (config.yml) + явные флаги поверх."""
    dca = bc.read_dca_section(args.config or DEFAULT_CONFIG)
    p = bc.dca_params_from_config(dca)
    if args.entry_usdt is not None:
        p.entry_usdt = args.entry_usdt
    if args.step_pct is not None:
        p.dca_step_pct = args.step_pct
    if args.steps is not None:
        p.max_docups = args.steps
    if args.multiplier is not None:
        p.multiplier = args.multiplier
    if args.tp_pct is not None:
        p.tp_pct = args.tp_pct
    if args.tp_escalation is not None:
        p.tp_escalation = tuple(
            float(x) for x in args.tp_escalation.split(",") if x.strip())
    if args.stop_pct is not None:
        p.stop_pct = args.stop_pct
    if args.max_hold_minutes is not None:
        p.max_hold_minutes = args.max_hold_minutes
    if args.fee_rate is not None:
        p.fee_rate = args.fee_rate
    p.validate()
    return p


# ── связка «скринер → направление сделки» ─────────────────────────────────────


def load_screener_cfg(config_path: str | None) -> "sc.Config":
    """Конфиг скринера из config.yml (секция screener) или значения по умолчанию.

    Через bot_config.read_section, чтобы не тащить pyyaml; незнакомые ключи
    (блэклисты, pump-фильтры) игнорируются — их знает только живой скринер.
    """
    raw = bc.read_section(config_path or DEFAULT_CONFIG, "screener")
    cfg = sc.Config()
    known = {f.name for f in sc.Config.__dataclass_fields__.values()}
    for k, v in raw.items():
        if k in known:
            setattr(cfg, k, v)
    sc.validate_config(cfg)
    return cfg


def books_to_rows(records: list[dict]) -> list[list]:
    """Снимки стакана → минутные свечи [ts, open, high, low, close, volume].

    Одна минута = один бар по mid-цене (лучший бид/аск снимка): open — первый
    mid в бакете, high/low — экстремумы, close — последний, volume = 1
    (индикаторы скринера — NATR и UHLO — объём не используют).
    """
    bars: dict[int, list] = {}
    for rec in records:
        bids = [b for b in rec.get("bids", []) if b[1] > 0]
        asks = [a for a in rec.get("asks", []) if a[1] > 0]
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        b = (int(rec["ts"]) // 60_000) * 60_000
        bar = bars.get(b)
        if bar is None:
            bars[b] = [b, mid, mid, mid, mid, 1.0]
        else:
            bar[2] = max(bar[2], mid)
            bar[3] = min(bar[3], mid)
            bar[4] = mid
    return [bars[k] for k in sorted(bars)]


def screener_decisions(rows: list[list], cfg: "sc.Config"):
    """Скринер по закрытым свечам: по одному Decision на каждый новый бар.

    Повторяет логику Backtest.run: окна SymbolState, агрегация старшего ТФ 15м,
    подавление повтора цвета и сброс при уходе в none. Ничего не открывает.
    """
    state = sc.SymbolState(
        max(cfg.natr_period + 2, cfg.uhlo_length * 2 + 2),
        cfg.uhlo_length * 2 + 2,
    )
    slow = bt.aggregate_minutes(rows, 15)
    slow_bucket = 15 * 60_000
    slow_ptr = 0
    for row in rows:
        while slow_ptr < len(slow) and slow[slow_ptr][0] + slow_bucket <= row[0]:
            state.push("slow", slow[slow_ptr][:5])
            slow_ptr += 1
        if not state.push("fast", row[:5]):
            continue
        yield sc.evaluate(list(state.fast), list(state.slow), cfg)


def screener_side(rows: list[list], cfg: "sc.Config") -> str | None:
    """Направление, на котором скринер сейчас стоит: 'Buy'/'Sell' либо None.

    Возвращает сторону последнего прошедшего цвета (повтор того же цвета не
    считается новым сигналом, уход в none сбрасывает направление) — ровно как
    скринер трактует своё состояние в связке с ботом.
    """
    last_color: str | None = None
    for d in screener_decisions(rows, cfg):
        if not d.passed:
            if d.reason in ("uhlo_no_color", "uhlo_slow_missing"):
                last_color = None
            continue
        if d.color != last_color:
            last_color = d.color
    return sc.color_to_side(last_color) if last_color else None


def screener_reason(rows: list[list], cfg: "sc.Config") -> str:
    """Почему скринер не даёт направления — для диагностики в CLI."""
    last = None
    for d in screener_decisions(rows, cfg):
        last = d
    if last is None:
        return "недостаточно закрытых свечей (NATR и UHLO нужна история)"
    if last.passed:
        return f"прошёл (цвет {last.color})"
    extra = f" NATR={last.natr:.4f}" if last.natr is not None else ""
    return f"{last.reason}{extra} {last.details or ''}".strip()


def seed_rows(symbol: str, seed_hours: int) -> list[list]:
    """Прогрев скринера: закрытые минутные свечи с биржи (mainnet, urllib)."""
    now_ms = int(time.time() * 1000)
    start = now_ms - seed_hours * 3_600_000
    rows = bt.fetch_klines(symbol, start, now_ms, "1")
    return [r for r in rows if r[0] + 60_000 <= now_ms]


def resolve_inst_params(symbol: str, qty_step: float | None, min_qty: float | None,
                        tick_size: float | None, fetch: bool) -> tuple[float, float, float]:
    """Шаги инструмента: явные флаги > реальный инструмент (fetch) > BTC-дефолты."""
    if qty_step is not None and min_qty is not None and tick_size is not None:
        return qty_step, min_qty, tick_size
    if fetch:
        inst = bt.fetch_instrument(symbol)
        return (qty_step if qty_step is not None else inst["qty_step"],
                min_qty if min_qty is not None else inst["min_qty"],
                tick_size if tick_size is not None else inst["tick_size"])
    return (qty_step if qty_step is not None else 0.001,
            min_qty if min_qty is not None else 0.001,
            tick_size if tick_size is not None else 0.01)


# ── общий прогон цикла по стакану ─────────────────────────────────────────────


def run_cycle(books: list[dict], params: DcaParams, symbol: str, side: str,
              qty_step: float, min_qty: float, tick_size: float) -> "dc.DcaCycle":
    if not books:
        raise SystemExit("пустой стакан: нечего прогонять")
    cycle = DcaCycle(params, symbol, side, qty_step, min_qty, tick_size)
    t0 = books[0]["ts"]
    cycle.open(books[0], t0)
    for book in books[1:]:
        cycle.step(book, book["ts"])
        if cycle.closed:
            break

    dur = (books[-1]["ts"] - t0) / 60_000
    state = "ЗАКРЫТ" if cycle.closed else "открыт"
    print(f"символ {symbol} ({side}), снимков {len(books)}, интервал {dur:.1f} мин")
    print(f"цикл: {state}, причина: {cycle.exit_reason or '-'}")
    print(f"  qty={cycle.qty:.6f}  avg={cycle.avg_entry:.4f}  docups={cycle.docups}")
    print(f"  TP={cycle.tp_level:.4f} (дист. {cycle.tp_level - cycle.avg_entry:+.4f})")
    if cycle.closed:
        print(f"  выход={cycle.exit_price:.4f}  PnL={cycle.pnl:+.4f} USDT  "
               f"(длит. {cycle.duration_minutes:.1f} мин)")
    else:
        print(f"  следующая докупка на {cycle.next_level:.4f}, "
               f"стоп {cycle.stop_level or 0:.4f}")
    return cycle


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="config.yml бота; по умолчанию config/config.yml корня репо")
    common.add_argument("--symbol", default=None,
                        help="пара (live: по умолчанию BTCUSDT; replay: символ из записи)")
    common.add_argument("--side", choices=("auto", "Buy", "Sell"), default="auto",
                        help="направление сделки; auto — решает скринер (по умолчанию)")
    common.add_argument("--entry-usdt", type=float, default=None)
    common.add_argument("--step-pct", type=float, default=None)
    common.add_argument("--steps", type=int, default=None)
    common.add_argument("--multiplier", type=float, default=None)
    common.add_argument("--tp-pct", type=float, default=None)
    common.add_argument("--tp-escalation", default=None,
                        help="через запятую, например 1.2,1.5,2.0")
    common.add_argument("--stop-pct", type=float, default=None)
    common.add_argument("--max-hold-minutes", type=int, default=None)
    common.add_argument("--fee-rate", type=float, default=None)
    common.add_argument("--qty-step", type=float, default=None)
    common.add_argument("--min-qty", type=float, default=None)
    common.add_argument("--tick-size", type=float, default=None)

    r = sub.add_parser("record", parents=[common])
    r.add_argument("--seconds", type=int, default=75)
    r.add_argument("--interval", type=float, default=2.0)
    r.add_argument("--out", default=None)
    r.add_argument("--base", default=bs.BYBIT_REST)

    p = sub.add_parser("replay", parents=[common])
    p.add_argument("file", help="JSONL-запись стакана (из record)")
    p.add_argument("--candles", default=None,
                   help="CSV со свечами для скринера (ts/open/high/low/close); "
                        "без него свечи восстанавливаются из стакана")

    l = sub.add_parser("live", parents=[common])
    l.add_argument("--seconds", type=int, default=120)
    l.add_argument("--interval", type=float, default=2.0)
    l.add_argument("--base", default=bs.BYBIT_REST)
    l.add_argument("--seed-hours", type=int, default=24,
                   help="глубина прогрев klines скринера, ч")
    l.add_argument("--universe", type=int, default=None,
                   help="вместо --symbol: скринер выбирает монету из топ-N по обороту")
    return ap


def cmd_record(args) -> int:
    symbol = args.symbol or "BTCUSDT"
    out = args.out or os.path.join("data", f"{symbol}-{int(time.time() * 1000)}.jsonl")
    deadline = time.monotonic() + args.seconds
    n = 0
    while time.monotonic() < deadline:
        book = bs.fetch_orderbook(args.base, symbol)
        bs.write_record(out, symbol, book)
        n += 1
        sys.stderr.write(f"\r[record] снимков: {n} → {out}    ")
        sys.stderr.flush()
        time.sleep(args.interval)
    sys.stderr.write("\n")
    print(f"записано {n} снимков → {out}")
    return 0


def cmd_replay(args) -> int:
    books = bs.read_records(args.file)
    symbol = args.symbol or next(
        (r.get("symbol") for r in books if r.get("symbol")), "BTCUSDT")
    params = resolve_params(args)

    cfg = load_screener_cfg(args.config)
    if args.side == "auto":
        if args.candles:
            rows = bt.read_csv(args.candles, None, None)
        else:
            rows = books_to_rows(books)
        side = screener_side(rows, cfg)
        if side is None:
            print(f"[screener] по {symbol} направления нет: {screener_reason(rows, cfg)}")
            print("[screener] запись слишком короткая или флэтовая — увеличьте --seconds"
                  " при record либо укажите --candles с историей свечей")
            return 1
        print(f"[screener] направление {symbol}: {side}")
    else:
        side = args.side

    qty_step, min_qty, tick_size = resolve_inst_params(symbol, args.qty_step,
                                                       args.min_qty, args.tick_size,
                                                       fetch=False)
    run_cycle(books, params, symbol, side, qty_step, min_qty, tick_size)
    return 0


def cmd_live(args) -> int:
    import asyncio

    cfg = load_screener_cfg(args.config)
    if args.universe is not None:
        symbol, side = _pick_by_universe(cfg, args.universe, args.seed_hours)
        if symbol is None:
            print(f"[screener] в топ-{args.universe} ни одна монета не дала направления "
                  f"(по всем причинам: {screener_reason([], cfg)})")
            return 1
        print(f"[screener] выбрана монета: {symbol} ({side})")
    else:
        symbol = args.symbol or "BTCUSDT"
        if args.side == "auto":
            rows = seed_rows(symbol, args.seed_hours)
            side = screener_side(rows, cfg)
            if side is None:
                print(f"[screener] по {symbol} направления нет: "
                      f"{screener_reason(rows, cfg)}")
                return 1
            print(f"[screener] направление {symbol}: {side}")
        else:
            side = args.side

    qty_step, min_qty, tick_size = resolve_inst_params(symbol, args.qty_step,
                                                       args.min_qty, args.tick_size,
                                                       fetch=True)

    q = asyncio.Queue()
    books: list[dict] = []
    stop = time.monotonic() + args.seconds

    async def collect() -> None:
        poller = asyncio.create_task(
            bs.poll_orderbook(q, args.base, symbol, interval_sec=args.interval))
        try:
            while time.monotonic() < stop:
                book = await m.drain_to_latest(q, timeout=args.interval + 2)
                books.append(book)
        except asyncio.TimeoutError:
            pass
        finally:
            poller.cancel()

    asyncio.run(collect())
    params = resolve_params(args)
    run_cycle(books, params, symbol, side, qty_step, min_qty, tick_size)
    return 0


def _pick_by_universe(cfg: "sc.Config", top_n: int, seed_hours: int) -> tuple[str | None, str | None]:
    """Скринер выбирает монету из топ-N: первая, давшая направление по klines."""
    symbols = bt.fetch_universe(top_n, cfg.required_leverage)
    for symbol in symbols:
        rows = seed_rows(symbol, seed_hours)
        side = screener_side(rows, cfg)
        if side is not None:
            return symbol, side
    return None, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"record": cmd_record, "replay": cmd_replay,
            "live": cmd_live}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
