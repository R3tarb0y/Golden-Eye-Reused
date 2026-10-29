from __future__ import annotations

"""Golden Eye - Harmony Panen vs Transport Validation Report.

This is a SEPARATE report from the Reused Photo bulletin.
V166 visual standard: one blue accent, bold typography, and V165 layout: Monthly Trend is separated per PT (PWP and DIN), while Estate/Afdeling retain SAMA/BERBEDA/TIDAK ADA TRANSPORT + large evidence.
Data source follows the dedicated dashboard menu "Validasi Foto Kerani Panen vs Transport"
through queries.load_harmony_transaction_validation(). The unit is one OPH transaction,
not a reused group.
"""

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

try:
    from queries import (
        load_harmony_transaction_validation,
        load_harmony_oph_coverage,
        load_sap_kpis,
        HARMONY_TRANSACTION_SAME_THRESHOLD,
        clear_data_cache,
    )
except Exception:
    load_harmony_transaction_validation = None
    load_harmony_oph_coverage = None
    load_sap_kpis = None
    clear_data_cache = lambda: None
    HARMONY_TRANSACTION_SAME_THRESHOLD = float(os.getenv("HARMONY_TRANSACTION_SAME_THRESHOLD", "83"))

import generate_pdf_reports as common

PAGE_W, PAGE_H = A4
MARGIN_X = common.MARGIN_X
NAVY = common.NAVY
RED = common.RED
DARK = common.DARK
SLATE = common.SLATE
MUTED = common.MUTED
GRAY = common.GRAY
BORDER = common.BORDER
SOFT_BLUE = common.SOFT_BLUE
SOFT_BLUE_2 = common.SOFT_BLUE_2
SOFT_BLUE_3 = common.SOFT_BLUE_3
SOFT_RED = common.SOFT_RED
SOFT_GRAY = common.SOFT_GRAY
ACCENT_BLUE_2 = common.ACCENT_BLUE_2
ACCENT_BLUE_3 = common.ACCENT_BLUE_3
ACCENT_BLUE_4 = common.ACCENT_BLUE_4
LABEL_BG = common.LABEL_BG
WHITE = common.WHITE
REPORTS_DIR = Path(os.getenv("TRANSPORT_PDF_REPORT_DIR", str(common.REPORTS_DIR)))
UNAVAILABLE_IMAGE_KEYS = common.UNAVAILABLE_IMAGE_KEYS


def _month_label_id(ym: str | None, year: int | None = None) -> str:
    months = {1:"Januari",2:"Februari",3:"Maret",4:"April",5:"Mei",6:"Juni",7:"Juli",8:"Agustus",9:"September",10:"Oktober",11:"November",12:"Desember"}
    if ym:
        s = str(ym).strip()
        try:
            if len(s) >= 7 and s[4] == '-':
                y = int(s[:4]); m = int(s[5:7])
                return f"{months.get(m, s)} {y}"
        except Exception:
            pass
    if year:
        return str(year)
    return ""


DIN_TRANSPORT_START_MONTH = "2026-03"


def _source_range_label(rows: pd.DataFrame, year: int | None = None) -> str:
    if rows is None or rows.empty:
        return str(year or "")
    vals = rows.get("month", pd.Series(dtype=str)).dropna().astype(str)
    vals = vals[vals.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")].sort_values()
    if vals.empty:
        return str(year or "")
    first = vals.iloc[0]
    last = vals.iloc[-1]
    first_lbl = _month_label_id(first, year)
    last_lbl = _month_label_id(last, year)
    if first == last:
        return last_lbl
    first_name = first_lbl.split()[0]
    return f"{first_name} hingga {last_lbl}"


def apply_transport_scope_rules(rows: pd.DataFrame) -> pd.DataFrame:
    """Fair transport-comparison scope for DIN."""
    if rows is None or rows.empty:
        return rows.copy()
    out = rows.copy()
    company = out.get("company", pd.Series("", index=out.index)).astype(str).str.upper()
    month = out.get("month", pd.Series("", index=out.index)).astype(str)
    mask = ~((company.eq("DIN")) & (month < DIN_TRANSPORT_START_MONTH))
    return out.loc[mask].copy()



def draw_header(c: canvas.Canvas, title: str, period: str, page_no: int) -> None:
    """Header khusus report Panen vs Transport; tidak memakai label Reused Bulletin."""
    c.setFillColor(WHITE); c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    common.draw_decor(c)
    common.draw_logo(c, MARGIN_X, PAGE_H - 31 * mm, 38 * mm, 12 * mm)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 11.5)
    c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 22 * mm, "AI POWERED GOLDEN EYE")
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 6.6)
    c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 29 * mm, "PANEN VS TRANSPORT VALIDATION REPORT")
    badge_w = 28 * mm
    common.rounded_rect(c, PAGE_W - MARGIN_X - badge_w, PAGE_H - 44 * mm, badge_w, 9 * mm, 3 * mm, fill=NAVY, stroke=NAVY, width=0)
    common.draw_fit(c, str(period).upper(), PAGE_W - MARGIN_X - badge_w + 2 * mm, PAGE_H - 41.0 * mm, badge_w - 4 * mm, "Helvetica-Bold", 5.7, WHITE, align="center", min_size=4.6)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 18)
    c.drawString(MARGIN_X, PAGE_H - 59 * mm, str(title).upper())
    c.setStrokeColor(NAVY); c.setLineWidth(1.2)
    c.line(MARGIN_X, PAGE_H - 64 * mm, MARGIN_X + 13 * mm, PAGE_H - 64 * mm)
    c.setFillColor(MUTED); c.setFont("Helvetica-Bold", 5.8)
    c.drawRightString(PAGE_W - MARGIN_X, 10 * mm, f"{page_no:02d}")
    c.setFillColor(NAVY); c.rect(0, 0, PAGE_W * 0.32, 5 * mm, fill=1, stroke=0)
    c.setFillColor(ACCENT_BLUE_2); c.rect(PAGE_W * 0.32, 0, PAGE_W * 0.34, 5 * mm, fill=1, stroke=0)
    c.setFillColor(NAVY); c.rect(PAGE_W * 0.66, 0, PAGE_W * 0.34, 5 * mm, fill=1, stroke=0)

def _demo_rows() -> pd.DataFrame:
    rows = []
    for company, estate_prefix in [("PWP", "BUKIT PENDULANGAN"), ("DIN", "LONG KEJIAK")]:
        for i in range(1, 55):
            month = 1 + (i % 7)
            score = 70 + (i * 3.7) % 29
            has_transport = (company != "PWP") and (i % 6 != 0)
            has_panen = i % 17 != 0
            ready = has_panen and has_transport
            decision = "SAMA" if ready and score >= HARMONY_TRANSACTION_SAME_THRESHOLD else "BERBEDA" if ready else "TIDAK ADA FOTO TRANSPORT" if has_panen else "TIDAK ADA FOTO PANEN"
            rows.append({
                "company": company,
                "period_year": 2026,
                "month": f"2026-{month:02d}",
                "tanggal": pd.Timestamp(2026, month, 1 + i % 26),
                "oph": f"260{month:02d}{i:03d}.120.G{i:04d}.{400+i}",
                "oph_key": f"260{month:02d}{i:03d}120G{i:04d}{400+i}",
                "estate": f"{estate_prefix} {1 + i % 3}",
                "afdeling": str(1 + i % 10),
                "blok": f"E{10 + i % 45}",
                "tph": f"{100 + i % 60:03d}",
                "kerani_panen": ["ABDUL RAHMAT", "FERDINANDES GUNAWAN MADU", "SAMPLE KP"][i % 3],
                "mandor": ["NURUL KHAIR", "ASRIN SULEMAN GARE", "SAMPLE MANDOR"][i % 3],
                "kerani_transport": ["KERANI TRANSPORT A", "KERANI TRANSPORT B", "KERANI TRANSPORT C"][i % 3],
                "panen_file_uid": f"PANEN-{company}-{i}",
                "panen_file_name": f"panen_{company.lower()}_{i:04d}.jpg" if has_panen else "",
                "panen_has_embedding": has_panen,
                "transport_file_uid": f"TRANS-{company}-{i}" if has_transport else "",
                "transport_file_name": f"before_{company.lower()}_{i:04d}.jpg" if has_transport else "",
                "transport_has_embedding": has_transport,
                "similarity_score": score if ready else None,
                "panen_candidate_count": 1,
                "transport_candidate_count": 1 if has_transport else 0,
                "decision": decision,
                "transaction_status": "SIAP TRANSAKSI" if ready else decision,
                "similarity_eligible": ready,
                "calculated_at": pd.Timestamp(2026, month, 2),
            })
    return pd.DataFrame(rows)


