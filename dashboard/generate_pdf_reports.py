from __future__ import annotations

"""
AI Powered Golden Eye - Reused Photo Monthly Bulletin Generator V166

Standalone PDF generator, not a Dash screenshot.
Reads PostgreSQL silver data directly and creates a formal monthly bulletin.

V17 focus:
- Top Pemanen/Personel keeps the original All Time page and appends a clean latest-month page.
- Highlighted evidence is sourced only from the latest available transaction month in the scoped data.
- Two latest-month highlights are shown: most frequent reused group and newest reused transaction.
- Harmony Transport Reuse for PWP and DIN shows one reused group with 4 photos: 2 Panen + 2 Transport; latest month is preferred with all-history fallback when needed.
- Production-data safe layout, not only demo data.
- Wider boxes, larger row heights, and stronger text clipping.
- Top Reused Groups paginates earlier so real long values do not become tiny/overlap.
- KPI cards use real vector icons instead of placeholder letters inside circles.
- Highlighted Examples use 4-card layouts for Top Group All Time and Latest Month evidence.
- Chart panels use larger rendered charts and consistent spacing.
- One combined report for HARMONY/PWP, HARMONY/DIN, ELHP/DAN, and ELHP/DWT by default on ALL/ALL.
- Report is organized as one PT/company per section, with narrative storytelling.
- Each PT section contains narrative, monthly pattern, and Top Reused by Month stacked chart so the repeated usage timeline is clear.
- Monthly pattern and top findings are explained inside each PT section.
- Harmony-only Similarity Transport section remains separated and explicit.
- Ticket percentage uses the dashboard formula and source: total usage / SAP tickets
  from gold.sap_oph_kpi_summary.
- Narrative average is fixed to Avg Usage per Reused Group (same logic as cover KPI).
- Cover footer spacing is fixed so text never touches divider lines.
- Harmony section now includes an example comparison page showing panen photo vs transport photo.
- Final page provides a clickable dashboard access button and contact email for presentation/follow-up.
- Every company section includes Top Kerani Panen per Case Reused (unique OPH) and Top Mandor charts, excluding blank personnel names.
- V166 visual standard: one blue accent for all Reused/Transport backgrounds and charts; all report text uses bold typography.

Examples:
  py generate_ai_powered_golden_eye_bulletin.py --system ALL --company ALL --period-label "Juni 2026"
  py generate_ai_powered_golden_eye_bulletin.py --system ELHP --company DAN --month 2026-06 --period-label "Juni 2026" --top 15 --examples 6
  py generate_ai_powered_golden_eye_bulletin.py --demo --system ELHP --company DAN --period-label "Juni 2026"
"""

import argparse
import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from PIL import Image, ImageDraw

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from queries import (
        load_groups,
        load_details,
        apply_filters,
        detail_for_groups,
        kpis,
        load_sap_kpis,
        transport_similarity_usage_summary,
        split_values,
        clean_text,
    )
except Exception:
    load_groups = load_details = apply_filters = detail_for_groups = kpis = load_sap_kpis = None
    transport_similarity_usage_summary = None
    split_values = lambda x: [v.strip() for v in re.split(r"[,;|]+", str(x or "")) if v.strip()]
    clean_text = lambda x: "" if x is None else str(x).strip()

try:
    from db import (
        fetch_image_bytes,
        get_image_storage_warnings,
    )
except Exception:
    fetch_image_bytes = None
    get_image_storage_warnings = lambda: {}

PAGE_W, PAGE_H = A4
SCRIPT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = Path(os.getenv("PDF_REPORT_DIR", "reports"))
LOGO_PATH = Path(
    os.getenv("PDF_LOGO_PATH", str(SCRIPT_DIR / "assets" / "dsn_group_logo.png"))
)
DEFAULT_DASHBOARD_URL = os.getenv("REPORT_DASHBOARD_URL", "https://throng-bronco-hefty.ngrok-free.dev/")
DEFAULT_CONTACT_EMAIL = os.getenv("REPORT_CONTACT_EMAIL", "muhammad.rezaputra@dsngroup.co.id")

# PDF size controls. Real evidence photos from BYTEA can be very large; embed a compressed
# copy only, because the report only displays thumbnails/panels.
PDF_IMAGE_MAX_DIM = int(os.getenv("PDF_IMAGE_MAX_DIM", "900"))
PDF_IMAGE_JPEG_QUALITY = int(os.getenv("PDF_IMAGE_JPEG_QUALITY", "62"))
PDF_CHART_DPI = int(os.getenv("PDF_CHART_DPI", "135"))

SYSTEM_COMPANY_DEFAULTS = [
    ("HARMONY", "PWP"),
    ("HARMONY", "DIN"),
    ("ELHP", "DAN"),
    ("ELHP", "DWT"),
]
UNAVAILABLE_IMAGE_KEYS: set[str] = set()

# Brand palette from DSN logo.
# V167: keep visual depth/gradation, but any background directly behind a text
# label uses one consistent navy family so users do not infer red-vs-blue status.
NAVY = colors.HexColor("#003B7A")
BLUE = colors.HexColor("#0B4A8F")
ACCENT_BLUE_2 = colors.HexColor("#2F6FA6")
ACCENT_BLUE_3 = colors.HexColor("#5C8FBC")
ACCENT_BLUE_4 = colors.HexColor("#8EB4D5")
LABEL_BG = NAVY
# Backward-compatible alias: legacy code that passed RED for a text-bearing
# badge/header now resolves to the same LABEL_BG instead of red.
RED = LABEL_BG
DARK = colors.HexColor("#1F2937")
SLATE = colors.HexColor("#475569")
MUTED = colors.HexColor("#64748B")
GRAY = colors.HexColor("#94A3B8")
BORDER = colors.HexColor("#DDE3EA")
GRID = colors.HexColor("#E7ECF3")
LIGHT = colors.HexColor("#F7F9FC")
SOFT_BLUE = colors.HexColor("#EFF6FF")
SOFT_BLUE_2 = colors.HexColor("#E7F0FA")
SOFT_BLUE_3 = colors.HexColor("#DCEAF6")
SOFT_RED = SOFT_BLUE_2
SOFT_GRAY = colors.HexColor("#F3F4F6")
WHITE = colors.white

REUSED_TEXT = BLUE
REUSED_ACCENT = BLUE

# Charts use a monochromatic blue gradient instead of one flat blue or red/blue.
CHART_BLUE = "#0B4A8F"
CHART_TEXT = "#334155"
CHART_PALETTE = ["#003B7A", "#0B4A8F", "#2F6FA6", "#5C8FBC", "#8EB4D5", "#B8D1E7"]
CHART_HATCHES = ["", "///", "\\", "xx", "..", "++", "oo", "**"]
MONTH_DISTINCT_PALETTE = ["#1D4ED8", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444", "#06B6D4", "#F97316", "#84CC16", "#EC4899", "#6366F1", "#14B8A6", "#A16207"]
TRANSPORT_STATUS_PALETTE = {"SAMA": "#16A34A", "BERBEDA": "#F59E0B", "TIDAK ADA TRANSPORT": "#DC2626"}
matplotlib.rcParams.update({
    "font.weight": "bold",
    "axes.labelweight": "bold",
    "axes.titleweight": "bold",
})

def style_chart_axis(ax) -> None:
    """V167: bold typography with neutral text and blue-gradient data marks."""
    for label in list(ax.get_xticklabels()) + list(ax.get_yticklabels()):
        label.set_fontweight("bold")
        label.set_color(CHART_TEXT)
    ax.xaxis.label.set_fontweight("bold")
    ax.yaxis.label.set_fontweight("bold")
    ax.xaxis.label.set_color(CHART_TEXT)
    ax.yaxis.label.set_color(CHART_TEXT)
    ax.title.set_fontweight("bold")
    ax.title.set_color(CHART_TEXT)
    for txt in ax.texts:
        txt.set_fontweight("bold")
        if str(txt.get_color()).lower() not in {"white", "#ffffff"}:
            txt.set_color(CHART_TEXT)
    legend = ax.get_legend()
    if legend is not None:
        for txt in legend.get_texts():
            txt.set_fontweight("bold")
            txt.set_color(CHART_TEXT)
        if legend.get_title() is not None:
            legend.get_title().set_fontweight("bold")
            legend.get_title().set_color(CHART_TEXT)

MARGIN_X = 17 * mm
FOOTER_H = 6 * mm


def fmt_int(x: Any) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", ".")
    except Exception:
        return "0"


def fmt_num(x: Any, decimals: int = 2) -> str:
    try:
        return f"{float(x):,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except Exception:
        return "0"


def fmt_pct(x: Any, decimals: int = 2) -> str:
    if x is None:
        return "N/A"
    try:
        return fmt_num(float(x), decimals) + "%"
    except Exception:
        return "N/A"


def safe_text(x: Any, default: str = "-") -> str:
    try:
        s = clean_text(x) if clean_text else str(x or "")
    except Exception:
        s = str(x or "")
    s = str(s or "").strip()
    if not s or s.lower() in {"nan", "none", "null", "nat", "n/a", "na", "-"}:
        return default
    return s


def split_first(x: Any, default: str = "-") -> str:
    try:
        vals = split_values(x)
    except Exception:
        vals = [v.strip() for v in re.split(r"[,;|]+", str(x or "")) if v.strip()]
    return vals[0] if vals else default


def csv_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in str(value).split(",") if x.strip()]


def sanitize_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "report")).strip("_") or "report"


def to_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0)


def text_width(c: canvas.Canvas, text: str, font: str, size: float) -> float:
    return c.stringWidth(str(text), font, size)


def ellipsize(c: canvas.Canvas, text: Any, max_width: float, font: str = "Helvetica-Bold", size: float = 7) -> str:
    s = safe_text(text, "")
    if text_width(c, s, font, size) <= max_width:
        return s
    suffix = "..."
    while s and text_width(c, s + suffix, font, size) > max_width:
        s = s[:-1]
    return (s + suffix) if s else suffix


def draw_fit(c: canvas.Canvas, text: Any, x: float, y: float, max_width: float, font: str = "Helvetica-Bold", size: float = 7, color=REUSED_TEXT, align: str = "left", min_size: float = 5.0) -> str:
    """Draw a single line that always stays inside max_width.

    It first reduces font size slightly, then ellipsizes. This is used for
    production data where OPH/file/category values can be much longer than demo.
    """
    s = safe_text(text, "")
    draw_size = float(size)
    while draw_size > min_size and text_width(c, s, font, draw_size) > max_width:
        draw_size -= 0.25
    s = ellipsize(c, s, max_width, font, draw_size)
    c.setFillColor(color)
    c.setFont(font, draw_size)
    if align == "center":
        c.drawCentredString(x + max_width / 2, y, s)
    elif align == "right":
        c.drawRightString(x + max_width, y, s)
    else:
        c.drawString(x, y, s)
    return s


def short_category(value: Any) -> str:
    s = safe_text(value, "-")
    repl = {
        "Blok Sama, TPH Berbeda": "Blok Sama / TPH Beda",
        "Blok Sama / TPH Berbeda": "Blok Sama / TPH Beda",
        "Tanggal Berbeda, Bulan Berbeda": "Tgl Beda / Bulan Beda",
        "Tanggal Berbeda, Bulan Sama": "Tgl Beda / Bulan Sama",
        "Afdeling Berbeda": "Afd Berbeda",
    }
    return repl.get(s, s)


def wrap_lines(c: canvas.Canvas, text: Any, max_width: float, font: str = "Helvetica-Bold", size: float = 8, max_lines: int = 3) -> list[str]:
    raw = safe_text(text, "")
    words = raw.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if text_width(c, test, font, size) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = ellipsize(c, lines[-1], max_width, font, size)
    return lines


def draw_text_lines(c: canvas.Canvas, lines: list[str], x: float, y: float, font="Helvetica-Bold", size=8, leading=10, color=REUSED_TEXT):
    c.setFont(font, size)
    c.setFillColor(color)
    yy = y
    for line in lines:
        c.drawString(x, yy, line)
        yy -= leading
    return yy


def rounded_rect(c: canvas.Canvas, x, y, w, h, radius=8, fill=WHITE, stroke=BORDER, width=0.7):
    c.saveState()
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(width)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)
    c.restoreState()


def draw_footer_bar(c: canvas.Canvas):
    c.saveState()
    c.setFillColor(NAVY); c.rect(0, 0, PAGE_W * 0.32, FOOTER_H, fill=1, stroke=0)
    c.setFillColor(ACCENT_BLUE_2); c.rect(PAGE_W * 0.32, 0, PAGE_W * 0.34, FOOTER_H, fill=1, stroke=0)
    c.setFillColor(ACCENT_BLUE_4); c.rect(PAGE_W * 0.66, 0, PAGE_W * 0.34, FOOTER_H, fill=1, stroke=0)
    c.restoreState()


def draw_decor(c: canvas.Canvas):
    # top-left corporate corner, static and neat
    c.saveState()
    c.setFillColor(NAVY)
    p = c.beginPath()
    p.moveTo(0, PAGE_H)
    p.lineTo(30 * mm, PAGE_H)
    p.lineTo(0, PAGE_H - 30 * mm)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor("#D6DFEA"))
    c.setLineWidth(0.55)
    c.line(16 * mm, PAGE_H - 3 * mm, 3 * mm, PAGE_H - 16 * mm)
    # subtle dotted pattern at right bottom, not behind text
    c.setFillColor(colors.HexColor("#DEE4EC"))
    for row in range(12):
        for col in range(7):
            c.circle(PAGE_W - (10 + col * 2.4) * mm, 76 * mm - row * 2.4 * mm, 0.22 * mm, fill=1, stroke=0)
    c.restoreState()


def draw_logo(c: canvas.Canvas, x: float, y: float, w: float, h: float):
    path = Path(LOGO_PATH)
    if path.exists():
        try:
            c.drawImage(ImageReader(str(path)), x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
            return
        except Exception:
            pass
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y + h * 0.35, "DSN")
    c.setFillColor(REUSED_TEXT)
    c.drawString(x + 27 * mm, y + h * 0.35, "GROUP")


def draw_header(c: canvas.Canvas, title: str, period: str, page_no: int):
    draw_decor(c)
    draw_logo(c, 21 * mm, PAGE_H - 25 * mm, 42 * mm, 13 * mm)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 16 * mm, "AI POWERED GOLDEN EYE")
    c.setFillColor(REUSED_TEXT)
    c.setFont("Helvetica-Bold", 7.3)
    c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 21 * mm, "REUSED PHOTO MONTHLY BULLETIN")
    rounded_rect(c, PAGE_W - 43 * mm, PAGE_H - 31 * mm, 27 * mm, 8 * mm, 3 * mm, fill=NAVY, stroke=NAVY, width=0)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawCentredString(PAGE_W - 29.5 * mm, PAGE_H - 28.4 * mm, period.upper())

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(MARGIN_X, PAGE_H - 46 * mm, title.upper())
    c.setStrokeColor(NAVY)
    c.setLineWidth(1.1)
    c.line(MARGIN_X, PAGE_H - 50 * mm, MARGIN_X + 13 * mm, PAGE_H - 50 * mm)

    c.setFillColor(REUSED_TEXT)
    c.setFont("Helvetica-Bold", 6)
    c.drawRightString(PAGE_W - 15 * mm, 9 * mm, f"{page_no:02d}")
    draw_footer_bar(c)


def draw_badge(c: canvas.Canvas, x, y, w, h, text: str, accent=NAVY):
    rounded_rect(c, x, y, w, h, h / 2, fill=WHITE, stroke=BORDER, width=0.65)
    c.setFillColor(accent)
    c.rect(x + 5 * mm, y + h / 2 - 1 * mm, 2 * mm, 2 * mm, fill=1, stroke=0)
    c.setFillColor(REUSED_TEXT)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x + 11 * mm, y + h / 2 - 2.2, ellipsize(c, text.upper(), w - 14 * mm, "Helvetica-Bold", 6.8))




def draw_calendar_period_badge(c: canvas.Canvas, x: float, y: float, w: float, h: float, period: str):
    """Blue period badge used on the cover, separated from KPI cards."""
    rounded_rect(c, x, y, w, h, 2.2 * mm, fill=NAVY, stroke=NAVY, width=0)
    # calendar icon - vector, no external font dependency
    ix = x + 5.0 * mm
    iy = y + h / 2
    c.saveState()
    c.setStrokeColor(WHITE)
    c.setFillColor(WHITE)
    c.setLineWidth(0.8)
    c.roundRect(ix - 2.0 * mm, iy - 2.4 * mm, 4.2 * mm, 4.8 * mm, 0.5 * mm, fill=0, stroke=1)
    c.rect(ix - 2.0 * mm, iy + 0.9 * mm, 4.2 * mm, 1.1 * mm, fill=1, stroke=0)
    c.circle(ix - 0.9 * mm, iy + 2.5 * mm, 0.25 * mm, fill=1, stroke=0)
    c.circle(ix + 1.0 * mm, iy + 2.5 * mm, 0.25 * mm, fill=1, stroke=0)
    c.restoreState()
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(x + 12.0 * mm, y + h / 2 - 3.1, period.upper())


def draw_metric_icon(c: canvas.Canvas, cx: float, cy: float, kind: str, color=WHITE):
    """Small vector icons for KPI circles. No placeholder letters are used.

    kind values:
    - group: people/users icon for Total Group
    - usage: checklist icon for Total Usage
    - max: bar chart icon for Max Usage
    - avg: trend line icon for Avg Usage
    """
    c.saveState()
    c.setFillColor(color)
    c.setStrokeColor(color)
    c.setLineWidth(0.8)

    if kind == "group":
        # Three heads
        c.circle(cx, cy + 1.4 * mm, 1.05 * mm, fill=1, stroke=0)
        c.circle(cx - 2.0 * mm, cy + 0.6 * mm, 0.85 * mm, fill=1, stroke=0)
        c.circle(cx + 2.0 * mm, cy + 0.6 * mm, 0.85 * mm, fill=1, stroke=0)
        # Shoulders
        c.roundRect(cx - 2.0 * mm, cy - 2.4 * mm, 4.0 * mm, 2.2 * mm, 1.0 * mm, fill=1, stroke=0)
        c.roundRect(cx - 4.0 * mm, cy - 2.7 * mm, 3.0 * mm, 1.8 * mm, 0.8 * mm, fill=1, stroke=0)
        c.roundRect(cx + 1.0 * mm, cy - 2.7 * mm, 3.0 * mm, 1.8 * mm, 0.8 * mm, fill=1, stroke=0)
    elif kind == "usage":
        # Small document/checklist
        c.roundRect(cx - 2.6 * mm, cy - 2.8 * mm, 5.2 * mm, 5.6 * mm, 0.8 * mm, fill=0, stroke=1)
        c.line(cx - 1.3 * mm, cy + 1.0 * mm, cx + 1.7 * mm, cy + 1.0 * mm)
        c.line(cx - 1.3 * mm, cy - 0.5 * mm, cx + 1.7 * mm, cy - 0.5 * mm)
        c.line(cx - 1.3 * mm, cy - 1.8 * mm, cx + 1.0 * mm, cy - 1.8 * mm)
        # Check mark
        p = c.beginPath()
        p.moveTo(cx - 2.0 * mm, cy + 2.0 * mm)
        p.lineTo(cx - 1.2 * mm, cy + 1.1 * mm)
        p.lineTo(cx + 0.4 * mm, cy + 2.7 * mm)
        c.drawPath(p, stroke=1, fill=0)
    elif kind == "max":
        # Bar chart
        bar_w = 1.0 * mm
        c.rect(cx - 2.4 * mm, cy - 2.4 * mm, bar_w, 2.8 * mm, fill=1, stroke=0)
        c.rect(cx - 0.5 * mm, cy - 2.4 * mm, bar_w, 4.1 * mm, fill=1, stroke=0)
        c.rect(cx + 1.4 * mm, cy - 2.4 * mm, bar_w, 5.4 * mm, fill=1, stroke=0)
    else:
        # Trend/average line
        c.setLineWidth(1.1)
        p = c.beginPath()
        p.moveTo(cx - 3.0 * mm, cy - 1.8 * mm)
        p.lineTo(cx - 1.2 * mm, cy - 0.4 * mm)
        p.lineTo(cx + 0.4 * mm, cy - 1.0 * mm)
        p.lineTo(cx + 2.8 * mm, cy + 2.0 * mm)
        c.drawPath(p, stroke=1, fill=0)
        c.circle(cx - 3.0 * mm, cy - 1.8 * mm, 0.45 * mm, fill=1, stroke=0)
        c.circle(cx - 1.2 * mm, cy - 0.4 * mm, 0.45 * mm, fill=1, stroke=0)
        c.circle(cx + 0.4 * mm, cy - 1.0 * mm, 0.45 * mm, fill=1, stroke=0)
        c.circle(cx + 2.8 * mm, cy + 2.0 * mm, 0.45 * mm, fill=1, stroke=0)

    c.restoreState()


def metric_card(c: canvas.Canvas, x, y, w, h, label: str, value: Any, accent=NAVY):
    """Clean KPI card with safe text positions for narrow and wide cards."""
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=colors.HexColor("#E1E7EF"), width=0.6)
    icon_cx = x + 8 * mm
    icon_cy = y + h - 8.2 * mm
    c.setFillColor(accent)
    c.circle(icon_cx, icon_cy, 4.0 * mm, fill=1, stroke=0)

    label_up = label.upper()
    if "GROUP" in label_up:
        icon_kind = "group"
    elif "MAX" in label_up:
        icon_kind = "max"
    elif "AVG" in label_up or "AVERAGE" in label_up:
        icon_kind = "avg"
    else:
        icon_kind = "usage"
    draw_metric_icon(c, icon_cx, icon_cy, icon_kind, WHITE)

    draw_fit(c, label.upper(), x + 15 * mm, y + h - 9.0 * mm, w - 18 * mm, "Helvetica-Bold", 6.1, SLATE, min_size=5.0)

    val = str(value)
    size = 15.0
    while size > 8.5 and text_width(c, val, "Helvetica-Bold", size) > w - 10 * mm:
        size -= 0.4
    draw_fit(c, val, x + 5 * mm, y + 5.8 * mm, w - 10 * mm, "Helvetica-Bold", size, DARK, align="center", min_size=8.2)

def _jpeg_reader_from_pil(img: Image.Image, quality: int | None = None) -> ImageReader:
    """Return ImageReader backed by compressed JPEG bytes, not raw full-size pixels."""
    if img.mode not in ("RGB", "L"):
        # White background handles PNG/WEBP transparency safely.
        bg = Image.new("RGB", img.size, "white")
        try:
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        except Exception:
            img = img.convert("RGB")
    else:
        img = img.convert("RGB")

    max_dim = max(320, int(PDF_IMAGE_MAX_DIM))
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

    bio = io.BytesIO()
    img.save(
        bio,
        format="JPEG",
        quality=int(quality or PDF_IMAGE_JPEG_QUALITY),
        optimize=True,
        progressive=True,
        subsampling=2,
    )
    bio.seek(0)
    return ImageReader(bio)


