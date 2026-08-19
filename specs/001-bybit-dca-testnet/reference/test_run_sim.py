"""
Автономные проверки связки «скринер → run_sim» (reference/run_sim.py).

Без сети и без зависимостей: восстановление минутных свечей из снимков стакана
(books_to_rows), решение скринера о направлении сделки (screener_side) на
синтетических трендах и флэте, диагностика отсутствия сигнала
(screener_reason), разбор конфига скринера из config.yml без pyyaml
(load_screener_cfg). Сетевые вызовы не трогаются.

Запуск (без pytest, без зависимостей):
    python3 specs/001-bybit-dca-testnet/reference/test_run_sim.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

_ref = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> "module":
    path = os.path.join(_ref, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rs = _load("run_sim")  # подтягивает dca_cycle, book_streamer, bot_config, screener
sc = rs.sc
bt = rs.bt

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def near(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


def bars(n: int, start: float = 100.0, drift: float = 0.15, rng: float = 1.0,
         ts0: int = 0, step_ms: int = 60_000, amp: float = 0.5, freq: float = 5.0) -> list[list]:
    """Свечи [ts, open, high, low, close, volume]; drift — % на бар, rng — размах %.

    Волнистая база (amp % с периодом freq баров) вместо идеально прямой:
    на прямой UHLO 1м стоит в «углу» (0 и 100 одновременно) и сигнал
    режется фильтром uhlo_corner.
    """
    import math
    out = []
    for i in range(n):
        base = start * (1 + drift * i / 100 + amp * math.sin(2 * math.pi * i / freq) / 100)
        out.append([ts0 + i * step_ms, base, base * (1 + rng / 100),
                    base, base * (1 + rng / 200), 1.0])
    return out


def book(mid: float, ts: int) -> dict:
    return {"bids": [[mid - 0.1, 1.0]], "asks": [[mid + 0.1, 1.0]], "ts": ts}


# ── books_to_rows: снимки стакана → минутные свечи ────────────────────────────

print("books_to_rows")
_t0 = 1_786_371_760_000
_recs = [book(100.2, _t0), book(100.3, _t0 + 1_000),
         book(100.0, _t0 + 60_000), book(100.1, _t0 + 61_000)]
_rows = rs.books_to_rows(_recs)
ok("две минуты → два бара", len(_rows) == 2, _rows)
ok("бакет выровнен к началу минуты",
   _rows[0][0] == (_t0 // 60_000) * 60_000, _rows[0][0])
ok("open — первый mid в бакете", near(_rows[0][1], 100.2), _rows[0][1])
ok("high/low — экстремумы mid в бакете",
   near(_rows[0][2], 100.3) and near(_rows[0][3], 100.2), _rows[0])
ok("close — последний mid в бакете", near(_rows[0][4], 100.3), _rows[0][4])
ok("свечи отсортированы по времени", _rows[1][0] > _rows[0][0], _rows)
ok("пустой ввод → пустой список", rs.books_to_rows([]) == [])

# ── screener_side: направление от скринера ────────────────────────────────────

print("\nscreener_side")
cfg = sc.Config()
up = bars(2000, drift=0.15, rng=1.2)
down = bars(2000, drift=-0.15, rng=1.2)
flat = bars(2000, drift=0.0, rng=0.01)
short = bars(3)

ok("восходящий тренд → Buy (совпало 1м и 15м)",
   rs.screener_side(up, cfg) == "Buy", rs.screener_side(up, cfg))
ok("нисходящий тренд → Sell", rs.screener_side(down, cfg) == "Sell",
   rs.screener_side(down, cfg))
ok("флэт (NATR ниже порога) → None", rs.screener_side(flat, cfg) is None)
ok("короткая история → None", rs.screener_side(short, cfg) is None)

# ── screener_reason: диагностика без сигнала ─────────────────────────────────

print("\nscreener_reason")
ok("короткая история объясняется",
   "insufficient_history" in rs.screener_reason(short, cfg)
   and "fast_bars" in rs.screener_reason(short, cfg),
   rs.screener_reason(short, cfg))
ok("флэт объясняется причиной отсечения",
   "natr" in rs.screener_reason(flat, cfg).lower(), rs.screener_reason(flat, cfg))

# ── load_screener_cfg: конфиг скринера из config.yml (без pyyaml) ────────────

print("\nload_screener_cfg")
_tmp = tempfile.mkdtemp(prefix="run-sim-test-")
_cfg_path = os.path.join(_tmp, "config.yml")
with open(_cfg_path, "w", encoding="utf-8") as f:
    f.write("dca:\n  entry_usdt: 20\n"
            "screener:\n  natr_min: 1.5\n  natr_max: 5.0\n  uhlo_length: 20\n"
            "  tf_fast: \"1\"\n  tf_slow: \"15\"\n")
_c = rs.load_screener_cfg(_cfg_path)
ok("natr-границы из конфига", _c.natr_min == 1.5 and _c.natr_max == 5.0,
   (_c.natr_min, _c.natr_max))
ok("tf из конфига остаются строками", _c.tf_fast == "1" and _c.tf_slow == "15",
   (_c.tf_fast, _c.tf_slow))
ok("uhlo_length из конфига", _c.uhlo_length == 20, _c.uhlo_length)
ok("незнакомые ключи конфига не ломают разбор",
   sc.validate_config(_c) is None and isinstance(_c.top_n_turnover, int))
ok("отсутствующий файл → значения по умолчанию",
   rs.load_screener_cfg("/no/such/config.yml").natr_min == sc.Config().natr_min)

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