def load_rows(companies: list[str], year: int, demo: bool = False) -> pd.DataFrame:
    if demo:
        out = _demo_rows()
        return out[out["company"].isin(companies)].copy()
    if not callable(load_harmony_transaction_validation):
        raise RuntimeError("queries.load_harmony_transaction_validation tidak tersedia")
    clear_data_cache()
    out = load_harmony_transaction_validation(companies, year)
    if out is None:
        return pd.DataFrame()
    return out.copy()


def latest_month(rows: pd.DataFrame) -> str:
    if rows is None or rows.empty:
        return ""
    vals = rows.get("month", pd.Series(dtype=str)).dropna().astype(str)
    vals = vals[vals.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")]
    return vals.max() if not vals.empty else ""


def filter_scope(rows: pd.DataFrame, month: str | None = None, company: str | None = None) -> pd.DataFrame:
    out = rows.copy()
    if month:
        out = out[out["month"].astype(str).eq(month)].copy()
    if company and company != "ALL":
        out = out[out["company"].astype(str).str.upper().eq(company.upper())].copy()
    return out


def calc_metrics(rows: pd.DataFrame, sap_total: int | None = None, panen_sap: int | None = None) -> dict[str, Any]:
    """Metrics for Panen-vs-Transport report.

    The report baseline is OPH with Foto Panen, not a separate SAP OPH denominator.
    `panen_sap` may still be supplied from the dashboard coverage source so the
    headline count stays consistent with the dashboard, but `sap_total` is not
    shown or used as a report KPI.
    """
    if rows is None:
        rows = pd.DataFrame()
    has_panen = rows.get("panen_file_name", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip().ne("")
    has_transport = rows.get("transport_file_name", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip().ne("")
    ready = rows.get("similarity_eligible", pd.Series(False, index=rows.index)).fillna(False).astype(bool)
    decision = rows.get("decision", pd.Series("", index=rows.index)).fillna("").astype(str).str.upper()
    scores = pd.to_numeric(rows.get("similarity_score", pd.Series(dtype=float)), errors="coerce")
    panen_count = int(has_panen.sum()) if panen_sap is None else int(panen_sap)
    ready_count = int(ready.sum())
    raw_transport_count = int((has_panen & has_transport).sum())
    # Report comparison population: a Transport OPH is counted only when the
    # Panen + Transport pair is valid and has reached the similarity-ready set.
    # This keeps OPH FOTO TRANSPORT identical to the population actually assessed.
    return {
        "panen": panen_count,
        "transport": ready_count,
        "transport_raw": raw_transport_count,
        "ready": ready_count,
        "same": int(decision.eq("SAMA").sum()),
        "different": int(decision.eq("BERBEDA").sum()),
        "no_panen": int((~has_panen).sum()),
        "no_transport": int((has_panen & ~has_transport).sum()),
        "coverage_similarity": (100.0 * ready_count / panen_count) if panen_count else 0.0,
        "coverage_transport": (100.0 * ready_count / panen_count) if panen_count else 0.0,
        "avg_similarity": float(scores.where(ready).mean()) if ready.any() else None,
    }


def company_availability(rows: pd.DataFrame, company: str) -> dict[str, Any]:
    d = filter_scope(rows, company=company)
    m = calc_metrics(d)
    return {
        "company": company,
        **m,
        "has_transport": bool(m.get("transport_raw", 0) > 0),
        "has_scored_transport": bool(m["transport"] > 0),
    }


def _clean_display_text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null"}:
        return fallback
    return text


def get_sap_denominator(companies: list[str], year: int, month: str | None, demo: bool) -> tuple[int | None, int | None]:
    if demo:
        return None, None
    sap_total = None
    panen = None
    months = [month] if month else None
    if callable(load_harmony_oph_coverage):
        try:
            cov = load_harmony_oph_coverage(companies, year, months)
            if bool(cov.get("source_ready")):
                sap_total = int(cov.get("sap_total") or 0)
                panen = int(cov.get("sap_with_photo") or 0)
                return sap_total, panen
        except Exception as exc:
            print(f"[TRANSPORT REPORT WARN] coverage OPH gagal: {exc}", flush=True)
    if callable(load_sap_kpis):
        try:
            k = load_sap_kpis("HARMONY", companies, None, months, None)
            value = k.get("total_sap_oph")
            sap_total = int(value) if value is not None else None
        except Exception as exc:
            print(f"[TRANSPORT REPORT WARN] SAP KPI gagal: {exc}", flush=True)
    return sap_total, panen


def _chart_bar(data: pd.DataFrame, label_col: str, value_col: str, out: Path, title: str, horizontal: bool = False, top_n: int = 15):
    out.parent.mkdir(parents=True, exist_ok=True)
    d = data.copy().head(top_n)
    if d.empty:
        d = pd.DataFrame({label_col: ["No data"], value_col: [0]})
    total = float(pd.to_numeric(d[value_col], errors="coerce").fillna(0).sum())
    fig, ax = plt.subplots(figsize=(8.0, 4.2), dpi=135)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    if horizontal:
        d = d.sort_values(value_col, ascending=True)
        vals = pd.to_numeric(d[value_col], errors="coerce").fillna(0)
        bars = ax.barh(d[label_col].astype(str), vals, color=[common.CHART_PALETTE[i % 4 + 1] for i in range(len(d))])
        mx = max(float(vals.max()), 1.0)
        for bar, val in zip(bars, vals):
            pct = (100.0 * float(val) / total) if total else 0.0
            ax.text(bar.get_width() + mx * 0.015, bar.get_y() + bar.get_height()/2, f"{int(val):,} ({pct:.1f}%)", va="center", fontsize=6.7)
        ax.grid(axis="x", linewidth=0.7, alpha=0.25)
    else:
        vals = pd.to_numeric(d[value_col], errors="coerce").fillna(0)
        bars = ax.bar(d[label_col].astype(str), vals, color=[common.CHART_PALETTE[i % 4 + 1] for i in range(len(d))])
        mx = max(float(vals.max()), 1.0)
        for bar, val in zip(bars, vals):
            pct = (100.0 * float(val) / total) if total else 0.0
            ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+mx*0.018, f"{int(val):,}\n({pct:.1f}%)", ha="center", fontsize=6.4)
        ax.tick_params(axis="x", rotation=20, labelsize=7)
        ax.grid(axis="y", linewidth=0.7, alpha=0.25)
    ax.set_title(title, fontsize=10, weight="bold")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=7)
    common.style_chart_axis(ax)
    plt.tight_layout(pad=0.8)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return out


def _chart_decision_breakdown(rows: pd.DataFrame, dimension: str, out: Path, title: str, top_n: int = 12):
    """Stacked location breakdown: SAMA + BERBEDA + TIDAK ADA TRANSPORT."""
    out.parent.mkdir(parents=True, exist_ok=True)
    category_cols = ["SAMA", "BERBEDA", "TIDAK ADA TRANSPORT"]

    if rows is None or rows.empty or dimension not in rows.columns:
        data = pd.DataFrame({"label": ["No data"], **{c: [0] for c in category_cols}, "total": [0]})
    else:
        d = rows.copy()
        decision = d.get("decision", pd.Series("", index=d.index)).fillna("").astype(str).str.upper()
        has_panen = d.get("panen_file_name", pd.Series("", index=d.index)).fillna("").astype(str).str.strip().ne("")
        has_transport = d.get("transport_file_name", pd.Series("", index=d.index)).fillna("").astype(str).str.strip().ne("")

        category = pd.Series("", index=d.index, dtype="object")
        category.loc[decision.eq("SAMA")] = "SAMA"
        category.loc[decision.eq("BERBEDA")] = "BERBEDA"
        category.loc[has_panen & ~has_transport] = "TIDAK ADA TRANSPORT"
        d["_category"] = category
        d = d[d["_category"].ne("")].copy()

        company = d.get("company", pd.Series("", index=d.index)).fillna("").astype(str).str.strip()
        estate = d.get("estate", pd.Series("", index=d.index)).fillna("").astype(str).str.strip()
        dim = d[dimension].fillna("").astype(str).str.strip()

        if dimension == "afdeling":
            valid = dim.ne("") & dim.ne("-")
            d = d[valid].copy()
            company = company.loc[d.index]
            estate = estate.loc[d.index]
            dim = dim.loc[d.index]
            d["_label"] = (company + " | " + estate + " / Afd " + dim).str.strip(" |/")
        elif dimension == "estate":
            valid = estate.ne("") & estate.ne("-")
            d = d[valid].copy()
            company = company.loc[d.index]
            estate = estate.loc[d.index]
            d["_label"] = (company + " | " + estate).str.strip(" |")
        else:
            valid = dim.ne("") & dim.ne("-")
            d = d[valid].copy()
            d["_label"] = dim.loc[d.index]

        if d.empty:
            data = pd.DataFrame({"label": ["No data"], **{c: [0] for c in category_cols}, "total": [0]})
        else:
            pvt = d.groupby(["_label", "_category"]).size().unstack(fill_value=0)
            for col in category_cols:
                if col not in pvt.columns:
                    pvt[col] = 0
            pvt["total"] = pvt[category_cols].sum(axis=1)
            data = (
                pvt.sort_values("total", ascending=False)
                .head(top_n)
                .sort_values("total", ascending=True)
                .reset_index()
                .rename(columns={"_label": "label"})
            )

    fig_h = max(3.8, min(6.8, 0.40 * len(data) + 1.8))
    fig, ax = plt.subplots(figsize=(8.4, fig_h), dpi=145)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    y = list(range(len(data)))
    left = pd.Series([0] * len(data), dtype=float)
    totals = pd.to_numeric(data.get("total", 0), errors="coerce").fillna(0).astype(float)
    overall = max(float(totals.sum()), 1.0)
    threshold = max(float(totals.max()) * 0.08 if len(totals) else 0.0, 1.0)
    for col in category_cols:
        values = pd.to_numeric(data.get(col, 0), errors="coerce").fillna(0).astype(float)
        bar_color = common.TRANSPORT_STATUS_PALETTE.get(col, common.CHART_PALETTE[category_cols.index(col) % 3 + 1])
        bars = ax.barh(y, values, left=left, height=0.56, label=col, color=bar_color, edgecolor="white", linewidth=0.6)
        for bar, val, tot in zip(bars, values, totals):
            if val <= 0 or tot <= 0:
                continue
            pct = 100.0 * float(val) / float(tot)
            if bar.get_width() >= threshold:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height()/2, f"{pct:.0f}%", ha="center", va="center", fontsize=6.1, color="white")
        left = left + values.reset_index(drop=True)

    ax.set_yticks(y)
    ax.set_yticklabels(data["label"].astype(str).tolist(), fontsize=6.7)
    ax.set_xlabel("OPH Foto Panen", fontsize=7.8, color=common.CHART_BLUE)
    ax.set_title(title, fontsize=10.0, weight="bold")
    ax.grid(axis="x", linewidth=0.7, alpha=0.22)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.11), ncol=3, frameon=False, fontsize=6.7)

    xmax = max(float(totals.max()) if len(totals) else 0.0, 1.0)
    for yi, total in zip(y, totals):
        pct_total = (100.0 * float(total) / overall) if overall else 0.0
        ax.text(float(total) + xmax * 0.012, yi, f"{int(total):,} ({pct_total:.1f}%)", va="center", fontsize=6.3, color=common.CHART_BLUE)

    common.style_chart_axis(ax)
    plt.tight_layout(pad=0.9)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return out