def placeholder_image(color="#E5E7EB", label="No Image", size=(360, 230)) -> ImageReader:
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="#CBD5E1", width=2)
    d.text((size[0] // 2 - 34, size[1] // 2 - 8), label, fill="#64748B")
    return _jpeg_reader_from_pil(img, quality=55)


def image_reader_from_bytes(b: bytes | None):
    if not b:
        return None
    try:
        img = Image.open(io.BytesIO(bytes(b)))
        return _jpeg_reader_from_pil(img)
    except Exception:
        return None


def get_image_for_key(key: str | None, demo_color: str = "#E5E7EB"):
    if key and fetch_image_bytes:
        try:
            res = fetch_image_bytes(key)
            if isinstance(res, tuple):
                b = res[0]
            elif isinstance(res, dict):
                b = res.get("image_bytes")
            else:
                b = res
            reader = image_reader_from_bytes(b)
            if reader:
                return reader
        except Exception:
            pass
    if key:
        UNAVAILABLE_IMAGE_KEYS.add(str(key).strip())
    return placeholder_image(demo_color, "Gambar perlu recovery")


def draw_image_fit(c: canvas.Canvas, reader, x, y, w, h):
    try:
        c.drawImage(reader, x, y, width=w, height=h, preserveAspectRatio=True, anchor="c", mask="auto")
    except Exception:
        c.drawImage(
            placeholder_image(label="Gambar perlu recovery"),
            x,
            y,
            width=w,
            height=h,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )


def dominant(groups: pd.DataFrame, col: str) -> tuple[str, float]:
    if groups is None or groups.empty or col not in groups.columns:
        return "-", 0
    df = groups.copy()
    df["usage_count"] = to_float_series(df.get("usage_count", pd.Series(dtype=float)))
    tmp = df.groupby(col, dropna=False)["usage_count"].sum().reset_index().sort_values("usage_count", ascending=False)
    if tmp.empty:
        return "-", 0
    return safe_text(tmp.iloc[0][col]), float(tmp.iloc[0]["usage_count"])


def top_group(groups: pd.DataFrame) -> pd.Series | None:
    if groups is None or groups.empty:
        return None
    return groups.sort_values("usage_count", ascending=False).iloc[0]


def make_kpi(groups: pd.DataFrame, details: pd.DataFrame) -> dict[str, Any]:
    if kpis:
        try:
            kp = kpis(groups, details)
        except Exception:
            kp = {}
    else:
        kp = {}
    if not kp:
        usage = to_float_series(groups.get("usage_count", pd.Series(dtype=float))) if not groups.empty else pd.Series(dtype=float)
        kp = {
            "total_group": len(groups),
            "total_usage": usage.sum(),
            "max_usage": usage.max() if len(usage) else 0,
            "avg_usage": usage.mean() if len(usage) else 0,
            "detail_rows": len(details),
        }
    return kp


def _parse_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def report_period_range(groups: pd.DataFrame, details: pd.DataFrame) -> tuple[str, str, str]:
    """Return (start_label, end_label, range_label) based on actual transaction months."""
    candidates = []
    for df in [details, groups]:
        if df is None or df.empty:
            continue
        for col in ["tanggal", "period_date", "sql_date", "month", "sql_date_list", "tanggal_list"]:
            if col in df.columns:
                ser = df[col].dropna().astype(str)
                # for list columns take first date-like token
                extracted = ser.str.extract(r"(\d{4}-\d{2}-\d{2}|\d{4}-\d{2})", expand=False).dropna()
                if not extracted.empty:
                    candidates.append(_parse_date_series(extracted))
    if not candidates:
        return "-", "-", "-"
    all_dates = pd.concat(candidates).dropna()
    if all_dates.empty:
        return "-", "-", "-"
    start = all_dates.min()
    end = all_dates.max()
    def fmt_month(dt):
        bulan = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        return f"{bulan[int(dt.month)-1]} {int(dt.year)}"
    start_s, end_s = fmt_month(start), fmt_month(end)
    return start_s, end_s, start_s if start_s == end_s else f"{start_s} - {end_s}"


def normalize_ticket_value(value: Any) -> str:
    s = safe_text(value, "")
    if not s:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", "", s).upper()


def collect_unique_tickets(groups: pd.DataFrame, details: pd.DataFrame) -> set[str]:
    """Collect unique harvest tickets involved in reused-photo findings."""
    tickets: set[str] = set()
    if details is not None and not details.empty:
        for col in ["oph", "harvest_ticket", "OPH", "HARVEST_TICKET", "ticket", "tiket_panen"]:
            if col in details.columns:
                vals = details[col].dropna().astype(str).tolist()
                for v in vals:
                    key = normalize_ticket_value(v)
                    if key:
                        tickets.add(key)
                if tickets:
                    break
    if groups is not None and not groups.empty and "oph_list" in groups.columns:
        for v in groups["oph_list"].dropna().tolist():
            for part in split_values(v):
                key = normalize_ticket_value(part)
                if key:
                    tickets.add(key)
    return tickets


def _sap_ticket_total(
    system: str,
    company: str,
    groups: pd.DataFrame,
    months: Any = None,
) -> tuple[int | None, str]:
    """Read the exact SAP denominator used by the dashboard."""
    if not callable(load_sap_kpis):
        return None, "sap_kpi_unavailable"
    try:
        sap_kpi = load_sap_kpis(system, company, groups, months)
        value = sap_kpi.get("total_sap_oph")
        if value is None:
            return None, "sap_kpi_unavailable"
        total = int(float(value))
        if total <= 0:
            return None, "sap_kpi_unavailable"
        return total, "gold.sap_oph_kpi_summary"
    except Exception:
        return None, "sap_kpi_unavailable"


def harvest_ticket_metrics(
    groups: pd.DataFrame,
    details: pd.DataFrame,
    kp: dict[str, Any],
    system: str = "",
    company: str = "",
    months: Any = None,
) -> dict[str, Any]:
    """Use the same % Tiket contract as the dashboard.

    % Tiket = total Usage Golden Eye / total tiket SAP x 100.
    The SAP denominator comes only from gold.sap_oph_kpi_summary through
    queries.load_sap_kpis. The value is deliberately not capped at 100%.
    """
    company_norm = str(company or "").upper().strip()
    if company_norm in {
        "",
        "ALL",
        "ALL COMPANY",
        "PWP + DAN + DWT",
        "PWP + DIN + DAN + DWT",
    } and groups is not None and not groups.empty:
        comp_col = "report_company" if "report_company" in groups.columns else ("source_company" if "source_company" in groups.columns else "")
        sys_col = "report_system" if "report_system" in groups.columns else ("system" if "system" in groups.columns else "")
        if comp_col:
            totals = {
                "harvest_tickets": 0,
                "reused_tickets": 0,
                "total_usage": 0,
            }
            bases = []
            sap_scope_ready = True
            for comp in sorted([c for c in groups[comp_col].dropna().astype(str).str.upper().unique().tolist() if c and c != "UNKNOWN"]):
                g_sub = groups[groups[comp_col].astype(str).str.upper().eq(comp)].copy()
                if details is not None and not details.empty:
                    d_comp_col = "report_company" if "report_company" in details.columns else ("source_company" if "source_company" in details.columns else "")
                    d_sub = details[details[d_comp_col].astype(str).str.upper().eq(comp)].copy() if d_comp_col else details.copy()
                else:
                    d_sub = pd.DataFrame()
                sys_val = "HARMONY" if comp == "PWP" else "ELHP"
                if sys_col and not g_sub.empty:
                    sys_val = safe_text(g_sub.iloc[0].get(sys_col), sys_val).upper()
                k_sub = make_kpi(g_sub, d_sub)
                m = harvest_ticket_metrics(
                    g_sub, d_sub, k_sub, sys_val, comp, months
                )
                sap_total = m.get("harvest_tickets")
                if sap_total is None:
                    sap_scope_ready = False
                else:
                    totals["harvest_tickets"] += int(sap_total or 0)
                totals["reused_tickets"] += int(m.get("reused_tickets", 0) or 0)
                totals["total_usage"] += int(m.get("total_usage", 0) or 0)
                bases.append(str(m.get("basis", "")))
            if sap_scope_ready and totals["harvest_tickets"]:
                pct_unique = totals["reused_tickets"] / totals["harvest_tickets"] * 100.0 if totals["harvest_tickets"] else 0.0
                pct_usage = totals["total_usage"] / totals["harvest_tickets"] * 100.0 if totals["harvest_tickets"] else 0.0
                intensity = totals["total_usage"] / totals["reused_tickets"] if totals["reused_tickets"] else 0.0
                return {
                    "tickets": totals["harvest_tickets"],
                    "harvest_tickets": totals["harvest_tickets"],
                    "reused_tickets": totals["reused_tickets"],
                    "total_usage": totals["total_usage"],
                    "ticket_pct": pct_unique,
                    "usage_ticket_pct": pct_usage,
                    "usage_intensity": intensity,
                    "basis": "gold.sap_oph_kpi_summary",
                    "sap_scope_ready": True,
                    "usage_pct_of_tickets": pct_usage,
                }
            return {
                "tickets": None,
                "harvest_tickets": None,
                "reused_tickets": totals["reused_tickets"],
                "total_usage": totals["total_usage"],
                "ticket_pct": None,
                "usage_ticket_pct": None,
                "usage_intensity": (
                    totals["total_usage"] / totals["reused_tickets"]
                    if totals["reused_tickets"]
                    else 0.0
                ),
                "basis": "sap_kpi_unavailable",
                "sap_scope_ready": False,
                "usage_pct_of_tickets": None,
            }

    total_usage = float(pd.to_numeric(pd.Series([kp.get("total_usage", 0)]), errors="coerce").fillna(0).iloc[0])
    reused_tickets_set = collect_unique_tickets(groups, details)
    reused_tickets = len(reused_tickets_set)
    if not reused_tickets:
        # Last fallback: detail rows represent usage, but do not create a misleading percentage.
        reused_tickets = int(min(total_usage, len(details) if details is not None else total_usage) or 0)
    harvest_tickets, basis = _sap_ticket_total(system, company, groups, months)
    ticket_pct = (
        reused_tickets / harvest_tickets * 100.0
        if harvest_tickets
        else None
    )
    usage_ticket_pct = (
        total_usage / harvest_tickets * 100.0
        if harvest_tickets
        else None
    )
    usage_intensity = (total_usage / reused_tickets) if reused_tickets else 0.0
    return {
        "tickets": harvest_tickets,
        "harvest_tickets": harvest_tickets,
        "reused_tickets": int(reused_tickets or 0),
        "total_usage": int(round(total_usage)),
        "ticket_pct": ticket_pct,
        "usage_ticket_pct": usage_ticket_pct,
        "usage_intensity": usage_intensity,
        "basis": basis,
        "sap_scope_ready": harvest_tickets is not None,
        "usage_pct_of_tickets": usage_ticket_pct,
    }


def ticket_story_sentence(tm: dict[str, Any], kp: dict[str, Any] | None = None) -> str:
    """Report-facing ticket narrative.

    Sesuai kebutuhan report, angka tiket temuan yang ditampilkan mengikuti total usage,
    agar sama dengan angka utama pada dashboard/HTML dan cover report.
    Avg usage tetap dihitung pada level reused group: total usage / total reused group.
    """
    total_usage = float(tm.get("total_usage", 0) or 0)
    total_group = 0.0
    if kp:
        try:
            total_group = float(kp.get("total_group", 0) or 0)
        except Exception:
            total_group = 0.0
    avg_group = (total_usage / total_group) if total_group else 0.0
    sap_total = tm.get("harvest_tickets", tm.get("tickets"))
    pct_usage = tm.get("usage_ticket_pct", tm.get("usage_pct_of_tickets"))
    if sap_total in (None, 0) or pct_usage is None:
        return (
            f"Total tiket SAP belum tersedia, sehingga % Tiket ditampilkan N/A. "
            f"Total temuan tetap {fmt_int(total_usage)} usage, dengan rata-rata "
            f"{fmt_num(avg_group, 2)} usage per reused group. Jalankan refresh-sap-kpi."
        )
    return (
        f"Dari {fmt_int(sap_total)} tiket SAP, terdapat {fmt_int(total_usage)} usage "
        f"Golden Eye ({fmt_num(pct_usage, 2)}% = usage / tiket SAP). "
        f"Rata-rata temuan adalah {fmt_num(avg_group, 2)} usage per reused group."
    )


def ticket_definition_note() -> str:
    return (
        "Catatan pembacaan: % Tiket = total usage Golden Eye / total tiket SAP. "
        "Avg usage dihitung pada level reused group, yaitu total usage dibagi total reused group."
    )

def month_usage_summary(groups: pd.DataFrame, details: pd.DataFrame) -> pd.DataFrame:
    """Monthly repeated-ticket usage distribution for PDF report."""
    rows = []
    if details is not None and not details.empty:
        d = details.copy()
        if "tanggal" in d.columns:
            d["_dt"] = pd.to_datetime(d["tanggal"], errors="coerce")
        elif "period_date" in d.columns:
            d["_dt"] = pd.to_datetime(d["period_date"], errors="coerce")
        elif "month" in d.columns:
            d["_dt"] = pd.to_datetime(d["month"].astype(str).str[:7] + "-01", errors="coerce")
        else:
            d["_dt"] = pd.NaT
        d["_month"] = d["_dt"].dt.strftime("%Y-%m").fillna("Tanpa Bulan")
        if "source_company" in d.columns:
            d["_company"] = d["source_company"].apply(lambda x: safe_text(x, "-").upper())
        elif "report_company" in d.columns:
            d["_company"] = d["report_company"].apply(lambda x: safe_text(x, "-").upper())
        else:
            d["_company"] = "-"
        agg = d.groupby(["_month", "_company"], dropna=False).size().reset_index(name="usage")
        return agg.rename(columns={"_month": "month", "_company": "company"})
    if groups is not None and not groups.empty:
        g = groups.copy()
        if "month" in g.columns:
            g["_month"] = g["month"].astype(str).str[:7]
        else:
            g["_month"] = "Tanpa Bulan"
        if "source_company" in g.columns:
            g["_company"] = g["source_company"].apply(lambda x: safe_text(x, "-").upper())
        elif "report_company" in g.columns:
            g["_company"] = g["report_company"].apply(lambda x: safe_text(x, "-").upper())
        else:
            g["_company"] = "-"
        g["_usage"] = pd.to_numeric(g.get("usage_count", 0), errors="coerce").fillna(0)
        return g.groupby(["_month", "_company"], dropna=False)["_usage"].sum().reset_index(name="usage").rename(columns={"_month": "month", "_company": "company"})
    return pd.DataFrame(columns=["month", "company", "usage"])


def chart_monthly_pattern(month_df: pd.DataFrame, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = month_df.copy()
    if data.empty:
        data = pd.DataFrame({"month": ["No Data"], "company": ["-"], "usage": [0]})
    data["usage"] = pd.to_numeric(data["usage"], errors="coerce").fillna(0)
    # Sort months lexically, Tanpa Bulan at end
    data["_sort"] = data["month"].apply(lambda x: "9999-99" if str(x) == "Tanpa Bulan" else str(x))
    data = data.sort_values("_sort")
    pivot = data.pivot_table(index="month", columns="company", values="usage", aggfunc="sum", fill_value=0)
    pivot = pivot.loc[sorted(pivot.index, key=lambda x: "9999-99" if str(x) == "Tanpa Bulan" else str(x))]
    total = pivot.sum(axis=1)
    fig, ax = plt.subplots(figsize=(8.0, 3.9), dpi=PDF_CHART_DPI)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    palette = CHART_PALETTE
    bottom = None
    x = list(range(len(pivot.index)))
    for i, col in enumerate(pivot.columns):
        vals = pivot[col].values
        ax.bar(x, vals, bottom=bottom, label=str(col), color=palette[i % len(palette)], edgecolor="white", linewidth=0.6, hatch=CHART_HATCHES[i % len(CHART_HATCHES)], width=0.62)
        bottom = vals if bottom is None else bottom + vals
    ax.plot(x, total.values, color=CHART_BLUE, marker="o", linewidth=2.2, linestyle="--", label="Total Usage")
    for xi, val in zip(x, total.values):
        ax.text(xi, val + max(total.max(), 1) * 0.035, fmt_int(val), ha="center", va="bottom", fontsize=7.4, color=CHART_BLUE)
    ax.set_xticks(x)
    ax.set_xticklabels(pivot.index.tolist(), fontsize=7.2, rotation=0)
    ax.tick_params(axis="y", labelsize=7.2, colors="#64748B")
    ax.set_ylabel("Usage", fontsize=8.0, color=CHART_BLUE)
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#E2E8F0")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=min(4, len(pivot.columns)+1), frameon=False, fontsize=7.2)
    style_chart_axis(ax)
    plt.tight_layout(pad=1.0)
    fig.savefig(out_path, facecolor="white", dpi=PDF_CHART_DPI, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return out_path




def top_reused_month_long(groups: pd.DataFrame, details: pd.DataFrame, top_n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (long stack data, top meta) for Top Reused by Month.

    Long data columns: group_key, month, usage.
    Meta columns: group_key, label, total_usage, group_id, estate, representative_image.
    """
    if groups is None or groups.empty:
        return pd.DataFrame(columns=["group_key", "month", "usage"]), pd.DataFrame()
    g = groups.copy()
    if "group_key" not in g.columns:
        g["group_key"] = g.get("group_id", pd.Series(range(len(g)))).astype(str)
    g["usage_count"] = pd.to_numeric(g.get("usage_count", 0), errors="coerce").fillna(0)
    meta = g.sort_values("usage_count", ascending=False).head(top_n).copy()
    keep = set(meta["group_key"].astype(str))

    rows = []
    if details is not None and not details.empty and "group_key" in details.columns:
        d = details.copy()
        d = d[d["group_key"].astype(str).isin(keep)]
        if not d.empty:
            if "tanggal" in d.columns:
                d["_dt"] = pd.to_datetime(d["tanggal"], errors="coerce")
            elif "period_date" in d.columns:
                d["_dt"] = pd.to_datetime(d["period_date"], errors="coerce")
            elif "month" in d.columns:
                d["_dt"] = pd.to_datetime(d["month"].astype(str).str[:7] + "-01", errors="coerce")
            else:
                d["_dt"] = pd.NaT
            d["month"] = d["_dt"].dt.strftime("%Y-%m").fillna("Tanpa Bulan")
            rows = d.groupby(["group_key", "month"], dropna=False).size().reset_index(name="usage")
    if isinstance(rows, list) or (hasattr(rows, "empty") and rows.empty):
        fallback = meta.copy()
        if "month" in fallback.columns:
            fallback["month"] = fallback["month"].astype(str).str[:7]
        elif "sql_date_list" in fallback.columns:
            fallback["month"] = fallback["sql_date_list"].astype(str).str.extract(r"(\d{4}-\d{2})", expand=False).fillna("Tanpa Bulan")
        else:
            fallback["month"] = "Tanpa Bulan"
        fallback["usage"] = fallback["usage_count"]
        rows = fallback[["group_key", "month", "usage"]].copy()
    rows["usage"] = pd.to_numeric(rows["usage"], errors="coerce").fillna(0)
    total = rows.groupby("group_key", dropna=False)["usage"].sum().reset_index(name="total_usage")
    meta = meta.drop(columns=["total_usage"], errors="ignore").merge(total, on="group_key", how="left")
    meta["total_usage"] = pd.to_numeric(meta["total_usage"], errors="coerce").fillna(meta["usage_count"])
    meta = meta.sort_values("total_usage", ascending=False).head(top_n).copy()
    meta["group_id"] = meta.get("group_id", meta["group_key"]).astype(str)
    if "estate_primary" in meta.columns:
        meta["estate"] = meta["estate_primary"].apply(lambda x: safe_text(x, "-"))
    elif "estate_list" in meta.columns:
        meta["estate"] = meta["estate_list"].apply(split_first)
    else:
        meta["estate"] = "-"
    meta["label"] = meta.apply(lambda r: f"G{safe_text(r.get('group_id'))} | {safe_text(r.get('estate'))}", axis=1)
    rows = rows[rows["group_key"].astype(str).isin(set(meta["group_key"].astype(str)))]
    return rows, meta


def chart_top_reused_by_month(groups: pd.DataFrame, details: pd.DataFrame, out_path: Path, top_n: int = 10):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    long, meta = top_reused_month_long(groups, details, top_n=top_n)
    if meta.empty:
        long = pd.DataFrame({"group_key": ["-"], "month": ["No Data"], "usage": [0]})
        meta = pd.DataFrame({"group_key": ["-"], "label": ["No Data"], "total_usage": [0]})
    order = meta.sort_values("total_usage", ascending=True)["group_key"].astype(str).tolist()
    label_map = dict(zip(meta["group_key"].astype(str), meta["label"].astype(str)))
    total_map = dict(zip(meta["group_key"].astype(str), pd.to_numeric(meta["total_usage"], errors="coerce").fillna(0)))
    pivot = long.pivot_table(index="group_key", columns="month", values="usage", aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(order).fillna(0)
    months = sorted(pivot.columns.tolist(), key=lambda x: "9999-99" if str(x) == "Tanpa Bulan" else str(x))
    fig, ax = plt.subplots(figsize=(8.1, 4.8), dpi=PDF_CHART_DPI)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    palette = MONTH_DISTINCT_PALETTE
    y_pos = list(range(len(order)))
    left = [0] * len(order)
    for i, m in enumerate(months):
        vals = pivot[m].values if m in pivot.columns else [0] * len(order)
        ax.barh(y_pos, vals, left=left, label=str(m), color=palette[i % len(palette)], edgecolor="white", linewidth=0.6, height=0.58)
        left = [a + b for a, b in zip(left, vals)]
    max_total = max([float(total_map.get(k, 0)) for k in order] + [1])
    for yi, gkey in zip(y_pos, order):
        total = float(total_map.get(gkey, 0))
        ax.text(total + max_total * 0.018, yi, fmt_int(total), va="center", fontsize=7.6, color=CHART_BLUE)
    ax.set_yticks(y_pos)
    ax.set_yticklabels([label_map.get(k, k) for k in order], fontsize=7.4, color=CHART_BLUE)
    ax.tick_params(axis="x", labelsize=7.2, colors="#64748B")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#E2E8F0")
    ax.set_xlabel("Usage", fontsize=8, color=CHART_BLUE)
    ax.set_xlim(0, max_total * 1.16)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=min(5, max(1, len(months))), frameon=False, fontsize=7.0, title="Bulan", title_fontsize=7.2)
    style_chart_axis(ax)
    plt.tight_layout(pad=1.0)
    fig.savefig(out_path, facecolor="white", dpi=PDF_CHART_DPI, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return out_path


def company_top_reused_month_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, kp: dict[str, Any], system: str, company: str, period: str, cache_dir: Path, page_no: int, section_no: int, top_n: int = 10) -> int:
    draw_header(c, f"Section {section_no:02d} - Top Reused {company}", period, page_no)
    start_s, end_s, range_s = report_period_range(groups, details)
    top_r = top_group(groups)
    top_group_txt = safe_text(top_r.get("group_id") if top_r is not None else "-")
    top_usage_txt = fmt_int(top_r.get("usage_count") if top_r is not None else 0)
    x, y, w, h = MARGIN_X, PAGE_H - 100 * mm, PAGE_W - 2 * MARGIN_X, 42 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 6 * mm, y + h - 10 * mm, "TOP REUSED BERDASARKAN BULAN")
    story = (
        f"Untuk {company}, grafik ini memperlihatkan top reused group pada periode {range_s}. "
        f"Satu bar mewakili satu group, sedangkan warna di dalam bar menunjukkan bulan penggunaan. "
        f"Group {top_group_txt} menjadi temuan paling menonjol dengan {top_usage_txt} usage."
    )
    draw_text_lines(c, wrap_lines(c, story, w - 12 * mm, "Helvetica-Bold", 7.5, max_lines=4), x + 6 * mm, y + h - 21 * mm, "Helvetica-Bold", 7.5, 8.6, SLATE)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cp = chart_top_reused_by_month(groups, details, cache_dir / f"top_reused_month_{sanitize_filename(system)}_{sanitize_filename(company)}.png", top_n=top_n)
    chart_panel(c, "Top Reused by Month", cp, MARGIN_X, 44 * mm, PAGE_W - 2 * MARGIN_X, 132 * mm, NAVY)
    c.showPage()
    return page_no + 1

def monthly_pattern_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    details: pd.DataFrame,
    kp: dict[str, Any],
    system: str,
    company: str,
    period: str,
    cache_dir: Path,
    page_no: int,
    selected_months: Any = None,
) -> int:
    draw_header(c, "Monthly Pattern", period, page_no)
    start_s, end_s, range_s = report_period_range(groups, details)
    tm = harvest_ticket_metrics(
        groups, details, kp, system, company, selected_months
    )
    x, y, w, h = MARGIN_X, PAGE_H - 96 * mm, PAGE_W - 2 * MARGIN_X, 42 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9)
    c.drawString(x + 6 * mm, y + h - 10 * mm, "SEBARAN TIKET PANEN BERULANG")
    bullets = [
        f"Periode data pada report ini dimulai dari {start_s} sampai {end_s}.",
        ticket_story_sentence(tm, kp),
        "Berikut sebaran data tiket panen yang dipakai berulang berdasarkan Berulang Monthly Pattern; garis hitam menunjukkan total usage bulanan.",
    ]
    yy = y + h - 19 * mm
    for b in bullets:
        c.setFillColor(NAVY); c.circle(x + 7 * mm, yy + 2.2 * mm, 1.4 * mm, fill=1, stroke=0)
        lines = wrap_lines(c, b, w - 18 * mm, "Helvetica-Bold", 7.3, max_lines=2)
        draw_text_lines(c, lines, x + 12 * mm, yy, "Helvetica-Bold", 7.3, 8.2, SLATE)
        yy -= 10 * mm
    mdf = month_usage_summary(groups, details)
    chart_path = chart_monthly_pattern(mdf, cache_dir / f"monthly_pattern_{sanitize_filename(system)}_{sanitize_filename(company)}.png")
    chart_panel(c, "Berulang Monthly Pattern", chart_path, MARGIN_X, 76 * mm, PAGE_W - 2 * MARGIN_X, 100 * mm, NAVY)

    # compact monthly table
    table_y = 30 * mm
    rounded_rect(c, MARGIN_X, table_y, PAGE_W - 2 * MARGIN_X, 38 * mm, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.5)
    c.drawString(MARGIN_X + 5 * mm, table_y + 29 * mm, "RINGKASAN BULANAN")
    pivot = mdf.pivot_table(index="month", columns="company", values="usage", aggfunc="sum", fill_value=0)
    if not pivot.empty:
        pivot["Total"] = pivot.sum(axis=1)
        pivot = pivot.sort_index().tail(6)
    cols = ["Bulan"] + [str(cn) for cn in pivot.columns[:4]]
    colw = (PAGE_W - 2 * MARGIN_X - 10 * mm) / max(len(cols), 1)
    hy = table_y + 20 * mm
    c.setFillColor(NAVY); c.rect(MARGIN_X + 5 * mm, hy, colw * len(cols), 7 * mm, fill=1, stroke=0)
    for idx, col in enumerate(cols):
        draw_fit(c, col, MARGIN_X + 6 * mm + idx * colw, hy + 2.2 * mm, colw - 2 * mm, "Helvetica-Bold", 6.3, WHITE)
    ry = hy - 7 * mm
    for ridx, (m, row) in enumerate(pivot.head(3).iterrows() if not pivot.empty else []):
        if ridx % 2 == 0:
            c.setFillColor(colors.HexColor("#F8FAFC")); c.rect(MARGIN_X + 5 * mm, ry, colw * len(cols), 7 * mm, fill=1, stroke=0)
        vals = [m] + [fmt_int(row.get(cn, 0)) for cn in pivot.columns[:4]]
        for idx, val in enumerate(vals):
            draw_fit(c, val, MARGIN_X + 6 * mm + idx * colw, ry + 2.2 * mm, colw - 2 * mm, "Helvetica-Bold", 6.2, SLATE)
        ry -= 7 * mm
    c.showPage()
    return page_no + 1



def _first_nonblank(row: pd.Series | dict, names: list[str], default: str = "-") -> str:
    for name in names:
        try:
            val = row.get(name, "")
        except Exception:
            val = ""
        s = safe_text(val, "")
        if s:
            return s
    return default


def _numeric_similarity_value(row: pd.Series | dict) -> tuple[float | None, str]:
    for col in [
        "transport_similarity_score", "transport_similarity_category",
        "similarity_transport", "similarity", "similarity_score", "score",
        "similarity_primary", "similarity_list",
    ]:
        try:
            raw = row.get(col, "")
        except Exception:
            raw = ""
        s = safe_text(raw, "")
        if not s:
            continue
        m = re.search(r"(\d+(?:[\.,]\d+)?)", s)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if val <= 1.0:
                    val *= 100.0
                return val, f"{fmt_num(val, 1)}%"
            except Exception:
                pass
        return None, s
    return None, "Perlu Verifikasi"


def pick_similarity_example(groups: pd.DataFrame, details: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pick one Harmony row that has both panen and transport evidence when possible."""
    if details is not None and not details.empty:
        d = details.copy()
        # Prefer rows where both photo sides exist.
        d["_panen_key"] = ""
        for col in ["image_name", "representative_image", "image_key", "file_name"]:
            if col in d.columns:
                d["_panen_key"] = d["_panen_key"].where(d["_panen_key"].astype(str).str.strip().ne(""), d[col].astype(str))
        d["_transport_key"] = ""
        for col in ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image"]:
            if col in d.columns:
                d["_transport_key"] = d["_transport_key"].where(d["_transport_key"].astype(str).str.strip().ne(""), d[col].astype(str))
        cand = d[d["_panen_key"].astype(str).str.strip().ne("") & d["_transport_key"].astype(str).str.strip().ne("")].copy()
        if cand.empty:
            cand = d.copy()
        if not cand.empty:
            # Prefer numeric/high similarity if available.
            scores = []
            for _, rr in cand.iterrows():
                val, _ = _numeric_similarity_value(rr)
                scores.append(val if val is not None else -1)
            cand["_sim_sort"] = scores
            rr = cand.sort_values("_sim_sort", ascending=False).iloc[0].to_dict()
            group_meta = {}
            if groups is not None and not groups.empty:
                if "group_key" in groups.columns and rr.get("group_key"):
                    gm = groups[groups["group_key"].astype(str).eq(str(rr.get("group_key")))]
                    if not gm.empty:
                        group_meta = gm.iloc[0].to_dict()
                elif "group_id" in groups.columns and rr.get("group_id"):
                    gm = groups[groups["group_id"].astype(str).eq(str(rr.get("group_id")))]
                    if not gm.empty:
                        group_meta = gm.iloc[0].to_dict()
            return rr, group_meta
    # Fallback from top group.
    if groups is not None and not groups.empty:
        rr = groups.sort_values("usage_count", ascending=False).iloc[0].to_dict()
        return rr, rr
    return {}, {}


def draw_photo_panel(c: canvas.Canvas, title: str, reader, x: float, y: float, w: float, h: float, color):
    rounded_rect(c, x, y, w, h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(LABEL_BG); c.roundRect(x, y + h - 10 * mm, w, 10 * mm, 4 * mm, fill=1, stroke=0)
    c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 8.0)
    c.drawCentredString(x + w / 2, y + h - 6.5 * mm, title)
    draw_image_fit(c, reader, x + 3 * mm, y + 3 * mm, w - 6 * mm, h - 15 * mm)


def legacy_similarity_transport_example_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, page_no: int) -> int:
    """Legacy single-example renderer kept only for backward reference; V17 uses the function defined later."""
    if str(system).upper() != "HARMONY":
        return page_no
    row, group_meta = pick_similarity_example(groups, details)
    if not row and not group_meta:
        return page_no
    draw_header(c, "Transport Reuse - Panen vs Transport", period, page_no)

    intro_x, intro_y, intro_w, intro_h = MARGIN_X, PAGE_H - 90 * mm, PAGE_W - 2 * MARGIN_X, 29 * mm
    rounded_rect(c, intro_x, intro_y, intro_w, intro_h, 5 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.45)
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 8.5)
    c.drawString(intro_x + 6 * mm, intro_y + intro_h - 9 * mm, "KHUSUS HARMONY - CONTOH KESAMAAN PANEN DAN TRANSPORT")
    intro = (
        "Halaman ini menampilkan contoh pasangan bukti foto panen dan foto transport yang terdeteksi memiliki kemiripan. "
        "Contoh ini dipakai sebagai referensi awal untuk verifikasi lapangan terhadap Kerani Panen dan Kerani Transport terkait."
    )
    draw_text_lines(c, wrap_lines(c, intro, intro_w - 12 * mm, "Helvetica-Bold", 7.4, max_lines=3), intro_x + 6 * mm, intro_y + intro_h - 18 * mm, "Helvetica-Bold", 7.4, 8.2, SLATE)

    panen_key = _first_nonblank(row, ["image_name", "representative_image", "image_key", "file_name"], "")
    if not panen_key:
        panen_key = _first_nonblank(group_meta, ["representative_image", "image_key"], "")
    transport_key = _first_nonblank(row, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image"], "")
    if not transport_key:
        transport_key = _first_nonblank(group_meta, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image"], "")
    panen_reader = get_image_for_key(panen_key, "#DCE2F1")
    transport_reader = get_image_for_key(transport_key, "#DCE2F1")

    photo_y = 108 * mm
    photo_h = 74 * mm
    gap = 6 * mm
    photo_w = (PAGE_W - 2 * MARGIN_X - gap) / 2
    draw_photo_panel(c, "Foto Panen / Kerani Panen", panen_reader, MARGIN_X, photo_y, photo_w, photo_h, NAVY)
    draw_photo_panel(c, "Foto Transport / Kerani Transport", transport_reader, MARGIN_X + photo_w + gap, photo_y, photo_w, photo_h, RED)

    sim_val, sim_txt = _numeric_similarity_value(row)
    badge_w, badge_h = 50 * mm, 16 * mm
    badge_x = PAGE_W / 2 - badge_w / 2
    badge_y = photo_y - 9 * mm
    rounded_rect(c, badge_x, badge_y, badge_w, badge_h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(RED); c.circle(badge_x + 8 * mm, badge_y + badge_h / 2, 4 * mm, fill=1, stroke=0)
    draw_metric_icon(c, badge_x + 8 * mm, badge_y + badge_h / 2, "check", WHITE)
    c.setFillColor(SLATE); c.setFont("Helvetica-Bold", 5.8); c.drawString(badge_x + 16 * mm, badge_y + 9.5 * mm, "SIMILARITY SCORE")
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 17); c.drawString(badge_x + 16 * mm, badge_y + 3.0 * mm, sim_txt)

    table_x, table_y, table_w, table_h = MARGIN_X, 35 * mm, 84 * mm, 57 * mm
    rounded_rect(c, table_x, table_y, table_w, table_h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.2); c.drawString(table_x + 4 * mm, table_y + table_h - 8 * mm, "DETAIL PERBANDINGAN BUKTI")
    rows = [
        ("Estate", _first_nonblank(row, ["estate", "estate_list"], _first_nonblank(group_meta, ["estate_primary", "estate_list"])), _first_nonblank(row, ["estate", "estate_list"], _first_nonblank(group_meta, ["estate_primary", "estate_list"]))),
        ("Afdeling", _first_nonblank(row, ["afdeling", "afdeling_list"], _first_nonblank(group_meta, ["afdeling_primary", "afdeling_list"])), _first_nonblank(row, ["afdeling", "afdeling_list"], _first_nonblank(group_meta, ["afdeling_primary", "afdeling_list"]))),
        ("Tanggal", _first_nonblank(row, ["tanggal", "sql_date_list"], _first_nonblank(group_meta, ["sql_date_list"])), _first_nonblank(row, ["tanggal", "sql_date_list"], _first_nonblank(group_meta, ["sql_date_list"]))),
        ("Kerani", _first_nonblank(row, ["kerani_panen", "nama_kerani_panen", "kerani_panen_list"], _first_nonblank(group_meta, ["kerani_panen_list"])), _first_nonblank(row, ["kerani_dt", "kerani_ft", "nama_kerani_transport_dt", "nama_kerani_transport_ft"], _first_nonblank(group_meta, ["kerani_dt_list", "kerani_ft_list"], "-"))),
        ("Status", "Perlu Verifikasi", "Perlu Verifikasi"),
    ]
    # headers
    ty = table_y + table_h - 17 * mm
    c.setFillColor(SOFT_BLUE); c.rect(table_x + 4 * mm, ty, table_w - 8 * mm, 7 * mm, fill=1, stroke=0)
    draw_fit(c, "Informasi", table_x + 5 * mm, ty + 2.2 * mm, 22 * mm, "Helvetica-Bold", 5.7, SLATE)
    draw_fit(c, "Kerani Panen", table_x + 28 * mm, ty + 2.2 * mm, 25 * mm, "Helvetica-Bold", 5.7, NAVY)
    draw_fit(c, "Kerani Transport", table_x + 55 * mm, ty + 2.2 * mm, 25 * mm, "Helvetica-Bold", 5.7, RED)
    ty -= 7 * mm
    for idx, (a, b, dval) in enumerate(rows):
        if idx % 2 == 0:
            c.setFillColor(colors.HexColor("#F8FAFC")); c.rect(table_x + 4 * mm, ty, table_w - 8 * mm, 7 * mm, fill=1, stroke=0)
        draw_fit(c, a, table_x + 5 * mm, ty + 2.1 * mm, 21 * mm, "Helvetica-Bold", 5.7, DARK)
        draw_fit(c, b, table_x + 28 * mm, ty + 2.1 * mm, 25 * mm, "Helvetica-Bold", 5.7, SLATE)
        draw_fit(c, dval, table_x + 55 * mm, ty + 2.1 * mm, 25 * mm, "Helvetica-Bold", 5.7, SLATE)
        ty -= 7 * mm

    ix, iy, iw, ih = MARGIN_X + table_w + 7 * mm, table_y, PAGE_W - MARGIN_X - (MARGIN_X + table_w + 7 * mm), table_h
    rounded_rect(c, ix, iy, iw, ih, 4 * mm, fill=SOFT_RED, stroke=BORDER, width=0.55)
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 8.2); c.drawString(ix + 4 * mm, iy + ih - 8 * mm, "INTERPRETASI TEMUAN")
    bullets = [
        "Kesamaan visual pada bukti panen dan transport perlu diverifikasi.",
        "Bandingkan tanggal, lokasi, kerani, dan konteks pengambilan foto.",
        "Hasil AI menjadi indikator awal, bukan keputusan final.",
    ]
    yy = iy + ih - 19 * mm
    for b in bullets:
        c.setFillColor(RED); c.circle(ix + 5 * mm, yy + 1.8 * mm, 1.1 * mm, fill=1, stroke=0)
        draw_text_lines(c, wrap_lines(c, b, iw - 13 * mm, "Helvetica-Bold", 6.7, max_lines=2), ix + 9 * mm, yy, "Helvetica-Bold", 6.7, 7.4, SLATE)
        yy -= 12 * mm
    c.showPage()
    return page_no + 1

def similarity_transport_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, cache_dir: Path, page_no: int) -> int:
    """Keep the original all-time insight and append a clean latest-month insight."""
    if groups is None or groups.empty:
        return page_no
    if "report_system" in groups.columns:
        has_harmony = groups["report_system"].astype(str).str.upper().eq("HARMONY").any()
    else:
        has_harmony = str(system or "").upper() == "HARMONY" or (
            groups.get("source_company", pd.Series(dtype=str)).astype(str).str.upper().isin(["PWP", "DIN"]).any()
            if "source_company" in groups.columns else False
        )
    if not has_harmony:
        return page_no

    cache_dir.mkdir(parents=True, exist_ok=True)
    sim_all = transport_similarity_summary_for_scope(details)
    _, month_details, latest_month = latest_month_scope(groups, details)
    sim_month = transport_similarity_summary_for_scope(month_details)
    latest_label = month_label_id(latest_month)

    def _draw_similarity_scope(scope_df: pd.DataFrame, title_suffix: str, accent, file_tag: str, narrative: str, current_page: int) -> int:
        if scope_df is None or scope_df.empty:
            return current_page
        draw_header(c, f"Similarity Transport - {title_suffix}", period, current_page)
        x, y, w, h = MARGIN_X, PAGE_H - 94 * mm, PAGE_W - 2 * MARGIN_X, 39 * mm
        rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        c.setFillColor(accent); c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 6 * mm, y + h - 10 * mm, f"KHUSUS HARMONY - {company}")
        draw_text_lines(c, wrap_lines(c, narrative, w - 12 * mm, "Helvetica-Bold", 7.5, max_lines=3), x + 6 * mm, y + h - 20 * mm, "Helvetica-Bold", 7.5, 9, SLATE)
        cp = chart_bar(scope_df, cache_dir / f"similarity_transport_{file_tag}_{sanitize_filename(company)}.png", CHART_BLUE if accent == NAVY else CHART_BLUE, 8)
        chart_panel(c, "Similarity Transport Usage", cp, MARGIN_X, 66 * mm, PAGE_W - 2 * MARGIN_X, 108 * mm, accent)
        note_y = 58 * mm
        rounded_rect(c, MARGIN_X, note_y, PAGE_W - 2 * MARGIN_X, 10 * mm, 3 * mm, fill=SOFT_BLUE_2, stroke=BORDER, width=0.35)
        note = "Keterangan: TIDAK ADA TRANSPORT = Foto Transport tidak ditemukan. SAMA = similarity >= 83%. BERBEDA = similarity < 83%."
        draw_fit(c, note, MARGIN_X + 5 * mm, note_y + 3.2 * mm, PAGE_W - 2 * MARGIN_X - 10 * mm, "Helvetica-Bold", 5.8, SLATE, min_size=4.8)
        c.showPage()
        return current_page + 1

    if not sim_all.empty:
        page_no = _draw_similarity_scope(
            sim_all,
            "All Time",
            NAVY,
            "all_time",
            f"Section ini menampilkan sebaran similarity transport sepanjang histori untuk Harmony/{company}. Data ELHP tidak memiliki basis transport sehingga tidak dimasukkan dalam metrik similarity transport.",
            page_no,
        )
    if not sim_month.empty and latest_month:
        dominant_label = safe_text(sim_month.iloc[0].get("label") if not sim_month.empty else "-", "-")
        page_no = _draw_similarity_scope(
            sim_month,
            latest_label,
            RED,
            f"month_{latest_month}",
            f"Kondisi {latest_label} ditampilkan sebagai insight periode terkini. Kategori similarity transport yang paling sering muncul pada periode ini adalah {dominant_label}.",
            page_no,
        )
    return page_no

def prepare_combined_data(args) -> tuple[pd.DataFrame, pd.DataFrame, list[tuple[str, str]]]:
    pairs = SYSTEM_COMPANY_DEFAULTS
    all_groups = []
    all_details = []
    for s, comp in pairs:
        g, d = prepare_data(s, comp, args)
        if g is not None and not g.empty:
            g = g.copy()
            g["report_system"] = s
            g["report_company"] = comp
            all_groups.append(g)
        if d is not None and not d.empty:
            d = d.copy()
            d["report_system"] = s
            d["report_company"] = comp
            all_details.append(d)
    groups = pd.concat(all_groups, ignore_index=True) if all_groups else pd.DataFrame()
    details = pd.concat(all_details, ignore_index=True) if all_details else pd.DataFrame()
    return groups, details, pairs




def filter_pair_frames(groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    g = groups.copy() if groups is not None else pd.DataFrame()
    d = details.copy() if details is not None else pd.DataFrame()
    s = str(system).upper()
    c = str(company).upper()
    if not g.empty:
        if "report_system" in g.columns:
            g = g[g["report_system"].astype(str).str.upper().eq(s)]
        elif "system" in g.columns:
            g = g[g["system"].astype(str).str.upper().eq(s)]
        if "report_company" in g.columns:
            g = g[g["report_company"].astype(str).str.upper().eq(c)]
        elif "source_company" in g.columns:
            g = g[g["source_company"].astype(str).str.upper().eq(c)]
    if not d.empty:
        if "report_system" in d.columns:
            d = d[d["report_system"].astype(str).str.upper().eq(s)]
        elif "system" in d.columns:
            d = d[d["system"].astype(str).str.upper().eq(s)]
        if "report_company" in d.columns:
            d = d[d["report_company"].astype(str).str.upper().eq(c)]
        elif "source_company" in d.columns:
            d = d[d["source_company"].astype(str).str.upper().eq(c)]
    if detail_for_groups and not g.empty:
        try:
            d = detail_for_groups(d, g)
        except Exception:
            pass
    return g.reset_index(drop=True), d.reset_index(drop=True)


def pt_title(system: str, company: str) -> str:
    system = str(system).upper()
    company = str(company).upper()
    if system == "HARMONY":
        return f"{company} - Harmony"
    return f"{company} - ELHP"


def pt_context_text(system: str, company: str) -> str:
    system = str(system).upper()
    if system == "HARMONY":
        return "Sumber Harmony memiliki data panen dan transport, sehingga section similarity transport ditampilkan khusus pada PT ini."
    return "Sumber ELHP berfokus pada evidence panen; data transport tidak dihitung sebagai missing transport pada section ini."


def company_story_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    details: pd.DataFrame,
    kp: dict[str, Any],
    system: str,
    company: str,
    period: str,
    cache_dir: Path,
    page_no: int,
    section_no: int,
    selected_months: Any = None,
) -> int:
    """Clean PT overview: narrative + KPI + monthly pattern only."""
    draw_header(c, f"Section {section_no:02d} - {company}", period, page_no)

    start_s, end_s, range_s = report_period_range(groups, details)
    tm = harvest_ticket_metrics(
        groups, details, kp, system, company, selected_months
    )
    dom_cat, _ = dominant(groups, "reuse_category")
    dom_est, _ = dominant(groups, "estate_primary")
    dom_date, _ = dominant(groups, "date_category")
    title = pt_title(system, company)

    bx, by, bw, bh = MARGIN_X, PAGE_H - 105 * mm, PAGE_W - 2 * MARGIN_X, 48 * mm
    rounded_rect(c, bx, by, bw, bh, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(bx + 6 * mm, by + bh - 11 * mm, title.upper())
    c.setFillColor(REUSED_TEXT)
    c.setFont("Helvetica-Bold", 7.0)
    c.drawString(bx + 6 * mm, by + bh - 20 * mm, f"SYSTEM {system}  |  COMPANY {company}  |  PERIODE {range_s}".upper())
    story = (
        f"Pada periode {range_s}, {company} mencatat {fmt_int(kp.get('total_group'))} reused group "
        f"dengan {fmt_int(kp.get('total_usage'))} total usage. "
        f"{ticket_story_sentence(tm, kp)} "
        f"Pola utama adalah {dom_cat}, date category dominan {dom_date}, dan lokasi tertinggi {dom_est}."
    )
    lines = wrap_lines(c, story, bw - 12 * mm, "Helvetica-Bold", 7.4, max_lines=4)
    draw_text_lines(c, lines, bx + 6 * mm, by + 14 * mm, "Helvetica-Bold", 7.4, 8.5, SLATE)

    gap_x = 6 * mm
    gap_y = 5 * mm
    kx = MARGIN_X
    kh = 20 * mm
    kw = (PAGE_W - 2 * MARGIN_X - gap_x) / 2
    row1_y = by - 27 * mm
    row2_y = row1_y - kh - gap_y
    metric_card(c, kx, row1_y, kw, kh, "Total Group", fmt_int(kp.get("total_group")), NAVY)
    metric_card(c, kx + kw + gap_x, row1_y, kw, kh, "Total Usage", fmt_int(kp.get("total_usage")), ACCENT_BLUE_2)
    metric_card(c, kx, row2_y, kw, kh, "Max Usage", fmt_int(kp.get("max_usage")), ACCENT_BLUE_3)
    metric_card(
        c,
        kx + kw + gap_x,
        row2_y,
        kw,
        kh,
        "% Tiket",
        fmt_pct(tm.get("usage_ticket_pct", tm.get("usage_pct_of_tickets")), 2),
        NAVY,
    )

    cx, cy, cw, ch = MARGIN_X, row2_y - 18 * mm, PAGE_W - 2 * MARGIN_X, 13 * mm
    rounded_rect(c, cx, cy, cw, ch, 4 * mm, fill=SOFT_BLUE if str(system).upper()=="HARMONY" else SOFT_BLUE_2, stroke=BORDER, width=0.4)
    c.setFillColor(REUSED_TEXT)
    c.circle(cx + 6 * mm, cy + 6.5 * mm, 1.6 * mm, fill=1, stroke=0)
    draw_text_lines(c, wrap_lines(c, pt_context_text(system, company), cw - 18 * mm, "Helvetica-Bold", 7.1, max_lines=2), cx + 11 * mm, cy + 7.2 * mm, "Helvetica-Bold", 7.1, 7.8, SLATE)

    cache_dir.mkdir(parents=True, exist_ok=True)
    mdf = month_usage_summary(groups, details)
    chart_path = chart_monthly_pattern(mdf, cache_dir / f"pt_monthly_{sanitize_filename(system)}_{sanitize_filename(company)}.png")
    chart_panel(c, "Berulang Monthly Pattern", chart_path, MARGIN_X, 28 * mm, PAGE_W - 2 * MARGIN_X, 83 * mm, NAVY)

    c.showPage()
    return page_no + 1


def company_insight_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    details: pd.DataFrame,
    kp: dict[str, Any],
    system: str,
    company: str,
    period: str,
    cache_dir: Path,
    page_no: int,
    section_no: int,
) -> int:
    """Preserve the original all-time analysis, then append the same analysis for the latest month."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    month_groups, _, latest_month = latest_month_scope(groups, details)
    latest_label = month_label_id(latest_month)

    def _draw_scope(scope_groups: pd.DataFrame, scope_label: str, accent, file_tag: str, current_page: int) -> int:
        if scope_groups is None or scope_groups.empty:
            return current_page
        draw_header(c, f"Section {section_no:02d} - Analisis {company} - {scope_label}", period, current_page)
        dom_cat, _ = dominant(scope_groups, "reuse_category")
        dom_est, _ = dominant(scope_groups, "estate_primary")
        dom_date, _ = dominant(scope_groups, "date_category")

        x, y, w, h = MARGIN_X, PAGE_H - 92 * mm, PAGE_W - 2 * MARGIN_X, 34 * mm
        rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        c.setFillColor(accent); c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 6 * mm, y + h - 10 * mm, "TEMUAN UTAMA PER KATEGORI")
        if scope_label == "All Time":
            narrative = (
                f"Sebaran temuan {company} menunjukkan kategori reuse dominan {dom_cat}, "
                f"date category dominan {dom_date}, dan konsentrasi lokasi tertinggi berada pada {dom_est}. "
                "Bagian ini membantu menentukan area prioritas untuk pemeriksaan lanjutan."
            )
        else:
            narrative = (
                f"Pada {scope_label}, kategori reuse dominan adalah {dom_cat}, date category dominan {dom_date}, "
                f"dan konsentrasi lokasi tertinggi berada pada {dom_est}. Insight ini memperlihatkan kondisi terbaru tanpa menghilangkan pola historis pada halaman sebelumnya."
            )
        draw_text_lines(c, wrap_lines(c, narrative, w - 12 * mm, "Helvetica-Bold", 7.5, max_lines=3), x + 6 * mm, y + h - 20 * mm, "Helvetica-Bold", 7.5, 8.8, SLATE)

        reuse_df = agg_usage(scope_groups, "reuse_category", 5)
        date_df = agg_usage(scope_groups, "date_category", 5)
        estate_df = agg_usage(scope_groups, "estate_primary", 6)
        afd_df = agg_usage(scope_groups, "afdeling_primary", 6) if "afdeling_primary" in scope_groups.columns else pd.DataFrame()
        p_reuse = chart_donut(reuse_df, cache_dir / f"ins_{file_tag}_reuse_{sanitize_filename(system)}_{sanitize_filename(company)}.png", CHART_PALETTE, 5)
        p_date = chart_donut(date_df, cache_dir / f"ins_{file_tag}_date_{sanitize_filename(system)}_{sanitize_filename(company)}.png", CHART_PALETTE, 5)
        p_estate = chart_bar(estate_df, cache_dir / f"ins_{file_tag}_estate_{sanitize_filename(system)}_{sanitize_filename(company)}.png", CHART_BLUE, 6)
        p_afd = chart_bar(afd_df, cache_dir / f"ins_{file_tag}_afd_{sanitize_filename(system)}_{sanitize_filename(company)}.png", CHART_BLUE, 6)

        gap = 6 * mm
        cw = (PAGE_W - 2 * MARGIN_X - gap) / 2
        ch = 58 * mm
        y1 = PAGE_H - 158 * mm
        y2 = PAGE_H - 224 * mm
        chart_panel(c, "Reuse Category", p_reuse, MARGIN_X, y1, cw, ch, REUSED_ACCENT)
        chart_panel(c, "Date Category", p_date, MARGIN_X + cw + gap, y1, cw, ch, RED)
        chart_panel(c, "Estate Focus", p_estate, MARGIN_X, y2, cw, ch, RED)
        chart_panel(c, "Afdeling Focus", p_afd, MARGIN_X + cw + gap, y2, cw, ch, NAVY)
        c.showPage()
        return current_page + 1

    page_no = _draw_scope(groups, "All Time", NAVY, "all_time", page_no)
    if latest_month and month_groups is not None and not month_groups.empty:
        page_no = _draw_scope(month_groups, latest_label, REUSED_ACCENT, f"month_{latest_month}", page_no)
    return page_no

def company_personnel_page(
    c: canvas.Canvas,
    details: pd.DataFrame,
    system: str,
    company: str,
    period: str,
    cache_dir: Path,
    page_no: int,
    section_no: int | None = None,
    top_n: int = 10,
    groups: pd.DataFrame | None = None,
) -> int:
    """Preserve the original all-time personnel page, then append a latest-month page."""
    top_n = max(1, int(top_n))
    prefix = f"Section {section_no:02d} - " if section_no is not None else ""
    all_groups = groups if groups is not None else pd.DataFrame()
    month_groups, month_details, latest_month = latest_month_scope(all_groups, details)
    latest_label = month_label_id(latest_month)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _draw_scope(scope_details: pd.DataFrame, scope_groups: pd.DataFrame, scope_label: str, file_tag: str, current_page: int) -> int:
        if scope_details is None or scope_details.empty:
            return current_page
        header_title = f"{prefix}Top Personel {company} - {scope_label}"
        draw_header(c, header_title, period, current_page)

        pemanen = personnel_usage_summary(scope_details, ["pemanen", "nama_pemanen", "employee_name", "pemanen_list"], top=top_n)
        kerani = personnel_case_summary(scope_details, ["kerani_panen", "nama_kerani_panen", "kerani_panen_list"], top=top_n)
        mandor = personnel_usage_summary(scope_details, ["mandor", "nama_mandor", "mandor_list"], top=top_n)

        def _top_row(frame: pd.DataFrame) -> tuple[str, int]:
            if frame is None or frame.empty:
                return "-", 0
            return safe_text(frame.iloc[0].get("label")), int(frame.iloc[0].get("total_usage", 0) or 0)

        top_pemanen, top_pemanen_usage = _top_row(pemanen)
        top_kerani, top_kerani_usage = _top_row(kerani)
        top_kerani_cases = int(kerani.iloc[0].get("case_count", 0) or 0) if not kerani.empty else 0
        top_mandor, top_mandor_usage = _top_row(mandor)

        x, y, w, h = MARGIN_X, PAGE_H - 92 * mm, PAGE_W - 2 * MARGIN_X, 34 * mm
        rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        c.setFillColor(REUSED_TEXT)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + 6 * mm, y + h - 10 * mm, "TOP PEMANEN & PERSONEL - REUSED PHOTO")
        narrative_lines = [
            f"Pemanen tertinggi: {top_pemanen} | {fmt_int(top_pemanen_usage)} usage",
            f"Kerani Panen tertinggi: {top_kerani} | {fmt_int(top_kerani_cases)} case reused",
            f"Mandor tertinggi: {top_mandor} | {fmt_int(top_mandor_usage)} usage",
        ]
        for index, line in enumerate(narrative_lines):
            draw_fit(c, line, x + 6 * mm, y + h - (20 + index * 6) * mm, w - 12 * mm, "Helvetica-Bold", 7.2, SLATE, min_size=6.0)

        pemanen_chart = chart_personnel_rank(pemanen, cache_dir / f"top_pemanen_{file_tag}_{sanitize_filename(system)}_{sanitize_filename(company)}.png", "#0B4A8F", top=top_n)
        kerani_chart = chart_personnel_rank(kerani, cache_dir / f"top_kerani_panen_{file_tag}_{sanitize_filename(system)}_{sanitize_filename(company)}.png", "#0B4A8F", top=top_n, value_col="case_count", x_label="Jumlah Case Reused")
        mandor_chart = chart_personnel_rank(mandor, cache_dir / f"top_mandor_{file_tag}_{sanitize_filename(system)}_{sanitize_filename(company)}.png", "#0B4A8F", top=top_n)

        chart_h = 52 * mm
        chart_panel(c, f"Top {top_n} Pemanen berdasarkan Reused Usage", pemanen_chart, MARGIN_X, 140 * mm, PAGE_W - 2 * MARGIN_X, chart_h, REUSED_ACCENT)
        chart_panel(c, f"Top {top_n} Kerani Panen berdasarkan Case Reused", kerani_chart, MARGIN_X, 82 * mm, PAGE_W - 2 * MARGIN_X, chart_h, REUSED_ACCENT)
        chart_panel(c, f"Top {top_n} Mandor berdasarkan Reused Usage", mandor_chart, MARGIN_X, 24 * mm, PAGE_W - 2 * MARGIN_X, chart_h, REUSED_ACCENT)
        c.showPage()
        return current_page + 1

    page_no = _draw_scope(details, all_groups, "All Time", "all_time", page_no)
    if latest_month and month_details is not None and not month_details.empty:
        page_no = _draw_scope(month_details, month_groups, latest_label, f"month_{latest_month}", page_no)
    return page_no

def overall_story_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    details: pd.DataFrame,
    kp: dict[str, Any],
    pairs: list[tuple[str, str]],
    period: str,
    page_no: int,
    selected_months: Any = None,
) -> int:
    """General opening page with clean narrative and route cards."""
    draw_header(c, "Ringkasan & Temuan", period, page_no)
    start_s, end_s, range_s = report_period_range(groups, details)
    tm = harvest_ticket_metrics(
        groups, details, kp, "ALL", "ALL", selected_months
    )

    x, y, w, h = MARGIN_X, PAGE_H - 132 * mm, PAGE_W - 2 * MARGIN_X, 68 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 6 * mm, y + h - 12 * mm, "RINGKASAN & TEMUAN UTAMA")
    bullets = [
        f"Data pada buletin ini dimulai dari {start_s} sampai {end_s}. Report dibuat satu file, namun setiap PT dibaca sebagai section tersendiri agar ceritanya jelas.",
        ticket_story_sentence(tm, kp),
        "Setiap section menampilkan ringkasan temuan, Berulang Monthly Pattern, Top Reused by Month, kategori reuse, fokus lokasi, Top Pemanen, Top Kerani Panen, Top Mandor, dan contoh evidence bulan terbaru. Similarity Transport hanya muncul untuk Harmony.",
    ]
    yy = y + h - 23 * mm
    for b in bullets:
        c.setFillColor(NAVY); c.circle(x + 7 * mm, yy + 2.2 * mm, 1.45 * mm, fill=1, stroke=0)
        draw_text_lines(c, wrap_lines(c, b, w - 18 * mm, "Helvetica-Bold", 7.3, 3), x + 12 * mm, yy, "Helvetica-Bold", 7.3, 8.3, SLATE)
        yy -= 15.0 * mm

    # Empat company harus selalu berada di dalam printable width. Versi lama
    # menghitung lebar untuk tiga kolom tetapi tetap menggambar empat kartu,
    # sehingga kartu DWT berada di luar halaman. Gunakan grid 2 x 2.
    gap_x = 6 * mm
    gap_y = 6 * mm
    card_w = (PAGE_W - 2 * MARGIN_X - gap_x) / 2
    card_h = 34 * mm
    cards_top = y - 6 * mm
    for idx, (s, comp) in enumerate(pairs):
        row = idx // 2
        col_idx = idx % 2
        cx = MARGIN_X + col_idx * (card_w + gap_x)
        card_y = cards_top - card_h - row * (card_h + gap_y)
        rounded_rect(c, cx, card_y, card_w, card_h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        col = NAVY if s == "HARMONY" else RED
        c.setFillColor(col); c.circle(cx + 8 * mm, card_y + 23 * mm, 4 * mm, fill=1, stroke=0)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 10)
        c.drawString(cx + 15 * mm, card_y + 25 * mm, comp)
        c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 6.8)
        c.drawString(cx + 15 * mm, card_y + 17 * mm, s)
        g, d = filter_pair_frames(groups, details, s, comp)
        k = make_kpi(g, d)
        c.setFillColor(SLATE); c.setFont("Helvetica-Bold", 6.6)
        c.drawString(cx + 6 * mm, card_y + 9 * mm, f"Group: {fmt_int(k.get('total_group'))}")
        c.drawString(cx + 6 * mm, card_y + 4 * mm, f"Usage: {fmt_int(k.get('total_usage'))}")

    note_y = 34 * mm
    rounded_rect(c, MARGIN_X, note_y, PAGE_W - 2 * MARGIN_X, 18 * mm, 4 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.35)
    c.setFillColor(MUTED); c.setFont("Helvetica-Bold", 6.7)
    draw_text_lines(c, ["Catatan: % Tiket pada report = total usage Golden Eye / total tiket SAP, sama dengan dashboard. Avg usage = total usage / reused group.", "Similarity transport hanya muncul pada Harmony/PWP dan Harmony/DIN karena ELHP tidak memiliki basis transport."], MARGIN_X + 5 * mm, note_y + 11 * mm, "Helvetica-Bold", 6.4, 6.8, MUTED)
    c.showPage()
    return page_no + 1

def agg_usage(groups: pd.DataFrame, col: str, top: int = 6) -> pd.DataFrame:
    if groups.empty or col not in groups.columns:
        return pd.DataFrame({"label": [], "total_usage": []})
    df = groups.copy()
    df["usage_count"] = to_float_series(df.get("usage_count", pd.Series(dtype=float)))
    out = df.groupby(col, dropna=False)["usage_count"].sum().reset_index(name="total_usage").sort_values("total_usage", ascending=False)
    out["label"] = out[col].apply(lambda x: safe_text(x))
    return out[["label", "total_usage"]].head(top)


def personnel_usage_summary(
    details: pd.DataFrame,
    candidates: list[str],
    top: int = 10,
) -> pd.DataFrame:
    """Rank personnel from scoped detail transactions (one row = one usage)."""
    if details is None or details.empty:
        return pd.DataFrame({"label": [], "total_usage": []})
    column = next((name for name in candidates if name in details.columns), None)
    if not column:
        return pd.DataFrame({"label": [], "total_usage": []})

    data = details[[column]].copy()
    data["label"] = data[column].apply(lambda value: safe_text(value, "").strip())
    invalid = {
        "", "-", "--", "N/A", "NA", "NAN", "NONE", "NULL", "UNKNOWN",
        "TIDAK ADA", "KOSONG",
    }
    data = data[~data["label"].str.upper().isin(invalid)].copy()
    if data.empty:
        return pd.DataFrame({"label": [], "total_usage": []})

    output = (
        data.groupby("label", dropna=False)
        .size()
        .reset_index(name="total_usage")
        .sort_values(["total_usage", "label"], ascending=[False, True])
    )
    return output.head(max(1, int(top)))[["label", "total_usage"]]



def personnel_case_summary(
    details: pd.DataFrame,
    candidates: list[str],
    top: int = 10,
) -> pd.DataFrame:
    """Rank Kerani Panen by Reused Photo case count, not reuse-group/category diversity.

    One case is one unique harvest transaction (OPH) for that Kerani Panen.
    If OPH is unavailable for a row, a transaction-level fallback key is built
    from the available detail fields so the row is still counted once.
    """
    empty = pd.DataFrame({"label": [], "case_count": [], "total_usage": []})
    if details is None or details.empty:
        return empty
    column = next((name for name in candidates if name in details.columns), None)
    if not column:
        return empty

    data = details.copy()
    data["label"] = data[column].apply(lambda value: safe_text(value, "").strip())
    invalid = {"", "-", "--", "N/A", "NA", "NAN", "NONE", "NULL", "UNKNOWN", "TIDAK ADA", "KOSONG"}
    data = data[~data["label"].str.upper().isin(invalid)].copy()
    if data.empty:
        return empty

    # OPH is the business transaction identifier, therefore the primary Case ID.
    oph_col = next((name for name in ["oph", "OPH", "harvest_ticket", "tiket_panen", "ticket"] if name in data.columns), None)
    if oph_col:
        oph = data[oph_col].apply(lambda value: safe_text(value, "").strip().upper())
    else:
        oph = pd.Series([""] * len(data), index=data.index, dtype="object")

    # Fallback only for rows with no OPH. It stays transaction-level and never
    # uses group_id/group_key as the Case definition.
    fallback_cols = [name for name in ["image_name", "tanggal", "estate", "afdeling", "blok", "tph"] if name in data.columns]
    if fallback_cols:
        fallback = data[fallback_cols].fillna("").astype(str).agg("|".join, axis=1)
    else:
        fallback = pd.Series([f"ROW|{idx}" for idx in data.index], index=data.index, dtype="object")
    fallback = fallback.where(fallback.str.strip("| " ).ne(""), pd.Series([f"ROW|{idx}" for idx in data.index], index=data.index))
    data["_case_key"] = oph.where(oph.ne(""), "FALLBACK|" + fallback.astype(str))

    usage = data.groupby("label", dropna=False).size().rename("total_usage")
    cases = data.groupby("label", dropna=False)["_case_key"].nunique().rename("case_count")
    output = usage.to_frame().join(cases, how="left").reset_index()
    output["case_count"] = pd.to_numeric(output["case_count"], errors="coerce").fillna(0).astype(int)
    output = output.sort_values(["case_count", "total_usage", "label"], ascending=[False, False, True])
    return output.head(max(1, int(top)))[["label", "case_count", "total_usage"]]


def chart_personnel_rank(
    data: pd.DataFrame,
    out_path: Path,
    color_hex: str,
    top: int = 10,
    value_col: str = "total_usage",
    x_label: str = "Usage",
) -> Path:
    """Render a wide horizontal chart so operational names remain readable."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ranked = data.copy().head(max(1, int(top)))
    if ranked.empty:
        ranked = pd.DataFrame({"label": ["Data personel tidak tersedia"], value_col: [0]})
    ranked["label"] = ranked["label"].astype(str).map(
        lambda value: value if len(value) <= 36 else value[:33] + "..."
    )
    if value_col not in ranked.columns:
        ranked[value_col] = 0
    ranked[value_col] = pd.to_numeric(
        ranked[value_col], errors="coerce"
    ).fillna(0)
    ranked = ranked.sort_values([value_col, "label"], ascending=[True, False])

    fig, ax = plt.subplots(figsize=(8.2, 3.25), dpi=PDF_CHART_DPI)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    bars = ax.barh(
        ranked["label"], ranked[value_col], color=[CHART_PALETTE[i % 4 + 1] for i in range(len(ranked))], height=0.56
    )
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#E2E8F0")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.8)
    ax.tick_params(axis="y", labelsize=7.5, length=0, colors="#334155")
    ax.tick_params(axis="x", labelsize=7.2, colors="#64748B")
    ax.set_xlabel(x_label, fontsize=8.0, color=CHART_BLUE)
    maximum = max(float(ranked[value_col].max()), 1.0)
    ax.set_xlim(0, maximum * 1.16)
    for bar in bars:
        value = bar.get_width()
        ax.text(
            value + maximum * 0.018,
            bar.get_y() + bar.get_height() / 2,
            fmt_int(value),
            va="center",
            fontsize=7.4,
            color=CHART_BLUE,
        )
    style_chart_axis(ax)
    plt.tight_layout(pad=0.8)
    fig.savefig(
        out_path,
        facecolor="white",
        dpi=PDF_CHART_DPI,
        bbox_inches="tight",
        pad_inches=0.08,
    )
    plt.close(fig)
    return out_path


def chart_bar(data: pd.DataFrame, out_path: Path, color_hex: str = CHART_BLUE, top: int = 6):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d = data.copy().head(top)
    if d.empty:
        d = pd.DataFrame({"label": ["No data"], "total_usage": [0]})
    raw_labels = d["label"].fillna("").astype(str).str.strip().tolist()
    d["total_usage"] = pd.to_numeric(d["total_usage"], errors="coerce").fillna(0)
    if "_order" in d.columns:
        d["_order"] = pd.to_numeric(d["_order"], errors="coerce").fillna(999)
        # barh draws first row at the bottom; descending storage produces the
        # requested top-to-bottom visual order 1 -> 2 -> 3.
        d = d.sort_values(["_order", "total_usage"], ascending=[False, True])
    else:
        d = d.sort_values("total_usage", ascending=True)
    raw_labels = d["label"].fillna("").astype(str).str.strip().tolist()
    d["label"] = d["label"].astype(str).map(lambda x: x if len(x) <= 22 else x[:19] + "...")
    fig, ax = plt.subplots(figsize=(6.2, 3.4), dpi=PDF_CHART_DPI)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    status_labels = {"TIDAK ADA TRANSPORT", "SAMA", "BERBEDA"}
    if raw_labels and set(raw_labels).issubset(status_labels):
        bar_colors = [TRANSPORT_STATUS_PALETTE.get(lbl, CHART_BLUE) for lbl in raw_labels]
    else:
        bar_colors = [CHART_PALETTE[i % 4 + 1] for i in range(len(d))]
    bars = ax.barh(d["label"], d["total_usage"], color=bar_colors, height=0.52)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#E2E8F0")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.8)
    ax.tick_params(axis="y", labelsize=8.2, length=0, colors="#334155")
    ax.tick_params(axis="x", labelsize=7.5, colors="#64748B")
    ax.set_xlabel("Usage", fontsize=8.0, color=CHART_BLUE)
    mx = max(float(d["total_usage"].max()), 1.0)
    for b in bars:
        val = b.get_width()
        ax.text(val + mx * 0.015, b.get_y() + b.get_height() / 2, fmt_int(val), va="center", fontsize=7.6, color=CHART_BLUE)
    style_chart_axis(ax)
    plt.tight_layout(pad=0.8)
    fig.savefig(out_path, facecolor="white", dpi=PDF_CHART_DPI, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return out_path


def chart_donut(data: pd.DataFrame, out_path: Path, palette: list[str], top: int = 5):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d = data.copy().head(top)
    if d.empty or float(pd.to_numeric(d.get("total_usage", 0), errors="coerce").fillna(0).sum()) <= 0:
        d = pd.DataFrame({"label": ["No data"], "total_usage": [1]})
    d["label"] = d["label"].astype(str).map(lambda x: x if len(x) <= 24 else x[:21] + "...")
    values = pd.to_numeric(d["total_usage"], errors="coerce").fillna(0).tolist()
    total_val = max(float(sum(values)), 1.0)
    labels = [f"{lbl} | {fmt_int(val)} | {100.0 * float(val) / total_val:.0f}%" for lbl, val in zip(d["label"].tolist(), values)]
    fig, ax = plt.subplots(figsize=(6.2, 3.4), dpi=PDF_CHART_DPI)
    fig.patch.set_facecolor("white")
    wedges, *_ = ax.pie(
        values,
        startangle=90,
        counterclock=False,
        colors=[MONTH_DISTINCT_PALETTE[i % len(MONTH_DISTINCT_PALETTE)] for i in range(len(values))],
        wedgeprops={"width": 0.42, "edgecolor": "white"},
        autopct=lambda p: f"{p:.0f}%" if p >= 6 else "",
        pctdistance=0.74,
        textprops={"fontsize": 8, "color": "white", "weight": "bold"},
    )
    ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(0.90, 0.5), fontsize=7.3, frameon=False)
    ax.set(aspect="equal")
    style_chart_axis(ax)
    plt.tight_layout(pad=0.8)
    fig.savefig(out_path, facecolor="white", dpi=PDF_CHART_DPI, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return out_path


def chart_panel(c: canvas.Canvas, title: str, img_path: Path, x: float, y: float, w: float, h: float, accent=NAVY):
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(accent)
    c.circle(x + 7.5 * mm, y + h - 7.2 * mm, 4.1 * mm, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 8.2)
    c.drawString(x + 14 * mm, y + h - 9.2 * mm, title.upper())
    if img_path.exists():
        c.drawImage(ImageReader(str(img_path)), x + 4 * mm, y + 4 * mm, width=w - 8 * mm, height=h - 16 * mm, preserveAspectRatio=True, anchor="c", mask="auto")


def top_table_small(c: canvas.Canvas, groups: pd.DataFrame, x, y, w, h, rows=4):
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 5 * mm, y + h - 9 * mm, "TOP REUSED GROUPS")
    data = groups.sort_values("usage_count", ascending=False).head(rows) if not groups.empty else pd.DataFrame()
    cols = [("GROUP", 25), ("COMPANY", 22), ("ESTATE", 22), ("USAGE", 18), ("REP FILE", 70)]
    header_y = y + h - 18 * mm
    c.setFillColor(NAVY); c.rect(x + 4 * mm, header_y, w - 8 * mm, 7 * mm, fill=1, stroke=0)
    c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 6.0)
    xx = x + 5 * mm
    for label, ww in cols:
        c.drawString(xx, header_y + 2.2 * mm, label)
        xx += ww * mm
    yy = header_y - 7 * mm
    for i in range(rows):
        if i % 2 == 0:
            c.setFillColor(colors.HexColor("#F8FAFC")); c.rect(x + 4 * mm, yy, w - 8 * mm, 7 * mm, fill=1, stroke=0)
        if i < len(data):
            r = data.iloc[i]
            vals = [safe_text(r.get("group_id")), safe_text(r.get("source_company")), split_first(r.get("estate_list")), fmt_int(r.get("usage_count")), safe_text(r.get("representative_image"))]
        else:
            vals = ["", "", "", "", ""]
        c.setFillColor(SLATE); c.setFont("Helvetica-Bold", 6.2)
        xx = x + 5 * mm
        for val, (_, ww) in zip(vals, cols):
            c.drawString(xx, yy + 2.2 * mm, ellipsize(c, val, ww * mm - 2 * mm, "Helvetica-Bold", 6.2))
            xx += ww * mm
        yy -= 7 * mm


def mini_examples(c: canvas.Canvas, groups: pd.DataFrame, x, y, w, h, n=3):
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 5 * mm, y + h - 9 * mm, "HIGHLIGHTED EXAMPLES")
    top = groups.sort_values("usage_count", ascending=False).head(n) if not groups.empty else pd.DataFrame()
    gap = 4 * mm
    card_w = (w - 10 * mm - gap * (n - 1)) / n
    card_h = h - 18 * mm
    cy = y + 5 * mm
    for i in range(n):
        cx = x + 5 * mm + i * (card_w + gap)
        rounded_rect(c, cx, cy, card_w, card_h, 3 * mm, fill=colors.HexColor("#FBFCFE"), stroke=colors.HexColor("#EEF2F7"), width=0.4)
        if i < len(top):
            r = top.iloc[i]
            reader = get_image_for_key(r.get("image_key") or r.get("representative_image"), ["#DCE2F1", "#DCE2F1", "#DCE2F1"][i % 3])
            label = safe_text(r.get("reuse_category"), "REUSED")
            title = f"Group {safe_text(r.get('group_id'))} | {fmt_int(r.get('usage_count'))} usage"
        else:
            reader = placeholder_image(); label = "REUSED"; title = "-"
        draw_image_fit(c, reader, cx + 2 * mm, cy + card_h - 24 * mm, card_w - 4 * mm, 19 * mm)
        c.setFillColor(NAVY if i % 2 == 0 else RED)
        c.roundRect(cx + 2 * mm, cy + card_h - 29 * mm, min(card_w - 4 * mm, 28 * mm), 5 * mm, 1.7 * mm, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 5.3)
        c.drawString(cx + 3.2 * mm, cy + card_h - 27.4 * mm, ellipsize(c, label.upper(), 24 * mm, "Helvetica-Bold", 5.3))
        c.setFillColor(DARK); c.setFont("Helvetica-Bold", 6.1)
        c.drawString(cx + 2 * mm, cy + 5 * mm, ellipsize(c, title, card_w - 4 * mm, "Helvetica-Bold", 6.1))


