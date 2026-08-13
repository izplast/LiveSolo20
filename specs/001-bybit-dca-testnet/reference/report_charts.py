"""
reference/report_charts.py — графики по результатам бэктестов и A/B-свипов.

Без внешних библиотек: SVG пишется вручную (XML), PNG — собственным
растровым writer'ом на zlib/struct (RGB, 8 бит). Это выдерживает конвенцию
проекта «только стандартная библиотека», чтобы отчёты строились прямо на
устройстве, где шёл прогон.

Графики:
  * эквити (кумулятивный PnL по закрытым циклам) и просадка от пика;
  * сводная столбчатая диаграмма по метрикам грид-свипа (ab_grid.py).

Вход (duck-typing, как в reference/metrics.py):
  * сделки бэктеста — итерация объектов/словарей с полями `exit_ts` и `pnl`
    (reference/backtest.Cycle или dict из JSON);
  * результаты A/B — JSON из ab_grid.py `--out`: [{label, params, metrics}],
    metrics — из ab_common.aggregate (total_pnl, wr, total, n_sl, avg_tp,
    avg_sl, mdd).

Запуск:

    python3 reference/report_charts.py --kind equity \\
        --input trades.json --out equity.svg
    python3 reference/report_charts.py --kind equity \\
        --input trades.json --out equity.png --width 900 --height 420
    python3 reference/report_charts.py --kind metrics \\
        --input ab_grid.json --metric total_pnl --out metrics.png
    python3 reference/report_charts.py --kind metrics \\
        --input ab_grid.json --metric wr --out metrics.svg

JSON для equity: список сделок [{exit_ts, pnl, exit_reason?}] или
{"cycles": [...], "title": "..."}. Для metrics: [{label, metrics}] или
{"results": [...], "title": "..."}.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import zlib
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Нормализация входа
# ---------------------------------------------------------------------------

def _item(value: Any, key: str):
    """Значение поля из объекта с атрибутом или из словаря."""
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def equity_points(closed: Iterable) -> list[tuple[int, float]]:
    """(exit_ts, кумулятивный PnL) по закрытым циклам, отсортированным по времени.

    Понимает и Cycle (атрибуты exit_ts/pnl), и dict (ключи exit_ts/pnl).
    Сделки без времени отбрасываются; pnl отсутствующий/не число — 0.
    """
    trades: list[tuple[int, float]] = []
    for c in closed:
        ts, pnl = _item(c, "exit_ts"), _item(c, "pnl")
        if not isinstance(ts, (int, float)):
            continue
        pnl = float(pnl) if isinstance(pnl, (int, float)) else 0.0
        trades.append((int(ts), pnl))
    trades.sort(key=lambda t: t[0])
    out: list[tuple[int, float]] = []
    run = 0.0
    for ts, pnl in trades:
        run += pnl
        out.append((ts, round(run, 6)))
    return out


def drawdown_series(points: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """Просадка (ts, dd) от пика эквити на каждой точке: dd >= 0, 0 у пика.

    points — уже кумулятивная кривая из equity_points: (ts, equity).
    """
    out: list[tuple[int, float]] = []
    peak = 0.0
    for ts, equity in points:
        if equity > peak:
            peak = equity
        out.append((ts, round(peak - equity, 6)))
    return out


def load_input(path: str) -> tuple[list, str]:
    """Читает JSON: возвращает (данные, title). Поддерживает обёртки
    {"cycles": [...]}, {"results": [...]}, {"trades": [...]}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    title = ""
    if isinstance(raw, dict):
        title = raw.get("title", "")
        for key in ("cycles", "results", "trades"):
            if isinstance(raw.get(key), list):
                return raw[key], title
        return [], title
    if isinstance(raw, list):
        return raw, title
    raise ValueError(f"{path}: ожидался список или объект с cycles/results")


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _esc(text: object) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt(v: float) -> str:
    return f"{v:.4g}"