def _chart_month(rows: pd.DataFrame, out: Path, company_label: str = ""):
    """Monthly stacked trend for one PT: SAMA + BERBEDA + TIDAK ADA TRANSPORT."""
    d = rows.copy()
    if d.empty:
        piv = pd.DataFrame({"SAMA": [0], "BERBEDA": [0], "TIDAK ADA TRANSPORT": [0]}, index=["No data"])
    else:
        month = d.get("month", pd.Series("", index=d.index)).fillna("").astype(str)
        decision = d.get("decision", pd.Series("", index=d.index)).fillna("").astype(str).str.upper()
        has_panen = d.get("panen_file_name", pd.Series("", index=d.index)).fillna("").astype(str).str.strip().ne("")
        has_transport = d.get("transport_file_name", pd.Series("", index=d.index)).fillna("").astype(str).str.strip().ne("")

        category = pd.Series("", index=d.index, dtype="object")
        category.loc[decision.eq("SAMA")] = "SAMA"
        category.loc[decision.eq("BERBEDA")] = "BERBEDA"
        category.loc[has_panen & ~has_transport] = "TIDAK ADA TRANSPORT"

        tmp = pd.DataFrame({"month": month, "category": category})
        tmp = tmp[tmp["month"].str.match(r"^20\d{2}-(0[1-9]|1[0-2])$") & tmp["category"].ne("")].copy()
        if tmp.empty:
            piv = pd.DataFrame({"SAMA": [0], "BERBEDA": [0], "TIDAK ADA TRANSPORT": [0]}, index=["No data"])
        else:
            counts = tmp.groupby(["month", "category"], dropna=False).size().reset_index(name="count")
            piv = counts.pivot_table(index="month", columns="category", values="count", aggfunc="sum", fill_value=0).sort_index()
            for col in ["SAMA", "BERBEDA", "TIDAK ADA TRANSPORT"]:
                if col not in piv.columns:
                    piv[col] = 0
            piv = piv[["SAMA", "BERBEDA", "TIDAK ADA TRANSPORT"]]

    total = piv[["SAMA", "BERBEDA", "TIDAK ADA TRANSPORT"]].sum(axis=1)
    fig, ax = plt.subplots(figsize=(8.8, 3.45), dpi=145)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    x = list(range(len(piv.index)))
    same = piv["SAMA"].to_numpy()
    diff = piv["BERBEDA"].to_numpy()
    no_transport = piv["TIDAK ADA TRANSPORT"].to_numpy()

    bars_same = ax.bar(x, same, label="SAMA", width=0.62, color=common.TRANSPORT_STATUS_PALETTE["SAMA"], edgecolor="white", linewidth=0.6)
    bars_diff = ax.bar(x, diff, bottom=same, label="BERBEDA", width=0.62, color=common.TRANSPORT_STATUS_PALETTE["BERBEDA"], edgecolor="white", linewidth=0.6)
    bars_nt = ax.bar(x, no_transport, bottom=same + diff, label="TIDAK ADA TRANSPORT", width=0.62, color=common.TRANSPORT_STATUS_PALETTE["TIDAK ADA TRANSPORT"], edgecolor="white", linewidth=0.6)

    ymax = max(float(total.max()) if len(total) else 0.0, 1.0)
    for xi, val in zip(x, total.values):
        ax.text(xi, float(val) + ymax * 0.022, f"{int(val):,}", ha="center", va="bottom", fontsize=6.9, color=common.CHART_BLUE)
    segment_threshold = max(ymax * 0.05, 1.0)
    for bars, vals, bottoms in [(bars_same, same, [0]*len(same)), (bars_diff, diff, same), (bars_nt, no_transport, same+diff)]:
        for bar, val, bottom, tot in zip(bars, vals, bottoms, total.values):
            if val <= 0 or tot <= 0:
                continue
            pct = 100.0 * float(val) / float(tot)
            if val >= segment_threshold:
                ax.text(bar.get_x() + bar.get_width()/2, float(bottom) + float(val)/2, f"{pct:.0f}%", ha="center", va="center", fontsize=6.2, color="white")

    ax.set_xticks(x)
    ax.set_xticklabels(piv.index.tolist(), fontsize=7.4)
    ax.set_ylabel("OPH Foto Panen", fontsize=8.0, color=common.CHART_BLUE)
    chart_title = "Monthly Trend"
    if company_label:
        chart_title += f" {company_label.upper()}"
    chart_title += " - SAMA vs BERBEDA vs TIDAK ADA TRANSPORT"
    ax.set_title(chart_title, fontsize=9.7, weight="bold")
    ax.grid(axis="y", linewidth=0.7, alpha=0.22)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.11), ncol=3, frameon=False, fontsize=7.2)
    common.style_chart_axis(ax)
    plt.tight_layout(pad=0.9)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return out


