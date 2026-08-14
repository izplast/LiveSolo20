"""
Проверки генератора графиков (reference/report_charts.py).

Синтетические сделки и результаты ab_grid → кривая эквити, просадка,
SVG/PNG. PNG проверяется полным разбором чанков (сигнатура, IHDR, IDAT,
распаковка zlib, размер строк). Без сети, без внешних библиотек.

Запуск:
    python3 specs/001-bybit-dca-testnet/reference/test_report_charts.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
import tempfile
import zlib
import xml.etree.ElementTree as ET

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_charts.py")
_spec = importlib.util.spec_from_file_location("report_charts_under_test", _path)
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
sys.modules["report_charts_under_test"] = rc
_spec.loader.exec_module(rc)

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok     {name}")
    else:
        FAIL += 1
        print(f"  FAIL   {name}   {extra}")


def write_json(data, suffix=".json") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


T0 = 1_760_000_000_000
HOUR = 3_600_000


def trades(n: int = 8, seed_pnl: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"exit_ts": T0 + i * HOUR,
                    "pnl": 0.5 if (i % 3 != 0) else -0.7,
                    "exit_reason": "take_profit"})
    return out


# ── equity_points / drawdown_series ─────────────────────────────────────────

print("equity_points")
pts = rc.equity_points(trades(4))
ok("сделки отсортированы по времени",
   [ts for ts, _ in pts] == sorted(ts for ts, _ in pts), pts)
raw_pnls = [t["pnl"] for t in trades(4)]
ok("кумулятивный PnL на последней точке — сумма исходных сделок",
   abs(pts[-1][1] - sum(raw_pnls)) < 1e-9, pts)
ok("порядок pnl сохранён",
   [v for _, v in pts] == [-0.7, -0.2, 0.3, -0.4], pts)

ok("пустой список → пусто", rc.equity_points([]) == [])
ok("сделки без времени отбрасываются",
   rc.equity_points([{"pnl": 1.0}, {"exit_ts": 5, "pnl": 2.0}])
   == [(5, 2.0)])
ok("Cycle-объекты (duck-typing) тоже понимаются",
   rc.equity_points([type("C", (), {"exit_ts": 10, "pnl": 1.5})()]) == [(10, 1.5)])

print("\ndrawdown_series")
pts2 = rc.equity_points([{"exit_ts": T0, "pnl": 1.0},
                         {"exit_ts": T0 + 1, "pnl": -0.5},
                         {"exit_ts": T0 + 2, "pnl": 2.0},
                         {"exit_ts": T0 + 3, "pnl": -1.0}])
dd = rc.drawdown_series(pts2)
ok("просадка на пике — 0", dd[0][1] == 0.0, dd)
ok("просадка после падения от пика",
   abs(dd[1][1] - 0.5) < 1e-9 and abs(dd[3][1] - 1.0) < 1e-9, dd)
ok("просадка не отрицательна", all(v >= 0 for _, v in dd))
ok("новая высота пика не растёт просадку",
   abs(dd[2][1] - 0.0) < 1e-9, dd)

# ── load_input ──────────────────────────────────────────────────────────────

print("\nload_input")
p = write_json({"cycles": trades(), "title": "Заголовок"})
data, title = rc.load_input(p)
ok("обёртка cycles разобрана", len(data) == 8 and title == "Заголовок")

p2 = write_json([{"exit_ts": 1, "pnl": 2.0}])
ok("голый список", len(rc.load_input(p2)[0]) == 1)

p3 = write_json({"results": [{"label": "a"}]})
ok("обёртка results разобрана", len(rc.load_input(p3)[0]) == 1)

# ── SVG ─────────────────────────────────────────────────────────────────────

print("\nrender_equity_svg")
svg = rc.render_equity_svg(trades(), "Прогон")
ET.fromstring(svg)  # валидный XML
ok("SVG содержит заголовок", "Прогон" in svg)
ok("SVG содержит кривую эквити", "<polyline" in svg and "2563eb" in svg)
ok("SVG содержит просадку", "dc2626" in svg)
ok("SVG содержит заливку", "<polygon" in svg)

svg_empty = rc.render_equity_svg([])
ok("пустой вход → сообщение, а не падение", "нет закрытых сделок" in svg_empty)

svg_dd = rc.render_equity_svg([{"exit_ts": T0, "pnl": 1.0},
                               {"exit_ts": T0 + 1, "pnl": -1.0}])
ok("SVG не падает на убыточной кривой", "polyline" in svg_dd)

svg_esc = rc.render_equity_svg(trades(), "A <B> & C")
ok("XML-эскейпинг в заголовке",
   "&lt;B&gt;" in svg_esc and "&amp;" in svg_esc and "<B>" not in svg_esc)

print("\nrender_metrics_svg")
res = [{"label": "base", "metrics": {"total_pnl": 3.1}},
       {"label": "tp=1.5", "metrics": {"total_pnl": -1.2}}]
ms = rc.render_metrics_svg(res, "total_pnl", "Грид")
ET.fromstring(ms)
ok("столбцы нарисованы", ms.count("fill=\"#16a34a\"") == 1
   and ms.count("fill=\"#dc2626\"") == 1, ms)
ok("положительный бар зелёный", "16a34a" in ms)
ok("отрицательный бар красный", "dc2626" in ms)
ok("значения подписаны", "3.1" in ms and "-1.2" in ms)

ms_no = rc.render_metrics_svg([{"label": "x", "metrics": {}}], "total_pnl")
ok("нет данных по метрике → сообщение", "нет данных" in ms_no)

# ── PNG ─────────────────────────────────────────────────────────────────────

def parse_png(path: str) -> tuple[int, int, bytes]:
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "сигнатура"
    w, h = struct.unpack(">II", data[16:24])
    bd, ct = data[24], data[25]
    assert (bd, ct) == (8, 2), (bd, ct)  # 8-бит RGB
    off = 8
    idat = b""
    while off < len(data):
        ln = struct.unpack(">I", data[off:off + 4])[0]
        tag = data[off + 4:off + 8]
        if tag == b"IDAT":
            idat += data[off + 8:off + 8 + ln]
        off += 12 + ln
    raw = zlib.decompress(idat)
    assert len(raw) == h * (w * 3 + 1), (len(raw), h * (w * 3 + 1))
    return w, h, raw


print("\nrender_equity_png")
p_png = os.path.join(tempfile.gettempdir(), "rc_eq_test.png")
with open(p_png, "wb") as f:
    f.write(rc.render_equity_png(trades(), "Тест", 500, 300))
w, h, raw = parse_png(p_png)
ok("размер PNG совпадает", (w, h) == (500, 300))
ok("есть синие пиксели кривой эквити",
   any(raw[i + 2] > 150 and raw[i] < 120
       for i in range(0, len(raw), 3)))
ok("есть красные пиксели просадки",
   any(raw[i] > 150 and raw[i + 1] < 120 for i in range(0, len(raw), 3)))
os.unlink(p_png)

print("\nrender_metrics_png")
p_png2 = os.path.join(tempfile.gettempdir(), "rc_m_test.png")
with open(p_png2, "wb") as f:
    f.write(rc.render_metrics_png(res, "total_pnl", "Grid", 500, 300))
w2, h2, raw2 = parse_png(p_png2)
ok("размер PNG совпадает", (w2, h2) == (500, 300))
ok("есть зелёные и красные столбцы",
   any(raw2[i + 1] > 150 and raw2[i] < 120 for i in range(0, len(raw2), 3))
   and any(raw2[i] > 150 and raw2[i + 1] < 120 for i in range(0, len(raw2), 3)))
os.unlink(p_png2)

print("\nCanvas (единичные примитивы)")
cv = rc.Canvas(20, 20)
cv.line(0, 0, 5, 0, (0, 0, 0))
ok("линия рисует пиксели", all(cv.px[(0 * 20 + x) * 3] == 0 for x in range(6)))
cv.rect(10, 10, 5, 5, (255, 0, 0))
ok("заливка прямоугольника",
   cv.px[(12 * 20 + 12) * 3] == 255 and cv.px[(12 * 20 + 12) * 3 + 1] == 0)

# ── многосерийный график по символам (T028) ─────────────────────────────────

print("\nmulti_equity_series")
series_in = [{"symbol": "BTCUSDT", "points": [[T0, 0.5], [T0 + HOUR, 1.5]]},
             {"symbol": "ETHUSDT", "points": [[T0, -1.0], [T0 + HOUR, -0.2]]}]
m = rc.multi_equity_series(series_in)
ok("список {symbol, points} → (label, points)",
   m == [("BTCUSDT", [(T0, 0.5), (T0 + HOUR, 1.5)]),
         ("ETHUSDT", [(T0, -1.0), (T0 + HOUR, -0.2)])], m)
m2 = rc.multi_equity_series({"по_символам_эквити": series_in})
ok("обёртка по_символам_эквити", m2 == m, m2)
m3 = rc.multi_equity_series({"BTCUSDT": [[T0, 0.5]], "ETHUSDT": [[T0, -1.0]]})
ok("dict symbol → точки, отсортирован по символам",
   [l for l, _ in m3] == ["BTCUSDT", "ETHUSDT"], m3)
m4 = rc.multi_equity_series({"series": [{"label": "SOL", "points": [[1, 2.0], [3, 4.0]]}]})
ok("обёртка series с label", m4 == [("SOL", [(1, 2.0), (3, 4.0)])], m4)
ok("нечисловые точки отбрасываются",
   rc.multi_equity_series([{"symbol": "A", "points": [[1, 2.0], ["x", 5], [3, "y"]]}])
   == [("A", [(1, 2.0)])], "")

print("\nrender_equity_multi_svg / render_equity_multi_png")
m_svg = rc.render_equity_multi_svg(m, title="По символам", width=800, height=400)
ok("SVG: две полилинии", m_svg.count("<polyline") == 2, m_svg.count("<polyline"))
ok("SVG: легенда с символами",
   "BTCUSDT" in m_svg and "ETHUSDT" in m_svg)
ok("SVG: два цвета серий",
   all(c in m_svg for c in ("#2563eb", "#16a34a")))
ok("SVG: пусто → сообщение", "нет данных по символам" in rc.render_equity_multi_svg([]))

p_multi = os.path.join(tempfile.gettempdir(), "rc_multi.png")
with open(p_multi, "wb") as f:
    f.write(rc.render_equity_multi_png(m, "BySymbol", 500, 300))
wm, hm, rawm = parse_png(p_multi)
ok("PNG: размер совпадает", (wm, hm) == (500, 300))
ok("PNG: есть синие и зелёные пиксели линий",
   any(rawm[i + 2] > 150 and rawm[i] < 120 for i in range(0, len(rawm), 3))
   and any(rawm[i + 1] > 150 and rawm[i] < 120 for i in range(0, len(rawm), 3)))
os.unlink(p_multi)

# ── CLI ─────────────────────────────────────────────────────────────────────

print("\nmain (CLI)")
eq_json = write_json({"cycles": trades(), "title": "CLI equity"})
out_svg = os.path.join(tempfile.gettempdir(), "rc_cli.svg")
rcode = rc.main(["--kind", "equity", "--input", eq_json,
                 "--out", out_svg, "--width", "700", "--height", "300"])
ok("equity SVG: rc=0 и файл создан", rcode == 0 and os.path.exists(out_svg))
ET.parse(out_svg)
os.unlink(out_svg)

out_png = os.path.join(tempfile.gettempdir(), "rc_cli.png")
rcode = rc.main(["--kind", "equity", "--input", eq_json, "--out", out_png])
ok("equity PNG: rc=0 и файл валиден", rcode == 0)
parse_png(out_png)
os.unlink(out_png)

m_json = write_json({"results": res, "title": "CLI metrics"})
out_svg2 = os.path.join(tempfile.gettempdir(), "rc_cli2.svg")
rcode = rc.main(["--kind", "metrics", "--input", m_json, "--metric", "total_pnl",
                 "--out", out_svg2])
ok("metrics SVG: rc=0 и файл создан", rcode == 0 and os.path.exists(out_svg2))
os.unlink(out_svg2)

out_png2 = os.path.join(tempfile.gettempdir(), "rc_cli2.png")
rcode = rc.main(["--kind", "metrics", "--input", m_json, "--out", out_png2])
ok("metrics PNG: rc=0 и файл валиден", rcode == 0)
parse_png(out_png2)
os.unlink(out_png2)

# ── гистограмма длительностей (T026) ─────────────────────────────────────────

print("\nrender_hist_svg / render_hist_png")
bins = [{"lo": 0.0, "hi": 30.0, "count": 2},
        {"lo": 30.0, "hi": 60.0, "count": 0},
        {"lo": 60.0, "hi": 90.0, "count": 5}]
h_svg = rc.render_hist_svg(bins, title="Удержание", x_label="мин")
ok("SVG: бары для ненулевых бинов",
   h_svg.count("<rect") == 3 and "5" in h_svg and "2" in h_svg, h_svg.count("<rect"))
h_empty = rc.render_hist_svg([], title="")
ok("SVG: пусто → сообщение", "нет данных" in h_empty)

p_hist = os.path.join(tempfile.gettempdir(), "rc_hist.png")
with open(p_hist, "wb") as f:
    f.write(rc.render_hist_png(bins, "Hold", 500, 300, x_label="min"))
w_hist, h_hist, rawh = parse_png(p_hist)
ok("PNG: размер совпадает", (w_hist, h_hist) == (500, 300))
ok("PNG: есть синие столбцы",
   any(rawh[i + 2] > 150 and rawh[i] < 120 for i in range(0, len(rawh), 3)))
os.unlink(p_hist)

print("\nmain --kind hist")
h_json = write_json({"hist": bins, "title": "CLI hist"})
out_h = os.path.join(tempfile.gettempdir(), "rc_cli_hist.svg")
rcode = rc.main(["--kind", "hist", "--input", h_json, "--out", out_h,
                 "--x-label", "мин"])
ok("hist SVG: rc=0 и файл создан", rcode == 0 and os.path.exists(out_h))
ET.parse(out_h)
os.unlink(out_h)

out_h2 = os.path.join(tempfile.gettempdir(), "rc_cli_hist.png")
rcode = rc.main(["--kind", "hist", "--input", h_json, "--out", out_h2])
ok("hist PNG: rc=0 и файл валиден", rcode == 0)
parse_png(out_h2)
os.unlink(out_h2)
os.unlink(h_json)

print("\nmain --kind equity-by-symbol")
bs_json = write_json({"по_символам_эквити": [
    {"symbol": "BTCUSDT", "points": [[T0, 0.5], [T0 + HOUR, 1.5]]},
    {"symbol": "ETHUSDT", "points": [[T0, -1.0], [T0 + HOUR, -0.2]]},
], "title": "CLI multi"})
out_bs = os.path.join(tempfile.gettempdir(), "rc_cli_bs.svg")
rcode = rc.main(["--kind", "equity-by-symbol", "--input", bs_json, "--out", out_bs])
ok("equity-by-symbol SVG: rc=0 и файл создан",
   rcode == 0 and os.path.exists(out_bs))
svg_bs = open(out_bs, encoding="utf-8").read()
ok("equity-by-symbol SVG: две серии и легенда",
   svg_bs.count("<polyline") == 2 and "BTCUSDT" in svg_bs)
os.unlink(out_bs)

out_bs2 = os.path.join(tempfile.gettempdir(), "rc_cli_bs.png")
rcode = rc.main(["--kind", "equity-by-symbol", "--input", bs_json, "--out", out_bs2])
ok("equity-by-symbol PNG: rc=0 и файл валиден", rcode == 0)
parse_png(out_bs2)
os.unlink(out_bs2)
os.unlink(bs_json)

for p in (p, p2, p3, eq_json, m_json):
    try:
        os.unlink(p)
    except OSError:
        pass

print(f"\nитог: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)