def svg_header(width: int, height: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">\n')


def _axes_lines(x0: int, y0: int, x1: int, y1: int, grid_y: list[float],
                y_px: callable) -> list[str]:
    """Оси и горизонтальная сетка (значения grid_y — в единицах данных)."""
    out = [f'  <line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#666" '
           f'stroke-width="1"/>\n',
           f'  <line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#666" '
           f'stroke-width="1"/>\n']
    for v in grid_y:
        y = round(y_px(v))
        out.append(f'  <line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" '
                   f'stroke="#ddd" stroke-width="1"/>\n')
    return out


def render_equity_svg(closed: Iterable, title: str = "",
                      width: int = 900, height: int = 420) -> str:
    """SVG с двумя панелями: кривая эквити и просадка от пика."""
    pts = equity_points(closed)
    dd = drawdown_series(pts)
    if not pts:
        return (svg_header(width, height)
                + f'  <text x="{width // 2}" y="{height // 2}" '
                f'text-anchor="middle" font-family="monospace" font-size="14" '
                f'fill="#333">нет закрытых сделок</text>\n</svg>\n')

    margin = {"l": 70, "r": 24, "t": 44, "b": 34}
    gap = 14  # между панелями
    mid = height // 2
    panel_h = mid - margin["t"] - gap // 2
    bottom_h = height - mid - margin["b"]

    def panel_rect(top: int, h: int) -> tuple[int, int, int, int]:
        return (margin["l"], top, width - margin["l"] - margin["r"], h)

    def map_x(ts: int, x0: int, x1: int) -> int:
        t_min, t_max = pts[0][0], pts[-1][0]
        span = max(1, t_max - t_min)
        return round(x0 + (ts - t_min) / span * (x1 - x0))

    def map_y(v: float, v_min: float, v_max: float, y1: int, y0: int) -> int:
        span = max(1e-9, v_max - v_min)
        return round(y1 + (v_max - v) / span * (y0 - y1))

    def series_lines(values: Sequence[tuple[int, float]], x0: int, x1: int,
                     y0: int, y1: int, v_min: float, v_max: float) -> str:
        return " ".join(f"{map_x(ts, x0, x1)},{map_y(v, v_min, v_max, y1, y0)}"
                        for ts, v in values)

    eq_min = min(v for _, v in pts) if pts else 0.0
    eq_max = max(v for _, v in pts) if pts else 0.0
    if eq_max == eq_min:
        eq_max = eq_min + 1.0
    pad = (eq_max - eq_min) * 0.08
    eq_min, eq_max = eq_min - pad, eq_max + pad
    zero_y = map_y(0.0, eq_min, eq_max, margin["t"] + panel_h, margin["t"])

    dd_max = max(v for _, v in dd) if dd else 0.0
    dd_pad = dd_max * 0.1 if dd_max else 1.0
    dd_top = mid + gap // 2
    dd_bottom = height - margin["b"]

    out = [svg_header(width, height),
           '  <rect width="100%" height="100%" fill="#ffffff"/>\n']
    if title:
        out.append(f'  <text x="{margin["l"]}" y="22" font-family="monospace" '
                   f'font-size="14" fill="#222">{_esc(title)}</text>\n')

    # Панель 1: эквити
    x0, y0, x1, y1 = panel_rect(margin["t"], panel_h)
    grid_y = [eq_min + (eq_max - eq_min) * i / 4 for i in range(5)]
    out += _axes_lines(x0, y0, x1, y1, grid_y, lambda v: map_y(v, eq_min, eq_max, y1, y0))
    for v in grid_y:
        out.append(f'  <text x="{x0 - 6}" y="{map_y(v, eq_min, eq_max, y1, y0) + 4}" '
                   f'text-anchor="end" font-family="monospace" font-size="10" '
                   f'fill="#666">{_fmt(v)}</text>\n')
    # заливка под кривой до нуля
    fill_pts = (f"{x0},{zero_y} " + series_lines(pts, x0, x1, y0, y1, eq_min, eq_max)
                + f" {x1},{zero_y}")
    out.append(f'  <polygon points="{fill_pts}" fill="rgba(37,99,235,0.12)"/>\n')
    out.append(f'  <polyline points="{series_lines(pts, x0, x1, y0, y1, eq_min, eq_max)}" '
               f'fill="none" stroke="#2563eb" stroke-width="2"/>\n')
    t0 = pts[0][0]
    out.append(f'  <text x="{x0}" y="{height - margin["b"] - 2}" font-family="monospace" '
               f'font-size="10" fill="#666">{datetime.fromtimestamp(t0 / 1000, tz=timezone.utc).strftime("%Y-%m-%d")}</text>\n')
    t1 = pts[-1][0]
    out.append(f'  <text x="{x1}" y="{height - margin["b"] - 2}" text-anchor="end" '
               f'font-family="monospace" font-size="10" fill="#666">{datetime.fromtimestamp(t1 / 1000, tz=timezone.utc).strftime("%Y-%m-%d")}</text>\n')

    # Панель 2: просадка
    x0b, y0b, x1b, y1b = panel_rect(dd_top, bottom_h)
    grid_dd = [dd_max * i / 4 for i in range(5)]
    out += _axes_lines(x0b, y0b, x1b, y1b, grid_dd,
                       lambda v: map_y(v, 0.0, dd_max, y1b, y0b))
    for v in grid_dd:
        out.append(f'  <text x="{x0b - 6}" y="{map_y(v, 0.0, dd_max, y1b, y0b) + 4}" '
                   f'text-anchor="end" font-family="monospace" font-size="10" '
                   f'fill="#666">{_fmt(v)}</text>\n')
    dd_fill = (f"{x0b},{y1b} " + series_lines(dd, x0b, x1b, y0b, y1b, 0.0, dd_max)
               + f" {x1b},{y1b}")
    out.append(f'  <polygon points="{dd_fill}" fill="rgba(220,38,38,0.18)"/>\n')
    out.append(f'  <polyline points="{series_lines(dd, x0b, x1b, y0b, y1b, 0.0, dd_max)}" '
               f'fill="none" stroke="#dc2626" stroke-width="2"/>\n')
    out.append(f'  <text x="{margin["l"]}" y="{y0b - 4}" font-family="monospace" '
               f'font-size="11" fill="#333">просадка, $</text>\n')
    out.append(f'  <text x="{margin["l"]}" y="{y0 - 4}" font-family="monospace" '
               f'font-size="11" fill="#333">эквити, $</text>\n')
    out.append("</svg>\n")
    return "".join(out)


def render_metrics_svg(results: Sequence[dict], metric: str = "total_pnl",
                       title: str = "", width: int = 900,
                       height: int = 420) -> str:
    """SVG столбчатая диаграмма по одной метрике результатов ab_grid."""
    rows = [(r.get("label", "?"), (r.get("metrics") or {}).get(metric))
            for r in results]
    rows = [(l, v) for l, v in rows if isinstance(v, (int, float))]
    if not rows:
        return (svg_header(width, height)
                + f'  <text x="{width // 2}" y="{height // 2}" '
                f'text-anchor="middle" font-family="monospace" font-size="14" '
                f'fill="#333">нет данных по метрике {_esc(metric)}</text>\n</svg>\n')

    margin = {"l": 70, "r": 24, "t": 44, "b": 60}
    x0, y0 = margin["l"], margin["t"]
    x1, y1 = width - margin["r"], height - margin["b"]
    values = [v for _, v in rows]
    v_min = min(0.0, min(values))
    v_max = max(values)
    if v_max == v_min:
        v_max = v_min + 1.0
    pad = (v_max - v_min) * 0.08
    v_min, v_max = v_min - pad, v_max + pad

    def y_px(v: float) -> int:
        return round(y1 + (v_max - v) / (v_max - v_min) * (y0 - y1))

    out = [svg_header(width, height),
           '  <rect width="100%" height="100%" fill="#ffffff"/>\n']
    if title:
        out.append(f'  <text x="{x0}" y="22" font-family="monospace" font-size="14" '
                   f'fill="#222">{_esc(title)}</text>\n')

    grid_y = [v_min + (v_max - v_min) * i / 4 for i in range(5)]
    out += _axes_lines(x0, y0, x1, y1, grid_y, y_px)
    for v in grid_y:
        out.append(f'  <text x="{x0 - 6}" y="{y_px(v) + 4}" text-anchor="end" '
                   f'font-family="monospace" font-size="10" fill="#666">'
                   f'{_fmt(v)}</text>\n')

    n = len(rows)
    slot = (x1 - x0) / n
    bar_w = max(6.0, slot * 0.62)
    for i, (label, v) in enumerate(rows):
        cx = x0 + slot * i + slot / 2
        bx0 = round(cx - bar_w / 2)
        by = y_px(v)
        color = "#16a34a" if v >= 0 else "#dc2626"
        if by != y1:
            out.append(f'  <rect x="{bx0}" y="{min(by, y1)}" '
                       f'width="{round(bar_w)}" height="{abs(by - y1)}" '
                       f'fill="{color}" opacity="0.85"/>\n')
        out.append(f'  <text x="{round(cx)}" y="{by - 4}" text-anchor="middle" '
                   f'font-family="monospace" font-size="10" fill="#333">'
                   f'{_fmt(v)}</text>\n')
        short = (label[:26] + "…") if len(label) > 27 else label
        out.append(f'  <text x="{round(cx)}" y="{y1 + 14}" text-anchor="end" '
                   f'transform="rotate(-55 {round(cx)} {y1 + 14})" '
                   f'font-family="monospace" font-size="9" fill="#555">'
                   f'{_esc(short)}</text>\n')
    out.append("</svg>\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# PNG (растровый writer, только stdlib)
# ---------------------------------------------------------------------------

# Минимальный точечный шрифт 5x7 для цифр и знаков подписей осей/значений.
# Символ → 7 строк, каждая — 5 бит (бит 4 = левый пиксель).
_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    "-": (0, 0, 0, 0x1F, 0, 0, 0),
    "+": (0, 0x04, 0x04, 0x1F, 0x04, 0x04, 0),
    ".": (0, 0, 0, 0, 0, 0x0C, 0x0C),
    ":": (0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0),
    "/": (0x01, 0x02, 0x04, 0x08, 0x10, 0, 0),
    "%": (0x11, 0x12, 0x04, 0x08, 0x09, 0x11, 0),
    "_": (0, 0, 0, 0, 0, 0, 0x1F),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "=": (0, 0, 0x1F, 0, 0x1F, 0, 0),
}


class Canvas:
    """Растровое полотно RGB; линии по Брезенхэму, заливка, текст 5x7."""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int] = (255, 255, 255)):
        self.w = int(width)
        self.h = int(height)
        self.px = bytearray([bg[0], bg[1], bg[2]]) * (self.w * self.h)

    def set(self, x: int, y: int, c: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i], self.px[i + 1], self.px[i + 2] = c

    def line(self, x0: int, y0: int, x1: int, y1: int,
             c: tuple[int, int, int]) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def hline(self, x0: int, x1: int, y: int, c: tuple[int, int, int]) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.set(x, y, c)

    def rect(self, x: int, y: int, w: int, h: int,
             c: tuple[int, int, int]) -> None:
        for yy in range(max(0, y), min(self.h, y + h)):
            for xx in range(max(0, x), min(self.w, x + w)):
                self.set(xx, yy, c)

    def text(self, x: int, y: int, s: str,
             c: tuple[int, int, int] = (0, 0, 0)) -> None:
        cx = x
        for ch in s:
            glyph = _FONT.get(ch)
            if glyph is None:
                cx += 6
                continue
            for row in range(7):
                bits = glyph[row]
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        self.set(cx + col, y + row, c)
            cx += 6

    def polyline(self, pts: Sequence[tuple[int, int]],
                 c: tuple[int, int, int]) -> None:
        for a, b in zip(pts, pts[1:]):
            self.line(*a, *b, c)

    def save(self) -> bytes:
        """PNG (RGB, 8 бит): сигнатура + IHDR + IDAT(zlib) + IEND."""
        raw = bytearray()
        stride = self.w * 3
        row = bytearray(stride)
        for y in range(self.h):
            row[:] = self.px[y * stride:(y + 1) * stride]
            raw.append(0)  # фильтр None
            raw += row
        idat = zlib.compress(bytes(raw), 6)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", idat)
                + chunk(b"IEND", b""))