def _metric(c: canvas.Canvas, x: float, y: float, w: float, h: float, label: str, value: str, sub: str = "", tone= NAVY):
    common.rounded_rect(c, x, y, w, h, 4*mm, fill=WHITE, stroke=BORDER, width=0.5)
    c.setFillColor(tone); c.setFont("Helvetica-Bold", 6.6); c.drawString(x+4*mm, y+h-7*mm, label.upper())
    c.setFillColor(DARK); c.setFont("Helvetica-Bold", 15); c.drawString(x+4*mm, y+h-16*mm, value)
    if sub:
        common.draw_fit(c, sub, x+4*mm, y+4*mm, w-8*mm, "Helvetica-Bold", 5.3, SLATE, min_size=4.5)


def cover(c: canvas.Canvas, rows: pd.DataFrame, year: int, latest: str, source_range_label: str = ""):
    c.setFillColor(WHITE); c.rect(0,0,PAGE_W,PAGE_H,fill=1,stroke=0)
    common.draw_decor(c)
    common.draw_logo(c, PAGE_W/2-42*mm, PAGE_H-45*mm, 84*mm, 22*mm)

    c.setFillColor(GRAY); c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(PAGE_W/2, PAGE_H-73*mm, "PANEN VS TRANSPORT REPORT")
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 29)
    c.drawCentredString(PAGE_W/2, PAGE_H-92*mm, "GOLDEN EYE")
    c.setFillColor(DARK); c.setFont("Helvetica-Bold", 25)
    c.drawCentredString(PAGE_W/2, PAGE_H-109*mm, "PANEN VS TRANSPORT")
    c.setFillColor(RED); c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(PAGE_W/2, PAGE_H-124*mm, "HARMONY - PWP + DIN")

    c.setFillColor(SLATE); c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(PAGE_W/2, PAGE_H-141*mm, "Perbandingan evidence Foto Panen dan Foto Transport per OPH.")

    latest_label = _month_label_id(latest, year)
    badge_w = 58 * mm
    badge_h = 12 * mm
    bx = PAGE_W/2 - badge_w/2
    by = PAGE_H - 164 * mm
    common.draw_calendar_period_badge(c, bx, by, badge_w, badge_h, latest_label.upper() if latest_label else str(year))

    c.showPage()