def cover_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    kp: dict[str, Any],
    system: str,
    company: str,
    period: str,
    *,
    show_scope_badges: bool = True,
):
    c.setFillColor(WHITE); c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    draw_decor(c)
    draw_logo(c, PAGE_W / 2 - 38 * mm, PAGE_H - 40 * mm, 76 * mm, 20 * mm)

    c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 11); c.drawString(MARGIN_X, PAGE_H - 66 * mm, "BULETIN BULANAN")
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 31); c.drawString(MARGIN_X, PAGE_H - 82 * mm, "AI POWERED")
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 38); c.drawString(MARGIN_X, PAGE_H - 100 * mm, "GOLDEN EYE")
    c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 37); c.drawString(MARGIN_X, PAGE_H - 118 * mm, "REUSED PHOTO")
    c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 14); c.drawString(MARGIN_X, PAGE_H - 132 * mm, "MONTHLY BULLETIN")
    c.setStrokeColor(NAVY); c.setLineWidth(1.2); c.line(MARGIN_X, PAGE_H - 139 * mm, MARGIN_X + 13 * mm, PAGE_H - 139 * mm)
    c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 10.5); c.drawString(MARGIN_X, PAGE_H - 151 * mm, "Laporan Bulanan Audit Reused Photo")
    # Period badge is placed as its own element, with a safe gap from KPI cards.
    draw_calendar_period_badge(c, MARGIN_X, PAGE_H - 168 * mm, 50 * mm, 10.5 * mm, period)

    if show_scope_badges:
        estate_dom, _ = dominant(groups, "estate_primary")
        chip_x = PAGE_W - 61 * mm
        draw_badge(c, chip_x, PAGE_H - 90 * mm, 44 * mm, 10 * mm, f"System {system}", NAVY)
        draw_badge(c, chip_x, PAGE_H - 104 * mm, 44 * mm, 10 * mm, f"Company {company}", RED)
        draw_badge(c, chip_x, PAGE_H - 118 * mm, 44 * mm, 10 * mm, f"Estate {estate_dom}", GRAY)

    # Metrics across full width, deliberately placed below the month badge.
    # This removes the collision where the first KPI card covered the period badge.
    gap = 4.5 * mm
    kx = MARGIN_X
    ky = 94 * mm
    kw = (PAGE_W - 2 * MARGIN_X - gap * 3) / 4
    kh = 27.5 * mm
    metric_card(c, kx, ky, kw, kh, "Total Group", fmt_int(kp.get("total_group")), NAVY)
    metric_card(c, kx + (kw + gap), ky, kw, kh, "Total Usage", fmt_int(kp.get("total_usage")), ACCENT_BLUE_2)
    metric_card(c, kx + 2 * (kw + gap), ky, kw, kh, "Max Usage", fmt_int(kp.get("max_usage")), ACCENT_BLUE_3)
    metric_card(c, kx + 3 * (kw + gap), ky, kw, kh, "Avg Usage", fmt_num(kp.get("avg_usage"), 2), ACCENT_BLUE_4)

    # Evidence strip, more compact and always below KPI cards.
    top = groups.sort_values("usage_count", ascending=False).head(4) if not groups.empty else pd.DataFrame()
    strip_y = 55 * mm
    strip_h = 28 * mm
    strip_gap = 5 * mm
    card_w = (PAGE_W - 2 * MARGIN_X - strip_gap * 3) / 4
    for i in range(4):
        x = MARGIN_X + i * (card_w + strip_gap)
        rounded_rect(c, x, strip_y, card_w, strip_h, 3 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        if i < len(top):
            r = top.iloc[i]
            reader = get_image_for_key(r.get("image_key") or r.get("representative_image"), ["#DCE2F1", "#DCE2F1", "#DCE2F1", "#DCE2F1"][i])
        else:
            reader = placeholder_image()
        draw_image_fit(c, reader, x + 2 * mm, strip_y + 2 * mm, card_w - 4 * mm, strip_h - 4 * mm)

    # Footer principles - clean four-column layout.
    # V14 fix: divider lines are between blocks only; text is centered below icons so it cannot overlap lines.
    base_y = 22 * mm
    slogan_x = MARGIN_X
    c.setStrokeColor(NAVY); c.setLineWidth(1.2)
    c.line(slogan_x, base_y - 1 * mm, slogan_x, base_y + 16 * mm)
    c.setFont("Helvetica-Bold", 9.0)
    c.setFillColor(NAVY); c.drawString(slogan_x + 6 * mm, base_y + 11.5 * mm, "INTEGRITAS")
    c.setFillColor(REUSED_TEXT); c.drawString(slogan_x + 6 * mm, base_y + 5.5 * mm, "AKURAT")
    c.setFillColor(REUSED_TEXT); c.drawString(slogan_x + 6 * mm, base_y - 0.5 * mm, "TRANSPARAN")

    principles = [
        ("DATA AKURAT", "KEPUTUSAN TEPAT", NAVY, "target"),
        ("AUDIT INDEPENDEN", "KUALITAS TERJAMIN", RED, "shield"),
        ("MONITORING BERKALA", "PERBAIKAN BERKELANJUTAN", GRAY, "bars"),
    ]
    block_lefts = [69 * mm, 112 * mm, 155 * mm]
    block_w = 36 * mm
    for idx, (a, b, col, kind) in enumerate(principles):
        bx = block_lefts[idx]
        cx = bx + block_w / 2
        if idx >= 0:
            c.setStrokeColor(colors.HexColor("#E1E7EF")); c.setLineWidth(0.55)
            c.line(bx - 6 * mm, base_y - 2 * mm, bx - 6 * mm, base_y + 17 * mm)
        c.setFillColor(col); c.circle(cx, base_y + 11 * mm, 4.4 * mm, fill=1, stroke=0)
        c.setStrokeColor(WHITE); c.setFillColor(WHITE); c.setLineWidth(0.8)
        if kind == "target":
            c.circle(cx, base_y + 11 * mm, 2.0 * mm, fill=0, stroke=1)
            c.line(cx - 2.8 * mm, base_y + 11 * mm, cx + 2.8 * mm, base_y + 11 * mm)
            c.line(cx, base_y + 8.2 * mm, cx, base_y + 13.8 * mm)
        elif kind == "shield":
            p = c.beginPath(); p.moveTo(cx - 2.0 * mm, base_y + 11.2 * mm); p.lineTo(cx - 0.4 * mm, base_y + 9.7 * mm); p.lineTo(cx + 2.2 * mm, base_y + 12.4 * mm); c.drawPath(p, stroke=1, fill=0)
        else:
            c.rect(cx - 2.3 * mm, base_y + 8.4 * mm, 1.0 * mm, 3.2 * mm, fill=1, stroke=0)
            c.rect(cx - 0.4 * mm, base_y + 8.4 * mm, 1.0 * mm, 5.0 * mm, fill=1, stroke=0)
            c.rect(cx + 1.5 * mm, base_y + 8.4 * mm, 1.0 * mm, 6.8 * mm, fill=1, stroke=0)
        draw_fit(c, a, bx, base_y + 3.4 * mm, block_w, "Helvetica-Bold", 5.8, DARK, align="center", min_size=4.8)
        draw_fit(c, b, bx, base_y - 2.0 * mm, block_w, "Helvetica-Bold", 5.2, SLATE, align="center", min_size=4.3)

    draw_footer_bar(c)
    c.showPage()


def summary_page(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    kp: dict[str, Any],
    system: str,
    company: str,
    period: str,
    cache_dir: Path,
    page_no: int,
    selected_months: Any = None,
):
    draw_header(c, "Ringkasan & Temuan Utama", period, page_no)
    content_top = PAGE_H - 58 * mm

    # Executive summary full-width. This prevents the small KPI cards from colliding with text.
    sx, sy, sw, sh = MARGIN_X, content_top - 34 * mm, PAGE_W - 2 * MARGIN_X, 34 * mm
    rounded_rect(c, sx, sy, sw, sh, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9.0); c.drawString(sx + 6 * mm, sy + sh - 9 * mm, "RINGKASAN EKSEKUTIF")
    top_r = top_group(groups)
    dom_cat, _ = dominant(groups, "reuse_category")
    dom_est, _ = dominant(groups, "estate_primary")
    start_s, end_s, range_s = report_period_range(groups, pd.DataFrame())
    tm = harvest_ticket_metrics(
        groups, pd.DataFrame(), kp, system, company, selected_months
    )
    bullets = [
        f"Periode data: {range_s}. Total {fmt_int(kp.get('total_group'))} reused group dengan {fmt_int(kp.get('total_usage'))} total usage.",
        f"Temuan terbesar adalah Group {safe_text(top_r.get('group_id') if top_r is not None else '-')} dengan {fmt_int(top_r.get('usage_count') if top_r is not None else 0)} usage.",
        f"Pola dominan: {dom_cat}; lokasi tertinggi: {dom_est}. {ticket_story_sentence(tm, kp)}",
    ]
    # Two-column bullet block inside a single executive box.
    left_x = sx + 7 * mm
    mid_x = sx + sw / 2 + 3 * mm
    yy = sy + sh - 18 * mm
    bullet_w = sw / 2 - 13 * mm
    for idx, b in enumerate(bullets):
        bx = left_x if idx < 2 else mid_x
        by = yy - (idx % 2) * 9.5 * mm if idx < 2 else yy
        c.setFillColor(NAVY); c.circle(bx, by + 2 * mm, 1.5 * mm, fill=1, stroke=0)
        lines = wrap_lines(c, b, bullet_w, "Helvetica-Bold", 7.1, max_lines=2)
        draw_text_lines(c, lines, bx + 5 * mm, by, "Helvetica-Bold", 7.1, 8.4, SLATE)

    # KPI row: four large enough cards, no clipped labels.
    gap = 4 * mm
    kx = MARGIN_X
    ky = sy - 29 * mm
    kw = (PAGE_W - 2 * MARGIN_X - gap * 3) / 4
    kh = 23 * mm
    metric_card(c, kx, ky, kw, kh, "Total Group", fmt_int(kp.get("total_group")), NAVY)
    metric_card(c, kx + (kw + gap), ky, kw, kh, "Total Usage", fmt_int(kp.get("total_usage")), ACCENT_BLUE_2)
    metric_card(c, kx + 2 * (kw + gap), ky, kw, kh, "Max Usage", fmt_int(kp.get("max_usage")), ACCENT_BLUE_3)
    metric_card(c, kx + 3 * (kw + gap), ky, kw, kh, "Avg Usage", fmt_num(kp.get("avg_usage"), 2), ACCENT_BLUE_4)

    # Charts occupy the lower page. No table/examples here to keep page clean.
    cache_dir.mkdir(parents=True, exist_ok=True)
    company_df = agg_usage(groups, "source_company", 6)
    estate_df = agg_usage(groups, "estate_primary", 6)
    reuse_df = agg_usage(groups, "reuse_category", 5)
    date_df = agg_usage(groups, "date_category", 5)
    p_company = chart_bar(company_df, cache_dir / f"company_{system}_{company}.png", CHART_BLUE, 6)
    p_estate = chart_bar(estate_df, cache_dir / f"estate_{system}_{company}.png", CHART_BLUE, 6)
    p_reuse = chart_donut(reuse_df, cache_dir / f"reuse_{system}_{company}.png", CHART_PALETTE, 5)
    p_date = chart_donut(date_df, cache_dir / f"date_{system}_{company}.png", CHART_PALETTE, 5)

    gap = 6 * mm
    cw = (PAGE_W - 2 * MARGIN_X - gap) / 2
    ch = 55 * mm
    y1 = ky - ch - 8 * mm
    y2 = y1 - ch - 8 * mm
    chart_panel(c, "Summary by Company", p_company, MARGIN_X, y1, cw, ch, NAVY)
    chart_panel(c, "Summary by Estate", p_estate, MARGIN_X + cw + gap, y1, cw, ch, RED)
    chart_panel(c, "Reuse Category", p_reuse, MARGIN_X, y2, cw, ch, NAVY)
    chart_panel(c, "Date Category", p_date, MARGIN_X + cw + gap, y2, cw, ch, RED)
    c.showPage()

def top_groups_pages(c: canvas.Canvas, groups: pd.DataFrame, system: str, company: str, period: str, top_n: int, page_no: int) -> int:
    data = groups.sort_values("usage_count", ascending=False).head(top_n) if not groups.empty else pd.DataFrame()
    rows_per_page = 12
    if data.empty:
        chunks = [data]
    else:
        chunks = [data.iloc[i:i + rows_per_page] for i in range(0, len(data), rows_per_page)]
    columns = [
        ("No", 7), ("Group", 16), ("Estate", 14), ("Afd", 10), ("Usage", 13),
        ("Reuse Category", 40), ("Date Category", 34), ("Sample OPH", 36),
    ]
    for page_idx, chunk in enumerate(chunks):
        draw_header(c, "Top Reused Groups", period, page_no)
        x = MARGIN_X; y = 29 * mm; w = PAGE_W - 2 * MARGIN_X; h = PAGE_H - 84 * mm
        rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 5 * mm, y + h - 10 * mm, "PRIORITAS REVIEW")
        c.setFillColor(MUTED); c.setFont("Helvetica-Bold", 7)
        c.drawRightString(x + w - 5 * mm, y + h - 10 * mm, f"{system} / {company}")
        header_y = y + h - 21 * mm
        c.setFillColor(NAVY); c.rect(x + 4 * mm, header_y, w - 8 * mm, 8 * mm, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 6.2)
        xx = x + 5 * mm
        for col, ww in columns:
            draw_fit(c, col.upper(), xx, header_y + 2.8 * mm, ww * mm - 1.5 * mm, "Helvetica-Bold", 6.2, WHITE, min_size=5.0)
            xx += ww * mm
        row_h = 11.0 * mm
        yy = header_y - row_h
        display_rows = max(1, len(chunk))
        for i in range(display_rows):
            abs_i = page_idx * rows_per_page + i
            if i % 2 == 0:
                c.setFillColor(colors.HexColor("#F8FAFC")); c.rect(x + 4 * mm, yy, w - 8 * mm, row_h, fill=1, stroke=0)
            if i < len(chunk):
                r = chunk.iloc[i]
                vals = [
                    str(abs_i + 1),
                    safe_text(r.get("group_id")),
                    split_first(r.get("estate_list")),
                    split_first(r.get("afdeling_list")),
                    fmt_int(r.get("usage_count")),
                    short_category(r.get("reuse_category")),
                    short_category(r.get("date_category")),
                    split_first(r.get("oph_list")),
                ]
            else:
                vals = ["", "No data", "", "", "", "", "", ""]
            xx = x + 5 * mm
            for val, (_, ww) in zip(vals, columns):
                draw_fit(c, val, xx, yy + 4.0 * mm, ww * mm - 2.0 * mm, "Helvetica-Bold", 6.4, SLATE, min_size=5.2)
                xx += ww * mm
            yy -= row_h
        c.showPage()
        page_no += 1
    return page_no