def _png_series(pts: Sequence[tuple[int, float]], cv: Canvas,
                x0: int, x1: int, y0: int, y1: int,
                v_min: float, v_max: float,
                color: tuple[int, int, int]) -> None:
    def m_x(ts: int) -> int:
        span = max(1, pts[-1][0] - pts[0][0])
        return round(x0 + (ts - pts[0][0]) / span * (x1 - x0))

    def m_y(v: float) -> int:
        span = max(1e-9, v_max - v_min)
        return round(y1 + (v_max - v) / span * (y0 - y1))

    cv.polyline([(m_x(ts), m_y(v)) for ts, v in pts], color)


def render_equity_png(closed: Iterable, title: str = "",
                      width: int = 900, height: int = 420) -> bytes:
    """PNG: эквити (сверху) и просадка (снизу)."""
    pts = equity_points(closed)
    dd = drawdown_series(pts)
    cv = Canvas(width, height)
    if not pts:
        cv.text(width // 2 - 60, height // 2, "NO CLOSED TRADES", (51, 51, 51))
        return cv.save()

    margin = (70, 24, 44, 34)  # l, r, t, b
    l, r, t, b = margin
    gap = 14
    mid = height // 2
    x0, x1 = l, width - r

    def panel(top: int, h: int) -> tuple[int, int, int, int]:
        return x0, top, x1, top + h

    def map_y(v: float, v_min: float, v_max: float, y_top: int, y_bot: int) -> int:
        span = max(1e-9, v_max - v_min)
        return round(y_bot + (v_max - v) / span * (y_top - y_bot))

    eq_min = min(v for _, v in pts)
    eq_max = max(v for _, v in pts)
    if eq_max == eq_min:
        eq_max = eq_min + 1.0
    pad = (eq_max - eq_min) * 0.08
    eq_min, eq_max = eq_min - pad, eq_max + pad

    dd_max = max(v for _, v in dd) if dd else 0.0
    if dd_max == 0:
        dd_max = 1.0

    # Ось и сетка эквити
    _, ay0, _, ay1 = panel(t, mid - t - gap // 2)
    for i in range(5):
        v = eq_min + (eq_max - eq_min) * i / 4
        yy = map_y(v, eq_min, eq_max, ay0, ay1)
        cv.hline(x0, x1, yy, (221, 221, 221))
        cv.text(x0 - 34, yy - 3, f"{v:.2f}", (102, 102, 102))
    cv.line(x0, ay0, x0, ay1, (102, 102, 102))
    cv.line(x0, ay1, x1, ay1, (102, 102, 102))

    zero_y = map_y(0.0, eq_min, eq_max, ay0, ay1)
    _png_series(pts, cv, x0, x1, ay0, ay1, eq_min, eq_max, (37, 99, 235))
    cv.line(x0, zero_y, x1, zero_y, (200, 200, 200))
    # заливка просадки по пикселям через line от кривой вниз (упрощённо: сетка
    # точек кривой) — экономим: рисуем только саму кривую и кривую просадки.

    # Ось и сетка просадки
    _, by0, _, by1 = panel(mid + gap // 2, height - (mid + gap // 2) - b)
    for i in range(5):
        v = dd_max * i / 4
        yy = map_y(v, 0.0, dd_max, by0, by1)
        cv.hline(x0, x1, yy, (221, 221, 221))
        cv.text(x0 - 34, yy - 3, f"{v:.2f}", (102, 102, 102))
    cv.line(x0, by0, x0, by1, (102, 102, 102))
    cv.line(x0, by1, x1, by1, (102, 102, 102))
    _png_series(dd, cv, x0, x1, by0, by1, 0.0, dd_max, (220, 38, 38))

    if title:
        cv.text(l, 12, _ascii_safe(title)[:40], (34, 34, 34))
    cv.text(l, height - 12, _ascii_safe(pts[0][0]), (102, 102, 102))
    t1 = pts[-1][0]
    cv.text(x1 - 60, height - 12, _ascii_safe(t1), (102, 102, 102))
    return cv.save()


def _ascii_safe(ms: int | str) -> str:
    """Метка времени → 'YYYY-MM-DD HH:MM' (поддерживается шрифтом 5x7)."""
    if isinstance(ms, (int, float)):
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
    return ms


def render_metrics_png(results: Sequence[dict], metric: str = "total_pnl",
                       title: str = "", width: int = 900,
                       height: int = 420) -> bytes:
    """PNG столбчатая диаграмма по одной метрике (подписи — значения и №)."""
    rows = [(i, r.get("label", "?"), (r.get("metrics") or {}).get(metric))
            for i, r in enumerate(results)]
    rows = [(i, l, v) for i, l, v in rows if isinstance(v, (int, float))]
    cv = Canvas(width, height)
    if not rows:
        cv.text(width // 2 - 70, height // 2, "NO DATA", (51, 51, 51))
        return cv.save()

    l, r, t, b = 70, 24, 44, 60
    x0, x1 = l, width - r
    y0, y1 = t, height - b
    values = [v for _, _, v in rows]
    v_min = min(0.0, min(values))
    v_max = max(values)
    if v_max == v_min:
        v_max = v_min + 1.0
    pad = (v_max - v_min) * 0.08
    v_min, v_max = v_min - pad, v_max + pad

    def m_y(v: float) -> int:
        return round(y1 + (v_max - v) / (v_max - v_min) * (y0 - y1))

    for i in range(5):
        v = v_min + (v_max - v_min) * i / 4
        yy = m_y(v)
        cv.hline(x0, x1, yy, (221, 221, 221))
        cv.text(x0 - 44, yy - 3, f"{v:.2f}", (102, 102, 102))
    cv.line(x0, y0, x0, y1, (102, 102, 102))
    cv.line(x0, y1, x1, y1, (102, 102, 102))

    n = len(rows)
    slot = (x1 - x0) / n
    bar_w = max(6, round(slot * 0.62))
    for i, label, v in rows:
        cx = round(x0 + slot * i + slot / 2)
        bx0 = cx - bar_w // 2
        by = m_y(v)
        color = (22, 163, 74) if v >= 0 else (220, 38, 38)
        if by != y1:
            cv.rect(bx0, min(by, y1), bar_w, abs(by - y1), color)
        cv.text(cx - 10, by - 12, f"{v:.2f}", (51, 51, 51))
        cv.text(bx0, y1 + 12, f"#{i + 1}", (85, 85, 85))
    if title:
        cv.text(l, 12, title[:40], (34, 34, 34))
    return cv.save()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write(path: str, data: str | bytes) -> None:
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, encoding=None if isinstance(data, bytes) else "utf-8") as f:
        f.write(data)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Графики по бэктестам и A/B-свипам (SVG/PNG, без зависимостей)")
    ap.add_argument("--kind", choices=("equity", "metrics"), required=True,
                    help="equity — эквити и просадка; metrics — бар-диаграмма")
    ap.add_argument("--input", required=True,
                    help="JSON: сделки [{exit_ts, pnl}] или результаты ab_grid")
    ap.add_argument("--out", required=True, help="файл .svg или .png")
    ap.add_argument("--metric", default="total_pnl",
                    help="метрика для --kind metrics (по умолчанию total_pnl)")
    ap.add_argument("--title", default="",
                    help="заголовок графика (по умолчанию из JSON или пусто)")
    ap.add_argument("--width", type=int, default=900)
    ap.add_argument("--height", type=int, default=420)
    args = ap.parse_args(argv)

    data, json_title = load_input(args.input)
    title = args.title or json_title
    ext = os.path.splitext(args.out)[1].lower()

    if args.kind == "equity":
        if ext in (".png", ".PNG"):
            data_bytes = render_equity_png(data, title, args.width, args.height)
            _write(args.out, data_bytes)
        else:
            _write(args.out, render_equity_svg(data, title, args.width, args.height))
    else:
        if ext in (".png", ".PNG"):
            data_bytes = render_metrics_png(data, args.metric, title,
                                            args.width, args.height)
            _write(args.out, data_bytes)
        else:
            _write(args.out, render_metrics_svg(data, args.metric, title,
                                                args.width, args.height))
    sys.stderr.write(f"[report_charts] {args.out}: {args.kind}, "
                     f"{len(data)} записей\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())