def summary_page(c: canvas.Canvas, rows: pd.DataFrame, metrics: dict[str, Any], period: str, page_no: int):
    draw_header(c, "Ringkasan & Temuan Utama", period, page_no)

    def fmt(v):
        return "N/A" if v is None else f"{int(v):,}" if isinstance(v, (int, float)) and float(v).is_integer() else str(v)

    same = int(metrics.get("same", 0) or 0)
    diff = int(metrics.get("different", 0) or 0)
    no_transport = int(metrics.get("no_transport", 0) or 0)
    population = same + diff + no_transport
    same_pct = (100.0 * same / population) if population else 0.0
    diff_pct = (100.0 * diff / population) if population else 0.0
    nt_pct = (100.0 * no_transport / population) if population else 0.0
    period_text = _source_range_label(rows)
    din_start_text = _month_label_id(DIN_TRANSPORT_START_MONTH)

    intro_y = PAGE_H - 104 * mm
    intro_h = 36 * mm
    common.rounded_rect(c, MARGIN_X, intro_y, PAGE_W - 2 * MARGIN_X, intro_h, 5 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.45)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.8)
    c.drawString(MARGIN_X + 6 * mm, intro_y + intro_h - 9 * mm, "RINGKASAN UTAMA")
    line1 = f"Data pada report ini mencakup {period_text}. Report dibuat satu file, namun PWP dan DIN dibaca sebagai section tersendiri agar ceritanya jelas."
    line2 = f"Dari {metrics['panen']:,} OPH Foto Panen, terdapat {metrics['transport']:,} OPH yang sudah memiliki pasangan Foto Panen dan Foto Transport untuk dinilai ({metrics['coverage_similarity']:.1f}% dari Foto Panen)."
    line3 = f"Khusus DIN, evaluasi Panen vs Transport dihitung mulai {din_start_text} agar lebih fair terhadap awal ketersediaan Foto Transport. Pada populasi trend: SAMA {same:,} ({same_pct:.1f}%), BERBEDA {diff:,} ({diff_pct:.1f}%), dan Tidak Ada Transport {no_transport:,} ({nt_pct:.1f}%)."
    common.draw_fit(c, line1, MARGIN_X + 6 * mm, intro_y + 20.5 * mm, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 6.7, SLATE, min_size=5.6)
    common.draw_fit(c, line2, MARGIN_X + 6 * mm, intro_y + 12.5 * mm, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 6.7, SLATE, min_size=5.5)
    line3_wrapped = common.wrap_lines(c, line3, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 5.7, max_lines=2)
    common.draw_text_lines(c, line3_wrapped, MARGIN_X + 6 * mm, intro_y + 6.4 * mm, "Helvetica-Bold", 5.7, 5.7, SLATE)

    hero_y = intro_y - 34 * mm
    hero_h = 30 * mm
    common.rounded_rect(c, MARGIN_X, hero_y, PAGE_W - 2 * MARGIN_X, hero_h, 5 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 8.2)
    c.drawString(MARGIN_X + 7 * mm, hero_y + hero_h - 9 * mm, "OPH FOTO PANEN")
    c.setFillColor(DARK); c.setFont("Helvetica-Bold", 27)
    c.drawString(MARGIN_X + 7 * mm, hero_y + 7 * mm, fmt(metrics["panen"]))

    gap = 4 * mm
    card_w = (PAGE_W - 2 * MARGIN_X - gap * 2) / 3
    card_h = 24 * mm
    y1 = hero_y - card_h - 6 * mm
    y2 = y1 - card_h - 5 * mm
    _metric(c, MARGIN_X, y1, card_w, card_h, "FOTO TRANSPORT", fmt(metrics["transport"]), "", ACCENT_BLUE_2)
    _metric(c, MARGIN_X + card_w + gap, y1, card_w, card_h, "COVERAGE", f"{metrics['coverage_similarity']:.1f}%", "", ACCENT_BLUE_3)
    avg = metrics["avg_similarity"]
    _metric(c, MARGIN_X + 2 * (card_w + gap), y1, card_w, card_h, "AVG SIMILARITY", "N/A" if avg is None or pd.isna(avg) else f"{avg:.2f}%", "", ACCENT_BLUE_4)
    third_w = (PAGE_W - 2 * MARGIN_X - gap * 2) / 3
    _metric(c, MARGIN_X, y2, third_w, card_h, "SAMA", fmt(metrics["same"]), "", ACCENT_BLUE_2)
    _metric(c, MARGIN_X + third_w + gap, y2, third_w, card_h, "BERBEDA", fmt(metrics["different"]), "", ACCENT_BLUE_3)
    _metric(c, MARGIN_X + 2 * (third_w + gap), y2, third_w, card_h, "TIDAK ADA TRANSPORT", fmt(metrics["no_transport"]), "", ACCENT_BLUE_4)

    box_y = 16 * mm
    box_h = 42 * mm
    common.rounded_rect(c, MARGIN_X, box_y, PAGE_W - 2 * MARGIN_X, box_h, 5 * mm, fill=WHITE, stroke=BORDER, width=0.55)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9.0)
    c.drawString(MARGIN_X + 6 * mm, box_y + box_h - 9 * mm, "RANGKUMAN COMPANY")

    pwp = company_availability(rows, "PWP")
    din = company_availability(rows, "DIN")
    col_gap = 7 * mm
    col_w = (PAGE_W - 2 * MARGIN_X - 12 * mm - col_gap) / 2
    left_x = MARGIN_X + 6 * mm
    right_x = left_x + col_w + col_gap
    sub_y = box_y + 7 * mm
    sub_h = 22 * mm
    for x, info, tone in [(left_x, pwp, NAVY), (right_x, din, RED)]:
        fill = SOFT_BLUE if info["company"] == "PWP" else SOFT_BLUE_2
        common.rounded_rect(c, x, sub_y, col_w, sub_h, 4 * mm, fill=fill, stroke=BORDER, width=0.35)
        c.setFillColor(tone); c.setFont("Helvetica-Bold", 8.0)
        c.drawString(x + 4 * mm, sub_y + sub_h - 7 * mm, info["company"])
        common.draw_fit(c, f"Foto Panen {info['panen']:,} | Foto Transport {info['transport']:,}", x + 4 * mm, sub_y + 8.5 * mm, col_w - 8 * mm, "Helvetica-Bold", 6.2, DARK, min_size=5.1)
        if info["company"] == "PWP" and not info["has_transport"]:
            note = "Foto Transport PWP belum tersedia pada source validasi."
        elif info["company"] == "DIN":
            note = f"Evaluasi DIN mulai {din_start_text} | Coverage {info['coverage_similarity']:.1f}%."
        else:
            note = f"Coverage {info['coverage_similarity']:.1f}% dari Foto Panen."
        common.draw_fit(c, note, x + 4 * mm, sub_y + 2.5 * mm, col_w - 8 * mm, "Helvetica-Bold", 5.5, SLATE, min_size=4.7)
    c.showPage()