def _transaction_datetime_series(details: pd.DataFrame) -> pd.Series:
    """Best-effort one datetime per detail row, preserving row index."""
    if details is None or details.empty:
        return pd.Series(dtype="datetime64[ns]")
    result = pd.Series(pd.NaT, index=details.index, dtype="datetime64[ns]")
    for col in ["tanggal", "period_date", "sql_date", "date", "posting_date", "month"]:
        if col not in details.columns:
            continue
        raw = details[col]
        parsed = pd.to_datetime(raw, errors="coerce")
        # YYYY-MM values are valid month markers; normalize them to month start.
        mask = result.isna() & parsed.notna()
        if mask.any():
            result.loc[mask] = parsed.loc[mask]
    return result


def latest_transaction_month(details: pd.DataFrame) -> str:
    dates = _transaction_datetime_series(details)
    if dates.empty or dates.dropna().empty:
        return ""
    return dates.max().strftime("%Y-%m")


def month_label_id(month_value: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})$", safe_text(month_value, ""))
    if not m:
        return safe_text(month_value, "Monthly Report")
    year, month = int(m.group(1)), int(m.group(2))
    names = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    if 1 <= month <= 12:
        return f"{names[month - 1]} {year}"
    return month_value



def latest_month_scope(groups: pd.DataFrame, details: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Return metadata groups + details recalculated for the latest transaction month."""
    if details is None or details.empty:
        return pd.DataFrame(), pd.DataFrame(), ""
    d = details.copy()
    d["_cmp_dt"] = _transaction_datetime_series(d)
    valid = d["_cmp_dt"].notna()
    if not valid.any():
        return pd.DataFrame(), pd.DataFrame(), ""
    latest_month = d.loc[valid, "_cmp_dt"].max().strftime("%Y-%m")
    d["_cmp_month"] = d["_cmp_dt"].dt.strftime("%Y-%m")
    dm = d[d["_cmp_month"].eq(latest_month)].copy()
    if groups is None or groups.empty or dm.empty:
        return pd.DataFrame(), dm.reset_index(drop=True), latest_month
    join_key = next((key for key in ["group_key", "group_id"] if key in groups.columns and key in dm.columns), None)
    if join_key is None:
        return pd.DataFrame(), dm.reset_index(drop=True), latest_month
    counts = dm.groupby(join_key, dropna=False).size().reset_index(name="_month_usage")
    gm = groups.copy()
    gm[join_key] = gm[join_key].astype(str)
    counts[join_key] = counts[join_key].astype(str)
    gm = gm.merge(counts, on=join_key, how="inner")
    gm["usage_count"] = pd.to_numeric(gm["_month_usage"], errors="coerce").fillna(0)
    gm["month"] = latest_month
    return gm.reset_index(drop=True), dm.reset_index(drop=True), latest_month


def transport_similarity_summary_for_scope(details: pd.DataFrame) -> pd.DataFrame:
    """Return only the three business statuses used in the Reused report.

    Display order is fixed: TIDAK ADA TRANSPORT -> SAMA -> BERBEDA.
    SAMA/BERBEDA are determined with the 83% transaction-similarity rule when
    a numeric score is available; an existing valid SAMA/BERBEDA category is
    used as fallback. Rows with transport but no usable similarity result are
    intentionally omitted from this three-status chart rather than exposed as
    a fourth "score unavailable" category.
    """
    columns = ["label", "total_usage", "_order"]
    if details is None or details.empty:
        return pd.DataFrame(columns=columns)

    td = details.copy()
    transport_key = td.apply(
        lambda row: _first_nonblank(row, ["foto_kt_before", "foto_dt", "foto_ft"], ""),
        axis=1,
    )
    transport_text = transport_key.fillna("").astype(str).str.strip()
    has_transport = transport_text.ne("") & ~transport_text.str.upper().str.contains(
        r"TIDAK ADA|NO IMAGE|NO FOTO|NO PHOTO|MISSING|EMPTY|KOSONG",
        regex=True,
        na=False,
    )

    raw_score = pd.to_numeric(
        td.get("transport_similarity_score", pd.Series([pd.NA] * len(td), index=td.index)),
        errors="coerce",
    )
    raw_category = td.get(
        "transport_similarity_category",
        pd.Series([""] * len(td), index=td.index),
    ).fillna("").astype(str).str.strip().str.upper()

    label = pd.Series("", index=td.index, dtype="object")
    label.loc[~has_transport] = "TIDAK ADA TRANSPORT"

    scored = has_transport & raw_score.notna()
    label.loc[scored & raw_score.ge(83.0)] = "SAMA"
    label.loc[scored & raw_score.lt(83.0)] = "BERBEDA"

    unresolved = has_transport & label.eq("")
    category_same = raw_category.str.contains(r"SAMA|SIMILAR", regex=True, na=False)
    category_diff = raw_category.str.contains(r"BERBEDA|DIFFERENT", regex=True, na=False)
    label.loc[unresolved & category_same] = "SAMA"
    label.loc[unresolved & category_diff] = "BERBEDA"

    # Business view intentionally contains only the three requested statuses.
    label = label[label.isin(["TIDAK ADA TRANSPORT", "SAMA", "BERBEDA"])]
    result = label.value_counts(dropna=False).rename_axis("label").reset_index(name="total_usage")
    order_map = {"TIDAK ADA TRANSPORT": 1, "SAMA": 2, "BERBEDA": 3}
    result["_order"] = result["label"].map(order_map).astype(int)
    # Ensure all three rows exist so All Time and Latest Month have identical ordering.
    base = pd.DataFrame({
        "label": ["TIDAK ADA TRANSPORT", "SAMA", "BERBEDA"],
        "_order": [1, 2, 3],
    })
    result = base.merge(result[["label", "total_usage"]], on="label", how="left")
    result["total_usage"] = pd.to_numeric(result["total_usage"], errors="coerce").fillna(0).astype(int)
    return result.sort_values("_order", ascending=True).reset_index(drop=True)

def resolve_report_period_label(args: Any, details: pd.DataFrame) -> str:
    explicit = safe_text(getattr(args, "period_label", ""), "")
    if explicit:
        return explicit
    selected = safe_text(getattr(args, "month", ""), "")
    if selected:
        # Multiple months may be supplied; use the latest selected month for the badge.
        months = sorted([x.strip() for x in selected.split(",") if re.match(r"^\d{4}-\d{2}$", x.strip())])
        if months:
            return month_label_id(months[-1])
        return selected
    latest = latest_transaction_month(details)
    return month_label_id(latest) if latest else "Monthly Report"


def detail_rows_for_latest_month(details: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if details is None or details.empty:
        return pd.DataFrame(), ""
    out = details.copy()
    out["_report_dt"] = _transaction_datetime_series(out)
    valid = out["_report_dt"].notna()
    if not valid.any():
        return pd.DataFrame(), ""
    latest_month = out.loc[valid, "_report_dt"].max().strftime("%Y-%m")
    out["_report_month"] = out["_report_dt"].dt.strftime("%Y-%m")
    out = out[out["_report_month"].eq(latest_month)].copy()
    return out, latest_month


def _group_meta_for_detail(groups: pd.DataFrame, row: pd.Series | dict) -> dict[str, Any]:
    if groups is None or groups.empty:
        return {}
    for key in ["group_key", "group_id"]:
        try:
            value = safe_text(row.get(key), "")
        except Exception:
            value = ""
        if not value or key not in groups.columns:
            continue
        matched = groups[groups[key].astype(str).eq(str(value))]
        if not matched.empty:
            return matched.iloc[0].to_dict()
    return {}


def build_latest_month_highlights(groups: pd.DataFrame, details: pd.DataFrame) -> tuple[list[dict[str, Any]], str]:
    """Return exactly the two audit-oriented highlights when possible.

    1) FREQUENT: group with the most detail usages in the latest transaction month.
    2) LATEST: newest image-bearing transaction in that same month, preferably a
       different group/evidence from FREQUENT.
    """
    latest, latest_month = detail_rows_for_latest_month(details)
    if latest.empty:
        return [], latest_month

    latest["_panen_key"] = latest.apply(
        lambda r: first_valid_evidence(
            r,
            ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"],
        ),
        axis=1,
    )
    latest = latest[latest["_panen_key"].map(is_valid_evidence_key)].copy()
    if latest.empty:
        return [], latest_month

    group_col = "group_key" if "group_key" in latest.columns else ("group_id" if "group_id" in latest.columns else None)
    if group_col:
        counts = latest.groupby(group_col, dropna=False).size().rename("_month_usage")
        latest = latest.join(counts, on=group_col)
    else:
        latest["_month_usage"] = 1

    latest = latest.sort_values(["_month_usage", "_report_dt"], ascending=[False, False])
    frequent = latest.iloc[0]

    newest_pool = latest.sort_values("_report_dt", ascending=False)
    frequent_group = safe_text(frequent.get(group_col), "") if group_col else ""
    frequent_key = evidence_key_clean(frequent.get("_panen_key", ""))
    latest_pick = None
    for _, rr in newest_pool.iterrows():
        rr_group = safe_text(rr.get(group_col), "") if group_col else ""
        rr_key = evidence_key_clean(rr.get("_panen_key", ""))
        if rr_key and (rr_key.lower() != frequent_key.lower()) and (not frequent_group or rr_group != frequent_group):
            latest_pick = rr
            break
    if latest_pick is None:
        for _, rr in newest_pool.iterrows():
            rr_key = evidence_key_clean(rr.get("_panen_key", ""))
            if rr_key and rr_key.lower() != frequent_key.lower():
                latest_pick = rr
                break
    if latest_pick is None:
        latest_pick = newest_pool.iloc[0]

    output: list[dict[str, Any]] = []
    for label, row in [("SERING MUNCUL", frequent), ("TRANSAKSI TERBARU", latest_pick)]:
        meta = _group_meta_for_detail(groups, row)
        item = row.to_dict()
        item["_highlight_label"] = label
        item["_group_meta"] = meta
        item["_month_usage"] = int(row.get("_month_usage", 1) or 1)
        output.append(item)
    return output, latest_month


def draw_latest_highlight_card(c: canvas.Canvas, item: dict[str, Any], x: float, y: float, w: float, h: float, accent) -> None:
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    meta = item.get("_group_meta") or {}
    label = safe_text(item.get("_highlight_label"), "HIGHLIGHT")
    group_label = _first_nonblank(item, ["group_id", "group_key"], _first_nonblank(meta, ["group_id", "group_key"], "-"))
    c.setFillColor(accent)
    c.roundRect(x + 5 * mm, y + h - 13 * mm, 49 * mm, 7 * mm, 2.5 * mm, fill=1, stroke=0)
    draw_fit(c, label, x + 7 * mm, y + h - 10.6 * mm, 45 * mm, "Helvetica-Bold", 6.2, WHITE, align="center", min_size=5.0)
    draw_fit(c, f"Group {group_label}", x + 60 * mm, y + h - 10.5 * mm, w - 66 * mm, "Helvetica-Bold", 9.2, DARK, min_size=7.0)

    image_key = first_valid_evidence(item, ["_panen_key", "image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"])
    draw_image_fit(c, get_image_for_key(image_key, "#DCE2F1"), x + 5 * mm, y + 12 * mm, 68 * mm, h - 31 * mm)

    info_x = x + 79 * mm
    info_w = w - 84 * mm
    tanggal = _first_nonblank(item, ["tanggal", "period_date", "sql_date", "date"], "-")
    estate = _first_nonblank(item, ["estate", "estate_list"], _first_nonblank(meta, ["estate_primary", "estate_list"], "-"))
    afd = _first_nonblank(item, ["afdeling", "afdeling_list"], _first_nonblank(meta, ["afdeling_primary", "afdeling_list"], "-"))
    pemanen = _first_nonblank(item, ["pemanen", "nama_pemanen", "employee_name", "pemanen_list"], _first_nonblank(meta, ["pemanen_list"], "-"))
    kerani = _first_nonblank(item, ["kerani_panen", "nama_kerani_panen", "kerani_panen_list"], _first_nonblank(meta, ["kerani_panen_list"], "-"))
    usage = int(item.get("_month_usage", 1) or 1)
    category = _first_nonblank(meta, ["reuse_category"], "REUSED")
    lines = [
        f"Tanggal: {tanggal}",
        f"Estate/Afd: {estate} / {afd}",
        f"Pemanen: {pemanen}",
        f"Kerani Panen: {kerani}",
        f"Kategori: {short_category(category)}",
    ]
    if label == "SERING MUNCUL":
        lines.insert(1, f"Muncul di bulan terakhir: {fmt_int(usage)} transaksi")
    yy = y + h - 26 * mm
    for line in lines:
        draw_fit(c, line, info_x, yy, info_w, "Helvetica-Bold", 7.1, SLATE, min_size=5.7)
        yy -= 8.2 * mm

def examples_pages(
    c: canvas.Canvas,
    groups: pd.DataFrame,
    details: pd.DataFrame,
    system: str,
    company: str,
    period: str,
    examples: int,
    page_no: int,
) -> int:
    """Latest-month evidence page: frequent reused + newest reused transaction."""
    highlights, latest_month = build_latest_month_highlights(groups, details)
    if not highlights:
        return page_no

    draw_header(c, "Highlighted Evidence - Bulan Terakhir", period, page_no)
    x, y, w, h = MARGIN_X, PAGE_H - 88 * mm, PAGE_W - 2 * MARGIN_X, 28 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.45)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9.0)
    c.drawString(x + 6 * mm, y + h - 9 * mm, f"EVIDENCE TRANSAKSI TERKINI - {latest_month or '-'}")
    intro = (
        "Gambar pada halaman ini hanya berasal dari transaksi pada bulan terakhir yang tersedia. "
        "Dua highlight dipilih untuk kebutuhan audit: reused yang paling sering muncul pada bulan tersebut dan transaksi reused paling terbaru."
    )
    draw_text_lines(c, wrap_lines(c, intro, w - 12 * mm, "Helvetica-Bold", 7.1, max_lines=2), x + 6 * mm, y + h - 18 * mm, "Helvetica-Bold", 7.1, 8.0, SLATE)

    card_h = 78 * mm
    card_w = PAGE_W - 2 * MARGIN_X
    first_y = PAGE_H - 174 * mm
    accents = [NAVY, RED]
    for idx, item in enumerate(highlights[:2]):
        cy = first_y - idx * (card_h + 8 * mm)
        draw_latest_highlight_card(c, item, MARGIN_X, cy, card_w, card_h, accents[idx % len(accents)])

    c.showPage()
    return page_no + 1

def make_demo_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    detail_rows = []
    cats = ["Blok Berbeda", "TPH Sama", "Afdeling Berbeda", "Blok Sama / TPH Berbeda"]
    dates = ["Tanggal Sama", "Bulan Sama", "Bulan Berbeda"]
    demo_pairs = [
        ("HARMONY", "PWP", "BP2"),
        ("HARMONY", "DIN", "DIN1"),
        ("ELHP", "DAN", "PU3"),
        ("ELHP", "DWT", "ME2"),
    ]
    gid = 1
    for system, comp, estate_base in demo_pairs:
        for i in range(1, 18):
            estate = estate_base if i % 3 else (
                "BP3"
                if comp == "PWP"
                else (
                    "DIN2"
                    if comp == "DIN"
                    else ("PU1" if comp == "DAN" else "ME3")
                )
            )
            usage = max(2, 84 - i * 3 + (i % 5) * 5)
            month = f"2026-{(1 + i % 5):02d}"
            rows.append({
                "system": system, "source_system": system, "source_company": comp, "source_label": f"{system} / {comp}",
                "group_key": f"{system}_{comp}_{gid}", "group_id": str(gid), "representative_image": f"sample_{gid}.jpg", "image_key": "",
                "usage_count": usage, "month": month, "reuse_category": cats[i % len(cats)], "date_category": dates[i % len(dates)],
                "estate_list": estate, "estate_primary": estate, "afdeling_list": str(1 + i % 12), "afdeling_primary": str(1 + i % 12),
                "blok_list": f"B{i:02d}, B{i+1:02d}", "blok_primary": f"B{i:02d}", "tph_list": f"{10+i}", "tph_primary": f"{10+i}",
                "sql_date_list": f"{month}-01, {month}-08", "oph_list": f"2606{i:02d}.001.A0001.{i:03d}",
                "pemanen_list": "Sample Pemanen", "kerani_panen_list": "Sample Kerani", "mandor_list": "Sample Mandor",
                "similarity_primary": "Transport Similar" if system == "HARMONY" and i % 2 else "Tidak Ada Gambar"
            })
            # detail rows by usage, spread across months
            for u in range(min(int(usage), 12)):
                mm = 1 + ((i + u) % 5)
                detail_rows.append({
                    "group_key": f"{system}_{comp}_{gid}", "group_id": str(gid), "system": system, "source_company": comp,
                    "tanggal": f"2026-{mm:02d}-{1 + (u % 24):02d}", "oph": f"2606{i:02d}.001.A0001.{i:03d}-{u}",
                    "bunch_pay": 0,
                    "image_name": f"sample_panen_{gid}_{u}.jpg",
                    "foto_dt": f"sample_transport_{gid}_{u}.jpg" if system == "HARMONY" else "",
                    "pemanen": f"PEMANEN {(i % 9) + 1:02d}",
                    "kerani_panen": f"KERANI PANEN {i:02d}",
                    "mandor": f"MANDOR {(i % 7) + 1:02d}",
                    "kerani_dt": f"KERANI TRANSPORT {i:02d}",
                    "estate": estate,
                    "afdeling": str(1 + i % 12),
                    "similarity": f"{96 - (i % 8) * 0.45:.1f}%" if system == "HARMONY" else "",
                })
            gid += 1
    groups = pd.DataFrame(rows)
    details = pd.DataFrame(detail_rows)
    return groups, details


def prepare_data(system: str, company: str, args) -> tuple[pd.DataFrame, pd.DataFrame]:
    if args.demo:
        groups, details = make_demo_data()
        if system != "ALL":
            groups = groups[groups["system"].astype(str).str.upper().eq(system)]
            details = details[details["system"].astype(str).str.upper().eq(system)]
        if company != "ALL":
            groups = groups[groups["source_company"].astype(str).str.upper().eq(company)]
            details = details[details["source_company"].astype(str).str.upper().eq(company)]
    else:
        if load_groups is None or load_details is None:
            raise RuntimeError("queries.py/db.py tidak bisa dimuat. Gunakan --demo untuk test layout.")
        groups = load_groups(system, company)
        details = load_details(system, company)
    filters = {
        "estate": csv_arg(args.estate),
        "afdeling": csv_arg(args.afdeling),
        "block": csv_arg(args.block),
        "months": csv_arg(args.month),
        "reuse_category": csv_arg(args.reuse_category),
        "date_category": csv_arg(args.date_category),
        "search": args.search or "",
    }
    if apply_filters:
        groups = apply_filters(groups, filters)
    if detail_for_groups:
        details = detail_for_groups(details, groups)
    return groups, details


def final_access_page(c: canvas.Canvas, period: str, page_no: int, dashboard_url: str, contact_email: str) -> int:
    """Clean final page: dashboard link + elegant contact block, no overlapping text."""
    dashboard_url = safe_text(dashboard_url, DEFAULT_DASHBOARD_URL)
    contact_email = safe_text(contact_email, DEFAULT_CONTACT_EMAIL)
    draw_header(c, "Akses Dashboard", period, page_no)

    x = MARGIN_X
    w = PAGE_W - 2 * MARGIN_X

    # Main dashboard panel is intentionally lower than the page title
    # so the box border never touches the header line.
    y = 137 * mm
    h = 76 * mm
    rounded_rect(c, x, y, w, h, 7 * mm, fill=WHITE, stroke=BORDER, width=0.55)

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(PAGE_W / 2, y + h - 18 * mm, "Akses Dashboard Interaktif")

    c.setFillColor(REUSED_TEXT)
    c.setFont("Helvetica-Bold", 7.8)
    c.drawCentredString(PAGE_W / 2, y + h - 28 * mm, "AI POWERED GOLDEN EYE")

    intro = (
        "Akses dashboard interaktif untuk menelusuri temuan secara lebih mendalam, "
        "memilih filter, membuka detail group, dan melihat preview evidence langsung dari database."
    )
    draw_text_lines(
        c,
        wrap_lines(c, intro, w - 34 * mm, "Helvetica-Bold", 8.2, max_lines=3),
        x + 17 * mm,
        y + h - 43 * mm,
        "Helvetica-Bold",
        8.2,
        9.8,
        SLATE,
    )

    btn_w, btn_h = 66 * mm, 15 * mm
    btn_x = x + (w - btn_w) / 2
    btn_y = y + 13 * mm
    rounded_rect(c, btn_x, btn_y, btn_w, btn_h, 4.8 * mm, fill=NAVY, stroke=NAVY, width=0)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(btn_x + btn_w / 2, btn_y + 5.2 * mm, "Klik Link")
    c.linkURL(dashboard_url, (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h), relative=0)

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(PAGE_W / 2, btn_y - 5.1 * mm, "Tombol ini akan membuka dashboard pada browser.")

    # Contact panel with safe spacing from main panel and footer.
    cx, cy, cw, ch = MARGIN_X, 68 * mm, PAGE_W - 2 * MARGIN_X, 45 * mm
    rounded_rect(c, cx, cy, cw, ch, 7 * mm, fill=SOFT_BLUE, stroke=colors.HexColor("#C7D9F2"), width=0.55)

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(cx + 10 * mm, cy + ch - 13 * mm, "Kontak Presentasi & Walkthrough")

    contact_text = "Untuk kebutuhan presentasi, diskusi temuan, atau pendampingan membaca dashboard, silakan menghubungi:"
    draw_text_lines(
        c,
        wrap_lines(c, contact_text, cw - 20 * mm, "Helvetica-Bold", 7.8, max_lines=2),
        cx + 10 * mm,
        cy + ch - 25 * mm,
        "Helvetica-Bold",
        7.8,
        9.2,
        SLATE,
    )

    mail_w, mail_h = 112 * mm, 12 * mm
    mail_x, mail_y = cx + 10 * mm, cy + 7 * mm
    rounded_rect(c, mail_x, mail_y, mail_w, mail_h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.5)
    draw_fit(c, contact_email, mail_x + 5 * mm, mail_y + 4.3 * mm, mail_w - 10 * mm, "Helvetica-Bold", 8.8, RED, min_size=7.0)
    c.linkURL(f"mailto:{contact_email}", (mail_x, mail_y, mail_x + mail_w, mail_y + mail_h), relative=0)

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.4)
    c.drawString(mail_x + mail_w + 6 * mm, mail_y + 4.2 * mm, "Klik email untuk membuat pesan.")

    close_y = 41 * mm
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawCentredString(PAGE_W / 2, close_y + 10 * mm, "Terima kasih")
    c.setFillColor(SLATE)
    c.setFont("Helvetica-Bold", 7.2)
    c.drawCentredString(PAGE_W / 2, close_y + 3 * mm, "Report ini disiapkan sebagai bahan monitoring bulanan dan tindak lanjut audit berbasis evidence.")

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.6)
    c.drawCentredString(PAGE_W / 2, 25 * mm, "AI Powered Golden Eye - Integritas, Akurat, Transparan")

    c.showPage()
    return page_no + 1


def generate_report(system: str, company: str, args, out_dir: Path) -> Path | None:
    groups, details = prepare_data(system, company, args)
    if groups.empty and not args.write_empty:
        print(f"SKIP {system}/{company}: data kosong")
        return None
    kp = make_kpi(groups, details)
    period = resolve_report_period_label(args, details)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / sanitize_filename(f"AI_Powered_Golden_Eye_Bulletin_{system}_{company}_{stamp}.pdf")
    c = canvas.Canvas(str(out_path), pagesize=A4, pageCompression=1)
    c.setPageCompression(1)
    c.setTitle(f"AI Powered Golden Eye Bulletin - {system} {company}")
    cache_dir = out_dir / "_chart_cache" / f"{system}_{company}_{stamp}"
    selected_months = csv_arg(args.month)
    cover_page(c, groups, kp, system, company, period)
    summary_page(
        c, groups, kp, system, company, period, cache_dir, 2, selected_months
    )
    page_no = 3
    page_no = company_personnel_page(
        c,
        details,
        system,
        company,
        period,
        cache_dir,
        page_no,
        top_n=args.top_personnel,
        groups=groups,
    )
    page_no = monthly_pattern_page(
        c,
        groups,
        details,
        kp,
        system,
        company,
        period,
        cache_dir,
        page_no,
        selected_months,
    )
    page_no = company_top_reused_month_page(c, groups, details, kp, system, company, period, cache_dir, page_no, 1, args.top_month)
    page_no = similarity_transport_page(c, groups, details, system, company, period, cache_dir, page_no)
    page_no = transport_reuse_single_group_page(c, groups, details, system, company, period, page_no)
    page_no = top_groups_pages(c, groups, system, company, period, args.top, page_no)
    if not args.no_images:
        page_no = top_group_examples_page(c, groups, details, system, company, period, page_no, top_n=4)
        page_no = latest_month_gallery_page(c, groups, details, system, company, period, page_no, top_n=4)
    if not args.no_final_page:
        page_no = final_access_page(c, period, page_no, args.dashboard_url, args.contact_email)
    c.save()
    compress_existing_pdf(out_path)
    print(f"OK {system}/{company}: {out_path}")
    return out_path


def generate_combined_report(args, out_dir: Path) -> Path | None:
    groups, details, pairs = prepare_combined_data(args)
    if groups.empty and not args.write_empty:
        print("SKIP COMBINED: data kosong")
        return None
    kp = make_kpi(groups, details)
    period = resolve_report_period_label(args, details)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / sanitize_filename(f"AI_Powered_Golden_Eye_Bulletin_ALL_COMPANY_{stamp}.pdf")
    c = canvas.Canvas(str(out_path), pagesize=A4, pageCompression=1)
    c.setPageCompression(1)
    c.setTitle("AI Powered Golden Eye Bulletin - One Company One Section")
    cache_dir = out_dir / "_chart_cache" / f"ALL_COMPANY_{stamp}"
    selected_months = csv_arg(args.month)
    cover_page(
        c,
        groups,
        kp,
        "HARMONY + ELHP",
        "PWP + DIN + DAN + DWT",
        period,
        show_scope_badges=False,
    )
    page_no = 2
    page_no = overall_story_page(
        c, groups, details, kp, pairs, period, page_no, selected_months
    )

    section_no = 1
    for s, comp in pairs:
        g, d = filter_pair_frames(groups, details, s, comp)
        if g.empty and not args.write_empty:
            continue
        k = make_kpi(g, d)
        page_no = company_story_page(
            c,
            g,
            d,
            k,
            s,
            comp,
            period,
            cache_dir,
            page_no,
            section_no,
            selected_months,
        )
        page_no = company_personnel_page(
            c,
            d,
            s,
            comp,
            period,
            cache_dir,
            page_no,
            section_no,
            args.top_personnel,
            groups=g,
        )
        page_no = company_top_reused_month_page(c, g, d, k, s, comp, period, cache_dir, page_no, section_no, args.top_month)
        page_no = company_insight_page(c, g, d, k, s, comp, period, cache_dir, page_no, section_no)
        if str(s).upper() == "HARMONY":
            page_no = similarity_transport_page(c, g, d, s, comp, period, cache_dir, page_no)
            page_no = transport_reuse_single_group_page(c, g, d, s, comp, period, page_no)
        page_no = top_groups_pages(c, g, s, comp, period, args.top, page_no)
        if not args.no_images:
            page_no = top_group_examples_page(c, g, d, s, comp, period, page_no, top_n=4)
            page_no = latest_month_gallery_page(c, g, d, s, comp, period, page_no, top_n=4)
        section_no += 1

    if not args.no_final_page:
        page_no = final_access_page(c, period, page_no, args.dashboard_url, args.contact_email)
    c.save()
    compress_existing_pdf(out_path)
    print(f"OK COMBINED: {out_path}")
    return out_path


# ======================================================================================
# V17 - Harmony Transport Reuse examples
# - latest transaction month only
# - reused-level similarity only (default >= 93%)
# - Panen and Transport evidence must both be valid
# ======================================================================================
INVALID_EVIDENCE_TERMS = [
    "", "-", "nan", "none", "null", "nat",
    "tidak ada", "tidak_ada", "no image", "no_image",
    "tidak ada foto", "tidak ada gambar", "tidak ada foto transport",
    "missing", "empty"
]


def evidence_key_clean(val: Any) -> str:
    s = safe_text(val, "").strip()
    if not s:
        return ""
    # Remove accidental list separator noise but keep filename intact.
    s = s.replace("\\", "/").split(";")[0].split(",")[0].strip()
    return s


def is_valid_evidence_key(val: Any) -> bool:
    s = evidence_key_clean(val)
    low = s.lower().strip()
    if not low:
        return False
    if low in INVALID_EVIDENCE_TERMS:
        return False
    if any(term in low for term in ["tidak ada", "no image", "no_image", "missing", "null", "nan"]):
        return False
    # require something that looks like a filename/key, but do not over-restrict DB keys
    return True


def first_valid_evidence(row: pd.Series | dict, names: list[str]) -> str:
    for name in names:
        try:
            raw = row.get(name, "")
        except Exception:
            raw = ""
        # value may be list-like string; test first few tokens
        values = split_values(raw) if isinstance(raw, str) and ("," in raw or ";" in raw or "|" in raw) else [raw]
        for v in values:
            s = evidence_key_clean(v)
            if is_valid_evidence_key(s):
                return s
    return ""


def has_image_reader_for_key(key: str, demo_color: str = "#E5E7EB") -> bool:
    """When DB image function is available, verify key resolves to image bytes.
    In demo mode / no DB, a syntactically valid key is enough.
    """
    if not is_valid_evidence_key(key):
        return False
    if not fetch_image_bytes:
        return True
    try:
        res = fetch_image_bytes(key)
        if isinstance(res, tuple):
            b = res[0]
        elif isinstance(res, dict):
            b = res.get("image_bytes")
        else:
            b = res
        return image_reader_from_bytes(b) is not None
    except Exception:
        # keep the row rather than failing report generation; the placeholder will still be safe
        return True


def build_similarity_candidates(groups: pd.DataFrame, details: pd.DataFrame, limit_check: int = 160, reused_only: bool = True) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if details is None or details.empty:
        return []
    d, latest_month = detail_rows_for_latest_month(details)
    if d.empty:
        return []
    d["_panen_key"] = d.apply(lambda r: first_valid_evidence(r, ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"]), axis=1)
    d["_transport_key"] = d.apply(lambda r: first_valid_evidence(r, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image", "transport_file"]), axis=1)
    d = d[d["_panen_key"].map(is_valid_evidence_key) & d["_transport_key"].map(is_valid_evidence_key)].copy()
    if d.empty:
        return []
    # Drop rows where both keys point to same evidence file/key.
    d = d[d["_panen_key"].astype(str).str.lower().ne(d["_transport_key"].astype(str).str.lower())].copy()
    if d.empty:
        return []

    # Add score and group usage for sorting.
    scores = []
    for _, rr in d.iterrows():
        val, _ = _numeric_similarity_value(rr)
        scores.append(val if val is not None else -1)
    d["_sim_sort"] = scores
    if reused_only:
        category_cols = [c for c in ["transport_similarity_category", "similarity", "similarity_category"] if c in d.columns]
        reused_mask = pd.Series(False, index=d.index, dtype=bool)
        for col in category_cols:
            reused_mask |= d[col].fillna("").astype(str).str.upper().str.contains("REUSED", regex=False)
        threshold = float(os.getenv("TRANSPORT_REUSED_THRESHOLD", "93.0"))
        reused_mask |= pd.to_numeric(d["_sim_sort"], errors="coerce").fillna(-1).ge(threshold)
        d = d[reused_mask].copy()
        if d.empty:
            return []
    if groups is not None and not groups.empty:
        use_map = {}
        if "group_key" in groups.columns:
            use_map.update(dict(zip(groups["group_key"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        if "group_id" in groups.columns:
            use_map.update(dict(zip(groups["group_id"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        def _usage_for(r):
            return use_map.get(str(r.get("group_key", "")), use_map.get(str(r.get("group_id", "")), 0))
        d["_group_usage"] = d.apply(_usage_for, axis=1)
    else:
        d["_group_usage"] = 0
    d = d.sort_values(["_report_dt", "_sim_sort", "_group_usage"], ascending=[False, False, False])

    selected = []
    seen_groups = set()
    seen_pairs = set()
    checked = 0
    for _, rr in d.iterrows():
        panen_key = rr.get("_panen_key", "")
        trans_key = rr.get("_transport_key", "")
        pair_key = (str(panen_key).lower(), str(trans_key).lower())
        if pair_key in seen_pairs:
            continue
        group_id = safe_text(rr.get("group_key"), safe_text(rr.get("group_id"), ""))
        if group_id and group_id in seen_groups:
            continue
        checked += 1
        if checked <= limit_check:
            if not has_image_reader_for_key(panen_key, "#DCE2F1") or not has_image_reader_for_key(trans_key, "#DCE2F1"):
                continue
        group_meta = {}
        if groups is not None and not groups.empty:
            if "group_key" in groups.columns and rr.get("group_key"):
                gm = groups[groups["group_key"].astype(str).eq(str(rr.get("group_key")))]
                if not gm.empty:
                    group_meta = gm.iloc[0].to_dict()
            if not group_meta and "group_id" in groups.columns and rr.get("group_id"):
                gm = groups[groups["group_id"].astype(str).eq(str(rr.get("group_id")))]
                if not gm.empty:
                    group_meta = gm.iloc[0].to_dict()
        selected.append((rr.to_dict(), group_meta))
        seen_pairs.add(pair_key)
        if group_id:
            seen_groups.add(group_id)
        if len(selected) >= 4:
            break
    return selected


def draw_small_photo_panel(c: canvas.Canvas, title: str, key: str, x: float, y: float, w: float, h: float, color, demo_color: str):
    rounded_rect(c, x, y, w, h, 3 * mm, fill=WHITE, stroke=BORDER, width=0.45)
    c.setFillColor(LABEL_BG)
    c.roundRect(x, y + h - 7 * mm, w, 7 * mm, 3 * mm, fill=1, stroke=0)
    draw_fit(c, title, x + 2 * mm, y + h - 5.1 * mm, w - 4 * mm, "Helvetica-Bold", 5.6, WHITE, align="center", min_size=4.8)
    reader = get_image_for_key(key, demo_color)
    draw_image_fit(c, reader, x + 2 * mm, y + 2 * mm, w - 4 * mm, h - 11 * mm)


def draw_similarity_pair_card(c: canvas.Canvas, row: dict[str, Any], meta: dict[str, Any], idx: int, x: float, y: float, w: float, h: float):
    rounded_rect(c, x, y, w, h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    group_label = _first_nonblank(row, ["group_id", "group_key"], _first_nonblank(meta, ["group_id", "group_key"], "-"))
    sim_val, sim_txt = _numeric_similarity_value(row)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.0)
    draw_fit(c, f"CONTOH {idx:02d} - GROUP {group_label}", x + 5 * mm, y + h - 8 * mm, w * 0.55, "Helvetica-Bold", 8.0, NAVY, min_size=6.2)
    # Score pill
    pill_w, pill_h = 42 * mm, 8 * mm
    pill_x, pill_y = x + w - pill_w - 5 * mm, y + h - 11 * mm
    rounded_rect(c, pill_x, pill_y, pill_w, pill_h, 3 * mm, fill=SOFT_BLUE_2 if sim_val is not None else SOFT_GRAY, stroke=BORDER, width=0.35)
    draw_fit(c, f"Similarity: {sim_txt}", pill_x + 3 * mm, pill_y + 2.3 * mm, pill_w - 6 * mm, "Helvetica-Bold", 6.0, BLUE if sim_val is not None else SLATE, align="center", min_size=5.0)

    panen_key = first_valid_evidence(row, ["_panen_key", "image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"])
    trans_key = first_valid_evidence(row, ["_transport_key", "foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image", "transport_file"])
    gap = 4 * mm
    photo_y = y + 25 * mm
    photo_h = h - 39 * mm
    photo_w = (w - 12 * mm - gap) / 2
    draw_small_photo_panel(c, "Foto Panen / Kerani Panen", panen_key, x + 5 * mm, photo_y, photo_w, photo_h, NAVY, "#DCE2F1")
    draw_small_photo_panel(c, "Foto Transport / Kerani Transport", trans_key, x + 7 * mm + photo_w + gap, photo_y, photo_w, photo_h, RED, "#DCE2F1")

    # Details, kept short to avoid overlap.
    estate = _first_nonblank(row, ["estate", "estate_list"], _first_nonblank(meta, ["estate_primary", "estate_list"], "-"))
    afd = _first_nonblank(row, ["afdeling", "afdeling_list"], _first_nonblank(meta, ["afdeling_primary", "afdeling_list"], "-"))
    tanggal = _first_nonblank(row, ["tanggal", "sql_date_list"], _first_nonblank(meta, ["sql_date_list"], "-"))
    kerani_panen = _first_nonblank(row, ["kerani_panen", "nama_kerani_panen", "kerani_panen_list"], _first_nonblank(meta, ["kerani_panen_list"], "-"))
    kerani_transport = _first_nonblank(row, ["kerani_dt", "kerani_ft", "nama_kerani_transport_dt", "nama_kerani_transport_ft"], _first_nonblank(meta, ["kerani_dt_list", "kerani_ft_list"], "-"))
    c.setFillColor(colors.HexColor("#F8FAFC")); c.roundRect(x + 5 * mm, y + 7 * mm, w - 10 * mm, 14 * mm, 3 * mm, fill=1, stroke=0)
    draw_fit(c, f"Estate {estate} | Afdeling {afd} | Tanggal {tanggal}", x + 8 * mm, y + 15.3 * mm, w - 16 * mm, "Helvetica-Bold", 6.2, SLATE, min_size=5.0)
    draw_fit(c, f"Kerani Panen: {kerani_panen}", x + 8 * mm, y + 10.5 * mm, (w - 19 * mm) / 2, "Helvetica-Bold", 5.8, SLATE, min_size=4.8)
    draw_fit(c, f"Kerani Transport: {kerani_transport}", x + 10 * mm + (w - 19 * mm) / 2, y + 10.5 * mm, (w - 19 * mm) / 2, "Helvetica-Bold", 5.8, SLATE, min_size=4.8)


def similarity_transport_example_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, page_no: int) -> int:
    if str(system).upper() != "HARMONY":
        return page_no
    examples = build_similarity_candidates(groups, details, reused_only=True)
    if not examples:
        # Do not create a page with "Tidak Ada Foto Transport". If no valid pair exists, skip cleanly.
        return page_no

    pages = [examples[:2]]
    for page_idx, pair_list in enumerate(pages, start=1):
        draw_header(c, "Transport Reuse - Panen vs Transport", period, page_no)
        intro_x, intro_y, intro_w, intro_h = MARGIN_X, PAGE_H - 85 * mm, PAGE_W - 2 * MARGIN_X, 25 * mm
        rounded_rect(c, intro_x, intro_y, intro_w, intro_h, 5 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.45)
        title = "KHUSUS HARMONY - CONTOH TRANSPORT REUSE BULAN TERBARU"
        if len(pages) > 1:
            title += f" ({page_idx}/{len(pages)})"
        c.setFillColor(RED); c.setFont("Helvetica-Bold", 8.2)
        c.drawString(intro_x + 6 * mm, intro_y + intro_h - 8 * mm, title)
        intro = (
            "Halaman ini hanya memakai transaksi bulan terbaru yang memiliki Foto Panen dan Foto Transport valid "
            "serta masuk kategori Transport Reuse (default similarity >= 93%). Evidence ditampilkan berdampingan untuk verifikasi."
        )
        draw_text_lines(c, wrap_lines(c, intro, intro_w - 12 * mm, "Helvetica-Bold", 7.1, max_lines=2), intro_x + 6 * mm, intro_y + intro_h - 17 * mm, "Helvetica-Bold", 7.1, 7.8, SLATE)

        card_h = 77 * mm
        card_w = PAGE_W - 2 * MARGIN_X
        start_y = PAGE_H - 168 * mm
        for local_idx, (row, meta) in enumerate(pair_list):
            global_idx = (page_idx - 1) * 2 + local_idx + 1
            y = start_y - local_idx * (card_h + 8 * mm)
            draw_similarity_pair_card(c, row, meta, global_idx, MARGIN_X, y, card_w, card_h)

        # Footer note, placed safely above bottom bar.
        note_y = 21 * mm
        rounded_rect(c, MARGIN_X, note_y, PAGE_W - 2 * MARGIN_X, 11 * mm, 4 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.35)
        draw_fit(c, "Catatan: Hasil AI adalah indikator awal. Keputusan final tetap memerlukan verifikasi lapangan dan pemeriksaan konteks transaksi.", MARGIN_X + 5 * mm, note_y + 4 * mm, PAGE_W - 2 * MARGIN_X - 10 * mm, "Helvetica-Bold", 6.4, SLATE, min_size=5.3)
        c.showPage()
        page_no += 1
    return page_no



def _collect_unique_names(frame: pd.DataFrame, columns: list[str], fallback: str = "-") -> str:
    if frame is None or frame.empty:
        return fallback
    names: list[str] = []
    seen: set[str] = set()
    for col in columns:
        if col not in frame.columns:
            continue
        for value in frame[col].fillna("").astype(str):
            for piece in str(value).replace(";", ",").split(","):
                name = safe_text(piece, "").strip()
                if not name or name == "-":
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(name)
                if len(names) >= 3:
                    return ", ".join(names)
    return ", ".join(names) if names else fallback


def _pick_readable_group_evidence(row: dict[str, Any], group_rows: pd.DataFrame | None = None) -> str:
    candidates: list[str] = []
    candidates.append(first_valid_evidence(row, ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"]))
    if group_rows is not None and not group_rows.empty:
        rr = group_rows.copy()
        rr["_report_dt"] = _transaction_datetime_series(rr)
        rr = rr.sort_values("_report_dt", ascending=False)
        for _, detail_row in rr.iterrows():
            candidates.append(first_valid_evidence(detail_row, ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"]))
    for key in candidates:
        cleaned = evidence_key_clean(key)
        if cleaned:
            return cleaned
    return ""


def build_top_group_examples(groups: pd.DataFrame, details: pd.DataFrame, top_n: int = 4) -> list[dict[str, Any]]:
    if groups is None or groups.empty:
        return []
    top = groups.copy().sort_values("usage_count", ascending=False).head(top_n)
    result: list[dict[str, Any]] = []
    detail_frame = details.copy() if details is not None and not details.empty else pd.DataFrame()
    if not detail_frame.empty:
        detail_frame["_report_dt"] = _transaction_datetime_series(detail_frame)
    for _, row in top.iterrows():
        item = row.to_dict()
        group_rows = pd.DataFrame()
        if not detail_frame.empty:
            if "group_key" in detail_frame.columns and item.get("group_key") is not None:
                group_rows = detail_frame[detail_frame["group_key"].astype(str).eq(str(item.get("group_key")))].copy()
            elif "group_id" in detail_frame.columns and item.get("group_id") is not None:
                group_rows = detail_frame[detail_frame["group_id"].astype(str).eq(str(item.get("group_id")))].copy()
        item["_image_key"] = _pick_readable_group_evidence(item, group_rows)
        item["_pelaku_pemanen"] = _collect_unique_names(group_rows, ["pemanen", "nama_pemanen", "employee_name"], safe_text(item.get("pemanen_list"), "-"))
        item["_pelaku_kerani"] = _collect_unique_names(group_rows, ["kerani_panen", "nama_kerani_panen"], safe_text(item.get("kerani_panen_list"), "-"))
        item["_pelaku_mandor"] = _collect_unique_names(group_rows, ["mandor", "nama_mandor"], safe_text(item.get("mandor_list"), "-"))
        item["_example_kind"] = "TOP_GROUP"
        item["_caption_title"] = f"Group {safe_text(item.get('group_id'), safe_text(item.get('group_key'), '-'))} | {fmt_int(item.get('usage_count'))} usage"
        result.append(item)
    return result


def build_latest_month_gallery(groups: pd.DataFrame, details: pd.DataFrame, top_n: int = 4) -> tuple[list[dict[str, Any]], str]:
    latest, latest_month = detail_rows_for_latest_month(details)
    if latest.empty:
        return [], latest_month
    latest = latest.copy()
    latest["_panen_key"] = latest.apply(
        lambda r: first_valid_evidence(
            r,
            ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"],
        ),
        axis=1,
    )
    latest = latest[latest["_panen_key"].map(is_valid_evidence_key)].copy()
    if latest.empty:
        return [], latest_month
    if groups is not None and not groups.empty:
        use_map = {}
        if "group_key" in groups.columns:
            use_map.update(dict(zip(groups["group_key"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        if "group_id" in groups.columns:
            use_map.update(dict(zip(groups["group_id"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        latest["_group_usage"] = latest.apply(lambda r: use_map.get(str(r.get("group_key", "")), use_map.get(str(r.get("group_id", "")), 0)), axis=1)
    else:
        latest["_group_usage"] = 0
    latest = latest.sort_values(["_report_dt", "_group_usage"], ascending=[False, False])
    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_groups: set[str] = set()
    for _, row in latest.iterrows():
        key = evidence_key_clean(row.get("_panen_key", ""))
        if not key or key.lower() in seen_keys:
            continue
        group_name = safe_text(row.get("group_key"), safe_text(row.get("group_id"), ""))
        # allow a second image from same group only if still lack images
        if group_name and group_name in seen_groups and len(selected) < min(2, top_n):
            continue
        meta = _group_meta_for_detail(groups, row)
        item = row.to_dict()
        item["_group_meta"] = meta
        item["_image_key"] = key
        item["_pelaku_pemanen"] = _collect_unique_names(pd.DataFrame([row]), ["pemanen", "nama_pemanen", "employee_name"], _first_nonblank(meta, ["pemanen_list"], "-"))
        item["_pelaku_kerani"] = _collect_unique_names(pd.DataFrame([row]), ["kerani_panen", "nama_kerani_panen"], _first_nonblank(meta, ["kerani_panen_list"], "-"))
        item["_pelaku_mandor"] = _collect_unique_names(pd.DataFrame([row]), ["mandor", "nama_mandor"], _first_nonblank(meta, ["mandor_list"], "-"))
        item["_example_kind"] = "LATEST_MONTH"
        item["_caption_title"] = f"{safe_text(item.get('tanggal'), latest_month)} | Group {safe_text(item.get('group_id'), safe_text(item.get('group_key'), '-'))}"
        selected.append(item)
        seen_keys.add(key.lower())
        if group_name:
            seen_groups.add(group_name)
        if len(selected) >= top_n:
            break
    return selected, latest_month


def draw_gallery_example_card(c: canvas.Canvas, item: dict[str, Any], x: float, y: float, w: float, h: float, accent, badge_text: str) -> None:
    rounded_rect(c, x, y, w, h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(LABEL_BG)
    c.roundRect(x + 4 * mm, y + h - 11 * mm, 42 * mm, 6.5 * mm, 2.3 * mm, fill=1, stroke=0)
    draw_fit(c, badge_text, x + 5.5 * mm, y + h - 8.7 * mm, 39 * mm, "Helvetica-Bold", 5.8, WHITE, align="center", min_size=4.8)
    draw_fit(c, safe_text(item.get("_caption_title"), "-"), x + 49 * mm, y + h - 9.2 * mm, w - 54 * mm, "Helvetica-Bold", 7.4, DARK, min_size=6.0)

    image_key = evidence_key_clean(item.get("_image_key", "")) or first_valid_evidence(item, ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"])
    draw_image_fit(c, get_image_for_key(image_key, "#DCE2F1"), x + 4 * mm, y + 27 * mm, w - 8 * mm, h - 43 * mm)

    info_y = y + 5.5 * mm
    info_h = 18 * mm
    c.setFillColor(colors.HexColor("#F8FAFC"))
    c.roundRect(x + 4 * mm, info_y, w - 8 * mm, info_h, 2.5 * mm, fill=1, stroke=0)
    meta = item.get("_group_meta") or item
    estate = _first_nonblank(item, ["estate", "estate_list"], _first_nonblank(meta, ["estate_primary", "estate_list"], "-"))
    afd = _first_nonblank(item, ["afdeling", "afdeling_list"], _first_nonblank(meta, ["afdeling_primary", "afdeling_list"], "-"))
    c.setFillColor(SLATE)
    draw_fit(c, f"Estate/Afd: {estate} / {afd}", x + 6 * mm, info_y + 12.0 * mm, w - 12 * mm, "Helvetica-Bold", 5.8, SLATE, min_size=4.8)
    draw_fit(c, f"Pemanen: {safe_text(item.get('_pelaku_pemanen'), '-')}", x + 6 * mm, info_y + 7.5 * mm, w - 12 * mm, "Helvetica-Bold", 5.6, SLATE, min_size=4.8)
    draw_fit(c, f"Kerani: {safe_text(item.get('_pelaku_kerani'), '-')}", x + 6 * mm, info_y + 4.0 * mm, (w - 14 * mm) / 2, "Helvetica-Bold", 5.4, SLATE, min_size=4.5)
    draw_fit(c, f"Mandor: {safe_text(item.get('_pelaku_mandor'), '-')}", x + 7 * mm + (w - 14 * mm) / 2, info_y + 4.0 * mm, (w - 14 * mm) / 2, "Helvetica-Bold", 5.4, SLATE, min_size=4.5)


def top_group_examples_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, page_no: int, top_n: int = 4) -> int:
    items = build_top_group_examples(groups, details, top_n=top_n)
    if not items:
        return page_no
    draw_header(c, "Top Group Reused - All Time", period, page_no)
    x, y, w, h = MARGIN_X, PAGE_H - 85 * mm, PAGE_W - 2 * MARGIN_X, 24 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.45)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.8)
    c.drawString(x + 6 * mm, y + h - 8.5 * mm, "TOP GROUP 4 GAMBAR - ALL TIME")
    intro = (
        "Halaman ini menampilkan 4 group reused dengan usage tertinggi sepanjang histori yang sedang dipilih. "
        "Setiap kartu memuat gambar utama dan keterangan pelaku utama: Pemanen, Kerani Panen, dan Mandor."
    )
    draw_text_lines(c, wrap_lines(c, intro, w - 12 * mm, "Helvetica-Bold", 7.0, max_lines=2), x + 6 * mm, y + h - 17 * mm, "Helvetica-Bold", 7.0, 7.8, SLATE)
    gap_x = 6 * mm
    gap_y = 7 * mm
    card_w = (PAGE_W - 2 * MARGIN_X - gap_x) / 2
    card_h = 78 * mm
    start_y = PAGE_H - 170 * mm
    for idx, item in enumerate(items[:4]):
        row = idx // 2
        col = idx % 2
        cx = MARGIN_X + col * (card_w + gap_x)
        cy = start_y - row * (card_h + gap_y)
        draw_gallery_example_card(c, item, cx, cy, card_w, card_h, NAVY if idx % 2 == 0 else RED, f"TOP {idx+1}")
    c.showPage()
    return page_no + 1


def latest_month_gallery_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, page_no: int, top_n: int = 4) -> int:
    items, latest_month = build_latest_month_gallery(groups, details, top_n=top_n)
    if not items:
        return page_no
    draw_header(c, "4 Gambar Terbaru - Bulan Terakhir", period, page_no)
    x, y, w, h = MARGIN_X, PAGE_H - 85 * mm, PAGE_W - 2 * MARGIN_X, 24 * mm
    rounded_rect(c, x, y, w, h, 5 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.45)
    c.setFillColor(REUSED_TEXT); c.setFont("Helvetica-Bold", 8.8)
    c.drawString(x + 6 * mm, y + h - 8.5 * mm, f"4 GAMBAR TERBARU - {latest_month or '-'}")
    intro = (
        "Gambar pada halaman ini hanya berasal dari bulan transaksi paling akhir yang tersedia. "
        "Urutannya dari transaksi paling baru, lalu dipilih maksimal 4 evidence berbeda agar rapi dan tidak berulang."
    )
    draw_text_lines(c, wrap_lines(c, intro, w - 12 * mm, "Helvetica-Bold", 7.0, max_lines=2), x + 6 * mm, y + h - 17 * mm, "Helvetica-Bold", 7.0, 7.8, SLATE)
    gap_x = 6 * mm
    gap_y = 7 * mm
    card_w = (PAGE_W - 2 * MARGIN_X - gap_x) / 2
    card_h = 78 * mm
    start_y = PAGE_H - 170 * mm
    for idx, item in enumerate(items[:4]):
        row = idx // 2
        col = idx % 2
        cx = MARGIN_X + col * (card_w + gap_x)
        cy = start_y - row * (card_h + gap_y)
        draw_gallery_example_card(c, item, cx, cy, card_w, card_h, RED if idx % 2 == 0 else NAVY, f"LATEST {idx+1}")
    c.showPage()
    return page_no + 1


def _prepare_transport_pair_rows(groups: pd.DataFrame, details: pd.DataFrame, *, latest_only: bool) -> tuple[pd.DataFrame, str]:
    if details is None or details.empty:
        return pd.DataFrame(), ""
    if latest_only:
        d, latest_month = detail_rows_for_latest_month(details)
    else:
        d = details.copy()
        d["_report_dt"] = _transaction_datetime_series(d)
        valid = d["_report_dt"].notna()
        latest_month = d.loc[valid, "_report_dt"].max().strftime("%Y-%m") if valid.any() else ""
    if d.empty:
        return pd.DataFrame(), latest_month
    if "_report_dt" not in d.columns:
        d["_report_dt"] = _transaction_datetime_series(d)
    d["_panen_key"] = d.apply(
        lambda r: first_valid_evidence(r, ["image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"]),
        axis=1,
    )
    d["_transport_key"] = d.apply(
        lambda r: first_valid_evidence(r, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image", "transport_file"]),
        axis=1,
    )
    d = d[d["_panen_key"].map(is_valid_evidence_key) & d["_transport_key"].map(is_valid_evidence_key)].copy()
    if d.empty:
        return d, latest_month
    d = d[d["_panen_key"].astype(str).str.lower().ne(d["_transport_key"].astype(str).str.lower())].copy()
    if d.empty:
        return d, latest_month

    # IMPORTANT REPORT RULE V115:
    # `details` has already been restricted to the reused groups visible in the
    # report through detail_for_groups(). Therefore a Panen row that belongs to
    # those reused groups and has a valid Transport evidence MUST remain eligible
    # for the Transport Reuse evidence page even when a transport-similarity
    # score/category has not been calculated yet.
    #
    # Similarity is still retained for ranking/display when available, but it is
    # no longer a gate that can hide valid DIN/PWP KT Before evidence.
    scores = []
    for _, rr in d.iterrows():
        val, _ = _numeric_similarity_value(rr)
        scores.append(val if val is not None else -1)
    d["_sim_sort"] = scores

    if groups is not None and not groups.empty:
        use_map: dict[str, float] = {}
        if "group_key" in groups.columns:
            use_map.update(dict(zip(groups["group_key"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        if "group_id" in groups.columns:
            use_map.update(dict(zip(groups["group_id"].astype(str), pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0))))
        d["_group_usage"] = d.apply(lambda r: use_map.get(str(r.get("group_key", "")), use_map.get(str(r.get("group_id", "")), 0)), axis=1)
    else:
        d["_group_usage"] = 0
    d["_group_identity"] = d.apply(lambda r: safe_text(r.get("group_key"), safe_text(r.get("group_id"), "")), axis=1)
    d = d[d["_group_identity"].astype(str).str.strip().ne("")].copy()
    return d, latest_month


def build_transport_group_four_evidence(groups: pd.DataFrame, details: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any], str, str, str]:
    """Build the mandatory transport-reuse report block.

    Preferred output:
    - 1 group reused with 2 Panen-Transport pairs (4 photos).

    Required fallback when such a group does not exist:
    - still show the best available reused rows that have valid transport evidence,
      preferably 2 pairs (4 photos) even if they come from different groups;
    - if only 1 valid pair exists, still render 1 pair (2 photos) instead of skipping.
    """
    source_scope = "LATEST_MONTH"
    display_month = ""

    def _dedup_pairs(frame: pd.DataFrame, within_group: bool) -> pd.DataFrame:
        if frame.empty:
            return frame
        out = frame.copy()
        out["_pair_key"] = out["_panen_key"].astype(str).str.lower() + "||" + out["_transport_key"].astype(str).str.lower()
        subset = ["_group_identity", "_pair_key"] if within_group else ["_pair_key"]
        return out.drop_duplicates(subset=subset).copy()

    def _pick_two_pairs(frame: pd.DataFrame, *, allow_mixed_groups: bool) -> list[dict[str, Any]]:
        if frame.empty:
            return []
        out = frame.copy().sort_values(["_report_dt", "_sim_sort", "_group_usage"], ascending=[False, False, False])
        selected: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        seen_panen: set[str] = set()
        seen_transport: set[str] = set()
        chosen_group = ""
        for _, rr in out.iterrows():
            panen = evidence_key_clean(rr.get("_panen_key", ""))
            transport = evidence_key_clean(rr.get("_transport_key", ""))
            group_id = safe_text(rr.get("_group_identity"), "")
            if not panen or not transport:
                continue
            pair = (panen.lower(), transport.lower())
            if pair in seen_pairs or panen.lower() in seen_panen:
                continue
            if chosen_group and not allow_mixed_groups and group_id != chosen_group:
                continue
            if transport.lower() in seen_transport and len(selected) < 2:
                continue
            selected.append(rr.to_dict())
            seen_pairs.add(pair)
            seen_panen.add(panen.lower())
            seen_transport.add(transport.lower())
            if not chosen_group:
                chosen_group = group_id
            if len(selected) >= 2:
                break
        if len(selected) < 2:
            for _, rr in out.iterrows():
                panen = evidence_key_clean(rr.get("_panen_key", ""))
                transport = evidence_key_clean(rr.get("_transport_key", ""))
                group_id = safe_text(rr.get("_group_identity"), "")
                if not panen or not transport:
                    continue
                pair = (panen.lower(), transport.lower())
                if pair in seen_pairs or panen.lower() in seen_panen:
                    continue
                if chosen_group and not allow_mixed_groups and group_id != chosen_group:
                    continue
                selected.append(rr.to_dict())
                seen_pairs.add(pair)
                seen_panen.add(panen.lower())
                if len(selected) >= 2:
                    break
        return selected

    # Preferred: one group with 2 valid pairs
    for latest_only in (True, False):
        d, latest_month = _prepare_transport_pair_rows(groups, details, latest_only=latest_only)
        if d.empty:
            continue
        display_month = latest_month
        d = _dedup_pairs(d, within_group=True)
        stats = (
            d.groupby("_group_identity", dropna=False)
            .agg(
                pair_count=("_pair_key", "nunique"),
                newest_dt=("_report_dt", "max"),
                max_similarity=("_sim_sort", "max"),
                group_usage=("_group_usage", "max"),
            )
            .reset_index()
        )
        stats = stats[stats["pair_count"].ge(2)].copy()
        if stats.empty:
            continue
        stats = stats.sort_values(["pair_count", "newest_dt", "max_similarity", "group_usage"], ascending=[False, False, False, False])
        chosen_group = str(stats.iloc[0]["_group_identity"])
        chosen = d[d["_group_identity"].astype(str).eq(chosen_group)].copy()
        selected = _pick_two_pairs(chosen, allow_mixed_groups=False)
        if selected:
            meta = _group_meta_for_detail(groups, selected[0])
            return selected[:2], meta, display_month, ("LATEST_MONTH" if latest_only else "ALL_TIME_FALLBACK"), "ONE_GROUP"

    # Required fallback: still show transport evidence, even if from mixed groups.
    for latest_only in (True, False):
        d, latest_month = _prepare_transport_pair_rows(groups, details, latest_only=latest_only)
        if d.empty:
            continue
        display_month = latest_month
        d = _dedup_pairs(d, within_group=False)
        selected = _pick_two_pairs(d, allow_mixed_groups=True)
        if selected:
            mode = "MIXED_GROUPS" if len(selected) >= 2 else "SINGLE_PAIR"
            meta = _group_meta_for_detail(groups, selected[0]) if selected else {}
            scope = "LATEST_MONTH_PAIR_FALLBACK" if latest_only else "ALL_TIME_PAIR_FALLBACK"
            return selected[:2], meta, display_month, scope, mode

    return [], {}, display_month, source_scope, "NONE"


def draw_transport_four_photo_panel(c: canvas.Canvas, title: str, key: str, x: float, y: float, w: float, h: float, color, demo_color: str, footer: str = "") -> None:
    rounded_rect(c, x, y, w, h, 3.0 * mm, fill=WHITE, stroke=BORDER, width=0.45)
    header_h = 6.5 * mm
    c.setFillColor(color)
    c.roundRect(x, y + h - header_h, w, header_h, 3.0 * mm, fill=1, stroke=0)
    draw_fit(c, title, x + 2 * mm, y + h - 4.9 * mm, w - 4 * mm, "Helvetica-Bold", 5.7, WHITE, align="center", min_size=4.9)
    if footer:
        img_y = y + 8.5 * mm
        img_h = h - 17.0 * mm
        draw_image_fit(c, get_image_for_key(key, demo_color), x + 2.0 * mm, img_y, w - 4.0 * mm, img_h)
        draw_fit(c, footer, x + 2.5 * mm, y + 2.7 * mm, w - 5 * mm, "Helvetica-Bold", 5.0, SLATE, align="center", min_size=4.3)
    else:
        draw_image_fit(c, get_image_for_key(key, demo_color), x + 2.0 * mm, y + 2.2 * mm, w - 4.0 * mm, h - 9.3 * mm)


def transport_reuse_single_group_page(c: canvas.Canvas, groups: pd.DataFrame, details: pd.DataFrame, system: str, company: str, period: str, page_no: int) -> int:
    company_u = str(company or "").upper()
    if str(system).upper() != "HARMONY" or company_u not in {"PWP", "DIN"}:
        return page_no
    pairs, group_meta, latest_month, source_scope, selection_mode = build_transport_group_four_evidence(groups, details)
    if not pairs:
        print(f"[REPORT TRANSPORT] {company_u}: tidak ditemukan pasangan Panen-Transport valid sama sekali; halaman dilewati.", flush=True)
        return page_no

    first = pairs[0]
    same_group = selection_mode == "ONE_GROUP"
    four_photos = len(pairs) >= 2
    if same_group:
        title = f"Transport Reuse {company_u} - 1 Group / 4 Foto"
        headline = f"GROUP {_first_nonblank(first, ['group_id', 'group_key'], _first_nonblank(group_meta, ['group_id', 'group_key'], '-'))} - 2 FOTO PANEN + 2 FOTO TRANSPORT"
        scope_note = "bulan terbaru" if source_scope == "LATEST_MONTH" else "histori terbaru yang memiliki dua pasangan lengkap"
        intro = (
            f"Khusus {company_u}, satu group Transport Reuse ditunjukkan dengan empat evidence dari group yang sama: "
            f"dua Foto Panen dan dua Foto Transport."
        )
    elif four_photos:
        title = f"Transport Reuse {company_u} - Evidence Tersedia"
        headline = "2 PASANG REUSED DENGAN FOTO TRANSPORT TERSEDIA"
        scope_note = "bulan terbaru" if source_scope == "LATEST_MONTH_PAIR_FALLBACK" else "histori terbaru dengan pasangan transport tersedia"
        intro = (
            f"Khusus {company_u}, belum ditemukan satu group yang mempunyai dua pasangan lengkap. "
            f"Agar evidence tetap tampil, report menampilkan dua pasangan reused terbaik yang memiliki Foto Panen dan Foto Transport valid."
        )
    else:
        title = f"Transport Reuse {company_u} - Evidence Tersedia"
        headline = "1 PASANG REUSED DENGAN FOTO TRANSPORT TERSEDIA"
        scope_note = "bulan terbaru" if source_scope == "LATEST_MONTH_PAIR_FALLBACK" else "histori terbaru dengan pasangan transport tersedia"
        intro = (
            f"Khusus {company_u}, belum ditemukan dua pasangan lengkap. "
            f"Namun satu pasangan reused yang memiliki Foto Panen dan Foto Transport valid tetap ditampilkan agar temuan transport tidak hilang."
        )

    draw_header(c, title, period, page_no)
    intro_x, intro_y, intro_w, intro_h = MARGIN_X, PAGE_H - 86 * mm, PAGE_W - 2 * MARGIN_X, 24 * mm
    rounded_rect(c, intro_x, intro_y, intro_w, intro_h, 5 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.45)
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 8.4)
    c.drawString(intro_x + 6 * mm, intro_y + intro_h - 8 * mm, headline)
    draw_text_lines(c, wrap_lines(c, intro, intro_w - 12 * mm, "Helvetica-Bold", 7.0, max_lines=2), intro_x + 6 * mm, intro_y + intro_h - 17 * mm, "Helvetica-Bold", 7.0, 7.8, SLATE)

    gap_x = 5 * mm
    gap_y = 6 * mm
    panel_w = (PAGE_W - 2 * MARGIN_X - gap_x) / 2
    panel_h = 62 * mm
    row1_y = PAGE_H - 157 * mm
    row2_y = row1_y - panel_h - gap_y
    layouts = []
    if len(pairs) >= 1:
        layouts.append((pairs[0], "PANEN #1", "TRANSPORT #1", row1_y))
    if len(pairs) >= 2:
        layouts.append((pairs[1], "PANEN #2", "TRANSPORT #2", row2_y))

    for pair_idx, (row, panen_title, transport_title, yy) in enumerate(layouts, start=1):
        panen_key = first_valid_evidence(row, ["_panen_key", "image_name", "representative_image", "image_key", "file_name", "output_file", "ffb_evidence_kp"])
        trans_key = first_valid_evidence(row, ["_transport_key", "foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image", "transport_file"])
        sim_val, sim_txt = _numeric_similarity_value(row)
        if sim_val is None:
            sim_txt = "N/A"
        tanggal = _first_nonblank(row, ["tanggal", "sql_date_list"], "-")
        group_text = _first_nonblank(row, ["group_id", "group_key"], "-")
        panen_footer = f"Group {group_text} | Tanggal {tanggal} | Similarity {sim_txt}"
        transport_footer = f"Group {group_text} | Pair {pair_idx} | Similarity {sim_txt}"
        draw_transport_four_photo_panel(c, panen_title, panen_key, MARGIN_X, yy, panel_w, panel_h, NAVY, "#DCE2F1", panen_footer)
        draw_transport_four_photo_panel(c, transport_title, trans_key, MARGIN_X + panel_w + gap_x, yy, panel_w, panel_h, RED, "#DCE2F1", transport_footer)

    if len(pairs) == 1:
        rounded_rect(c, MARGIN_X, row2_y, PAGE_W - 2 * MARGIN_X, panel_h, 4 * mm, fill=colors.HexColor("#F8FAFC"), stroke=BORDER, width=0.4)
        draw_fit(c, "Pasangan kedua belum tersedia. Report tetap menampilkan pasangan reused yang mempunyai Foto Transport valid agar insight transport tidak hilang.", MARGIN_X + 8 * mm, row2_y + panel_h/2 - 2 * mm, PAGE_W - 2 * MARGIN_X - 16 * mm, "Helvetica-Bold", 7.2, SLATE, align="center", min_size=5.8)

    info_y = 25 * mm
    info_h = 28 * mm
    rounded_rect(c, MARGIN_X, info_y, PAGE_W - 2 * MARGIN_X, info_h, 4 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.4)
    estate = _first_nonblank(first, ["estate", "estate_list"], _first_nonblank(group_meta, ["estate_primary", "estate_list"], "-"))
    afd = _first_nonblank(first, ["afdeling", "afdeling_list"], _first_nonblank(group_meta, ["afdeling_primary", "afdeling_list"], "-"))
    pemanen = _collect_unique_names(pd.DataFrame(pairs), ["pemanen", "nama_pemanen", "employee_name"], _first_nonblank(group_meta, ["pemanen_list"], "-"))
    kerani_panen = _collect_unique_names(pd.DataFrame(pairs), ["kerani_panen", "nama_kerani_panen"], _first_nonblank(group_meta, ["kerani_panen_list"], "-"))
    kerani_transport = _collect_unique_names(pd.DataFrame(pairs), ["kerani_dt", "kerani_ft", "nama_kerani_transport_dt", "nama_kerani_transport_ft"], _first_nonblank(group_meta, ["kerani_dt_list", "kerani_ft_list"], "-"))
    group_summary = _first_nonblank(first, ["group_id", "group_key"], _first_nonblank(group_meta, ["group_id", "group_key"], "-")) if same_group else "Multiple groups"
    draw_fit(c, f"Company: {company_u} | Group: {group_summary} | Estate/Afd: {estate} / {afd} | Source: {scope_note}", MARGIN_X + 5 * mm, info_y + 19 * mm, PAGE_W - 2 * MARGIN_X - 10 * mm, "Helvetica-Bold", 6.3, NAVY, min_size=5.0)
    draw_fit(c, f"Pemanen: {pemanen}", MARGIN_X + 5 * mm, info_y + 13 * mm, PAGE_W - 2 * MARGIN_X - 10 * mm, "Helvetica-Bold", 5.9, SLATE, min_size=4.8)
    draw_fit(c, f"Kerani Panen: {kerani_panen}", MARGIN_X + 5 * mm, info_y + 7 * mm, (PAGE_W - 2 * MARGIN_X - 13 * mm) / 2, "Helvetica-Bold", 5.8, SLATE, min_size=4.7)
    draw_fit(c, f"Kerani Transport: {kerani_transport}", PAGE_W / 2 + 2 * mm, info_y + 7 * mm, (PAGE_W - 2 * MARGIN_X - 13 * mm) / 2, "Helvetica-Bold", 5.8, SLATE, min_size=4.7)

    c.showPage()
    photo_count = len(pairs) * 2
    similarity_available = sum(1 for row in pairs if _numeric_similarity_value(row)[0] is not None)
    print(
        f"[REPORT TRANSPORT] {company_u}: mode={selection_mode} pairs={len(pairs)} "
        f"photos={photo_count} similarity_available={similarity_available}/{len(pairs)} "
        f"scope={source_scope} month={latest_month or '-'}",
        flush=True,
    )
    return page_no + 1


def resolve_pairs(system: str, company: str) -> list[tuple[str, str]]:
    system = (system or "ALL").upper()
    company = (company or "ALL").upper()
    if system == "ALL" and company == "ALL":
        return SYSTEM_COMPANY_DEFAULTS
    if company == "ALL":
        return [(s, c) for s, c in SYSTEM_COMPANY_DEFAULTS if s == system]
    if system == "ALL":
        return [(s, c) for s, c in SYSTEM_COMPANY_DEFAULTS if c == company]
    return [(system, company)]



def compress_existing_pdf(input_pdf: Path, output_pdf: Path | None = None) -> Path:
    """Optional second-pass compression using pikepdf when installed.

    The main saving already comes from downsampling BYTEA photos before embedding.
    This step is safe to skip if pikepdf is not available.
    """
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf) if output_pdf else input_pdf
    try:
        import pikepdf
        tmp = output_pdf.with_suffix(".tmp_compressed.pdf")
        with pikepdf.Pdf.open(str(input_pdf)) as pdf:
            pdf.save(
                str(tmp),
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=False,
            )
        tmp.replace(output_pdf)
    except Exception:
        pass
    return output_pdf


def parse_args(argv: Iterable[str] = sys.argv[1:]) -> argparse.Namespace:
    p = argparse.ArgumentParser("Generate AI Powered Golden Eye monthly bulletin PDFs.")
    p.add_argument("--system", default="ALL", choices=["ALL", "HARMONY", "ELHP"])
    p.add_argument(
        "--company",
        default="ALL",
        choices=["ALL", "PWP", "DIN", "DAN", "DWT"],
    )
    p.add_argument("--estate", default="")
    p.add_argument("--afdeling", default="")
    p.add_argument("--block", default="")
    p.add_argument("--month", default="")
    p.add_argument("--reuse-category", default="")
    p.add_argument("--date-category", default="")
    p.add_argument("--search", default="")
    p.add_argument("--period-label", default="", help="Label periode. Jika kosong, otomatis memakai bulan transaksi terbaru.")
    p.add_argument("--top", type=int, default=12, help="Jumlah top reused group pada halaman prioritas review.")
    p.add_argument("--top-month", type=int, default=10, help="Jumlah top reused group pada chart Top Reused by Month.")
    p.add_argument(
        "--top-personnel",
        type=int,
        default=10,
        help="Jumlah Kerani Panen dan Mandor pada chart personel.",
    )
    p.add_argument("--examples", type=int, default=4, help="Compatibility option; report V18 menampilkan 4 Top Group All Time dan 4 Gambar Terbaru bulan terakhir.")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--write-empty", action="store_true")
    p.add_argument("--output-dir", default="")
    p.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL, help="URL dashboard yang disamarkan pada tombol 'Klik Link' di halaman terakhir.")
    p.add_argument("--contact-email", default=DEFAULT_CONTACT_EMAIL, help="Email kontak pada halaman terakhir.")
    p.add_argument("--no-final-page", action="store_true", help="Jangan tambahkan halaman akses dashboard di akhir report.")
    p.add_argument("--combined", action="store_true", help="Buat satu PDF gabungan untuk HARMONY/PWP, HARMONY/DIN, ELHP/DAN, ELHP/DWT.")
    p.add_argument("--separate", action="store_true", help="Untuk --system ALL --company ALL, tetap buat PDF terpisah per company.")
    return p.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> None:
    args = parse_args(argv)
    out_dir = Path(args.output_dir) if args.output_dir else REPORTS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Default ALL/ALL menjadi satu report gabungan untuk empat company.
    if (args.combined or (str(args.system).upper() == "ALL" and str(args.company).upper() == "ALL" and not args.separate)):
        generate_combined_report(args, out_dir)
    else:
        for system, company in resolve_pairs(args.system, args.company):
            generate_report(system, company, args, out_dir)
    storage_warnings = get_image_storage_warnings() if get_image_storage_warnings else {}
    if storage_warnings:
        print("PERINGATAN STORAGE GAMBAR (report tetap selesai):")
        for relation, message in sorted(storage_warnings.items()):
            print(f"  - {relation}: {message}")
    if UNAVAILABLE_IMAGE_KEYS:
        recovery_report = out_dir / "image_recovery_needed.csv"
        pd.DataFrame(
            {
                "image_key": sorted(UNAVAILABLE_IMAGE_KEYS),
                "status": "IMAGE_NOT_READABLE_OR_PENDING_RECOVERY",
            }
        ).to_csv(recovery_report, index=False, encoding="utf-8-sig")
        print(
            f"FILE GAMBAR PERLU RECOVERY: {recovery_report} "
            f"({len(UNAVAILABLE_IMAGE_KEYS):,} image key)"
        )
    print(f"Selesai. Folder output: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