def _monthly_condition_metrics(rows: pd.DataFrame) -> dict[str, int]:
    """Return the three mutually exclusive monthly-trend populations for one PT."""
    if rows is None or rows.empty:
        return {"same": 0, "different": 0, "no_transport": 0, "population": 0}
    decision = rows.get("decision", pd.Series("", index=rows.index)).fillna("").astype(str).str.upper()
    has_panen = rows.get("panen_file_name", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip().ne("")
    has_transport = rows.get("transport_file_name", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip().ne("")
    same = int(decision.eq("SAMA").sum())
    different = int(decision.eq("BERBEDA").sum())
    no_transport = int((has_panen & ~has_transport).sum())
    return {
        "same": same,
        "different": different,
        "no_transport": no_transport,
        "population": same + different + no_transport,
    }


def charts_page(c: canvas.Canvas, rows: pd.DataFrame, cache: Path, period: str, page_no: int):
    """Monthly Trend is intentionally separated per PT; PWP and DIN must never be stacked together."""
    draw_header(c, "Monthly Trend per PT", period, page_no)

    companies_present = []
    for company in ["PWP", "DIN"]:
        company_rows = filter_scope(rows, company=company)
        if not company_rows.empty:
            companies_present.append((company, company_rows))

    # Keep single-company runs useful while the ALL report uses two independent panels.
    if len(companies_present) <= 1:
        company = companies_present[0][0] if companies_present else "PT"
        company_rows = companies_present[0][1] if companies_present else rows.iloc[0:0].copy()
        chart = _chart_month(company_rows, cache / f"trend_month_{company.lower()}.png", company)
        common.chart_panel(
            c,
            f"{company} - SAMA vs BERBEDA vs TIDAK ADA TRANSPORT",
            chart,
            MARGIN_X,
            67 * mm,
            PAGE_W - 2 * MARGIN_X,
            127 * mm,
            NAVY if company == "PWP" else RED,
        )
        m = _monthly_condition_metrics(company_rows)
        box_y = 25 * mm
        common.rounded_rect(c, MARGIN_X, box_y, PAGE_W - 2 * MARGIN_X, 29 * mm, 4 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.4)
        c.setFillColor(NAVY if company == "PWP" else RED); c.setFont("Helvetica-Bold", 8.0)
        c.drawString(MARGIN_X + 6 * mm, box_y + 19 * mm, f"POPULASI BULANAN {company}: {m['population']:,} OPH FOTO PANEN")
        common.draw_fit(c, f"SAMA {m['same']:,} | BERBEDA {m['different']:,} | TIDAK ADA TRANSPORT {m['no_transport']:,}", MARGIN_X + 6 * mm, box_y + 10.5 * mm, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 6.7, DARK, min_size=5.5)
        note = f"Trend hanya menggunakan transaksi {company}; tidak dicampur dengan PT lain."
        if company == "DIN":
            note += f" DIN dihitung mulai {_month_label_id(DIN_TRANSPORT_START_MONTH)}."
        common.draw_fit(c, note, MARGIN_X + 6 * mm, box_y + 4.2 * mm, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 5.6, SLATE, min_size=4.8)
        c.showPage()
        return

    # ALL report: two independent monthly trends on the same page.
    panel_h = 66 * mm
    pwp_y = 128 * mm
    din_y = 56 * mm
    for company, company_rows, y, tone in [
        ("PWP", filter_scope(rows, company="PWP"), pwp_y, NAVY),
        ("DIN", filter_scope(rows, company="DIN"), din_y, RED),
    ]:
        chart = _chart_month(company_rows, cache / f"trend_month_{company.lower()}.png", company)
        common.chart_panel(
            c,
            f"{company} - SAMA vs BERBEDA vs TIDAK ADA TRANSPORT",
            chart,
            MARGIN_X,
            y,
            PAGE_W - 2 * MARGIN_X,
            panel_h,
            tone,
        )

    # Compact, independent population summaries. No combined PWP+DIN denominator is shown.
    pwp_m = _monthly_condition_metrics(filter_scope(rows, company="PWP"))
    din_m = _monthly_condition_metrics(filter_scope(rows, company="DIN"))
    gap = 5 * mm
    cw = (PAGE_W - 2 * MARGIN_X - gap) / 2
    box_y = 22 * mm
    box_h = 27 * mm
    for x, company, m, tone, fill in [
        (MARGIN_X, "PWP", pwp_m, NAVY, SOFT_BLUE),
        (MARGIN_X + cw + gap, "DIN", din_m, ACCENT_BLUE_2, SOFT_BLUE_2),
    ]:
        common.rounded_rect(c, x, box_y, cw, box_h, 4 * mm, fill=fill, stroke=BORDER, width=0.4)
        c.setFillColor(tone); c.setFont("Helvetica-Bold", 7.4)
        c.drawString(x + 4 * mm, box_y + box_h - 7 * mm, f"{company} | {m['population']:,} CASE BULANAN")
        common.draw_fit(c, f"SAMA {m['same']:,} | BERBEDA {m['different']:,}", x + 4 * mm, box_y + 11.5 * mm, cw - 8 * mm, "Helvetica-Bold", 5.7, DARK, min_size=4.8)
        common.draw_fit(c, f"TIDAK ADA TRANSPORT {m['no_transport']:,}", x + 4 * mm, box_y + 5.5 * mm, cw - 8 * mm, "Helvetica-Bold", 5.4, SLATE, min_size=4.6)

    c.showPage()

def company_location_page(c: canvas.Canvas, rows: pd.DataFrame, cache: Path, period: str, page_no: int):
    draw_header(c, "Breakdown Company", period, page_no)
    company_rows = []
    for company in ["PWP", "DIN"]:
        info = company_availability(rows, company)
        company_rows.append({"company": company, "Foto Panen": info["panen"], "Foto Transport": info["transport"]})
    comp = pd.DataFrame(company_rows)

    out = cache / "company_coverage.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8.0, 4.0), dpi=140)
    fig.patch.set_facecolor("white"); ax.set_facecolor("white")
    x = list(range(len(comp))); width = 0.28
    panen_vals = pd.to_numeric(comp["Foto Panen"], errors="coerce").fillna(0)
    transport_vals = pd.to_numeric(comp["Foto Transport"], errors="coerce").fillna(0)
    bars_p = ax.bar([i - width/2 for i in x], panen_vals, width=width, label="Foto Panen", color=common.CHART_PALETTE[1], edgecolor="white", linewidth=0.6, hatch=common.CHART_HATCHES[0])
    bars_t = ax.bar([i + width/2 for i in x], transport_vals, width=width, label="Foto Transport", color=common.CHART_PALETTE[3], edgecolor="white", linewidth=0.6, hatch=common.CHART_HATCHES[1])
    ymax = max(float(max(panen_vals.max() if len(panen_vals) else 0, transport_vals.max() if len(transport_vals) else 0)), 1.0)
    for bar, val in zip(bars_p, panen_vals):
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+ymax*0.02, f"{int(val):,}", ha="center", fontsize=6.6)
    for bar, val, base in zip(bars_t, transport_vals, panen_vals):
        pct = (100.0 * float(val) / float(base)) if float(base) else 0.0
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+ymax*0.02, f"{int(val):,}\n({pct:.1f}%)", ha="center", fontsize=6.2)
    ax.set_xticks(x); ax.set_xticklabels(comp["company"].tolist(), fontsize=8)
    ax.set_ylabel("OPH", fontsize=7.8, color=common.CHART_BLUE)
    ax.set_title("Foto Panen vs Foto Transport", fontsize=10.2, weight="bold")
    ax.grid(axis="y", linewidth=0.7, alpha=0.22)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, fontsize=7.2)
    common.style_chart_axis(ax)
    common.style_chart_axis(ax)
    plt.tight_layout(pad=0.9); fig.savefig(out, bbox_inches="tight", pad_inches=0.08, facecolor="white"); plt.close(fig)
    common.chart_panel(c, "PWP vs DIN - Foto Panen dan Foto Transport", out, MARGIN_X, 105 * mm, PAGE_W - 2 * MARGIN_X, 88 * mm, NAVY)

    pwp = company_availability(rows, "PWP")
    if pwp["transport"] == 0:
        common.draw_fit(c, "Catatan: pada scope ini Foto Transport PWP belum tersedia, sehingga evaluasi SAMA/BERBEDA PWP belum dapat dinilai.", MARGIN_X, 97 * mm, PAGE_W - 2 * MARGIN_X, "Helvetica-Bold", 6.2, SLATE, min_size=5.1)
    din = company_availability(rows, "DIN")
    gap = 7 * mm; cw = (PAGE_W - 2 * MARGIN_X - gap) / 2; y = 46 * mm; h = 45 * mm
    for x0, info, tone, fill in [(MARGIN_X, pwp, NAVY, SOFT_BLUE), (MARGIN_X + cw + gap, din, ACCENT_BLUE_2, SOFT_BLUE_2)]:
        common.rounded_rect(c, x0, y, cw, h, 4 * mm, fill=fill, stroke=BORDER, width=0.4)
        c.setFillColor(tone); c.setFont("Helvetica-Bold", 10); c.drawString(x0 + 5 * mm, y + h - 9 * mm, info["company"])
        common.draw_fit(c, f"Foto Panen  {info['panen']:,}", x0 + 5 * mm, y + h - 19 * mm, cw - 10 * mm, "Helvetica-Bold", 7.0, DARK, min_size=5.8)
        common.draw_fit(c, f"Foto Transport  {info['transport']:,}", x0 + 5 * mm, y + h - 27 * mm, cw - 10 * mm, "Helvetica-Bold", 7.0, DARK, min_size=5.8)
        if info["company"] == "PWP" and not info["has_transport"]:
            note = "Foto Transport belum tersedia pada source validasi."
        else:
            note = (f"Evaluasi mulai {_month_label_id(DIN_TRANSPORT_START_MONTH)} | Coverage {info['coverage_similarity']:.1f}%." if info["company"] == "DIN" else f"Coverage {info['coverage_similarity']:.1f}% dari Foto Panen.")
        common.draw_fit(c, note, x0 + 5 * mm, y + 8 * mm, cw - 10 * mm, "Helvetica-Bold", 5.8, SLATE, min_size=4.8)
    c.showPage()

def location_breakdown_page(c: canvas.Canvas, rows: pd.DataFrame, cache: Path, period: str, page_no: int):
    """Two full-size pages so all three categories remain readable."""
    estate_chart = _chart_decision_breakdown(
        rows,
        "estate",
        cache / "estate_decision.png",
        "SAMA vs BERBEDA vs TIDAK ADA TRANSPORT per Estate",
        top_n=12,
    )
    draw_header(c, "Breakdown Estate", period, page_no)
    common.chart_panel(c, "Estate", estate_chart, MARGIN_X, 52 * mm, PAGE_W - 2 * MARGIN_X, 142 * mm, NAVY)
    c.showPage()
    page_no += 1

    afd_chart = _chart_decision_breakdown(
        rows,
        "afdeling",
        cache / "afdeling_decision.png",
        "SAMA vs BERBEDA vs TIDAK ADA TRANSPORT per Afdeling",
        top_n=15,
    )
    draw_header(c, "Breakdown Afdeling", period, page_no)
    common.chart_panel(c, "Afdeling", afd_chart, MARGIN_X, 46 * mm, PAGE_W - 2 * MARGIN_X, 148 * mm, RED)
    c.showPage()
    return page_no + 1


def people_page(c: canvas.Canvas, rows: pd.DataFrame, cache: Path, period: str, page_no: int):
    draw_header(c, "Personel pada BERBEDA", period, page_no)
    berbeda = rows[rows.get("decision", pd.Series(dtype=str)).astype(str).str.upper().eq("BERBEDA")].copy() if not rows.empty else rows

    def cnt(df, col):
        if df is None or df.empty or col not in df.columns:
            return pd.DataFrame(columns=["label", "count"])
        s = df[col].fillna("").astype(str).str.strip()
        s = s[s.ne("") & s.ne("-") & s.str.lower().ne("none")]
        return s.value_counts().head(15).rename_axis("label").reset_index(name="count")

    kp = cnt(berbeda, "kerani_panen")
    md = cnt(berbeda, "mandor")
    cp1 = _chart_bar(kp, "label", "count", cache / "kp_diff.png", "Top Kerani Panen pada BERBEDA", horizontal=True)
    cp2 = _chart_bar(md, "label", "count", cache / "mandor_diff.png", "Top Mandor pada BERBEDA", horizontal=True)

    common.chart_panel(c, "Kerani Panen - BERBEDA", cp1, MARGIN_X, 112 * mm, PAGE_W - 2 * MARGIN_X, 82 * mm, NAVY)
    common.chart_panel(c, "Mandor - BERBEDA", cp2, MARGIN_X, 27 * mm, PAGE_W - 2 * MARGIN_X, 77 * mm, RED)
    c.showPage()

def _pick_examples(rows: pd.DataFrame, decision: str, n: int = 4, latest_only: bool = False) -> list[dict[str,Any]]:
    d=rows.copy()
    if d.empty: return []
    d=d[d.get("decision",pd.Series(dtype=str)).astype(str).str.upper().eq(decision.upper())].copy()
    d=d[d.get("panen_file_name",pd.Series("",index=d.index)).fillna("").astype(str).str.strip().ne("") & d.get("transport_file_name",pd.Series("",index=d.index)).fillna("").astype(str).str.strip().ne("")].copy()
    if latest_only and not d.empty:
        lm=latest_month(d); d=d[d["month"].astype(str).eq(lm)].copy()
    d["_score"]=pd.to_numeric(d.get("similarity_score"),errors="coerce")
    if decision.upper()=="SAMA": d=d.sort_values(["_score","tanggal"],ascending=[False,False])
    else: d=d.sort_values(["_score","tanggal"],ascending=[True,False])
    return d.head(n).to_dict("records")


def _evidence_card(c: canvas.Canvas, row: dict[str, Any], x: float, y: float, w: float, h: float, idx: int, decision: str):
    """Larger-image Panen-vs-Transport evidence card."""
    common.rounded_rect(c, x, y, w, h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.5)
    tone = LABEL_BG
    score = row.get("similarity_score")
    score_txt = "N/A" if score is None or pd.isna(score) else f"{float(score):.2f}%"

    oph_txt = _clean_display_text(row.get("oph"))
    company_txt = _clean_display_text(row.get("company"))
    estate_txt = _clean_display_text(row.get("estate"))
    afd_txt = _clean_display_text(row.get("afdeling"))
    blok_txt = _clean_display_text(row.get("blok"))
    tph_txt = _clean_display_text(row.get("tph"))
    kp_txt = _clean_display_text(row.get("kerani_panen"))
    kt_txt = _clean_display_text(row.get("kerani_transport"))
    mandor_txt = _clean_display_text(row.get("mandor"))

    c.setFillColor(tone)
    c.roundRect(x + 4 * mm, y + h - 11 * mm, 31 * mm, 7 * mm, 2.3 * mm, fill=1, stroke=0)
    common.draw_fit(c, f"{decision} #{idx}", x + 5 * mm, y + h - 8.6 * mm, 29 * mm, "Helvetica-Bold", 5.9, WHITE, align="center", min_size=4.9)
    common.draw_fit(c, f"OPH {oph_txt}  |  Similarity {score_txt}", x + 40 * mm, y + h - 8.8 * mm, w - 45 * mm, "Helvetica-Bold", 7.0, DARK, min_size=5.6)

    gap = 5 * mm
    pw = (w - 13 * mm - gap) / 2
    py = y + 14 * mm
    ph = h - 26 * mm
    panen_key = _clean_display_text(row.get("panen_file_name"), "")
    transport_key = _clean_display_text(row.get("transport_file_name"), "")
    if hasattr(common, "draw_transport_four_photo_panel"):
        common.draw_transport_four_photo_panel(c, "FOTO PANEN", panen_key, x + 4 * mm, py, pw, ph, NAVY, "#DCE2F1", "")
        common.draw_transport_four_photo_panel(c, "FOTO TRANSPORT", transport_key, x + 4 * mm + pw + gap, py, pw, ph, RED, "#DCE2F1", "")
    else:
        common.draw_photo_panel(c, "FOTO PANEN", common.get_image_for_key(panen_key, "#DCE2F1"), x + 4 * mm, py, pw, ph, NAVY)
        common.draw_photo_panel(c, "FOTO TRANSPORT", common.get_image_for_key(transport_key, "#DCE2F1"), x + 4 * mm + pw + gap, py, pw, ph, RED)

    dt = pd.to_datetime(row.get("tanggal"), errors="coerce")
    date_txt = dt.strftime("%Y-%m-%d") if pd.notna(dt) else "-"
    overlay_h = 11 * mm
    overlay_y = y + 2.5 * mm
    common.rounded_rect(c, x + 4 * mm, overlay_y, w - 8 * mm, overlay_h, 2.8 * mm, fill=SOFT_GRAY, stroke=BORDER, width=0.2)
    loc = f"{company_txt}  |  {estate_txt} / Afd {afd_txt} / Blok {blok_txt} / TPH {tph_txt}  |  {date_txt}"
    common.draw_fit(c, loc, x + 6 * mm, overlay_y + 6.1 * mm, w - 12 * mm, "Helvetica-Bold", 5.5, SLATE, min_size=4.6)
    third = (w - 20 * mm) / 3
    common.draw_fit(c, f"Kerani Panen: {kp_txt}", x + 6 * mm, overlay_y + 1.8 * mm, third, "Helvetica-Bold", 5.0, SLATE, min_size=4.1)
    common.draw_fit(c, f"Kerani Transport: {kt_txt}", x + 8 * mm + third, overlay_y + 1.8 * mm, third, "Helvetica-Bold", 5.0, SLATE, min_size=4.1)
    common.draw_fit(c, f"Mandor: {mandor_txt}", x + 10 * mm + 2 * third, overlay_y + 1.8 * mm, third, "Helvetica-Bold", 5.0, SLATE, min_size=4.1)

def evidence_pages(c: canvas.Canvas, rows: pd.DataFrame, period: str, page_no: int) -> int:
    """Two large evidence examples per decision for readability."""
    for decision, title in [("SAMA", "Evidence SAMA"), ("BERBEDA", "Evidence BERBEDA")]:
        items = _pick_examples(rows, decision, 2)
        if not items:
            continue
        draw_header(c, title, period, page_no)
        note = (
            f"SAMA: similarity >= {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%. Foto Panen dan Foto Transport ditampilkan berdampingan."
            if decision == "SAMA" else
            f"BERBEDA: similarity < {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%. Contoh dipilih dari skor terendah untuk memperjelas perbedaan visual."
        )
        common.rounded_rect(c, MARGIN_X, PAGE_H - 78 * mm, PAGE_W - 2 * MARGIN_X, 13 * mm, 4 * mm, fill=SOFT_BLUE_2 if decision == "SAMA" else SOFT_BLUE, stroke=BORDER, width=0.35)
        common.draw_fit(c, note, MARGIN_X + 6 * mm, PAGE_H - 70.5 * mm, PAGE_W - 2 * MARGIN_X - 12 * mm, "Helvetica-Bold", 6.2, SLATE, min_size=5.1)
        card_h = 84 * mm
        gap = 6 * mm
        y1 = PAGE_H - 176 * mm
        y2 = y1 - card_h - gap
        for i, item in enumerate(items[:2]):
            _evidence_card(c, item, MARGIN_X, y1 if i == 0 else y2, PAGE_W - 2 * MARGIN_X, card_h, i + 1, decision)
        c.showPage(); page_no += 1
    return page_no

def company_latest_page(c: canvas.Canvas, rows: pd.DataFrame, company: str, period: str, page_no: int) -> int:
    d = filter_scope(rows, company=company)
    lm = latest_month(d)
    if not lm:
        return page_no
    d = filter_scope(d, month=lm)
    if d.empty:
        return page_no

    m = calc_metrics(d)
    title_month = common.month_label_from_ym(lm) if hasattr(common, "month_label_from_ym") else lm

    if m["transport"] == 0:
        draw_header(c, f"{company} - Kondisi Bulan Terakhir", period, page_no)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 12); c.drawString(MARGIN_X, PAGE_H - 75 * mm, title_month)
        common.rounded_rect(c, MARGIN_X, 90 * mm, PAGE_W - 2 * MARGIN_X, 85 * mm, 5 * mm, fill=SOFT_BLUE, stroke=BORDER, width=0.45)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 11); c.drawString(MARGIN_X + 7 * mm, 160 * mm, f"FOTO TRANSPORT {company.upper()} BELUM TERSEDIA")
        common.draw_fit(c, f"Foto Panen tersedia, tetapi source validasi bulan terakhir belum menyediakan pasangan Foto Transport untuk {company.upper()}.", MARGIN_X + 7 * mm, 145 * mm, PAGE_W - 2 * MARGIN_X - 14 * mm, "Helvetica-Bold", 7.1, SLATE, min_size=5.8)
        common.draw_fit(c, "Evidence SAMA/BERBEDA belum dapat ditampilkan untuk company ini.", MARGIN_X + 7 * mm, 130 * mm, PAGE_W - 2 * MARGIN_X - 14 * mm, "Helvetica-Bold", 6.5, RED, min_size=5.3)
        c.showPage()
        return page_no + 1

    for decision in ["SAMA", "BERBEDA"]:
        items = _pick_examples(d, decision, 2, latest_only=False)
        draw_header(c, f"{company} - {decision} Bulan Terakhir", period, page_no)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 11.2)
        c.drawString(MARGIN_X, PAGE_H - 75 * mm, title_month)

        card_h = 84 * mm
        gap = 6 * mm
        y1 = PAGE_H - 176 * mm
        y2 = y1 - card_h - gap
        if items:
            for i, item in enumerate(items[:2]):
                _evidence_card(c, item, MARGIN_X, y1 if i == 0 else y2, PAGE_W - 2 * MARGIN_X, card_h, i + 1, decision)
        else:
            common.rounded_rect(c, MARGIN_X, y1, PAGE_W - 2 * MARGIN_X, card_h, 4 * mm, fill=WHITE, stroke=BORDER, width=0.5)
            common.draw_fit(c, f"Tidak ada evidence {decision} pada bulan ini.", MARGIN_X + 8 * mm, y1 + card_h/2, PAGE_W - 2*MARGIN_X - 16*mm, "Helvetica-Bold", 7.0, SLATE, align="center", min_size=5.5)
        c.showPage()
        page_no += 1
    return page_no

def final_page(c: canvas.Canvas, period: str, page_no: int):
    return

def generate(args) -> Path:
    companies=["PWP","DIN"] if args.company=="ALL" else [args.company]
    rows=load_rows(companies,args.year,args.demo)
    if rows.empty and not args.write_empty:
        raise RuntimeError("Tidak ada data Harmony Panen vs Transport untuk scope yang dipilih")
    raw_rows = rows.copy()
    source_range_label = _source_range_label(raw_rows, args.year)
    rows = apply_transport_scope_rules(rows)
    lm=latest_month(rows if not rows.empty else raw_rows)
    scope_rows=filter_scope(rows,args.month if args.month else None)
    sap_total,panen_sap=get_sap_denominator(companies,args.year,args.month if args.month else None,args.demo)
    metrics=calc_metrics(scope_rows,sap_total,panen_sap)
    if args.period_label:
        period = args.period_label
    elif args.month:
        period = _month_label_id(args.month, args.year)
    else:
        period = _month_label_id(lm, args.year)
    out_dir=Path(args.output_dir) if args.output_dir else REPORTS_DIR/datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=out_dir/f"AI_Powered_Golden_Eye_Transport_Validation_{'_'.join(companies)}_{stamp}.pdf"
    c=canvas.Canvas(str(out),pagesize=A4,pageCompression=1)
    c.setTitle("AI Powered Golden Eye - Panen vs Transport Validation")
    cache=out_dir/"_transport_chart_cache"; cache.mkdir(parents=True,exist_ok=True)
    cover(c,scope_rows,args.year,lm,source_range_label)
    page=2
    summary_page(c,scope_rows,metrics,period,page); page+=1
    charts_page(c,scope_rows,cache,period,page); page+=1
    company_location_page(c,scope_rows,cache,period,page); page+=1
    page=location_breakdown_page(c,scope_rows,cache,period,page)
    people_page(c,scope_rows,cache,period,page); page+=1
    page=evidence_pages(c,scope_rows,period,page)
    if not args.month:
        for company in companies:
            page=company_latest_page(c,scope_rows,company,period,page)
    c.save()
    common.compress_existing_pdf(out)
    if UNAVAILABLE_IMAGE_KEYS:
        recovery=out_dir/"transport_report_image_recovery_needed.csv"
        pd.DataFrame({"image_key":sorted(UNAVAILABLE_IMAGE_KEYS),"status":"IMAGE_NOT_READABLE_OR_PENDING_RECOVERY"}).to_csv(recovery,index=False,encoding="utf-8-sig")
        print(f"FILE GAMBAR TRANSPORT REPORT PERLU RECOVERY: {recovery} ({len(UNAVAILABLE_IMAGE_KEYS):,} image key)")
    print(f"OK TRANSPORT VALIDATION REPORT: {out}")
    return out


def parse_args():
    p=argparse.ArgumentParser("Generate separate Harmony Panen vs Transport validation PDF report")
    p.add_argument("--company",default="ALL",choices=["ALL","PWP","DIN"])
    p.add_argument("--year",type=int,default=2026)
    p.add_argument("--month",default="",help="Optional YYYY-MM. Kosong = seluruh periode + halaman bulan terakhir per company")
    p.add_argument("--period-label",default="")
    p.add_argument("--output-dir",default="")
    p.add_argument("--demo",action="store_true")
    p.add_argument("--write-empty",action="store_true")
    return p.parse_args()


if __name__=="__main__":
    generate(parse_args())
