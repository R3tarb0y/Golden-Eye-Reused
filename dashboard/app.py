from __future__ import annotations

import hashlib
import io
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import traceback
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
from flask import Response, g, jsonify, request

from db import fetch_corpus_image_bytes, fetch_image_bytes, ping, clear_query_cache
from queries import (
    HARMONY_TRANSACTION_SAME_THRESHOLD,
    TRANSPORT_NA_ELHP,
    SYSTEM_OPTIONS,
    apply_filters,
    detail_for_groups,
    get_company_options,
    get_filter_options,
    kpis,
    load_audit_summary,
    load_dashboard_monthly,
    load_details,
    load_group_details,
    load_group_bunch_pay_aggregate,
    load_groups,
    load_harmony_transaction_validation,
    load_harmony_oph_coverage,
    load_sap_kpis,
    load_transport_status_summary,
    load_geo_boundaries,
    load_geo_tph_points,
    selected_detail,
    source_breakdown,
    transport_similarity_usage_summary,
    agg_count,
    top_reused,
    usage_distribution,
    preload_all_data,
    clear_data_cache,
)

APP_TITLE = os.getenv("APP_TITLE", "AI-powered Golden Eye")
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8050"))
APP_DEBUG = os.getenv("APP_DEBUG", "false").lower() in {"1", "true", "yes", "y"}
DEFAULT_SYSTEM = str(os.getenv("DEFAULT_SYSTEM", "HARMONY") or "HARMONY").upper().strip()
DEFAULT_COMPANY = str(os.getenv("DEFAULT_COMPANY", "PWP") or "PWP").upper().strip()
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "600"))
TOP_N = int(os.getenv("TOP_N", "25"))
AUTO_OPEN_FIRST_GROUP = os.getenv("AUTO_OPEN_FIRST_GROUP", "false").lower() in {"1", "true", "yes", "y"}
SIMILARITY_LIST_LIMIT = int(os.getenv("SIMILARITY_LIST_LIMIT", "200"))
GROUP_TABLE_ROW_LIMIT = int(os.getenv("GROUP_TABLE_ROW_LIMIT", "1000"))
# V186: skip heavy plots when their page is hidden. Opt-in for native migration,
# on by default in the Docker pilot; no KPI or data filter rules are changed.
LAZY_PAGE_FIGURES = os.getenv("LAZY_PAGE_FIGURES", "true").lower() in {"1", "true", "yes", "y"}
HARMONY_TRANSACTION_TABLE_LIMIT = int(
    os.getenv("HARMONY_TRANSACTION_TABLE_LIMIT", "500")
)
# Geospatial detail uses Plotly MapLibre, not Mapbox.
# Default OpenStreetMap gives the normal geographic backdrop (islands/roads) without an API key.
# Imported SHP block boundaries, TPH anchors/radius, and reused occurrences are layered above it.
def _safe_geo_basemap_style(value: str | None) -> str:
    style = str(value or "open-street-map").strip().lower()
    allowed = {"white-bg", "open-street-map", "carto-positron", "carto-darkmatter"}
    # Never allow Mapbox-token styles such as streets/satellite/outdoors/light/dark.
    return style if style in allowed else "open-street-map"


GEO_BASEMAP_STYLE = _safe_geo_basemap_style(os.getenv("GEO_BASEMAP_STYLE", "open-street-map"))
try:
    GEO_TPH_RADIUS_METERS = max(5.0, float(os.getenv("GEO_TPH_RADIUS_METERS", "30")))
except Exception:
    GEO_TPH_RADIUS_METERS = 30.0

_SYSTEM_VALUES = {
    str(option.get("value", "")).upper().strip()
    for option in (SYSTEM_OPTIONS or [])
    if option.get("value") not in (None, "")
}
if _SYSTEM_VALUES and DEFAULT_SYSTEM not in _SYSTEM_VALUES:
    DEFAULT_SYSTEM = "HARMONY" if "HARMONY" in _SYSTEM_VALUES else sorted(_SYSTEM_VALUES)[0]

# V15: semua frame tetap ditampilkan. Kecepatan didapat dari lazy thumbnail + cache/prebuild,
# bukan dari membatasi jumlah gambar. FRAME_GALLERY_LIMIT=0 berarti unlimited.
FRAME_GALLERY_LIMIT = int(os.getenv("FRAME_GALLERY_LIMIT", "0"))
THUMB_LOADING = os.getenv("THUMB_LOADING", "eager").lower().strip()  # eager | lazy

THUMB_CACHE_DIR = Path(
    os.getenv(
        "THUMB_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "thumb_cache"),
    )
)
THUMB_MAX_SIZE = int(os.getenv("THUMB_MAX_SIZE", "300"))
THUMB_JPEG_QUALITY = int(os.getenv("THUMB_JPEG_QUALITY", "70"))

app = Dash(__name__, title=APP_TITLE, suppress_callback_exceptions=True, update_title="")
server = app.server

# V186: HTTP timing is generated at the Flask response boundary; it includes
# callback calculations as well as request serialization, unlike SQL-only timing.
_LOG_HTTP = os.getenv("DASHBOARD_HTTP_TIMING", "true").lower() in {"1", "true", "yes"}
_HTTP_SLOW_MS = max(0, int(os.getenv("DASHBOARD_HTTP_SLOW_MS", "1000")))
_MEDIA_GATEWAY_URL = os.getenv("MEDIA_GATEWAY_URL", "").strip().rstrip("/")
_MEDIA_GATEWAY_TOKEN = os.getenv("MEDIA_GATEWAY_TOKEN", "").strip()
_MEDIA_GATEWAY_TIMEOUT = max(1, int(os.getenv("MEDIA_GATEWAY_TIMEOUT", "20")))
_MEDIA_GATEWAY_MAX_BYTES = max(1024, int(os.getenv("MEDIA_GATEWAY_MAX_BYTES", str(70 * 1024 * 1024))))
if _MEDIA_GATEWAY_URL and not _MEDIA_GATEWAY_TOKEN:
    raise RuntimeError("MEDIA_GATEWAY_TOKEN wajib jika MEDIA_GATEWAY_URL diaktifkan")

@server.before_request
def _dashboard_request_started():
    if _LOG_HTTP:
        g.ge_request_started = time.perf_counter()

@server.after_request
def _dashboard_request_finished(response):
    if not _LOG_HTTP:
        return response
    start = getattr(g, "ge_request_started", None)
    if start is None:
        return response
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    response.headers["X-GoldenEye-Server-Ms"] = f"{elapsed_ms:.1f}"
    length = response.calculate_content_length()
    if length is not None:
        response.headers["X-GoldenEye-Response-Bytes"] = str(length)
    if elapsed_ms >= _HTTP_SLOW_MS or (length or 0) > 1024 * 1024:
        print(
            f"[DASHBOARD PERF] path={request.path[:100]} status={response.status_code} "
            f"ms={elapsed_ms:.0f} bytes={length if length is not None else '-'}",
            flush=True,
        )
    return response

@server.get("/api/healthz")
def dashboard_healthz():
    return jsonify({"status": "UP", "service": "dashboard"})

@server.get("/api/readyz")
def dashboard_readyz():
    try:
        ping()
        return jsonify({"status": "READY", "service": "dashboard"})
    except Exception:
        return jsonify({"status": "NOT_READY", "service": "dashboard"}), 503


def _proxy_media(image_key: str, image_type: str) -> Response:
    """Forward only the predeclared image endpoints to the trusted Windows NAS gateway.

    Images are fetched on the Windows host where original NAS paths are valid.
    No user-supplied URL or arbitrary filesystem path can be proxied.
    """
    quoted_key = urllib.parse.quote(str(image_key), safe="")
    url = f"{_MEDIA_GATEWAY_URL}/api/{image_type}/{quoted_key}"
    req = urllib.request.Request(
        url,
        headers={"X-GoldenEye-Gateway-Token": _MEDIA_GATEWAY_TOKEN},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_MEDIA_GATEWAY_TIMEOUT) as upstream:
            payload = upstream.read(_MEDIA_GATEWAY_MAX_BYTES + 1)
            if len(payload) > _MEDIA_GATEWAY_MAX_BYTES:
                raise ValueError("upstream image exceeds limit")
            response = Response(
                payload,
                status=upstream.status,
                content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            )
            for header in ("Cache-Control", "X-Golden-Eye-Image"):
                if upstream.headers.get(header):
                    response.headers[header] = upstream.headers[header]
            response.headers["X-GoldenEye-Media-Source"] = "windows-gateway"
            return response
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"[MEDIA GATEWAY WARN] kind={image_type} reason={type(exc).__name__}", flush=True)
        return Response("Media gateway unavailable", status=503, mimetype="text/plain")

if os.getenv("PRELOAD_DATA_ON_START", "false").lower() in {"1", "true", "yes", "y"}:
    preload_all_data()


def _safe_image_key(image_key: str) -> str:
    return str(image_key or "").strip().strip('"').strip("'")


def _missing_image_response() -> Response:
    """Placeholder netral untuk bukti yang memang belum tersedia.

    Endpoint tetap mengembalikan gambar valid (HTTP 200), sehingga browser
    tidak menampilkan broken-image/error merah. File yang ada tetap dicari
    lebih dahulu melalui indeks NAS dan hot store.
    """
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="440" viewBox="0 0 640 440">
    <rect width="640" height="440" fill="#f8fafc"/>
    <rect x="18" y="18" width="604" height="404" rx="20" fill="none" stroke="#cbd5e1" stroke-width="3"/>
    <text x="320" y="220" dominant-baseline="middle" text-anchor="middle" fill="#64748b" font-family="Arial" font-size="24" font-weight="700">Foto tidak tersedia</text>
    </svg>"""
    response = Response(svg, status=200, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Golden-Eye-Image"] = "missing"
    return response


@server.route("/api/image/<path:image_key>")
def api_image(image_key: str):
    if _MEDIA_GATEWAY_URL:
        return _proxy_media(image_key, "image")
    key = _safe_image_key(image_key)
    row = fetch_image_bytes(key)
    if not row:
        return _missing_image_response()
    image_bytes, mime_type, _file_name = row
    return Response(bytes(image_bytes), mimetype=mime_type or "image/jpeg")


def _thumb_path(image_key: str) -> Path:
    digest = hashlib.sha1(str(image_key).encode("utf-8", errors="replace")).hexdigest()
    return THUMB_CACHE_DIR / f"{digest}.jpg"


@server.route("/api/thumb/<path:image_key>")
def api_thumb(image_key: str):
    if _MEDIA_GATEWAY_URL:
        return _proxy_media(image_key, "thumb")
    return _thumbnail_response(image_key, fetch_image_bytes)


@server.route("/api/corpus-thumb/<path:image_key>")
def api_corpus_thumb(image_key: str):
    if _MEDIA_GATEWAY_URL:
        return _proxy_media(image_key, "corpus-thumb")
    return _thumbnail_response(image_key, fetch_corpus_image_bytes)


def _thumbnail_response(image_key: str, fetcher):
    key = _safe_image_key(image_key)
    try:
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _thumb_path(key)
        if p.exists() and p.stat().st_size > 0:
            resp = Response(p.read_bytes(), mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
            resp.headers["X-GoldenEye-Thumb-Cache"] = "HIT"
            return resp
    except Exception:
        p = None

    row = fetcher(key)
    if not row:
        return _missing_image_response()
    image_bytes, mime_type, _file_name = row
    raw = bytes(image_bytes)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
        data = out.getvalue()
        if p:
            try:
                p.write_bytes(data)
            except Exception:
                pass
        resp = Response(data, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
        resp.headers["X-GoldenEye-Thumb-Cache"] = "BUILT"
        return resp
    except Exception:
        resp = Response(raw, mimetype=mime_type or "image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp


def fmt_num(v: Any, decimals: int = 0) -> str:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "0"
        v = float(v)
        if decimals:
            return f"{v:,.{decimals}f}"
        return f"{v:,.0f}"
    except Exception:
        return str(v or "0")


def fmt_optional_num(v: Any, decimals: int = 0) -> str:
    """Format optional operational counts without turning missing data into zero."""
    if v is None:
        return "-"
    try:
        if pd.isna(v) or str(v).strip() == "":
            return "-"
    except Exception:
        pass
    try:
        number = float(v)
        return f"{number:,.{decimals}f}" if decimals else f"{number:,.0f}"
    except Exception:
        return display_text(v)


def fmt_similarity(v: Any) -> str:
    try:
        if v is None or pd.isna(v):
            return "-"
        return f"{float(v):.2f}%"
    except Exception:
        return "-"


def kpi_card(label: str, value: Any, note: str = "", tone: str = ""):
    # V23: KPI dibuat clean. Tidak ada tulisan kecil di bawah angka.
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div(fmt_num(value, 2 if isinstance(value, float) and label.lower().startswith("avg") else 0), className="kpi-value"),
    ], className=f"kpi-card {tone}".strip())


def transaction_kpi_card(label: str, value: Any, note: str = "", tone: str = ""):
    """KPI Panen vs Transport: label + angka saja agar dashboard tetap clean."""
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div(
            value if isinstance(value, str) else fmt_num(value, 0),
            className="kpi-value",
        ),
    ], className=f"kpi-card transaction-kpi-card {tone}".strip())


def transaction_kpi_section(title: str, cards: list[Any], note: str = ""):
    return html.Div([
        html.Div([
            html.Div(title, className="transaction-kpi-section-title"),
        ], className="transaction-kpi-section-head"),
        html.Div(cards, className="transaction-kpi-grid"),
    ], className="transaction-kpi-section")


def _readout_chip(label: str, value: Any, tone: str = ""):
    return html.Div([
        html.Span(label, className="readout-chip-label"),
        html.Span(display_text(value), className="readout-chip-value"),
    ], className=f"readout-chip {tone}".strip())


def transaction_journey(
    panen_count: int,
    transport_count: int,
    ready_count: int,
    same_count: int,
    different_count: int,
    transport_missing_count: int = 0,
    pending_similarity_count: int = 0,
    panen_coverage_pct: float | None = None,
    transport_coverage_pct: float | None = None,
    similarity_coverage_pct: float | None = None,
):
    def _pct(v):
        return "" if v is None else f"{float(v):.1f}%"

    steps = [
        ("01", "Foto Panen", f"{int(panen_count):,}", _pct(panen_coverage_pct), "blue"),
        ("02", "Foto Transport", f"{int(transport_count):,}", _pct(transport_coverage_pct), "teal"),
        ("03", "Similarity", f"{int(ready_count):,}", _pct(similarity_coverage_pct), "purple"),
    ]
    step_nodes = []
    for no, label, value, pct, tone in steps:
        step_nodes.append(html.Div([
            html.Div(no, className="journey-step-no"),
            html.Div([
                html.Div(label, className="journey-step-label"),
                html.Div(value, className="journey-step-value"),
                html.Div(pct, className="journey-step-pct") if pct else None,
            ], className="journey-step-copy"),
        ], className=f"journey-step {tone}"))

    return html.Div([
        html.Div(step_nodes, className="transaction-journey-steps"),
        html.Div([
            html.Div([
                html.Div("SAMA", className="journey-result-label"),
                html.Div(f"{int(same_count):,}", className="journey-result-value"),
            ], className="journey-result same"),
            html.Div([
                html.Div("BERBEDA", className="journey-result-label"),
                html.Div(f"{int(different_count):,}", className="journey-result-value"),
            ], className="journey-result different"),
        ], className="transaction-journey-results"),
        html.Div([
            _readout_chip("Tanpa Transport", f"{int(transport_missing_count):,}", "warning"),
            _readout_chip("Belum Similarity", f"{int(pending_similarity_count):,}", "warning"),
        ], className="transaction-journey-foot"),
    ], className="transaction-journey")


def card(children, title: str = "", note: str = "", class_name: str = ""):
    body = []
    if title or note:
        body.append(html.Div([
            html.Div(title, className="section-title"),
            html.Div(note, className="section-note"),
        ], className="section-head"))
    if isinstance(children, list):
        body.extend(children)
    else:
        body.append(children)
    return html.Div(body, className=f"card {class_name}".strip())


def empty_fig(title: str = "Tidak ada data"):
    fig = go.Figure()
    fig.update_layout(
        title=None,
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=32, r=24, t=18, b=32),
        height=330,
        annotations=[dict(text="Tidak ada data", x=0.5, y=0.5, showarrow=False, font=dict(color="#64748b", size=15))],
    )
    return fig


def _short_label(v: Any, max_len: int = 34) -> str:
    s = display_text(v, "")
    if not s:
        return "-"
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _unique_count_from_list(value: Any) -> int:
    vals = split_meta_values(value)
    seen = []
    for v in vals:
        key = v.upper()
        if key not in seen:
            seen.append(key)
    return len(seen)


def _compact_list(value: Any, max_items: int = 3, max_len: int = 42) -> str:
    vals = split_meta_values(value)
    if not vals:
        return "-"
    shown = vals[:max_items]
    txt = ", ".join(shown)
    if len(vals) > max_items:
        txt += f" +{len(vals)-max_items}"
    return _short_label(txt, max_len)


def prepare_group_list(groups: pd.DataFrame | None) -> pd.DataFrame:
    """HTML-like Reused Group list.

    Satu row = satu group. Count utama sudah terlihat di list sehingga tidak perlu halaman count terpisah.
    Klik row langsung membuka Detail Group.
    """
    base_cols = [
        "no", "source", "group_id", "representative_image", "usage_count", "total_bunch_pay",
        "location", "category", "similarity", "transport", "quick_read", "indicator",
        "count_summary", "people_summary", "sample_oph", "sample_tanggal", "group_key"
    ]
    if groups is None or groups.empty:
        return pd.DataFrame(columns=base_cols)

    df = groups.sort_values(["usage_count", "group_id"], ascending=[False, True]).copy().reset_index(drop=True)
    out = pd.DataFrame()
    out["no"] = range(1, len(df) + 1)

    sys_col = df["system"] if "system" in df.columns else pd.Series(["-"] * len(df))
    comp_col = df["source_company"] if "source_company" in df.columns else (df["company"] if "company" in df.columns else pd.Series(["-"] * len(df)))
    out["source"] = [f"{display_text(a)} / {display_text(b)}" for a, b in zip(sys_col, comp_col)]
    out["group_id"] = df.get("group_id", "").apply(lambda v: display_text(v)) if "group_id" in df.columns else "-"
    out["representative_image"] = df.get("representative_image", "").apply(lambda v: _short_label(v, 52)) if "representative_image" in df.columns else "-"
    out["usage_count"] = pd.to_numeric(df.get("usage_count", 0), errors="coerce").fillna(0).astype(int)
    out["total_bunch_pay"] = total_bunch_pay_series(df).round(2)

    estate = df.get("estate_list", df.get("estate_primary", pd.Series([""] * len(df))))
    afd = df.get("afdeling_list", df.get("afdeling_primary", pd.Series([""] * len(df))))
    blok = df.get("blok_list", pd.Series([""] * len(df)))
    tph = df.get("tph_list", pd.Series([""] * len(df)))
    out["location"] = [
        f"{_compact_list(e, 2, 16)} / Afd {_compact_list(a, 3, 18)} / Blok {_compact_list(b, 3, 24)} / TPH {_compact_list(t, 3, 20)}"
        for e, a, b, t in zip(estate, afd, blok, tph)
    ]

    reuse = df.get("reuse_category", pd.Series([""] * len(df)))
    datecat = df.get("date_category", pd.Series([""] * len(df)))
    out["category"] = [f"{display_text(r)} | {display_text(d)}" for r, d in zip(reuse, datecat)]
    out["similarity"] = df.get("similarity_list", df.get("similarity_primary", pd.Series([""] * len(df)))).apply(lambda v: _compact_list(v, 2, 36))
    out["transport"] = df.get("status_transport_list", pd.Series([""] * len(df))).apply(lambda v: _compact_list(v, 2, 36))

    tanggal_count = df["sql_date_list"].apply(_unique_count_from_list) if "sql_date_list" in df.columns else pd.Series([0] * len(df))
    blok_count = df["blok_list"].apply(_unique_count_from_list) if "blok_list" in df.columns else pd.Series([0] * len(df))
    tph_count = df["tph_list"].apply(_unique_count_from_list) if "tph_list" in df.columns else pd.Series([0] * len(df))
    oph_count = df["oph_list"].apply(_unique_count_from_list) if "oph_list" in df.columns else pd.Series([0] * len(df))
    pemanen_count = df["pemanen_list"].apply(_unique_count_from_list) if "pemanen_list" in df.columns else pd.Series([0] * len(df))
    kerani_count = df["kerani_panen_list"].apply(_unique_count_from_list) if "kerani_panen_list" in df.columns else pd.Series([0] * len(df))
    mandor_count = df["mandor_list"].apply(_unique_count_from_list) if "mandor_list" in df.columns else pd.Series([0] * len(df))

    out["count_summary"] = [
        f"OPH {int(o)} • Tgl {int(tg)} • Blok {int(b)} • TPH {int(tp)}"
        for o, tg, b, tp in zip(oph_count, tanggal_count, blok_count, tph_count)
    ]
    out["quick_read"] = [
        f"{int(u)}× dipakai • {int(o)} OPH • {int(tg)} tanggal • {int(b)} blok"
        for u, o, tg, b in zip(out["usage_count"], oph_count, tanggal_count, blok_count)
    ]
    out["indicator"] = [
        " • ".join([x for x in [display_text(r, ""), display_text(d, "")] if x]) or "-"
        for r, d in zip(reuse, datecat)
    ]
    out["people_summary"] = [
        f"Pemanen {int(p)} • Kerani {int(k)} • Mandor {int(m)}"
        for p, k, m in zip(pemanen_count, kerani_count, mandor_count)
    ]
    out["sample_oph"] = df.get("oph_list", pd.Series([""] * len(df))).apply(lambda v: _compact_list(v, 2, 56))
    out["sample_tanggal"] = df.get("sql_date_list", pd.Series([""] * len(df))).apply(lambda v: _compact_list(v, 3, 44))
    out["bunch_pay_source"] = df.get("total_bunch_pay_source", pd.Series([""] * len(df))).astype(str)
    out["group_key"] = df.get("group_key", df.get("group_id", pd.Series([""] * len(df)))).astype(str)
    return out

def _numeric_series(df: pd.DataFrame | None, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series([default] * (0 if df is None else len(df)))
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def enrich_bunch_pay(groups: pd.DataFrame | None, detail: pd.DataFrame | None = None) -> pd.DataFrame:
    """Pastikan Total Bunch Pay punya sumber yang jelas.

    Prioritas nilai group:
      1. Kolom group summary jika memang ada nilainya.
      2. Rekalkulasi dari detail.bunch_pay per group_key.

    detail.bunch_pay sudah dinormalisasi dari kolom CSV/Excel seperti
    bunch_pay_total_per_oph, Bunch Pay / OPH, jjg_kerani_panen_lookup, bunch, atau std_bunch.
    """
    if groups is None or groups.empty:
        return groups if groups is not None else pd.DataFrame()
    out = groups.copy()
    explicit = pd.to_numeric(out.get("total_bunch_pay_group", 0), errors="coerce").fillna(0)
    out["total_bunch_pay_group_raw"] = explicit
    out["total_bunch_pay_calc"] = 0.0

    if detail is not None and not detail.empty and "group_key" in detail.columns:
        d = detail.copy()
        if "bunch_pay" not in d.columns:
            d["bunch_pay"] = 0
        d["bunch_pay_num"] = pd.to_numeric(d["bunch_pay"], errors="coerce").fillna(0)
        sums = d.groupby(d["group_key"].astype(str))["bunch_pay_num"].sum()
        out["total_bunch_pay_calc"] = out["group_key"].astype(str).map(sums).fillna(0.0) if "group_key" in out.columns else 0.0

    calc = pd.to_numeric(out["total_bunch_pay_calc"], errors="coerce").fillna(0)
    out["total_bunch_pay_effective"] = explicit.where(explicit > 0, calc)
    out["total_bunch_pay_source"] = [
        "group_summary" if e > 0 else ("detail_bunch_pay" if c > 0 else "not_available")
        for e, c in zip(explicit, calc)
    ]
    return out


def total_bunch_pay_series(groups: pd.DataFrame | None) -> pd.Series:
    if groups is None or groups.empty:
        return pd.Series(dtype=float)
    col = "total_bunch_pay_effective" if "total_bunch_pay_effective" in groups.columns else "total_bunch_pay_group"
    return pd.to_numeric(groups.get(col, 0), errors="coerce").fillna(0)


def style_fig(
    fig,
    height: int = 360,
    show_legend: bool = True,
    margin: dict | None = None,
    x_title: str = "",
    y_title: str = "",
):
    # Chart title tidak dipakai di dalam Plotly supaya tidak nabrak dengan judul card.
    fig.update_layout(
        title=None,
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Segoe UI, Arial", size=11, color="#111827"),
        margin=margin or dict(l=56, r=34, t=14, b=54),
        height=height,
        hovermode="closest",
        uniformtext_minsize=10,
        uniformtext_mode="hide",
        transition_duration=120,
    )
    fig.update_xaxes(
        title_text=x_title,
        showgrid=True,
        gridcolor="#edf2f7",
        zeroline=False,
        automargin=True,
        tickfont=dict(size=10),
        title=dict(font=dict(size=11, color="#475569")),
    )
    fig.update_yaxes(
        title_text=y_title,
        showgrid=True,
        gridcolor="#edf2f7",
        zeroline=False,
        automargin=True,
        tickfont=dict(size=10),
        title=dict(font=dict(size=11, color="#475569")),
    )
    if show_legend:
        fig.update_layout(
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.16,
                xanchor="left",
                x=0,
                font=dict(size=10),
                itemwidth=30,
            )
        )
    else:
        fig.update_layout(showlegend=False)
    return fig




def _geo_norm(value: Any) -> str:
    import re as _re
    return _re.sub(r"[^A-Z0-9]", "", display_text(value, "").upper())



def _geo_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters; accurate enough for TPH audit radius."""
    r = 6371008.8
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _geo_circle(lat: float, lon: float, radius_m: float, steps: int = 64) -> tuple[list[float], list[float]]:
    """Return an approximate geodesic circle around a TPH anchor."""
    earth = 6371008.8
    lat1 = math.radians(float(lat))
    lon1 = math.radians(float(lon))
    angular = float(radius_m) / earth
    out_lat: list[float] = []
    out_lon: list[float] = []
    for i in range(steps + 1):
        bearing = 2 * math.pi * i / steps
        lat2 = math.asin(
            math.sin(lat1) * math.cos(angular)
            + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
        )
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(angular) * math.cos(lat1),
            math.cos(angular) - math.sin(lat1) * math.sin(lat2),
        )
        out_lat.append(math.degrees(lat2))
        out_lon.append(math.degrees(lon2))
    return out_lat, out_lon


def _match_tph_anchor(row: pd.Series, tph_points: pd.DataFrame) -> pd.Series | None:
    if tph_points is None or tph_points.empty:
        return None
    tn = row.get("_tph_norm", "")
    bn = row.get("_blok_norm", "")
    en = row.get("_estate_norm", "")
    an = row.get("_afdeling_norm", "")
    if not tn:
        return None
    cand = tph_points[tph_points["tph_norm"].eq(tn)].copy()
    if bn:
        cand = cand[cand["blok_norm"].eq(bn)]
    exact = cand.copy()
    if en:
        exact = exact[exact["estate_norm"].eq(en)]
    if an:
        exact = exact[exact["afdeling_norm"].eq(an)]
    if len(exact) == 1:
        return exact.iloc[0]
    # Fallback is allowed only when Block+TPH uniquely identifies one anchor.
    if len(cand) == 1:
        return cand.iloc[0]
    return None


def reused_group_location_panel(company: str, det: pd.DataFrame, group_row: Any):
    """Contextual geospatial view for one reused group.

    The map answers one audit question only: where did this same reused image
    appear? Exact transaction GPS is preferred. When a transaction has no GPS,
    a matched block centroid may be shown as an area reference and is labelled
    explicitly as non-exact.
    """
    group_id = display_text(group_row.get("group_id") if group_row is not None else "", "-")
    company = str(company or "").upper().strip()
    if det is None or det.empty:
        return html.Div(
            "Lokasi perulangan belum tersedia karena detail transaksi group ini kosong.",
            className="muted big-empty",
        )

    data = det.copy().reset_index(drop=True)
    for col in ["estate", "afdeling", "blok", "tph", "oph", "tanggal", "latitude", "longitude"]:
        if col not in data.columns:
            data[col] = ""
    for col in ["estate", "afdeling", "blok", "tph"]:
        data[f"_{col}_norm"] = data[col].apply(_geo_norm)

    # Stable chronological occurrence number for audit readability.
    data["_date_sort"] = pd.to_datetime(data["tanggal"], errors="coerce")
    data["_original_order"] = range(len(data))
    data = data.sort_values(["_date_sort", "_original_order"], na_position="last").reset_index(drop=True)
    data["occurrence_no"] = range(1, len(data) + 1)
    data["latitude_num"] = pd.to_numeric(data["latitude"], errors="coerce")
    data["longitude_num"] = pd.to_numeric(data["longitude"], errors="coerce")
    data["gps_valid"] = data["latitude_num"].between(-90, 90) & data["longitude_num"].between(-180, 180)

    # TPH reference is inferred from historical transaction GPS for the same
    # Company/Estate/Afdeling/Block/TPH. It is an audit anchor, not a surveyed
    # cadastral point. Confidence/dispersion are retained in PostgreSQL.
    tph_points = load_geo_tph_points(company)
    if tph_points is None:
        tph_points = pd.DataFrame()
    if not tph_points.empty:
        for col in ["estate_norm", "afdeling_norm", "blok_norm", "tph_norm"]:
            if col not in tph_points.columns:
                tph_points[col] = ""
            tph_points[col] = tph_points[col].fillna("").astype(str)

    data["tph_anchor_id"] = ""
    data["tph_anchor_lat"] = pd.NA
    data["tph_anchor_lon"] = pd.NA
    data["tph_anchor_confidence"] = ""
    data["tph_anchor_observations"] = 0
    data["tph_anchor_dispersion_p90_m"] = pd.NA
    for idx, row in data.iterrows():
        anchor = _match_tph_anchor(row, tph_points)
        if anchor is None:
            continue
        data.at[idx, "tph_anchor_id"] = str(anchor.get("tph_point_id") or "")
        data.at[idx, "tph_anchor_lat"] = anchor.get("latitude")
        data.at[idx, "tph_anchor_lon"] = anchor.get("longitude")
        data.at[idx, "tph_anchor_confidence"] = str(anchor.get("confidence") or "")
        data.at[idx, "tph_anchor_observations"] = int(anchor.get("observation_count") or 0)
        data.at[idx, "tph_anchor_dispersion_p90_m"] = anchor.get("dispersion_p90_m")
    data["tph_anchor_lat"] = pd.to_numeric(data["tph_anchor_lat"], errors="coerce")
    data["tph_anchor_lon"] = pd.to_numeric(data["tph_anchor_lon"], errors="coerce")
    data["tph_anchor_valid"] = data["tph_anchor_lat"].between(-90, 90) & data["tph_anchor_lon"].between(-180, 180)
    data["distance_to_tph_m"] = pd.NA
    for idx, row in data[data["gps_valid"] & data["tph_anchor_valid"]].iterrows():
        data.at[idx, "distance_to_tph_m"] = _geo_distance_m(
            row["latitude_num"], row["longitude_num"], row["tph_anchor_lat"], row["tph_anchor_lon"]
        )
    data["distance_to_tph_m"] = pd.to_numeric(data["distance_to_tph_m"], errors="coerce")

    boundaries = load_geo_boundaries(company)
    matched = pd.DataFrame()
    if boundaries is not None and not boundaries.empty:
        b = boundaries.copy()
        for col in ["estate_norm", "afdeling_norm", "blok_norm"]:
            if col not in b.columns:
                b[col] = ""
            b[col] = b[col].fillna("").astype(str)
        block_boundary_counts = b.groupby("blok_norm")["boundary_id"].transform("count")
        b["_block_unique"] = block_boundary_counts.eq(1)
        selected_ids = set()
        occurrence_counts: dict[str, int] = {}
        for _, row in data.iterrows():
            bn = row.get("_blok_norm", "")
            if not bn:
                continue
            en = row.get("_estate_norm", "")
            an = row.get("_afdeling_norm", "")
            exact = b[b["blok_norm"].eq(bn)].copy()
            if en:
                exact = exact[exact["estate_norm"].eq(en)]
            if an:
                exact = exact[exact["afdeling_norm"].eq(an)]
            if exact.empty:
                exact = b[b["blok_norm"].eq(bn) & b["_block_unique"]].copy()
            for bid in exact.get("boundary_id", pd.Series(dtype=str)).astype(str).tolist():
                selected_ids.add(bid)
                occurrence_counts[bid] = occurrence_counts.get(bid, 0) + 1
        if selected_ids:
            matched = b[b["boundary_id"].astype(str).isin(selected_ids)].copy()
            matched["occurrence_count"] = matched["boundary_id"].astype(str).map(occurrence_counts).fillna(0).astype(int)

    fig = go.Figure()
    map_lats: list[float] = []
    map_lons: list[float] = []

    # Highlight only block boundaries involved in this group, not the whole estate.
    if matched is not None and not matched.empty:
        features = []
        locations = []
        z = []
        custom = []
        for _, row in matched.iterrows():
            geom = row.get("geometry_geojson")
            if isinstance(geom, str):
                try:
                    import json as _json
                    geom = _json.loads(geom)
                except Exception:
                    geom = None
            if not isinstance(geom, dict):
                continue
            bid = str(row.get("boundary_id") or "")
            features.append({"type": "Feature", "id": bid, "properties": {"boundary_id": bid}, "geometry": geom})
            locations.append(bid)
            z.append(int(row.get("occurrence_count") or 0))
            custom.append([
                display_text(row.get("estate"), "-"),
                display_text(row.get("afdeling"), "-"),
                display_text(row.get("blok"), "-"),
                int(row.get("occurrence_count") or 0),
            ])
            try:
                if pd.notna(row.get("centroid_lat")) and pd.notna(row.get("centroid_lon")):
                    map_lats.append(float(row.get("centroid_lat")))
                    map_lons.append(float(row.get("centroid_lon")))
            except Exception:
                pass
        if features:
            fig.add_trace(go.Choroplethmap(
                geojson={"type": "FeatureCollection", "features": features},
                locations=locations,
                z=z,
                featureidkey="properties.boundary_id",
                colorscale="Blues",
                marker_opacity=0.34,
                marker_line_width=1.8,
                showscale=False,
                customdata=custom,
                hovertemplate=(
                    "<b>Blok %{customdata[2]}</b><br>"
                    "Estate %{customdata[0]} · Afd %{customdata[1]}<br>"
                    "Kemunculan group di area ini: %{customdata[3]:,}<extra></extra>"
                ),
                name="Boundary lokasi reused",
            ))

    # TPH anchor + operational radius. One circle per unique canonical TPH
    # involved in this reused group. This is intentionally separate from the
    # exact transaction GPS markers.
    tph_draw = data[data["tph_anchor_valid"] & data["tph_anchor_id"].ne("")].copy()
    if not tph_draw.empty:
        tph_draw = tph_draw.drop_duplicates(subset=["tph_anchor_id"]).copy()
        for _, tr in tph_draw.iterrows():
            lat0 = float(tr["tph_anchor_lat"])
            lon0 = float(tr["tph_anchor_lon"])
            circle_lat, circle_lon = _geo_circle(lat0, lon0, GEO_TPH_RADIUS_METERS)
            fig.add_trace(go.Scattermap(
                lat=circle_lat, lon=circle_lon, mode="lines", fill="toself",
                line=dict(width=1.5), opacity=0.18, hoverinfo="skip", showlegend=False,
                name=f"Radius TPH {display_text(tr.get('tph'), '-')}",
            ))
            dispersion = pd.to_numeric(pd.Series([tr.get("tph_anchor_dispersion_p90_m")]), errors="coerce").iloc[0]
            dispersion_text = f"{float(dispersion):.1f} m" if pd.notna(dispersion) else "-"
            fig.add_trace(go.Scattermap(
                lat=[lat0], lon=[lon0], mode="markers+text",
                text=[f"TPH {display_text(tr.get('tph'), '-')}"] if len(tph_draw) <= 12 else None,
                textposition="bottom center", marker=dict(size=14, opacity=0.95),
                hovertext=[
                    f"TPH {display_text(tr.get('tph'), '-')}"
                    f"<br>{display_text(tr.get('estate'), '-')} / Afd {display_text(tr.get('afdeling'), '-')} / Blok {display_text(tr.get('blok'), '-')}"
                    f"<br>Anchor dari {int(tr.get('tph_anchor_observations') or 0)} observasi GPS historis"
                    f"<br>Confidence {display_text(tr.get('tph_anchor_confidence'), '-')} · P90 spread {dispersion_text}"
                    f"<br>Radius audit {GEO_TPH_RADIUS_METERS:.0f} m"
                ],
                hovertemplate="%{hovertext}<extra></extra>", name="Titik referensi TPH",
            ))
            map_lats.extend(circle_lat)
            map_lons.extend(circle_lon)

    gps = data[data["gps_valid"]].copy()
    if not gps.empty:
        hover = []
        for _, row in gps.iterrows():
            dist = pd.to_numeric(pd.Series([row.get("distance_to_tph_m")]), errors="coerce").iloc[0]
            if pd.notna(dist):
                radius_status = "DALAM" if float(dist) <= GEO_TPH_RADIUS_METERS else "DI LUAR"
                tph_line = f"<br>Jarak ke TPH anchor {float(dist):.1f} m · {radius_status} radius {GEO_TPH_RADIUS_METERS:.0f} m"
            elif row.get("_tph_norm"):
                tph_line = "<br>TPH anchor belum tersedia"
            else:
                tph_line = ""
            hover.append(
                f"Kemunculan #{int(row['occurrence_no'])}"
                f"<br>Tanggal {display_text(row.get('tanggal'), '-')}"
                f"<br>OPH {display_text(row.get('oph'), '-')}"
                f"<br>{display_text(row.get('estate'), '-')} / Afd {display_text(row.get('afdeling'), '-')} / Blok {display_text(row.get('blok'), '-')} / TPH {display_text(row.get('tph'), '-')}"
                f"<br>GPS transaksi {float(row['latitude_num']):.6f}, {float(row['longitude_num']):.6f}"
                + tph_line
            )
        show_numbers = len(gps) <= 25
        fig.add_trace(go.Scattermap(
            lat=gps["latitude_num"],
            lon=gps["longitude_num"],
            mode="markers+text" if show_numbers else "markers",
            text=[str(int(v)) for v in gps["occurrence_no"]] if show_numbers else None,
            textposition="top center",
            marker=dict(size=11, opacity=0.9),
            hovertext=hover,
            hovertemplate="%{hovertext}<extra></extra>",
            name="GPS transaksi",
        ))
        map_lats.extend(gps["latitude_num"].astype(float).tolist())
        map_lons.extend(gps["longitude_num"].astype(float).tolist())

    # For locations with no transaction GPS, show one block-centroid marker only.
    # This is deliberately not presented as an exact photo coordinate.
    missing_gps = data[~data["gps_valid"] & data["_blok_norm"].ne("")].copy()
    centroid_rows = []
    if not missing_gps.empty and matched is not None and not matched.empty:
        for (en, an, bn), rows in missing_gps.groupby(["_estate_norm", "_afdeling_norm", "_blok_norm"], dropna=False):
            cand = matched[matched["blok_norm"].eq(bn)].copy()
            if en:
                cand = cand[cand["estate_norm"].eq(en)]
            if an:
                cand = cand[cand["afdeling_norm"].eq(an)]
            if cand.empty:
                cand = matched[matched["blok_norm"].eq(bn)].copy()
            if len(cand) != 1:
                continue
            br = cand.iloc[0]
            lat = pd.to_numeric(pd.Series([br.get("centroid_lat")]), errors="coerce").iloc[0]
            lon = pd.to_numeric(pd.Series([br.get("centroid_lon")]), errors="coerce").iloc[0]
            if pd.isna(lat) or pd.isna(lon):
                continue
            centroid_rows.append({
                "lat": float(lat), "lon": float(lon), "count": int(len(rows)),
                "estate": display_text(br.get("estate"), "-"),
                "afdeling": display_text(br.get("afdeling"), "-"),
                "blok": display_text(br.get("blok"), "-"),
            })
    if centroid_rows:
        cdf = pd.DataFrame(centroid_rows)
        fig.add_trace(go.Scattermap(
            lat=cdf["lat"], lon=cdf["lon"], mode="markers",
            marker=dict(size=15, opacity=0.45, symbol="circle"),
            hovertext=[
                f"Area Blok {r['blok']}<br>{r['estate']} / Afd {r['afdeling']}"
                f"<br>{r['count']} kemunculan tanpa GPS transaksi"
                "<br>Marker = centroid blok, bukan titik foto exact"
                for _, r in cdf.iterrows()
            ],
            hovertemplate="%{hovertext}<extra></extra>",
            name="Centroid blok (GPS tidak tersedia)",
        ))
        map_lats.extend(cdf["lat"].tolist())
        map_lons.extend(cdf["lon"].tolist())

    if map_lats and map_lons:
        lat_span = max(map_lats) - min(map_lats) if len(map_lats) > 1 else 0.02
        lon_span = max(map_lons) - min(map_lons) if len(map_lons) > 1 else 0.02
        span = max(lat_span, lon_span)
        zoom = 14 if span < 0.015 else 13 if span < 0.03 else 12 if span < 0.07 else 11 if span < 0.15 else 10
        center = {"lat": sum(map_lats) / len(map_lats), "lon": sum(map_lons) / len(map_lons)}
        fig.update_layout(
            map=dict(style=GEO_BASEMAP_STYLE, center=center, zoom=zoom),
            margin=dict(l=0, r=0, t=0, b=0), height=520,
            paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=0.01, xanchor="left", x=0.01),
            uirevision=f"reused-group-{group_id}",
        )
    else:
        fig = empty_fig("Koordinat GPS dan boundary block untuk group ini belum tersedia")
        fig.update_layout(height=420)

    # Transaction-level location trace; every row remains visible even if GPS is missing.
    location_rows = data.copy()
    location_rows["gps_status"] = location_rows["gps_valid"].map({True: "GPS transaksi", False: "Tanpa GPS"})
    location_rows["latitude"] = location_rows["latitude_num"].round(6)
    location_rows["longitude"] = location_rows["longitude_num"].round(6)
    location_rows["jarak_ke_tph_m"] = pd.to_numeric(location_rows["distance_to_tph_m"], errors="coerce").round(1)
    location_rows["radius_tph_status"] = ""
    has_dist = location_rows["jarak_ke_tph_m"].notna()
    location_rows.loc[has_dist, "radius_tph_status"] = location_rows.loc[has_dist, "jarak_ke_tph_m"].apply(
        lambda v: f"DALAM {GEO_TPH_RADIUS_METERS:.0f}m" if float(v) <= GEO_TPH_RADIUS_METERS else f"DI LUAR {GEO_TPH_RADIUS_METERS:.0f}m"
    )
    location_cols = ["occurrence_no", "tanggal", "estate", "afdeling", "blok", "tph", "oph", "gps_status", "latitude", "longitude", "jarak_ke_tph_m", "radius_tph_status"]
    location_rows = location_rows[[c for c in location_cols if c in location_rows.columns]].copy()

    unique_estate = data["_estate_norm"].replace("", pd.NA).dropna().nunique()
    unique_afdeling = data["_afdeling_norm"].replace("", pd.NA).dropna().nunique()
    unique_blok = data["_blok_norm"].replace("", pd.NA).dropna().nunique()
    exact_gps = int(data["gps_valid"].sum())
    boundary_count = int(len(matched)) if matched is not None else 0
    tph_anchor_count = int(data.loc[data["tph_anchor_valid"], "tph_anchor_id"].replace("", pd.NA).dropna().nunique())
    inside_radius = int((pd.to_numeric(data["distance_to_tph_m"], errors="coerce") <= GEO_TPH_RADIUS_METERS).fillna(False).sum())

    return html.Div([
        html.Div([
            html.Div([
                html.Div("REUSED LOCATION TRACE", className="eyebrow"),
                html.H4(f"Lokasi Perulangan Gambar · Group ID {group_id}"),
            ]),
            html.Div(
                "Layer: peta normal → boundary blok SHP → titik/radius TPH → GPS kemunculan reused.",
                className="section-note-left",
            ),
        ], className="reused-geo-head"),
        html.Div([
            kpi_card("Kemunculan", len(data)),
            kpi_card("Estate", unique_estate),
            kpi_card("Afdeling", unique_afdeling),
            kpi_card("Blok", unique_blok),
            kpi_card("GPS Transaksi", exact_gps),
            kpi_card("Titik TPH", tph_anchor_count),
            kpi_card(f"Dalam Radius {GEO_TPH_RADIUS_METERS:.0f}m", inside_radius),
            kpi_card("Boundary Match", boundary_count),
        ], className="mini-kpi-row reused-geo-kpis"),
        dcc.Graph(
            figure=fig,
            config={"displaylogo": False, "displayModeBar": True, "responsive": True, "scrollZoom": True},
            className="reused-location-map",
        ),
        html.Div(
            f"Marker bernomor = GPS transaksi. Titik TPH adalah median GPS historis TPH yang sama; lingkaran = radius audit {GEO_TPH_RADIUS_METERS:.0f} m. Centroid blok hanya fallback bila GPS transaksi/TPH tidak tersedia.",
            className="geo-location-note",
        ),
        html.Details([
            html.Summary("Lihat daftar lokasi per kemunculan"),
            html.Div(table(location_rows, page_size=15), className="reused-location-table"),
        ], open=False, className="reused-location-details"),
    ], className="reused-location-panel")


def records(df: pd.DataFrame | None):
    if df is None or df.empty:
        return []
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    return out.fillna("").to_dict("records")


def table(
    df: pd.DataFrame | None,
    table_id: str | None = None,
    selectable: bool = False,
    page_size: int = 12,
    style_data_conditional_extra: list[dict[str, Any]] | None = None,
):
    if df is None:
        df = pd.DataFrame()
    # Jangan tampilkan kolom teknis terlalu banyak di awal, tapi tetap pertahankan data penting.
    cols = [{"name": str(c).replace("_", " ").title(), "id": str(c)} for c in df.columns]
    props = dict(
        data=records(df),
        columns=cols,
        page_size=page_size,
        filter_action="native",
        sort_action="native",
        style_as_list_view=True,
        style_table={"overflowX": "auto", "width": "100%"},
        style_header={"fontWeight": "800", "backgroundColor": "#f8fafc", "border": "1px solid #e2e8f0"},
        style_cell={
            "fontFamily": "Inter, Segoe UI, Arial",
            "fontSize": 12,
            "padding": "8px 10px",
            "minWidth": "96px",
            "maxWidth": "280px",
            "whiteSpace": "normal",
            "height": "auto",
        },
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#fbfdff"},
            *((style_data_conditional_extra or [])),
            {"if": {"state": "selected"}, "backgroundColor": "#dbeafe", "border": "1px solid #2563eb"},
        ],
    )
    if table_id:
        props["id"] = table_id
    if selectable:
        props["row_selectable"] = "single"
        props["selected_rows"] = []
    return dash_table.DataTable(**props)


# Dash html.Img versi yang Bapak pakai tidak mendukung prop `loading`.
# Jadi lazy-loading dibuat sendiri dengan assets/lazy_images.js.
# Semua frame tetap dirender, tetapi src thumbnail baru diisi ketika gambar mendekati viewport/terlihat.
PLACEHOLDER_SVG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220'>"
    "<rect width='100%25' height='100%25' fill='%23f8fafc'/>"
    "<text x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
    "fill='%2394a3b8' font-family='Arial' font-size='14'>Loading image...</text>"
    "</svg>"
)




def _count_unique_from_series(series: pd.Series | None) -> int:
    if series is None:
        return 0
    vals = []
    try:
        iterable = series.tolist()
    except Exception:
        iterable = []
    for v in iterable:
        s = display_text(v, "")
        if s and s.upper() not in [x.upper() for x in vals]:
            vals.append(s)
    return len(vals)


def _detail_breakdown(det: pd.DataFrame, cols: list[str], name: str) -> pd.DataFrame:
    if det is None or det.empty:
        return pd.DataFrame(columns=[name, "total_transaksi", "unique_oph", "total_bunch_pay"])
    data = det.copy()
    for c in cols:
        if c not in data.columns:
            data[c] = "-"
        data[c] = data[c].apply(lambda v: display_text(v))
    if "oph" not in data.columns:
        data["oph"] = ""
    if "bunch_pay" not in data.columns:
        data["bunch_pay"] = 0
    data["bunch_pay_num"] = pd.to_numeric(data["bunch_pay"], errors="coerce").fillna(0)
    out = data.groupby(cols, dropna=False).agg(
        total_transaksi=(cols[0], "size"),
        unique_oph=("oph", lambda x: _count_unique_from_series(x)),
        total_bunch_pay=("bunch_pay_num", "sum"),
    ).reset_index()
    out = out.sort_values(["total_transaksi", "total_bunch_pay"], ascending=[False, False]).reset_index(drop=True)
    if len(cols) == 1:
        out = out.rename(columns={cols[0]: name})
    return out


def _group_list_count(row: Any, col: str) -> int:
    if row is None:
        return 0
    try:
        return _unique_count_from_list(row.get(col, ""))
    except Exception:
        return 0


def _sample_list(row: Any, col: str, max_items: int = 5, max_len: int = 110) -> str:
    if row is None:
        return "-"
    try:
        return _compact_list(row.get(col, ""), max_items=max_items, max_len=max_len)
    except Exception:
        return "-"


def group_count_panel(row: Any, det: pd.DataFrame, image_key: str):
    """Halaman antara sebelum Detail Group, mengikuti pola HTML lama: group dipilih dulu,
    user melihat count/ringkasan group, baru membuka detail frame transaksi.
    """
    if row is None:
        return card(html.Div("Pilih group dari chart Top Reused Photo atau tabel Reused Groups untuk melihat count summary.", className="muted big-empty"), "Group Summary")

    is_elhp = str(row.get("source_system", "")).upper().startswith("ELHP")
    detail_rows = 0 if det is None else int(len(det))
    usage_count = int(float(row.get("usage_count", 0) or 0))
    bunch = float(pd.to_numeric(pd.Series([row.get("total_bunch_pay_group", 0)]), errors="coerce").fillna(0).iloc[0])

    def det_unique(col, list_col):
        if det is not None and not det.empty and col in det.columns:
            c = _count_unique_from_series(det[col])
            if c:
                return c
        return _group_list_count(row, list_col)

    unique_oph = det_unique("oph", "oph_list")
    unique_tanggal = det_unique("tanggal", "sql_date_list")
    unique_blok = det_unique("blok", "blok_list")
    unique_tph = det_unique("tph", "tph_list")
    unique_pemanen = det_unique("pemanen", "pemanen_list")
    unique_kerani = det_unique("kerani_panen", "kerani_panen_list")
    unique_mandor = det_unique("mandor", "mandor_list")

    if is_elhp:
        transport_note = "ELHP N/A"
        transport_ada = "N/A"
        transport_missing = "N/A"
    else:
        has_kt = det["foto_kt_before"].apply(lambda v: bool(split_image_keys(v))).sum() if det is not None and not det.empty and "foto_kt_before" in det.columns else 0
        has_dt = det["foto_dt"].apply(lambda v: bool(split_image_keys(v))).sum() if det is not None and not det.empty and "foto_dt" in det.columns else 0
        has_ft = det["foto_ft"].apply(lambda v: bool(split_image_keys(v))).sum() if det is not None and not det.empty and "foto_ft" in det.columns else 0
        transport_ada = int(max(has_kt, has_dt, has_ft))
        transport_missing = max(0, detail_rows - transport_ada)
        transport_note = "Harmony only"

    list_rows = pd.DataFrame([
        {"dimensi": "OPH", "jumlah_unik": unique_oph, "sample": _sample_list(row, "oph_list", 3, 120)},
        {"dimensi": "Tanggal", "jumlah_unik": unique_tanggal, "sample": _sample_list(row, "sql_date_list", 5, 120)},
        {"dimensi": "Estate", "jumlah_unik": _group_list_count(row, "estate_list"), "sample": _sample_list(row, "estate_list", 5, 120)},
        {"dimensi": "Afdeling", "jumlah_unik": _group_list_count(row, "afdeling_list"), "sample": _sample_list(row, "afdeling_list", 5, 120)},
        {"dimensi": "Blok", "jumlah_unik": unique_blok, "sample": _sample_list(row, "blok_list", 5, 120)},
        {"dimensi": "TPH", "jumlah_unik": unique_tph, "sample": _sample_list(row, "tph_list", 5, 120)},
        {"dimensi": "Pemanen", "jumlah_unik": unique_pemanen, "sample": _sample_list(row, "pemanen_list", 3, 120)},
        {"dimensi": "Kerani Panen", "jumlah_unik": unique_kerani, "sample": _sample_list(row, "kerani_panen_list", 3, 120)},
        {"dimensi": "Mandor", "jumlah_unik": unique_mandor, "sample": _sample_list(row, "mandor_list", 3, 120)},
    ])

    by_date = _detail_breakdown(det, ["tanggal"], "tanggal").head(20)
    by_location = _detail_breakdown(det, ["estate", "afdeling", "blok", "tph"], "lokasi").head(30)
    by_people = _detail_breakdown(det, ["pemanen", "kerani_panen", "mandor"], "orang").head(30)

    return card([
        html.Div([
            html.Div([thumb_img(image_key, "main-image")], className="count-image-box"),
            html.Div([
                html.Div(f"{display_text(row.get('source_label', ''))} | Group ID: {display_text(row.get('group_id', ''))}", className="detail-title"),
                html.Div(display_text(row.get("representative_image", "")), className="detail-file"),
                html.Div([
                    kpi_card("Usage Count", usage_count, "jumlah reuse di group"),
                    kpi_card("Group Similarity", fmt_similarity(row.get("mean_embedding_similarity"))),
                    kpi_card("Min Member Similarity", fmt_similarity(row.get("min_member_mean_similarity"))),
                    kpi_card("Detail Rows", detail_rows, "baris transaksi"),
                    kpi_card("Unique OPH", unique_oph, "sebelum detail"),
                    kpi_card("Unique Tanggal", unique_tanggal, "sebelum detail"),
                    kpi_card("Unique Blok", unique_blok, "sebelum detail"),
                    kpi_card("Unique TPH", unique_tph, "sebelum detail"),
                    kpi_card("Total Bunch Pay", bunch, "group summary"),
                    kpi_card("Transport Ada", transport_ada, transport_note),
                    kpi_card("Transport Missing", transport_missing, transport_note),
                ], className="count-kpi-grid"),
                html.Div([
                    html.Button("Buka Detail Transaksi + Gambar", id="open-detail-button", n_clicks=0, className="primary-btn"),
                    html.Div("", className="section-note-left"),
                ], className="count-actions"),
            ], className="count-meta"),
        ], className="count-layout"),
        html.Div([
            html.Div(card(table(list_rows, page_size=12), "Count Dimensi Group"), className="count-col"),
            html.Div(card(table(by_date, page_size=10), "Count per Tanggal"), className="count-col"),
        ], className="grid two"),
        html.Div([
            card(table(by_location, page_size=12), "Count per Lokasi", "Estate / Afdeling / Blok / TPH"),
            card(table(by_people, page_size=12), "Count per Orang", "Pemanen / Kerani Panen / Mandor"),
        ], className="grid two"),
    ], "Group Summary", "", "selected-card count-card")

def thumb_img(image_key: str, class_name: str = "thumb-img"):
    key = str(image_key or "").strip()
    if not key:
        return html.Div("Tidak ada image_key", className="empty-image")

    img_props = {
        "src": PLACEHOLDER_SVG,
        "className": f"{class_name} lazy-thumb",
        "alt": key,
        "title": key,
        # data-* valid di Dash html components. JS akan mengubah data-src menjadi src saat visible.
        "data-src": f"/api/thumb/{key}",
    }
    return html.A(
        html.Img(**img_props),
        href=f"/api/image/{key}",
        target="_blank",
        title="Klik untuk buka full image",
    )




def valid_image_key(value: Any) -> str:
    """Bersihkan key gambar agar nilai seperti NaN/null tidak ikut tampil di gallery."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    s = str(value).strip().strip('"').strip("'")
    if not s:
        return ""
    if s.lower() in {"nan", "none", "null", "nat", "unknown", "-", "--", "n/a", "na", "#n/a", "tidak ada", "kosong"}:
        return ""
    # endpoint sudah bisa handle path, tapi untuk tampilan lebih aman ambil basename jika path lengkap
    s2 = s.replace("\\", "/").split("?")[0].split("#")[0]
    return Path(s2).name or s


def split_image_keys(value: Any) -> list[str]:
    """Split kolom foto yang kadang berupa string comma-separated, lalu buang NaN/null."""
    s = valid_image_key(value)
    if not s:
        return []
    out = []
    for part in str(value).replace("\\", "/").split(","):
        k = valid_image_key(part)
        if k and k.upper() not in [x.upper() for x in out]:
            out.append(k)
    return out


def display_text(value: Any, fallback: str = "-") -> str:
    """Tampilan aman agar NaN/None/null tidak muncul ke dashboard."""
    if value is None:
        return fallback
    try:
        if pd.isna(value):
            return fallback
    except Exception:
        pass
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "nat", "unknown", "-", "--", "n/a", "na", "#n/a", "kosong"}:
        return fallback
    return s


def split_meta_values(value: Any) -> list[str]:
    s = display_text(value, "")
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if display_text(x, "")]


def reused_case_readout(row: Any):
    if row is None:
        return html.Div()
    usage = int(pd.to_numeric(pd.Series([row.get("usage_count", 0)]), errors="coerce").fillna(0).iloc[0])
    oph_n = len({x.upper() for x in split_meta_values(row.get("oph_list", ""))})
    date_n = len({x.upper() for x in split_meta_values(row.get("sql_date_list", ""))})
    block_n = len({x.upper() for x in split_meta_values(row.get("blok_list", ""))})
    tph_n = len({x.upper() for x in split_meta_values(row.get("tph_list", ""))})
    reuse = display_text(row.get("reuse_category"), "-")
    datecat = display_text(row.get("date_category"), "-")

    sentence = (
        f"Foto ini tercatat dipakai {usage:,} kali pada {oph_n:,} OPH, "
        f"{date_n:,} tanggal, {block_n:,} blok, dan {tph_n:,} TPH."
    )
    return html.Div([
        html.Div([
            html.Div("KESIMPULAN KASUS", className="case-readout-eyebrow"),
            html.Div(sentence, className="case-readout-title"),
        ], className="case-readout-copy"),
        html.Div([
            html.Span(reuse, className="case-badge reused"),
            html.Span(datecat, className="case-badge date"),
        ], className="case-readout-badges"),
    ], className="case-readout")


def group_meta_value(group_row: Any, col: str, idx: int, fallback: str = "-") -> str:
    if group_row is None:
        return fallback
    try:
        vals = split_meta_values(group_row.get(col, ""))
    except Exception:
        vals = []
    if not vals:
        return fallback
    pos = max(0, idx - 1)
    if pos < len(vals):
        return vals[pos]
    return vals[0]


def row_or_group_value(row: Any, field: str, idx: int, group_row: Any = None, fallback: str = "-") -> str:
    direct = display_text(row.get(field, ""), "")
    if direct:
        return direct
    group_map = {
        "oph": "oph_list",
        "tanggal": "sql_date_list",
        "estate": "estate_list",
        "afdeling": "afdeling_list",
        "blok": "blok_list",
        "tph": "tph_list",
        "pemanen": "pemanen_list",
        "kerani_panen": "kerani_panen_list",
        "mandor": "mandor_list",
    }
    gcol = group_map.get(field)
    if gcol:
        return group_meta_value(group_row, gcol, idx, fallback)
    return fallback


def thumb_box(image_key: str, title: str, mode: str = ""):
    key = valid_image_key(image_key)
    if not key:
        return html.Div([
            html.Div(title, className="frame-img-title"),
            html.Div("Tidak ada foto", className="frame-empty"),
        ], className="frame-img-block")
    return html.Div([
        html.Div(title, className="frame-img-title"),
        thumb_img(key, "frame-thumb"),
        html.Div((mode + " " if mode else "") + key, className="frame-img-caption"),
    ], className="frame-img-block")


def frame_gallery(det: pd.DataFrame, representative_key: str, limit: int = 120, group_row: Any = None):
    """Gallery seperti HTML lama: setiap frame/row menampilkan panen/reused + transport DT/FT milik OPH/baris yang sama.

    Penting: jangan gabungkan semua transport image ke satu gallery besar tanpa konteks, karena itu terlihat seolah
    gambar transport tidak cocok. Transport harus ditempelkan pada row transaksi yang sama.
    """
    if det is None or det.empty:
        return html.Div("Tidak ada detail transaksi untuk group ini.", className="muted")

    cards = []
    # limit <= 0 berarti semua frame keluar. Ini bukan paging/limit;
    # kecepatan dijaga oleh prebuilt thumbnail cache dan browser cache.
    det_iter = det if not limit or int(limit) <= 0 else det.head(int(limit))
    for idx, (_, r) in enumerate(det_iter.iterrows(), start=1):
        panen_keys = split_image_keys(r.get("image_name"))
        if not panen_keys:
            panen_keys = [valid_image_key(representative_key)] if valid_image_key(representative_key) else []
        is_elhp = str(r.get("source_system", "")).upper().startswith("ELHP")
        kt_keys = [] if is_elhp else split_image_keys(r.get("foto_kt_before"))
        dt_keys = [] if is_elhp else split_image_keys(r.get("foto_dt"))
        ft_keys = [] if is_elhp else split_image_keys(r.get("foto_ft"))

        img_blocks = []
        img_blocks.append(thumb_box(panen_keys[0] if panen_keys else "", "Foto Panen / Reused", "PANEN"))
        if is_elhp:
            img_blocks.append(html.Div([
                html.Div("Transport", className="frame-img-title"),
                html.Div("N/A - ELHP tidak memiliki foto transport", className="frame-empty"),
            ], className="frame-img-block muted-transport"))
        else:
            img_blocks.append(thumb_box(kt_keys[0] if kt_keys else "", "Foto Transport KT Before", "KT BEFORE"))
            img_blocks.append(thumb_box(dt_keys[0] if dt_keys else "", "Foto Transport DT", "DT"))
            img_blocks.append(thumb_box(ft_keys[0] if ft_keys else "", "Foto Transport FT", "FT"))

        # Jika ada lebih dari satu DT/FT dalam satu row, tampilkan sebagai additional thumbnails row itu juga.
        extra = []
        for k in kt_keys[1:]:
            extra.append(thumb_box(k, "Extra KT Before", "KT BEFORE"))
        for k in dt_keys[1:]:
            extra.append(thumb_box(k, "Extra DT", "DT"))
        for k in ft_keys[1:]:
            extra.append(thumb_box(k, "Extra FT", "FT"))

        cards.append(html.Div([
            html.Div([
                html.Div(f"Frame {idx}", className="frame-no"),
                html.Div(display_text(r.get("transport_status", ""), TRANSPORT_NA_ELHP if str(r.get("source_system", "")).upper().startswith("ELHP") else "-"), className="frame-status"),
            ], className="frame-head"),
            html.Div([
                html.B("OPH: "), html.Span(row_or_group_value(r, "oph", idx, group_row)), html.Br(),
                html.B("Tanggal: "), html.Span(row_or_group_value(r, "tanggal", idx, group_row)), html.Br(),
                html.B("Lokasi: "), html.Span(
                    f"{row_or_group_value(r, 'estate', idx, group_row)} / "
                    f"{row_or_group_value(r, 'afdeling', idx, group_row)} / "
                    f"{row_or_group_value(r, 'blok', idx, group_row)} / "
                    f"TPH {row_or_group_value(r, 'tph', idx, group_row)}"
                ), html.Br(),
                html.B("Pemanen: "), html.Span(row_or_group_value(r, "pemanen", idx, group_row)), html.Br(),
                html.B("Kerani Panen: "), html.Span(row_or_group_value(r, "kerani_panen", idx, group_row)), html.Br(),
                html.B("Mandor: "), html.Span(row_or_group_value(r, "mandor", idx, group_row)),
            ], className="frame-meta"),
            html.Div([
                html.Div([
                    html.Div("JJG Kerani Panen", className="frame-jjg-label"),
                    html.Div(fmt_optional_num(r.get("jjg_kerani_panen")), className="frame-jjg-value"),
                ], className="frame-jjg-item"),
                html.Div([
                    html.Div("JJG FT", className="frame-jjg-label"),
                    html.Div(fmt_optional_num(r.get("jjg_ft")), className="frame-jjg-value"),
                ], className="frame-jjg-item"),
                html.Div([
                    html.Div("JJG DT", className="frame-jjg-label"),
                    html.Div(fmt_optional_num(r.get("jjg_dt")), className="frame-jjg-value"),
                ], className="frame-jjg-item"),
                html.Div([
                    html.Div("Bunch Pay / OPH", className="frame-jjg-label"),
                    html.Div(fmt_optional_num(r.get("bunch_pay")), className="frame-jjg-value"),
                ], className="frame-jjg-item"),
            ], className="frame-jjg-grid"),
            html.Div(img_blocks + extra, className="frame-images"),
        ], className="frame-card"))

    return html.Div(cards, className="frame-gallery")


def image_gallery(keys: list[str], limit: int = 30):
    clean = []
    for k in keys:
        kk = str(k or "").strip()
        if kk and kk.upper() not in [x.upper() for x in clean]:
            clean.append(kk)
    if not clean:
        return html.Div("Tidak ada gambar untuk group ini.", className="muted")
    return html.Div([
        html.Div([
            thumb_img(k),
            html.Div(k, className="thumb-caption"),
        ], className="thumb-card") for k in clean[:limit]
    ], className="gallery")


MONTH_COLORS = [
    "#003D79", "#C9182B", "#6B7280", "#2F80ED", "#9CA3AF", "#0F766E",
    "#F59E0B", "#7C3AED", "#DB2777", "#16A34A", "#64748B", "#991B1B",
]


def _month_label(v: Any) -> str:
    s = str(v or "").strip()
    if not s or s.lower() in {"nan", "none", "nat", "null"}:
        return "Tanpa Bulan"
    if len(s) >= 7 and s[4:5] == "-":
        return s[:7]
    return s


def _month_sort_key(v: Any):
    s = _month_label(v)
    if s == "Tanpa Bulan":
        return (9999, 99, s)
    try:
        return (int(s[:4]), int(s[5:7]), s)
    except Exception:
        return (9998, 99, s)


def _month_display_label(v: Any) -> str:
    """Label bulan untuk chart agar Plotly tidak menganggap '2026-01' sebagai date axis."""
    s = _month_label(v)
    if s == "Tanpa Bulan":
        return s
    try:
        year = int(s[:4])
        month = int(s[5:7])
        names = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        if 1 <= month <= 12:
            return f"{names[month-1]} {year}"
    except Exception:
        pass
    return str(s)


def _month_display_order(values: Any) -> list[str]:
    raw = pd.Series(values).dropna().astype(str).unique().tolist()
    raw = sorted(raw, key=_month_sort_key)
    return [_month_display_label(x) for x in raw]


def _balanced_top_group_keys(
    base: pd.DataFrame,
    monthly: pd.DataFrame | None,
    months_filter: Any,
    limit: int,
) -> list[str]:
    """Select Top-N groups round-robin per month, then fill by global usage.

    A purely global Top-N lets the newest/high-volume months occupy every bar.
    The monthly chart then appears to have lost January-April even when those
    months exist. Round-robin keeps at least the strongest group of every
    available month visible without increasing the configured chart size.
    """
    global_keys = base.sort_values("usage_count", ascending=False)["group_key"].astype(str).tolist()
    if limit <= 0:
        return global_keys
    if monthly is None or monthly.empty or "group_key" not in monthly.columns:
        return global_keys[:limit]

    allowed = set(global_keys)
    data = monthly[monthly["group_key"].astype(str).isin(allowed)].copy()
    month_series = (
        data["month"]
        if "month" in data.columns
        else pd.Series(["Tanpa Bulan"] * len(data), index=data.index)
    )
    data["month_label"] = month_series.apply(_month_label)
    selected_months = {str(value) for value in (months_filter or []) if str(value or "").strip()}
    if selected_months:
        data = data[data["month_label"].isin(selected_months)].copy()
    if data.empty:
        return global_keys[:limit]

    if "usage_weight" in data.columns:
        data["month_usage"] = pd.to_numeric(data["usage_weight"], errors="coerce").fillna(0)
    else:
        data["month_usage"] = 1
    ranked = (
        data.groupby(["month_label", "group_key"], dropna=False)["month_usage"]
        .sum()
        .reset_index()
    )
    months = sorted(ranked["month_label"].astype(str).unique().tolist(), key=_month_sort_key)
    candidates = {
        month: ranked[ranked["month_label"].astype(str).eq(month)]
        .sort_values(["month_usage", "group_key"], ascending=[False, True])["group_key"]
        .astype(str)
        .tolist()
        for month in months
    }
    positions = {month: 0 for month in months}
    picked: list[str] = []
    picked_set: set[str] = set()
    while len(picked) < limit:
        added = False
        for month in months:
            month_candidates = candidates[month]
            while (
                positions[month] < len(month_candidates)
                and month_candidates[positions[month]] in picked_set
            ):
                positions[month] += 1
            if positions[month] >= len(month_candidates):
                continue
            key = month_candidates[positions[month]]
            positions[month] += 1
            picked.append(key)
            picked_set.add(key)
            added = True
            if len(picked) >= limit:
                break
        if not added:
            break
    for key in global_keys:
        if len(picked) >= limit:
            break
        if key not in picked_set:
            picked.append(key)
            picked_set.add(key)
    return picked


def _top_month_stack(top: pd.DataFrame, details: pd.DataFrame | None = None, months_filter: Any = None) -> tuple[pd.DataFrame, list[str]]:
    """Return a wide table for stacked Top Reused by month.

    HTML lama menampilkan satu bar per reused group, tetapi warna bar dibagi per bulan.
    Untuk akurasi, pecahan bulan dihitung dari detail transaksi per group_key. Kalau detail
    tidak tersedia, fallback ke usage_count group pada month utama group.
    """
    if top is None or top.empty:
        return pd.DataFrame(), []

    base = top.sort_values("usage_count", ascending=False).copy()
    selected_keys = _balanced_top_group_keys(base, details, months_filter, TOP_N)
    if selected_keys:
        order = {key: idx for idx, key in enumerate(selected_keys)}
        base = base[base["group_key"].astype(str).isin(set(selected_keys))].copy()
        base["_balanced_order"] = base["group_key"].astype(str).map(order)
        base = base.sort_values("_balanced_order").drop(columns="_balanced_order")
    elif TOP_N and TOP_N > 0:
        base = base.head(TOP_N)

    keys = base["group_key"].astype(str).tolist() if "group_key" in base.columns else []
    mfilter = {str(x) for x in (months_filter or []) if str(x or "").strip()}
    rows = []

    if details is not None and not details.empty and "group_key" in details.columns:
        d = details[details["group_key"].astype(str).isin(keys)].copy()
        if "month" in d.columns:
            d["month_label"] = d["month"].apply(_month_label)
        elif "tanggal" in d.columns:
            d["month_label"] = d["tanggal"].apply(_month_label)
        else:
            d["month_label"] = "Tanpa Bulan"
        if mfilter:
            d = d[d["month_label"].isin(mfilter)]
        if not d.empty:
            if "usage_weight" in d.columns:
                d["usage_weight"] = pd.to_numeric(
                    d["usage_weight"], errors="coerce"
                ).fillna(0)
                rows = (
                    d.groupby(["group_key", "month_label"], dropna=False)["usage_weight"]
                    .sum()
                    .reset_index(name="month_usage")
                    .to_dict("records")
                )
            else:
                rows = (
                    d.groupby(["group_key", "month_label"], dropna=False)
                    .size()
                    .reset_index(name="month_usage")
                    .to_dict("records")
                )

    # Fallback untuk group yang tidak punya detail month breakdown.
    have = {str(r["group_key"]) for r in rows}
    for _, r in base.iterrows():
        gkey = str(r.get("group_key", ""))
        if not gkey or gkey in have:
            continue
        m = _month_label(r.get("month"))
        if mfilter and m not in mfilter:
            continue
        rows.append({"group_key": gkey, "month_label": m, "month_usage": int(pd.to_numeric(r.get("usage_count", 0), errors="coerce") or 0)})

    if not rows:
        return pd.DataFrame(), []

    long = pd.DataFrame(rows)
    wide = (
        long.pivot_table(index="group_key", columns="month_label", values="month_usage", aggfunc="sum", fill_value=0)
        .reset_index()
    )
    meta_cols = [
        "group_key", "source_company", "group_id", "representative_image", "usage_count",
        "reuse_category", "date_category", "source_label", "estate_primary",
    ]
    meta = base[[c for c in meta_cols if c in base.columns]].copy()
    out = meta.merge(wide, on="group_key", how="left").fillna(0)
    month_cols = [c for c in wide.columns if c != "group_key"]
    month_cols = sorted(month_cols, key=_month_sort_key)
    out["stack_total"] = out[month_cols].sum(axis=1) if month_cols else out["usage_count"]
    out = out.sort_values("stack_total", ascending=True).reset_index(drop=True)
    # Label dibuat pendek supaya bar Top Reused tidak hilang/terpotong di dashboard.
    # Detail file tetap muncul lengkap di hover.
    out["plot_label"] = out.apply(
        lambda r: f"G{display_text(r.get('group_id'), '-')} | {display_text(r.get('estate_primary'), '-')} | {_short_label(r.get('representative_image'), 18)}",
        axis=1,
    )
    return out, month_cols


def build_top_reused_fig(df: pd.DataFrame, details: pd.DataFrame | None = None, months_filter: Any = None):
    """Top reused rapi: 1 bar = 1 group, stacked by month, tanpa tabrakan layout."""
    if df is None or df.empty:
        return empty_fig("Top Reused Photo by Month")

    top, month_cols = _top_month_stack(df, details, months_filter)
    if top.empty or not month_cols:
        return empty_fig("Top Reused Photo by Month")

    top = top.sort_values("stack_total", ascending=True).reset_index(drop=True)
    y_labels = top["plot_label"].astype(str).tolist()
    fig = go.Figure()
    custom = top[["group_key", "group_id", "representative_image", "reuse_category", "date_category", "source_label", "estate_primary", "stack_total"]].astype(str).to_numpy()

    for i, month in enumerate(month_cols):
        vals = pd.to_numeric(top[month], errors="coerce").fillna(0)
        fig.add_trace(go.Bar(
            x=vals,
            y=y_labels,
            name=str(month),
            orientation="h",
            marker=dict(
                color=MONTH_COLORS[i % len(MONTH_COLORS)],
                line=dict(color="rgba(255,255,255,.75)", width=0.8),
            ),
            text=[f"{int(v):,}" if float(v) >= 3 else "" for v in vals],
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(size=9, color="white"),
            cliponaxis=False,
            customdata=custom,
            hovertemplate=(
                "<b>Group %{customdata[1]}</b><br>"
                "Representative: %{customdata[2]}<br>"
                "Group Key: %{customdata[0]}<br>"
                "Source: %{customdata[5]}<br>"
                "Estate: %{customdata[6]}<br>"
                "Bulan: " + str(month) + "<br>"
                "Usage bulan ini: %{x:,.0f}<br>"
                "Total usage group: %{customdata[7]}<br>"
                "Reuse: %{customdata[3]}<br>"
                "Date Category: %{customdata[4]}<extra></extra>"
            ),
        ))

    max_total = float(pd.to_numeric(top["stack_total"], errors="coerce").fillna(0).max() or 0)
    for _, r in top.iterrows():
        total = float(pd.to_numeric(r.get("stack_total", 0), errors="coerce") or 0)
        fig.add_annotation(
            x=total,
            y=r["plot_label"],
            text=f"{int(total):,}",
            showarrow=False,
            xanchor="left",
            xshift=10,
            font=dict(size=11, color="#0f172a"),
        )

    chart_height = max(560, min(1500, 150 + len(top) * 28))
    fig.update_layout(
        barmode="stack",
        bargap=0.25,
        hovermode="y unified",
        xaxis=dict(range=[0, max_total * 1.20 if max_total else 1], showgrid=True, zeroline=False),
    )
    fig.update_yaxes(
        categoryorder="array",
        categoryarray=y_labels,
        automargin=True,
        tickfont=dict(size=10),
    )

    fig = style_fig(
        fig,
        height=chart_height,
        show_legend=False,
        margin=dict(l=260, r=110, t=86, b=60),
        x_title="Usage",
        y_title="",
    )
    fig.update_layout(
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            title_text="Bulan penggunaan",
            font=dict(size=10),
            bgcolor="rgba(255,255,255,.88)",
            bordercolor="rgba(226,232,240,.9)",
            borderwidth=1,
            traceorder="normal",
        ),
    )
    return fig


def build_bar(df: pd.DataFrame, x: str, y: str, title: str, custom: list[str] | None = None):
    if df is None or df.empty:
        return empty_fig(title)
    custom = custom or [x]
    data = df.copy()
    data[x] = data[x].apply(lambda v: _short_label(v, 24))
    fig = px.bar(data, x=x, y=y, text=y, custom_data=custom)
    fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False)
    fig.update_layout(bargap=0.32)
    fig.update_xaxes(tickangle=-20 if len(data) > 4 else 0)
    return style_fig(fig, height=360, show_legend=False, margin=dict(l=54, r=64, t=18, b=80), x_title="", y_title="Total")


def build_hbar(
    df: pd.DataFrame,
    label_col: str,
    value_col: str,
    title: str,
    top_n: int = 30,
    *,
    compact: bool = False,
):
    if df is None or df.empty:
        return empty_fig(title)
    data = df.sort_values(value_col, ascending=False).head(top_n).copy()
    data = data.sort_values(value_col, ascending=True)
    full_labels = data[label_col].astype(str).tolist()
    data["plot_label"] = [_short_label(v, 28) for v in full_labels]
    compact_bar_width = 0.22 if len(data) == 1 else 0.38
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=data[value_col],
        y=data["plot_label"],
        orientation="h",
        text=data[value_col],
        texttemplate="%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
        customdata=data[[label_col, value_col]].astype(str).to_numpy(),
        hovertemplate="<b>%{customdata[0]}</b><br>Total: %{x:,.0f}<extra></extra>",
        width=compact_bar_width if compact else 0.64,
    ))
    fig.update_layout(bargap=0.42 if compact else 0.28)
    chart_height = 260 if compact else max(360, min(840, 90 + len(data) * 30))
    return style_fig(
        fig,
        height=chart_height,
        show_legend=False,
        margin=dict(l=112 if compact else 140, r=78, t=12 if compact else 18, b=42),
        x_title="Total",
        y_title="",
    )


def build_pie(df: pd.DataFrame, names: str, values: str, title: str):
    if df is None or df.empty:
        return empty_fig(title)
    data = df.sort_values(values, ascending=False).copy()
    fig = px.pie(data, names=names, values=values, hole=0.52, custom_data=[names, values])
    # Label panjang dipindah ke legend; di pie hanya persen supaya tidak nabrak.
    fig.update_traces(
        textposition="inside",
        textinfo="percent",
        insidetextorientation="radial",
        hovertemplate="<b>%{label}</b><br>Total: %{value:,.0f}<br>%{percent}<extra></extra>",
    )
    fig.update_layout(legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.02, font=dict(size=10)))
    return style_fig(fig, height=380, show_legend=True, margin=dict(l=18, r=150, t=16, b=36))


def build_usage_dist(df: pd.DataFrame):
    if df is None or df.empty:
        return empty_fig("Usage Distribution")
    data = df.copy()
    fig = px.bar(data, x="bucket", y="total_group", text="total_group")
    fig.update_traces(texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False)
    fig.update_layout(bargap=0.34)
    return style_fig(fig, height=360, show_legend=False, margin=dict(l=54, r=64, t=18, b=72), x_title="Usage Bucket", y_title="Group")



def _series_or_default(df: pd.DataFrame, col: str, default: str = "") -> pd.Series:
    if df is None:
        return pd.Series(dtype=object)
    if col in df.columns:
        return df[col]
    return pd.Series([default] * len(df), index=df.index)


def _month_usage_long(groups: pd.DataFrame | None, details: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly usage basis for charts.

    Prioritas memakai detail transaksi karena usage_count = jumlah pemakaian foto di detail.
    Jika detail tidak tersedia, fallback ke usage_count di group summary.
    """
    if groups is None or groups.empty:
        return pd.DataFrame(columns=["group_key", "month", "source_company", "estate", "afdeling", "reuse_category", "date_category", "similarity", "usage"])

    g = groups.copy()
    g_key_map = g.set_index(g["group_key"].astype(str)) if "group_key" in g.columns else pd.DataFrame()
    rows = []

    if details is not None and not details.empty and "group_key" in details.columns:
        d = details.copy()
        d = d[d["group_key"].astype(str).isin(set(g["group_key"].astype(str)))] if "group_key" in g.columns else d
        if not d.empty:
            d["month"] = _series_or_default(d, "month", "").apply(_month_label)
            # Fallback detail fields from group when detail field is blank.
            for _, r in d.iterrows():
                gkey = str(r.get("group_key", ""))
                grow = g_key_map.loc[gkey] if not g_key_map.empty and gkey in g_key_map.index else None
                def pick(detail_col, group_col, fallback="-"):
                    v = display_text(r.get(detail_col, ""), "")
                    if v:
                        return v
                    if grow is not None:
                        return display_text(grow.get(group_col, ""), fallback)
                    return fallback
                usage_weight = pd.to_numeric(
                    r.get("usage_weight", 1), errors="coerce"
                )
                rows.append({
                    "group_key": gkey,
                    "month": _month_label(r.get("month") or r.get("tanggal")),
                    "source_company": pick("source_company", "source_company"),
                    "estate": pick("estate", "estate_primary"),
                    "afdeling": pick("afdeling", "afdeling_primary"),
                    "reuse_category": pick("reuse_category", "reuse_category"),
                    "date_category": pick("date_category", "date_category"),
                    "similarity": pick("similarity", "similarity_primary"),
                    "kerani_panen": pick("kerani_panen", "kerani_panen_list"),
                    "mandor": pick("mandor", "mandor_list"),
                    "pemanen": pick("pemanen", "pemanen_list"),
                    "usage": float(usage_weight) if pd.notna(usage_weight) else 0.0,
                })

    if rows:
        return pd.DataFrame(rows)

    # Fallback group summary.
    out = pd.DataFrame()
    out["group_key"] = g.get("group_key", g.get("group_id", "")).astype(str)
    out["month"] = g.get("month", "").apply(_month_label) if "month" in g.columns else "Tanpa Bulan"
    out["source_company"] = g.get("source_company", "-").apply(lambda v: display_text(v)) if "source_company" in g.columns else "-"
    out["estate"] = g.get("estate_primary", "-").apply(lambda v: display_text(v)) if "estate_primary" in g.columns else "-"
    out["afdeling"] = g.get("afdeling_primary", "-").apply(lambda v: display_text(v)) if "afdeling_primary" in g.columns else "-"
    out["reuse_category"] = g.get("reuse_category", "-").apply(lambda v: display_text(v)) if "reuse_category" in g.columns else "-"
    out["date_category"] = g.get("date_category", "-").apply(lambda v: display_text(v)) if "date_category" in g.columns else "-"
    out["similarity"] = g.get("similarity_primary", "-").apply(lambda v: display_text(v)) if "similarity_primary" in g.columns else "-"
    out["kerani_panen"] = g.get("kerani_panen_list", "-").apply(lambda v: _compact_list(v, 1, 36)) if "kerani_panen_list" in g.columns else "-"
    out["mandor"] = g.get("mandor_list", "-").apply(lambda v: _compact_list(v, 1, 36)) if "mandor_list" in g.columns else "-"
    out["pemanen"] = g.get("pemanen_list", "-").apply(lambda v: _compact_list(v, 1, 36)) if "pemanen_list" in g.columns else "-"
    out["usage"] = pd.to_numeric(g.get("usage_count", 0), errors="coerce").fillna(0)
    return out


def build_monthly_pattern_fig(
    groups: pd.DataFrame,
    details: pd.DataFrame | None = None,
    precomputed_long: pd.DataFrame | None = None,
):
    long = (
        precomputed_long.copy()
        if precomputed_long is not None
        else _month_usage_long(groups, details)
    )
    if long.empty:
        return empty_fig("Monthly Pattern")

    data = long.groupby(["month", "source_company"], dropna=False)["usage"].sum().reset_index()
    data = data.sort_values("month", key=lambda s: s.map(_month_sort_key))
    data["month_label"] = data["month"].apply(_month_display_label)
    month_order = _month_display_order(data["month"])

    month_total = data.groupby(["month", "month_label"], dropna=False)["usage"].sum().reset_index(name="total_usage")
    month_total = month_total.sort_values("month", key=lambda s: s.map(_month_sort_key))

    fig = px.bar(
        data,
        x="month_label",
        y="usage",
        color="source_company",
        text="usage",
        custom_data=["source_company", "month"],
        category_orders={"month_label": month_order},
    )
    fig.update_traces(
        texttemplate="%{text:,.0f}",
        textposition="inside",
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>Company: %{customdata[0]}<br>Usage: %{y:,.0f}<extra></extra>",
    )
    fig.add_trace(go.Scatter(
        x=month_total["month_label"],
        y=month_total["total_usage"],
        mode="lines+markers+text",
        name="Total Usage",
        text=[f"{int(v):,}" for v in month_total["total_usage"]],
        textposition="top center",
        line=dict(color="#0f172a", width=3),
        marker=dict(color="#0f172a", size=8),
        hovertemplate="<b>%{x}</b><br>Total Usage: %{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        barmode="stack",
        bargap=0.34,
        legend_title_text="Company",
        hovermode="x unified",
    )
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=month_order, tickangle=0)
    return style_fig(fig, height=400, show_legend=True, margin=dict(l=58, r=52, t=18, b=82), x_title="Bulan", y_title="Usage")


def build_entity_monthly_fig(
    groups: pd.DataFrame,
    details: pd.DataFrame | None,
    entity_col: str,
    title: str,
    top_n: int = 8,
    precomputed_long: pd.DataFrame | None = None,
):
    long = (
        precomputed_long.copy()
        if precomputed_long is not None
        else _month_usage_long(groups, details)
    )
    if long.empty or entity_col not in long.columns:
        return empty_fig(title)
    long[entity_col] = long[entity_col].apply(lambda v: display_text(v))
    totals = long.groupby(entity_col)["usage"].sum().sort_values(ascending=False)
    keep = set(totals.head(top_n).index.astype(str))
    data = long.copy()
    data[entity_col] = data[entity_col].astype(str).where(data[entity_col].astype(str).isin(keep), "Lainnya")
    agg = data.groupby(["month", entity_col], dropna=False)["usage"].sum().reset_index()
    agg = agg.sort_values("month", key=lambda s: s.map(_month_sort_key))
    agg["month_label"] = agg["month"].apply(_month_display_label)
    month_order = _month_display_order(agg["month"])
    fig = px.bar(
        agg,
        x="month_label",
        y="usage",
        color=entity_col,
        text="usage",
        custom_data=[entity_col, "month"],
        category_orders={"month_label": month_order},
    )
    fig.update_traces(
        texttemplate="%{text:,.0f}",
        textposition="inside",
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>" + entity_col.replace("_", " ").title() + ": %{customdata[0]}<br>Usage: %{y:,.0f}<extra></extra>",
    )
    fig.update_layout(barmode="stack", bargap=0.34, legend_title_text=entity_col.replace("_", " ").title())
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=month_order, tickangle=0)
    return style_fig(fig, height=390, show_legend=True, margin=dict(l=58, r=34, t=18, b=86), x_title="Bulan", y_title="Usage")


def build_similarity_fig(
    groups: pd.DataFrame,
    details: pd.DataFrame | None = None,
):
    data = transport_similarity_usage_summary(details)
    if not data.empty:
        return build_hbar(
            data,
            "label",
            "total_usage",
            "Similarity Usage Transport",
            top_n=12,
        )
    if groups is None or groups.empty:
        return empty_fig("Similarity Usage Transport")
    # Fallback kompatibilitas untuk demo/cache lama tanpa detail. Produksi
    # Harmony selalu memakai summary detail KT Before di atas.
    data = agg_count(groups, "similarity_primary") if "similarity_primary" in groups.columns else pd.DataFrame()
    if data.empty:
        return empty_fig("Similarity Usage Transport")
    return build_hbar(data, "similarity_primary", "total_usage", "Similarity Usage Transport", top_n=12)


def build_reuse_date_heatmap(groups: pd.DataFrame):
    if groups is None or groups.empty or "reuse_category" not in groups.columns or "date_category" not in groups.columns:
        return empty_fig("Reuse x Date")
    data = groups.copy()
    data["usage_num"] = pd.to_numeric(data.get("usage_count", 0), errors="coerce").fillna(0)
    pivot = data.pivot_table(index="reuse_category", columns="date_category", values="usage_num", aggfunc="sum", fill_value=0)
    if pivot.empty:
        return empty_fig("Reuse x Date")
    pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=True).index]
    fig = go.Figure(data=go.Heatmap(
        z=pivot.values,
        x=[_short_label(c, 24) for c in pivot.columns],
        y=[_short_label(i, 28) for i in pivot.index],
        colorscale=[[0, "#eff6ff"], [0.5, "#60a5fa"], [1, "#003D79"]],
        text=pivot.values,
        texttemplate="%{text:,.0f}",
        hovertemplate="Reuse: %{y}<br>Date: %{x}<br>Usage: %{z:,.0f}<extra></extra>",
        colorbar=dict(title="Usage"),
    ))
    return style_fig(fig, height=max(360, min(620, 160 + 34 * len(pivot))), show_legend=False, margin=dict(l=160, r=64, t=18, b=88), x_title="Date Category", y_title="Reuse Category")


def build_top_people_fig(
    groups: pd.DataFrame,
    details: pd.DataFrame | None,
    col: str,
    title: str,
    top_n: int = 15,
    precomputed_long: pd.DataFrame | None = None,
):
    long = (
        precomputed_long.copy()
        if precomputed_long is not None
        else _month_usage_long(groups, details)
    )
    if long.empty or col not in long.columns:
        return empty_fig(title)
    data = long.copy()
    data[col] = data[col].apply(lambda v: display_text(v, "-"))
    data = data[~data[col].astype(str).str.upper().isin(["-", "UNKNOWN", "N/A", "TIDAK ADA"])]
    if data.empty:
        return empty_fig(title)
    agg = data.groupby(col, dropna=False)["usage"].sum().reset_index().sort_values("usage", ascending=False).head(top_n)
    agg = agg.sort_values("usage", ascending=True)
    agg["plot_label"] = agg[col].apply(lambda v: _short_label(v, 30))
    fig = go.Figure(go.Bar(
        x=agg["usage"], y=agg["plot_label"], orientation="h", text=agg["usage"],
        texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False,
        customdata=agg[[col, "usage"]].astype(str).to_numpy(),
        hovertemplate="%{customdata[0]}<br>Usage: %{x:,.0f}<extra></extra>",
    ))
    return style_fig(fig, height=max(360, min(620, 120 + len(agg) * 30)), show_legend=False, margin=dict(l=170, r=72, t=18, b=46), x_title="Usage", y_title="")


def filter_payload(months, estate, afdeling, block, reuse_category, date_category, transport_status, similarity, search):
    return {
        "months": months or [],
        "estate": estate or [],
        "afdeling": afdeling or [],
        "block": block or [],
        "reuse_category": reuse_category or [],
        "date_category": date_category or [],
        "transport_status": transport_status or [],
        "similarity": similarity or [],
        "search": search or "",
    }




def _options_values(options):
    return {str(o.get("value", "")) for o in (options or []) if not o.get("disabled")}


def _sanitize_value(value, options, multi=True):
    allowed = _options_values(options)
    if not allowed:
        return [] if multi else None
    if multi:
        vals = value if isinstance(value, list) else ([value] if value not in (None, "") else [])
        out = [v for v in vals if str(v) in allowed]
        return out or None
    if isinstance(value, list):
        value = value[0] if value else None
    return value if value is not None and str(value) in allowed else None


def _company_valid_for_system(system, company):
    s = str(system or "HARMONY").upper()
    elhp_companies = {"DAN", "DWT"}

    def accepted(value):
        code = str(value or "").upper().strip()
        if not code:
            return False
        if s == "HARMONY":
            return code not in elhp_companies
        if s == "ELHP":
            return code in elhp_companies
        return True

    raw_values = company if isinstance(company, (list, tuple, set)) else [company]
    values = []
    for value in raw_values:
        code = str(value or "").upper().strip()
        if accepted(code) and code not in values:
            values.append(code)
    return values


def _first_enabled_option_value(options):
    for option in options or []:
        if option.get("disabled"):
            continue
        value = option.get("value")
        if value not in (None, ""):
            return value
    return None


def _choose_company_value(system, current_value, company_options):
    current = _sanitize_value(
        _company_valid_for_system(system, current_value), company_options, multi=True
    ) or []
    if current:
        return current
    # Pada dropdown multi, list kosong berarti user sengaja memilih semua
    # company dalam System aktif. Jangan paksa kembali ke DEFAULT_COMPANY.
    if isinstance(current_value, list) and not current_value:
        return []
    configured_default = _sanitize_value(
        _company_valid_for_system(system, DEFAULT_COMPANY), company_options, multi=True
    ) or []
    if configured_default:
        return configured_default
    first = _first_enabled_option_value(company_options)
    return [first] if first is not None else []


def _group_kpis(g: pd.DataFrame) -> dict[str, Any]:
    if g is None or g.empty:
        return dict(total_group=0, total_usage=0, max_usage=0, avg_usage=0, unique_images=0, total_bunch_pay=0, pwp_group=0, dan_group=0, dwt_group=0)
    usage = pd.to_numeric(g.get("usage_count", 0), errors="coerce").fillna(0)
    bunch = total_bunch_pay_series(g)
    company = g.get("source_company", pd.Series(dtype=str)).astype(str).str.upper()
    return {
        "total_group": int(len(g)),
        "total_usage": int(usage.sum()),
        "max_usage": int(usage.max()) if len(usage) else 0,
        "avg_usage": float(usage.mean()) if len(usage) else 0,
        "unique_images": int(g["representative_image"].nunique()) if "representative_image" in g.columns else 0,
        "total_bunch_pay": float(bunch.sum()) if len(bunch) else 0,
        "pwp_group": int((company == "PWP").sum()),
        "dan_group": int((company == "DAN").sum()),
        "dwt_group": int((company == "DWT").sum()),
    }


def _valid_similarity_image_key(value: Any) -> str:
    """Validasi foto untuk halaman Similarity Harmony.

    Berbeda dari valid_image_key biasa, fungsi ini juga membuang placeholder status
    seperti "Tidak Ada Foto Transport" supaya contoh similarity tidak salah.
    """
    key = valid_image_key(value)
    if not key:
        return ""
    up = str(key).upper()
    bad_phrases = [
        "TIDAK ADA", "NO IMAGE", "NO FOTO", "NO PHOTO", "NAN", "NULL", "NONE",
        "TRANSPORT TIDAK ADA", "TIDAK ADA FOTO TRANSPORT", "TIDAK ADA GAMBAR",
        "MISSING", "EMPTY", "KOSONG",
    ]
    if any(x in up for x in bad_phrases):
        return ""
    return key


def _first_valid_similarity_image(row: Any, cols: list[str]) -> str:
    for c in cols:
        try:
            val = row.get(c, "")
        except Exception:
            val = ""
        # kolom bisa berisi comma separated images
        raw = str(val or "")
        for part in raw.replace("\\", "/").split(","):
            k = _valid_similarity_image_key(part)
            if k:
                return k
    return ""


def _pick_text(row: Any, cols: list[str], fallback: str = "-") -> str:
    for c in cols:
        try:
            v = display_text(row.get(c, ""), "")
        except Exception:
            v = ""
        if v:
            return v
    return fallback



def _first_any_similarity_image(row: Any, cols: list[str]) -> str:
    """Ambil image valid jika tersedia; placeholder transport tidak dianggap image."""
    return _first_valid_similarity_image(row, cols)


def build_similarity_transport_rows(groups: pd.DataFrame, details: pd.DataFrame, limit: int | None = None) -> pd.DataFrame:
    """Bangun baris Similarity Transport dari SELURUH reused group Harmony.

    Berbeda dengan versi sebelumnya yang hanya mengambil pasangan valid, halaman ini mengikuti
    pola HTML lama: semua reused group ikut tampil. Group yang belum/tidak punya foto transport
    tetap masuk dengan kategori/status "Tidak Ada Foto Transport" sehingga chart total tetap benar.
    """
    cols = [
        "group_key", "group_id", "usage_count", "similarity", "estate", "afdeling", "blok", "tph", "tanggal",
        "kerani_panen", "kerani_transport", "transport_status", "similarity_score",
        "panen_image", "transport_image", "has_transport",
    ]
    if groups is None or groups.empty:
        return pd.DataFrame(columns=cols)

    g = groups.copy()
    if "group_key" not in g.columns:
        g["group_key"] = g.get("group_id", pd.Series(range(len(g)))).astype(str)

    if limit and int(limit) > 0:
        sort_cols = [col for col in ("usage_count", "group_id") if col in g.columns]
        if sort_cols:
            ascending = [False if col == "usage_count" else True for col in sort_cols]
            g = g.sort_values(sort_cols, ascending=ascending)
        g = g.head(int(limit)).copy()

    dgroups = {}
    if details is not None and not details.empty and "group_key" in details.columns:
        d = details.copy()
        valid_keys = set(g["group_key"].astype(str))
        d = d[d["group_key"].astype(str).isin(valid_keys)].copy()
        if not d.empty:
            dgroups = {str(k): v for k, v in d.groupby(d["group_key"].astype(str), dropna=False)}

    rows = []
    for _, gr in g.iterrows():
        gkey = str(gr.get("group_key", ""))
        dsub = dgroups.get(gkey)
        drow = None
        if dsub is not None and not dsub.empty:
            # Prioritaskan row yang memiliki transport valid agar detail bisa memperlihatkan pasangan.
            chosen = None
            for _, rr in dsub.iterrows():
                tr = _first_any_similarity_image(rr, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image"])
                if tr:
                    chosen = rr
                    break
            if chosen is None:
                chosen = dsub.iloc[0]
            drow = chosen

        def gp(cols, fallback="-"):
            if drow is not None:
                v = _pick_text(drow, cols, "")
                if v:
                    return v
            return _pick_text(gr, cols, fallback)

        group_id = _pick_text(gr, ["group_id"], gkey)
        usage = 0
        try:
            usage = int(float(gr.get("usage_count", 0) or 0))
        except Exception:
            usage = 0

        panen = ""
        if drow is not None:
            panen = _first_any_similarity_image(drow, ["image_name", "representative_image", "image_key", "file_name", "output_file"])
        if not panen:
            panen = _first_any_similarity_image(gr, ["representative_image", "image_key", "image_name", "file_name", "output_file"])

        transport = ""
        if drow is not None:
            transport = _first_any_similarity_image(drow, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image"])
        if not transport:
            transport = _first_any_similarity_image(gr, ["foto_kt_before", "foto_dt", "foto_ft", "transport_kt_before_image", "transport_dt_image", "transport_ft_image", "transport_image"])

        # Similarity pada Gold adalah similarity reuse foto panen, bukan skor
        # panen-vs-transport. Jangan membawa label legacy "Tidak Ada Gambar"
        # ke halaman Similarity Transport setelah KT Before sudah tersedia.
        sim = gp(
            [
                "transport_similarity_category",
                "similarity_transport",
                "transport_similarity",
            ],
            "",
        )
        status = gp(["transport_status", "status_transport", "status_transport_list"], "")
        similarity_score = None
        if drow is not None:
            try:
                raw_score = drow.get("transport_similarity_score")
                if raw_score is not None and not pd.isna(raw_score):
                    similarity_score = float(raw_score)
            except (TypeError, ValueError):
                similarity_score = None
        if not transport:
            # Untuk chart total, group tanpa transport tetap masuk sebagai kategori ini.
            sim = "Tidak Ada Foto Transport"
            status = "Tidak Ada Foto Transport"
        else:
            if not sim or sim == "-" or "tidak ada" in sim.lower():
                sim = "Perlu Verifikasi"
            if not status or status == "-" or "tidak ada" in status.lower():
                has_kt_before = bool(
                    drow is not None
                    and _first_any_similarity_image(
                        drow,
                        ["foto_kt_before", "transport_kt_before_image"],
                    )
                )
                status = (
                    "Ada Foto Transport KT Before"
                    if has_kt_before
                    else "Ada Foto Transport"
                )

        kerani_transport = "-"
        if drow is not None:
            kerani_transport = _pick_text(drow, ["kerani_dt", "kerani_ft", "nama_kerani_transport_dt", "nama_kerani_transport_ft", "kerani_transport"], "-")
        if kerani_transport == "-":
            kerani_transport = _pick_text(gr, ["kerani_dt_list", "kerani_ft_list", "kerani_transport", "status_transport_list"], "-")

        rows.append({
            "group_key": gkey,
            "group_id": group_id,
            "usage_count": usage,
            "similarity": sim,
            "estate": gp(["estate", "estate_primary", "estate_list"], "-"),
            "afdeling": gp(["afdeling", "afdeling_primary", "afdeling_list"], "-"),
            "blok": gp(["blok", "blok_primary", "blok_list"], "-"),
            "tph": gp(["tph", "tph_primary", "tph_list"], "-"),
            "tanggal": gp(["tanggal", "period_date", "date", "sql_date_list"], "-"),
            "kerani_panen": gp(["kerani_panen", "nama_kerani_panen", "kerani_panen_list"], "-"),
            "kerani_transport": kerani_transport,
            "transport_status": status,
            "similarity_score": similarity_score,
            "panen_image": panen,
            "transport_image": transport,
            "has_transport": bool(transport),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    out = out.sort_values(["usage_count", "group_id"], ascending=[False, True]).reset_index(drop=True)
    out["no"] = range(1, len(out) + 1)
    if limit and int(limit) > 0:
        out = out.head(int(limit)).reset_index(drop=True)
        out["no"] = range(1, len(out) + 1)
    return out


# Backward compatible alias for older callback names.
def build_similarity_pairs(groups: pd.DataFrame, details: pd.DataFrame, limit: int = 80) -> pd.DataFrame:
    return build_similarity_transport_rows(groups, details, limit=limit)


def similarity_page_info(system: str, rows: pd.DataFrame):
    # V38: tidak ada header naratif di halaman Similarity Transport.
    # Nama halaman sudah ada pada tab dan judul card. Ini sengaja dibuat kosong.
    if str(system or "").upper() != "HARMONY":
        return card(html.Div("Similarity Transport hanya tersedia untuk System Harmony.", className="muted big-empty"), "Similarity Transport")
    return html.Div()


def similarity_kpi_cards(rows: pd.DataFrame):
    if rows is None or rows.empty:
        return [kpi_card("Total Group", 0, "reused group")]
    has_transport = rows.get("has_transport", pd.Series([False] * len(rows))).astype(bool)
    usage = pd.to_numeric(rows.get("usage_count", 0), errors="coerce").fillna(0)
    return [
        kpi_card("Total Group", int(len(rows)), "reused group"),
        kpi_card("Total Usage", int(usage.sum()), "sum usage_count"),
        kpi_card("Ada Transport", int(has_transport.sum()), "group"),
        kpi_card("Tidak Ada Transport", int((~has_transport).sum()), "group"),
        kpi_card("Kerani Panen", int(rows["kerani_panen"].nunique()) if "kerani_panen" in rows.columns else 0, "unik"),
        kpi_card("Kerani Transport", int(rows.loc[has_transport, "kerani_transport"].nunique()) if "kerani_transport" in rows.columns else 0, "unik"),
    ]


def build_similarity_page_fig(
    rows: pd.DataFrame,
    details: pd.DataFrame | None = None,
):
    """Chart Similarity Transport dari seluruh reused group, termasuk Tidak Ada Foto Transport."""
    exact = transport_similarity_usage_summary(details)
    if not exact.empty:
        agg = exact.copy().rename(columns={"label": "similarity_clean"})
        agg = agg.sort_values("total_usage", ascending=True).tail(12)
        agg["plot_label"] = agg["similarity_clean"].apply(
            lambda v: _short_label(v, 30)
        )
        fig = go.Figure(go.Bar(
            x=agg["total_usage"],
            y=agg["plot_label"],
            orientation="h",
            text=agg["total_usage"],
            texttemplate="%{text:,.0f}",
            textposition="outside",
            cliponaxis=False,
            marker=dict(color="#003b7a"),
            customdata=agg[["similarity_clean", "total_usage"]].astype(str).to_numpy(),
            hovertemplate="<b>%{customdata[0]}</b><br>Total Usage: %{x:,.0f}<extra></extra>",
        ))
        fig.update_layout(bargap=0.34)
        return style_fig(
            fig,
            height=310,
            show_legend=False,
            margin=dict(l=70, r=54, t=20, b=48),
            x_title="Total Usage",
            y_title="",
        )
    if rows is None or rows.empty or "similarity" not in rows.columns:
        return empty_fig("Similarity Transport Usage")

    data = rows.copy()
    data["similarity_clean"] = data["similarity"].apply(lambda v: display_text(v, "Tidak Ada Foto Transport"))
    data["usage_num"] = pd.to_numeric(data.get("usage_count", 0), errors="coerce").fillna(0)
    # Jika usage kosong, tetap hitung 1 agar group muncul.
    data.loc[data["usage_num"] <= 0, "usage_num"] = 1
    agg = data.groupby("similarity_clean", dropna=False)["usage_num"].sum().reset_index(name="total_usage")
    agg = agg.sort_values("total_usage", ascending=False).head(12).sort_values("total_usage", ascending=True)
    agg["plot_label"] = agg["similarity_clean"].apply(lambda v: _short_label(v, 30))

    fig = go.Figure(go.Bar(
        x=agg["total_usage"],
        y=agg["plot_label"],
        orientation="h",
        text=agg["total_usage"],
        texttemplate="%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
        marker=dict(color="#003b7a"),
        customdata=agg[["similarity_clean", "total_usage"]].astype(str).to_numpy(),
        hovertemplate="<b>%{customdata[0]}</b><br>Total Usage: %{x:,.0f}<extra></extra>",
    ))
    fig.update_layout(bargap=0.34)
    return style_fig(
        fig,
        height=310,
        show_legend=False,
        margin=dict(l=180, r=80, t=12, b=48),
        x_title="Usage",
        y_title="",
    )


def sim_thumb(image_key: str, class_name: str = "similarity-list-thumb"):
    key = valid_image_key(image_key)
    if not key:
        return html.Div("No photo", className="similarity-mini-empty")
    return html.Img(
        src=PLACEHOLDER_SVG,
        className=f"{class_name} lazy-thumb",
        alt=key,
        title=key,
        **{"data-src": f"/api/thumb/{key}"},
    )


def corpus_thumb(image_key: str, class_name: str = "similarity-list-thumb"):
    key = valid_image_key(image_key)
    if not key:
        return html.Div("No photo", className="similarity-mini-empty")
    return html.Img(
        src=PLACEHOLDER_SVG,
        className=f"{class_name} lazy-thumb",
        alt=key,
        title=key,
        **{"data-src": f"/api/corpus-thumb/{key}"},
    )


TRANSACTION_TABLE_COLUMNS = [
    {"name": "Company", "id": "company"},
    {"name": "Tanggal", "id": "tanggal"},
    {"name": "OPH", "id": "oph"},
    {"name": "Estate", "id": "estate"},
    {"name": "Afdeling", "id": "afdeling"},
    {"name": "Blok", "id": "blok"},
    {"name": "TPH", "id": "tph"},
    {"name": "Kerani Panen", "id": "kerani_panen"},
    {"name": "Kerani Transport", "id": "kerani_transport"},
    {"name": "Kandidat Foto KP", "id": "panen_candidate_count"},
    {"name": "Kandidat Foto KT", "id": "transport_candidate_count"},
    {"name": "Similarity %", "id": "similarity_score"},
    {"name": "Keputusan", "id": "decision"},
]


def harmony_transaction_decision_fig(rows: pd.DataFrame):
    if rows is None or rows.empty:
        return empty_fig("Keputusan KP vs Transport")
    order = [
        "SAMA", "BERBEDA", "TIDAK ADA FOTO PANEN",
        "TIDAK ADA FOTO TRANSPORT",
        "SKOR TIDAK TERSEDIA",
    ]
    counts = rows["decision"].value_counts().reindex(order, fill_value=0)
    counts = counts[counts.gt(0)]
    if counts.empty:
        return empty_fig("Keputusan KP vs Transport")
    total = int(counts.sum())
    colors = {
        "SAMA": "#16a34a",
        "BERBEDA": "#dc2626",
        "TIDAK ADA FOTO PANEN": "#f97316",
        "TIDAK ADA FOTO TRANSPORT": "#94a3b8",
        "SKOR TIDAK TERSEDIA": "#f59e0b",
    }
    text_labels = [
        f"{int(value):,}<br>({(100.0 * int(value) / total if total else 0.0):.1f}%)"
        for value in counts.values.tolist()
    ]
    fig = go.Figure(go.Bar(
        x=counts.index.tolist(),
        y=counts.values.tolist(),
        text=text_labels,
        texttemplate="%{text}",
        textposition="outside",
        cliponaxis=False,
        marker_color=[colors.get(value, "#64748b") for value in counts.index],
        customdata=[[label, int(value)] for label, value in zip(counts.index.tolist(), counts.values.tolist())],
        hovertemplate="<b>%{customdata[0]}</b><br>Pasangan: %{y:,.0f}<br>Persentase: %{text}<extra></extra>",
    ))
    return style_fig(
        fig,
        height=320,
        show_legend=False,
        margin=dict(l=45, r=25, t=15, b=55),
        x_title="Keputusan",
        y_title="Jumlah OPH",
    )




TRANSACTION_DECISION_ORDER = [
    "SAMA", "BERBEDA", "TIDAK ADA FOTO PANEN",
    "TIDAK ADA FOTO TRANSPORT",
    "SKOR TIDAK TERSEDIA",
]
TRANSACTION_DECISION_COLORS = {
    "SAMA": "#16a34a",
    "BERBEDA": "#dc2626",
    "TIDAK ADA FOTO PANEN": "#f97316",
    "TIDAK ADA FOTO TRANSPORT": "#94a3b8",
    "SKOR TIDAK TERSEDIA": "#f59e0b",
}


def _transaction_filter_options(rows: pd.DataFrame, column: str):
    if rows is None or rows.empty or column not in rows.columns:
        return []
    values = sorted({
        str(value).strip()
        for value in rows[column].tolist()
        if str(value or "").strip()
        and str(value).strip().lower() not in {"nan", "nat", "none"}
    })
    return [{"label": value, "value": value} for value in values]


def harmony_transaction_breakdown_fig(
    rows: pd.DataFrame,
    dimension: str,
    title: str,
    *,
    top_n: int = 15,
    horizontal: bool = False,
):
    """Stacked breakdown dari populasi transaksi terfilter yang sama, lengkap dengan persentase dan total.
    Untuk breakdown afdeling, label dipisahkan per estate agar Afd yang sama di estate berbeda tidak tercampur.
    """
    if rows is None or rows.empty or dimension not in rows.columns:
        return empty_fig(title)

    base_cols = [dimension, "decision"]
    for extra in ("company", "estate", "afdeling"):
        if extra in rows.columns and extra not in base_cols:
            base_cols.append(extra)
    data = rows[base_cols].copy()
    for col in (dimension, "company", "estate", "afdeling"):
        if col in data.columns:
            data[col] = data[col].fillna("").astype(str).str.strip()
            data.loc[data[col].isin(["", "nan", "NaT", "None"]), col] = "TIDAK ADA"
    data["decision"] = data["decision"].fillna(
        "SKOR TIDAK TERSEDIA"
    ).astype(str).str.upper()
    data.loc[
        ~data["decision"].isin(TRANSACTION_DECISION_ORDER), "decision"
    ] = "SKOR TIDAK TERSEDIA"

    if dimension == "afdeling":
        company_series = data.get("company", pd.Series(["TIDAK ADA"] * len(data), index=data.index)).fillna("TIDAK ADA").astype(str)
        estate_series = data.get("estate", pd.Series(["TIDAK ADA"] * len(data), index=data.index)).fillna("TIDAK ADA").astype(str)
        afd_series = data.get("afdeling", pd.Series(["TIDAK ADA"] * len(data), index=data.index)).fillna("TIDAK ADA").astype(str)
        data["group_key"] = company_series + "|||" + estate_series + "|||" + afd_series
        data["group_label"] = company_series + " | " + estate_series + " / Afd " + afd_series
    elif dimension == "estate":
        company_series = data.get("company", pd.Series(["TIDAK ADA"] * len(data), index=data.index)).fillna("TIDAK ADA").astype(str)
        estate_series = data.get("estate", pd.Series(["TIDAK ADA"] * len(data), index=data.index)).fillna("TIDAK ADA").astype(str)
        data["group_key"] = company_series + "|||" + estate_series
        data["group_label"] = company_series + " | " + estate_series
    else:
        dim_series = data[dimension].fillna("TIDAK ADA").astype(str)
        data["group_key"] = dim_series
        data["group_label"] = dim_series

    grouped = (
        data.groupby(["group_key", "group_label", "decision"], dropna=False)
        .size()
        .reset_index(name="total")
    )
    totals = grouped.groupby(["group_key", "group_label"], dropna=False)["total"].sum().reset_index()
    totals = totals.sort_values("total", ascending=False)

    if dimension == "month":
        totals = totals.sort_values("group_key", ascending=True)
        category_keys = totals["group_key"].astype(str).tolist()
    elif dimension == "company":
        totals = totals.sort_values("group_key", ascending=True)
        category_keys = totals["group_key"].astype(str).tolist()
    else:
        category_keys = totals.head(max(1, int(top_n)))["group_key"].astype(str).tolist()
        if horizontal:
            category_keys = list(reversed(category_keys))

    grouped = grouped[grouped["group_key"].astype(str).isin(category_keys)].copy()
    label_map = {
        str(row["group_key"]): str(row["group_label"])
        for _, row in totals.iterrows()
    }
    label_order = [
        _short_label(label_map.get(str(key), str(key)), 34 if horizontal else 22)
        for key in category_keys
    ]
    total_map = {
        str(row["group_key"]): int(row["total"])
        for _, row in totals.iterrows()
    }
    overall_total = sum(total_map.get(str(key), 0) for key in category_keys)

    segment_label_threshold = 2.0 if dimension == "afdeling" else 4.0
    fig = go.Figure()
    for decision in TRANSACTION_DECISION_ORDER:
        part = grouped[grouped["decision"].eq(decision)].set_index("group_key")
        values = [int(part.at[key, "total"]) if key in part.index else 0 for key in category_keys]
        percents = [
            (100.0 * val / total_map.get(str(cat), 0)) if total_map.get(str(cat), 0) else 0.0
            for cat, val in zip(category_keys, values)
        ]
        texts = [
            f"{pct:.1f}%" if val > 0 and pct >= segment_label_threshold else ""
            for val, pct in zip(values, percents)
        ]
        custom = []
        for cat, val, pct in zip(category_keys, values, percents):
            cat_str = str(cat)
            parts = cat_str.split("|||")
            company_val = parts[0] if len(parts) >= 1 else ""
            estate_val = parts[1] if len(parts) >= 2 else ""
            afd_val = parts[2] if len(parts) >= 3 else ""
            custom.append([
                cat_str,
                label_map.get(cat_str, cat_str),
                decision,
                val,
                total_map.get(cat_str, 0),
                pct,
                company_val,
                estate_val,
                afd_val,
            ])
        if horizontal:
            fig.add_trace(go.Bar(
                name=decision,
                x=values,
                y=label_order,
                orientation="h",
                marker_color=TRANSACTION_DECISION_COLORS[decision],
                text=texts,
                texttemplate="%{text}",
                textposition="inside",
                insidetextanchor="middle",
                textfont=dict(size=10, color="#FFFFFF"),
                cliponaxis=False,
                customdata=custom,
                hovertemplate="<b>%{customdata[1]}</b><br>%{customdata[2]}: %{customdata[3]:,}<br>Total lokasi: %{customdata[4]:,}<br>Persentase lokasi: %{customdata[5]:.1f}%<extra></extra>",
            ))
        else:
            fig.add_trace(go.Bar(
                name=decision,
                x=label_order,
                y=values,
                marker_color=TRANSACTION_DECISION_COLORS[decision],
                text=texts,
                texttemplate="%{text}",
                textposition="inside",
                insidetextanchor="middle",
                customdata=custom,
                hovertemplate="<b>%{customdata[1]}</b><br>%{customdata[2]}: %{customdata[3]:,}<br>Total periode: %{customdata[4]:,}<br>Persentase periode: %{customdata[5]:.1f}%<extra></extra>",
            ))
    fig.update_layout(barmode="stack", bargap=0.28)

    if horizontal:
        max_total = max([total_map.get(str(cat), 0) for cat in category_keys] or [0])
        for label, value in zip(label_order, category_keys):
            total_value = total_map.get(str(value), 0)
            pct_all = (100.0 * total_value / overall_total) if overall_total else 0.0
            fig.add_annotation(
                x=total_value + max(max_total * 0.015, 1),
                y=label,
                text=f"{total_value:,} ({pct_all:.1f}%)",
                showarrow=False,
                xanchor="left",
                yanchor="middle",
                font=dict(size=10, color="#334155"),
            )
    else:
        max_total = max([total_map.get(str(cat), 0) for cat in category_keys] or [0])
        for label, value in zip(label_order, category_keys):
            total_value = total_map.get(str(value), 0)
            fig.add_annotation(
                x=label,
                y=total_value + max(max_total * 0.02, 1),
                text=f"{total_value:,}",
                showarrow=False,
                xanchor="center",
                yanchor="bottom",
                font=dict(size=10, color="#334155"),
            )

    base_height = 185 + (34 * len(category_keys) if horizontal else 0)
    if dimension == "afdeling" and horizontal:
        base_height += 30
    left_margin = 220 if dimension == "afdeling" and horizontal else (190 if horizontal else 55)
    right_margin = 85 if horizontal else 35
    return style_fig(
        fig,
        height=max(360, min(760, base_height)),
        show_legend=True,
        margin=dict(l=left_margin, r=right_margin, t=18, b=70),
        x_title="Jumlah OPH" if horizontal else ("Periode" if dimension == "month" else dimension.replace("_", " ").title()),
        y_title="" if horizontal else "Jumlah OPH",
    )




def harmony_transaction_status_fig(rows: pd.DataFrame):
    title = "Breakdown Ketersediaan Foto"
    if rows is None or rows.empty or "transaction_status" not in rows.columns:
        return empty_fig(title)
    values = rows["transaction_status"].fillna("SKOR TIDAK TERSEDIA").astype(str).str.strip()
    values = values.where(values.ne(""), "SKOR TIDAK TERSEDIA")
    values = values[values.str.upper().ne("SIAP TRANSAKSI")]
    if values.empty:
        return empty_fig(title)
    counts = values.value_counts().sort_values(ascending=True)
    total = int(counts.sum())
    colors = [
        "#f97316" if value == "TIDAK ADA FOTO PANEN"
        else "#94a3b8" if value == "TIDAK ADA FOTO TRANSPORT"
        else "#f59e0b"
        for value in counts.index
    ]
    texts = [f"{int(value):,} ({(100.0 * int(value) / total if total else 0.0):.1f}%)" for value in counts.values]
    fig = go.Figure(go.Bar(
        x=counts.values,
        y=[_short_label(value, 38) for value in counts.index],
        orientation="h",
        text=texts,
        texttemplate="%{text}",
        textposition="outside",
        cliponaxis=False,
        marker_color=colors,
        customdata=[[label, int(value), (100.0 * int(value) / total if total else 0.0)] for label, value in zip(counts.index, counts.values)],
        hovertemplate="<b>%{customdata[0]}</b><br>OPH: %{customdata[1]:,}<br>Persentase: %{customdata[2]:.1f}%<extra></extra>",
    ))
    return style_fig(
        fig,
        height=max(320, min(520, 150 + 34 * len(counts))),
        show_legend=False,
        margin=dict(l=210, r=90, t=18, b=50),
        x_title="Jumlah OPH",
        y_title="",
    )




def harmony_transaction_people_fig(
    rows: pd.DataFrame,
    column: str,
    title: str,
    top_n: int = 15,
):
    """Top kerani pada unit transaksi OPH (bukan jumlah file gambar), dengan persentase."""
    if rows is None or rows.empty or column not in rows.columns:
        return empty_fig(title)
    values = (
        rows[column]
        .fillna("")
        .astype(str)
        .str.split(r"\s*(?:/|,)\s*", regex=True)
        .explode()
        .str.strip()
    )
    values = values[
        values.ne("")
        & ~values.str.upper().isin({"-", "UNKNOWN", "N/A", "TIDAK ADA", "NAN"})
    ]
    if values.empty:
        return empty_fig(title)
    counts = values.value_counts().head(max(1, int(top_n)))
    total = int(counts.sum())
    counts = counts.sort_values(ascending=True)
    texts = [f"{int(value):,} ({(100.0 * int(value) / total if total else 0.0):.1f}%)" for value in counts.values]
    fig = go.Figure(go.Bar(
        x=counts.values,
        y=[_short_label(value, 32) for value in counts.index],
        orientation="h",
        text=texts,
        texttemplate="%{text}",
        textposition="outside",
        cliponaxis=False,
        customdata=[[name, int(value), (100.0 * int(value) / total if total else 0.0)] for name, value in zip(counts.index, counts.values)],
        hovertemplate="<b>%{customdata[0]}</b><br>OPH: %{customdata[1]:,}<br>Persentase: %{customdata[2]:.1f}%<extra></extra>",
        marker_color="#003d79" if column == "kerani_panen" else "#0f766e",
    ))
    return style_fig(
        fig,
        height=max(340, min(600, 150 + 30 * len(counts))),
        show_legend=False,
        margin=dict(l=190, r=95, t=18, b=48),
        x_title="Jumlah OPH",
        y_title="",
    )




def harmony_transaction_detail(row: dict[str, Any] | None):
    if not row:
        return card(
            html.Div(
                "Pilih satu transaksi pada tabel. Foto Panen dan Transport akan dibandingkan berdampingan di sini.",
                className="muted big-empty",
            ),
            "Bukti Transaksi",
            class_name="transaction-detail-card",
        )

    decision = display_text(row.get("decision"), "SKOR TIDAK TERSEDIA").upper()
    score = fmt_similarity(row.get("similarity_score"))
    if decision == "SAMA":
        badge_class = "transaction-decision same"
        verdict_tone = "same"
        verdict_text = (
            f"Similarity berada pada atau di atas ambang {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%. "
            "Foto Transport konsisten secara visual dengan foto Panen."
        )
    elif decision == "BERBEDA":
        badge_class = "transaction-decision different"
        verdict_tone = "different"
        verdict_text = (
            f"Similarity berada di bawah ambang {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%. "
            "Pasangan foto ini menjadi prioritas pemeriksaan visual."
        )
    elif "TRANSPORT" in decision and ("TIDAK" in decision or "ADA" in decision):
        badge_class = "transaction-decision pending"
        verdict_tone = "missing"
        verdict_text = "Foto Transport belum tersedia, sehingga pasangan Panen vs Transport belum dapat dinilai."
    else:
        badge_class = "transaction-decision pending"
        verdict_tone = "pending"
        verdict_text = "Similarity belum tersedia untuk transaksi ini. Periksa ketersediaan foto dan proses similarity."

    panen_image_key = valid_image_key(row.get("panen_file_uid"))
    transport_image_key = valid_image_key(row.get("transport_file_uid"))

    return card([
        html.Div([
            html.Div([
                html.Div("FOTO PANEN", className="compare-photo-kicker"),
                html.Div("Kerani Panen", className="compare-photo-title"),
                corpus_thumb(panen_image_key, "transaction-photo compare-photo"),
                html.Div(_short_label(row.get("panen_file_name"), 72), className="transaction-file-name"),
                html.Div(display_text(row.get("kerani_panen")), className="compare-person"),
            ], className="compare-photo-panel panen"),
            html.Div([
                html.Div("HASIL", className="compare-verdict-kicker"),
                html.Span(decision, className=badge_class),
                html.Div(score, className="compare-score"),
                html.Div("Similarity", className="compare-score-label"),
                html.Div(verdict_text, className="compare-verdict-text"),
            ], className=f"compare-verdict {verdict_tone}"),
            html.Div([
                html.Div("FOTO TRANSPORT", className="compare-photo-kicker"),
                html.Div("Kerani Transport", className="compare-photo-title"),
                sim_thumb(transport_image_key, "transaction-photo compare-photo"),
                html.Div(_short_label(row.get("transport_file_name"), 72), className="transaction-file-name"),
                html.Div(display_text(row.get("kerani_transport")), className="compare-person"),
            ], className="compare-photo-panel transport"),
        ], className="transaction-compare-grid"),
        html.Div([
            _readout_chip("Company", row.get("company")),
            _readout_chip("Tanggal", row.get("tanggal")),
            _readout_chip("OPH", row.get("oph"), "wide"),
            _readout_chip("Estate", row.get("estate")),
            _readout_chip("Afdeling", row.get("afdeling")),
            _readout_chip("Blok", row.get("blok")),
            _readout_chip("TPH", row.get("tph")),
            _readout_chip("Mandor", row.get("mandor"), "wide"),
        ], className="transaction-context-chips"),
    ], "Bukti Transaksi", class_name="transaction-detail-card transaction-detail-v179")


def similarity_list_with_photos(rows: pd.DataFrame, selected_idx: int | None = None, limit: int | None = None):
    if rows is None or rows.empty:
        return card(html.Div("Tidak ada data similarity transport pada filter ini.", className="muted big-empty"), "List Similarity Transport")
    if limit is None:
        limit = SIMILARITY_LIST_LIMIT
    show = rows.copy() if not limit or int(limit) <= 0 else rows.head(int(limit)).copy()
    items = []
    for idx, (_, r) in enumerate(show.iterrows()):
        has_tr = bool(r.get("has_transport"))
        row_class = "similarity-list-row selected" if selected_idx == idx else "similarity-list-row"
        status_class = "similarity-list-badge ok" if has_tr else "similarity-list-badge missing"
        items.append(html.Div([
            html.Div([
                sim_thumb(r.get("panen_image"), "similarity-list-thumb"),
                sim_thumb(r.get("transport_image"), "similarity-list-thumb"),
            ], className="similarity-list-photos"),
            html.Div([
                html.Div([
                    html.Span(f"Group {display_text(r.get('group_id'))}", className="similarity-list-title"),
                    html.Span(_short_label(r.get("similarity"), 24), className=status_class),
                ], className="similarity-list-head"),
                html.Div(f"Usage: {fmt_num(r.get('usage_count'), 0)} | {display_text(r.get('estate'))} / Afd {display_text(r.get('afdeling'))} / Blok {display_text(r.get('blok'))} / TPH {display_text(r.get('tph'))}", className="similarity-list-line"),
                html.Div(f"Tanggal: {display_text(r.get('tanggal'))}", className="similarity-list-line"),
                html.Div(f"Panen: {_short_label(r.get('kerani_panen'), 34)}  |  Transport: {_short_label(r.get('kerani_transport'), 34)}", className="similarity-list-line strong"),
            ], className="similarity-list-meta"),
        ],
            id={"type": "similarity-row", "index": idx},
            n_clicks=0,
            role="button",
            tabIndex=0,
            className=row_class,
            **{"data-group-key": str(r.get("group_key", "")), "data-group-id": str(r.get("group_id", ""))}
        ))
    return card(html.Div(items, className="similarity-list"), "List Similarity Transport")


def similarity_group_card_list(rows: pd.DataFrame, limit: int | None = None):
    """V40: list estetik per reused group untuk Similarity Transport.

    Satu card = satu group. Klik card membuka Detail Group.
    Tidak memakai kolom company/source supaya tampilan tidak stretch di awal.
    """
    if rows is None or rows.empty:
        return card(html.Div("Tidak ada data similarity transport pada filter ini.", className="muted big-empty"), "Group Similarity Transport")
    if limit is None:
        limit = SIMILARITY_LIST_LIMIT
    show = rows.copy() if not limit or int(limit) <= 0 else rows.head(int(limit)).copy()
    items = []
    for _, r in show.iterrows():
        has_tr = bool(r.get("has_transport"))
        status_class = "similarity-list-badge ok" if has_tr else "similarity-list-badge missing"
        group_key = str(r.get("group_key", "") or r.get("group_id", ""))
        items.append(html.Div([
            html.Div([
                sim_thumb(r.get("panen_image"), "similarity-list-thumb"),
                sim_thumb(r.get("transport_image"), "similarity-list-thumb"),
            ], className="similarity-list-photos"),
            html.Div([
                html.Div([
                    html.Span(f"Group {display_text(r.get('group_id'))}", className="similarity-list-title"),
                    html.Span(_short_label(r.get("similarity"), 26), className=status_class),
                ], className="similarity-list-head"),
                html.Div([
                    html.Span(f"Usage {fmt_num(r.get('usage_count'), 0)}", className="similarity-chip"),
                    html.Span(f"{display_text(r.get('estate'))}", className="similarity-chip soft"),
                    html.Span(f"Afd {display_text(r.get('afdeling'))}", className="similarity-chip soft"),
                    html.Span(f"Blok {display_text(r.get('blok'))}", className="similarity-chip soft"),
                    html.Span(f"TPH {display_text(r.get('tph'))}", className="similarity-chip soft"),
                ], className="similarity-chip-row"),
                html.Div(f"Tanggal: {display_text(r.get('tanggal'))}", className="similarity-list-line"),
                html.Div(f"Panen: {_short_label(r.get('kerani_panen'), 34)}  |  Transport: {_short_label(r.get('kerani_transport'), 34)}", className="similarity-list-line strong"),
                html.Div(f"Status Transport: {_short_label(r.get('transport_status'), 46)}", className="similarity-list-line"),
                html.Div(
                    "Skor KP–KT: "
                    + (
                        f"{float(r.get('similarity_score')):.2f}%"
                        if r.get("similarity_score") is not None
                        and not pd.isna(r.get("similarity_score"))
                        else "Skor tidak tersedia"
                    ),
                    className="similarity-list-line strong",
                ),
            ], className="similarity-list-meta"),
        ],
            id={"type": "similarity-group-card", "index": group_key},
            n_clicks=0,
            role="button",
            tabIndex=0,
            className="similarity-list-row group-card",
        ))
    return card(html.Div(items, className="similarity-list group-list"), "Group Similarity Transport")

def similarity_detail_panel(pairs: pd.DataFrame, selected_idx: int | None = None):
    if pairs is None or pairs.empty:
        return card(html.Div("Tidak ada detail similarity transport pada filter ini.", className="muted big-empty"), "Detail Similarity Transport")
    if selected_idx is None or selected_idx < 0 or selected_idx >= len(pairs):
        return html.Div([
            html.Div("Klik salah satu group pada list untuk membuka detail foto panen dan transport.", className="similarity-detail-hint")
        ], id="similarity-detail-anchor", className="similarity-detail-empty")
    r = pairs.iloc[int(selected_idx)]
    return card(html.Div([
        html.Div([
            html.Div([
                html.Div(f"Group {display_text(r.get('group_id'))}", className="similarity-card-title"),
                html.Div(f"Similarity: {display_text(r.get('similarity'))}", className="similarity-badge"),
            ], className="similarity-card-head"),
            html.Div([
                thumb_box(r.get("panen_image"), "Foto Panen / Kerani Panen", "PANEN"),
                thumb_box(r.get("transport_image"), "Foto Transport / Kerani Transport", "TRANSPORT"),
            ], className="similarity-photo-row detail"),
        ], className="similarity-detail-photos"),
        html.Div([
            html.B("Lokasi: "), html.Span(f"{display_text(r.get('estate'))} / Afd {display_text(r.get('afdeling'))} / Blok {display_text(r.get('blok'))} / TPH {display_text(r.get('tph'))}"), html.Br(),
            html.B("Tanggal: "), html.Span(display_text(r.get("tanggal"))), html.Br(),
            html.B("Kerani Panen: "), html.Span(_short_label(r.get("kerani_panen"), 60)), html.Br(),
            html.B("Kerani Transport: "), html.Span(_short_label(r.get("kerani_transport"), 60)), html.Br(),
            html.B("Status: "), html.Span(display_text(r.get("transport_status"))), html.Br(),
            html.B("Skor KP–KT: "), html.Span(
                f"{float(r.get('similarity_score')):.2f}%"
                if r.get("similarity_score") is not None
                and not pd.isna(r.get("similarity_score"))
                else "Skor tidak tersedia"
            ), html.Br(),
            html.B("Foto Panen: "), html.Span(_short_label(r.get("panen_image"), 70)), html.Br(),
            html.B("Foto Transport: "), html.Span(_short_label(r.get("transport_image"), 70)),
        ], className="similarity-meta detail"),
    ], className="similarity-detail-card"), "Detail Similarity Transport")



def similarity_fast_preview(rows: pd.DataFrame, selected_group_key: str | None = None):
    """Preview cepat untuk Similarity Transport.

    Tidak pindah ke Detail Group dan tidak render frame_gallery besar.
    Hanya memakai thumbnail/icon dari similarity rows yang sudah tersimpan di dcc.Store.
    """
    if rows is None or rows.empty or not selected_group_key:
        return html.Div()
    data = rows.copy()
    if "group_key" not in data.columns:
        return html.Div()
    hit = data[data["group_key"].astype(str) == str(selected_group_key)]
    if hit.empty:
        return html.Div()
    r = hit.iloc[0]
    has_tr = bool(r.get("has_transport"))
    status_class = "similarity-list-badge ok" if has_tr else "similarity-list-badge missing"
    return card(html.Div([
        html.Div([
            html.Div([
                html.Div(f"Group {display_text(r.get('group_id'))}", className="similarity-card-title"),
                html.Div(_short_label(r.get("similarity"), 34), className=status_class),
            ], className="similarity-card-head"),
            html.Div([
                html.Div([
                    html.Div("Foto Panen", className="similarity-preview-label"),
                    sim_thumb(r.get("panen_image"), "similarity-preview-thumb"),
                ], className="similarity-preview-img-box"),
                html.Div([
                    html.Div("Foto Transport", className="similarity-preview-label"),
                    sim_thumb(r.get("transport_image"), "similarity-preview-thumb"),
                ], className="similarity-preview-img-box"),
            ], className="similarity-preview-photos"),
        ], className="similarity-preview-left"),
        html.Div([
            html.Div([
                html.Span(f"Usage {fmt_num(r.get('usage_count'), 0)}", className="similarity-chip"),
                html.Span(f"{display_text(r.get('estate'))}", className="similarity-chip soft"),
                html.Span(f"Afd {display_text(r.get('afdeling'))}", className="similarity-chip soft"),
                html.Span(f"Blok {display_text(r.get('blok'))}", className="similarity-chip soft"),
                html.Span(f"TPH {display_text(r.get('tph'))}", className="similarity-chip soft"),
            ], className="similarity-chip-row"),
            html.Div(f"Tanggal: {display_text(r.get('tanggal'))}", className="similarity-list-line"),
            html.Div(f"Kerani Panen: {_short_label(r.get('kerani_panen'), 56)}", className="similarity-list-line strong"),
            html.Div(f"Kerani Transport: {_short_label(r.get('kerani_transport'), 56)}", className="similarity-list-line strong"),
            html.Div(f"Status Transport: {_short_label(r.get('transport_status'), 70)}", className="similarity-list-line"),
            html.Div(
                "Skor KP–KT: "
                + (
                    f"{float(r.get('similarity_score')):.2f}%"
                    if r.get("similarity_score") is not None
                    and not pd.isna(r.get("similarity_score"))
                    else "Skor tidak tersedia"
                ),
                className="similarity-list-line strong",
            ),
        ], className="similarity-preview-meta"),
    ], className="similarity-preview-card"), "Preview Similarity Transport")


def _initial_company_state():
    try:
        options = get_company_options(DEFAULT_SYSTEM)
        value = _choose_company_value(DEFAULT_SYSTEM, None, options)
        print(
            "[DASHBOARD] initial "
            f"system={DEFAULT_SYSTEM} company={value or '-'} "
            f"options={[o.get('value') for o in options]}",
            flush=True,
        )
        return options, value
    except Exception as exc:
        print(f"[DASHBOARD][INITIAL FILTER ERROR] {exc}", flush=True)
        traceback.print_exc()
        return [], None


INITIAL_COMPANY_OPTIONS, INITIAL_COMPANY_VALUE = _initial_company_state()


app.layout = html.Div([
    dcc.Store(id="selected-group-store"),
    dcc.Store(id="selected-similarity-store"),
    dcc.Store(id="groups-current-store"),
    dcc.Store(id="similarity-rows-store"),
    html.Div(id="similarity-scroll-dummy", style={"display": "none"}),

    html.Div([
        html.Div([
            html.Div("AI-powered Golden Eye", className="eyebrow"),
            html.H1("Golden Eye Control Center"),
        ]),
    ], className="hero clean-hero"),

    html.Div([
        dcc.Tabs(
            id="module-tabs",
            value="reused-dashboard",
            className="module-tabs",
            children=[
                dcc.Tab(
                    label="Dashboard Foto Berulang",
                    value="reused-dashboard",
                    className="module-tab",
                    selected_className="module-tab selected",
                ),
                dcc.Tab(
                    label="Validasi Panen vs Transport",
                    value="harmony-transaction",
                    className="module-tab",
                    selected_className="module-tab selected",
                ),
            ],
        ),
    ], className="module-nav-shell"),

    html.Div([
    html.Div([
        dcc.Tabs(
            id="page-tabs",
            value="overview",
            className="nav-tabs",
            children=[
                dcc.Tab(label="Ringkasan", value="overview", className="nav-tab", selected_className="nav-tab selected"),
                dcc.Tab(label="Reused Groups", value="groups", className="nav-tab", selected_className="nav-tab selected"),
                dcc.Tab(label="Similarity Transport", value="similarity", className="nav-tab", selected_className="nav-tab selected"),
                dcc.Tab(label="Detail Group", value="detail", className="nav-tab", selected_className="nav-tab selected"),
            ],
        )
    ], className="nav-shell"),

    html.Div([
        html.Div([html.Label("System"), dcc.Dropdown(id="system-filter", options=SYSTEM_OPTIONS, value=DEFAULT_SYSTEM, clearable=False)]),
        html.Div([html.Label("Company"), dcc.Dropdown(
            id="company-filter",
            options=INITIAL_COMPANY_OPTIONS,
            value=INITIAL_COMPANY_VALUE,
            multi=True,
            placeholder="Semua company",
            clearable=True,
        )]),
        html.Div([html.Label("Estate"), dcc.Dropdown(id="estate-filter", multi=True, placeholder="Mengikuti company")]),
        html.Div([html.Label("Afdeling"), dcc.Dropdown(id="afdeling-filter", multi=True, placeholder="Mengikuti estate")]),
        html.Div([html.Label("Blok"), dcc.Dropdown(id="block-filter", multi=True, placeholder="Mengikuti afdeling")]),
        html.Div([html.Label("Bulan"), dcc.Dropdown(id="month-filter", multi=True, placeholder="Mengikuti lokasi")]),
        html.Div([html.Label("Reuse Category"), dcc.Dropdown(id="reuse-filter", multi=True, placeholder="Semua kategori")]),
        html.Div([html.Label("Date Category"), dcc.Dropdown(id="date-filter", multi=True, placeholder="Semua tanggal")]),
        html.Div([html.Label("Similarity"), dcc.Dropdown(id="similarity-filter", multi=True, placeholder="Semua similarity")]),
        html.Div([html.Label("Cari OPH / File / Nama"), dcc.Input(id="search-box", type="text", placeholder="ketik OPH / file / nama...", debounce=True)]),
        html.Button("Reset Pilihan Group", id="reset-selection", n_clicks=0, className="ghost-btn"),
    ], className="filter-grid filter-panel"),

    html.Div(id="kpi-row", className="kpi-row"),

    html.Div([
        html.Div([
            card(
                dcc.Graph(
                    id="source-summary-chart",
                    className="company-summary-graph",
                    style={"height": "260px", "minHeight": "260px", "maxHeight": "260px", "width": "100%"},
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Company Summary",
                class_name="company-summary-card",
            ),
        ], className="grid one company-summary-row"),

        html.Div([
            card(
                dcc.Graph(
                    id="top-reused-chart",
                    className="top-reused-graph",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": False},
                ),
                "Top Reused Photo by Month",
                class_name="top-reused-card",
            ),
        ], className="grid one"),

        html.Div([
            card(dcc.Graph(id="monthly-pattern-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Monthly Pattern"),
            card(dcc.Graph(id="estate-monthly-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Estate Monthly Pattern"),
        ], className="grid two overview-main"),

        html.Div([
            card(dcc.Graph(id="reuse-category-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Reuse Category"),
            card(dcc.Graph(id="date-category-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Date Category"),
        ], className="grid two"),

        html.Div([
            card(dcc.Graph(id="reuse-date-heatmap-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Reuse x Date Category"),
            card(dcc.Graph(id="afdeling-monthly-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Afdeling Monthly Pattern"),
        ], className="grid two"),

        html.Div([
            card(dcc.Graph(id="estate-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Estate Summary"),
            html.Div(
                card(dcc.Graph(id="similarity-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Similarity Usage Transport (Harmony)"),
                id="similarity-card-wrap",
            ),
            card(dcc.Graph(id="usage-dist-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Usage Distribution"),
        ], className="grid three"),

        html.Div([
            card(dcc.Graph(id="top-pemanen-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Top Pemanen Reused"),
            card(dcc.Graph(id="top-kerani-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Top Kerani Panen"),
            card(dcc.Graph(id="top-mandor-chart", config={"displaylogo": False, "displayModeBar": False, "responsive": True}), "Top Mandor"),
        ], className="grid three"),
    ], id="page-overview", className="view-page active"),

    html.Div([
        card([
            html.Div(id="group-list-summary"),
            table(pd.DataFrame(), table_id="group-table", selectable=True, page_size=25),
        ], "Reused Group List"),
    ], id="page-groups", className="view-page"),

    html.Div([
        html.Div(id="similarity-harmony-info"),
        html.Div(id="similarity-page-kpi-row", className="kpi-row"),
        html.Div([
            card(dcc.Graph(
                id="similarity-page-chart",
                className="similarity-page-graph",
                config={"displaylogo": False, "displayModeBar": False, "responsive": False},
            ), "Similarity Transport Usage", class_name="similarity-chart-card compact"),
            html.Div(id="similarity-pair-table-wrap"),
        ], className="similarity-list-first-grid"),
        html.Div(id="similarity-example-wrap", className="similarity-detail-wrap"),
    ], id="page-similarity", className="view-page"),

    html.Div([
        html.Div(id="selected-detail-panel"),
        card(html.Div(id="detail-table-wrap"), "Detail Transaksi"),
    ], id="page-detail", className="view-page"),

    ], id="module-reused-dashboard"),

    html.Div([
        html.Div([
            html.Div([
                html.Div("Harmony Transaction Intelligence", className="eyebrow"),
                html.H2("Validasi Foto Kerani Panen vs Transport"),
            ]),
            html.Div([
                html.Span(
                    f"SAMA ≥ {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%",
                    className="transaction-threshold same",
                ),
                html.Span(
                    f"BERBEDA < {HARMONY_TRANSACTION_SAME_THRESHOLD:g}%",
                    className="transaction-threshold different",
                ),
            ], className="transaction-thresholds"),
        ], className="transaction-hero"),

        html.Div([
            html.Div([html.Label("Company Harmony"), dcc.Dropdown(
                id="transaction-company-filter",
                options=[
                    {"label": "PWP", "value": "PWP"},
                    {"label": "DIN", "value": "DIN"},
                ],
                value=["PWP", "DIN"],
                multi=True,
                clearable=False,
            )]),
            html.Div([html.Label("Tahun"), dcc.Dropdown(
                id="transaction-year-filter",
                options=[
                    {"label": "2026", "value": 2026},
                    {"label": "2027", "value": 2027},
                ],
                value=2026,
                clearable=False,
            )]),
            html.Div([html.Label("Bulan"), dcc.Dropdown(
                id="transaction-month-filter",
                options=[],
                multi=True,
                placeholder="Semua bulan",
            )]),
            html.Div([html.Label("Estate"), dcc.Dropdown(
                id="transaction-estate-filter",
                options=[],
                multi=True,
                placeholder="Semua estate",
            )]),
            html.Div([html.Label("Afdeling"), dcc.Dropdown(
                id="transaction-afdeling-filter",
                options=[],
                multi=True,
                placeholder="Semua afdeling",
            )]),
            html.Div([html.Label("Blok"), dcc.Dropdown(
                id="transaction-block-filter",
                options=[],
                multi=True,
                placeholder="Semua blok",
            )]),
            html.Div([html.Label("Keputusan"), dcc.Dropdown(
                id="transaction-decision-filter",
                options=[
                    {"label": "SAMA", "value": "SAMA"},
                    {"label": "BERBEDA", "value": "BERBEDA"},
                    {
                        "label": "TIDAK ADA FOTO PANEN",
                        "value": "TIDAK ADA FOTO PANEN",
                    },
                    {
                        "label": "TIDAK ADA FOTO TRANSPORT",
                        "value": "TIDAK ADA FOTO TRANSPORT",
                    },
                    {
                        "label": "SKOR TIDAK TERSEDIA",
                        "value": "SKOR TIDAK TERSEDIA",
                    },
                ],
                multi=True,
                placeholder="Semua keputusan",
            )]),
            html.Div([html.Label("Ketersediaan Foto"), dcc.Dropdown(
                id="transaction-status-filter",
                options=[],
                multi=True,
                placeholder="Semua status",
            )]),
            html.Div([html.Label("Cari OPH / File / Kerani"), dcc.Input(
                id="transaction-search",
                type="text",
                debounce=True,
                placeholder="ketik OPH / file / nama...",
            )]),
            html.Button(
                "Muat Ulang Data",
                id="transaction-refresh",
                n_clicks=0,
                className="ghost-btn",
            ),
        ], className="filter-grid transaction-filter-panel"),

        html.Div(id="transaction-summary", className="transaction-summary"),
        html.Div(id="transaction-kpi-row", className="transaction-kpi-shell"),
        html.Div([
            card(
                dcc.Graph(
                    id="transaction-decision-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Keputusan Foto per Transaksi",
            ),
            card(
                dcc.Graph(
                    id="transaction-company-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Breakdown Company",
            ),
        ], className="grid two transaction-breakdown-grid"),
        html.Div([
            card(
                dcc.Graph(
                    id="transaction-month-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Breakdown Bulanan",
            ),
            card(
                dcc.Graph(
                    id="transaction-status-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Breakdown Ketersediaan Foto",
            ),
        ], className="grid two transaction-breakdown-grid"),
        html.Div([
                card(
                    dcc.Graph(
                        id="transaction-top-kerani-panen-chart",
                        config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                    ),
                "Top Kerani Panen pada BERBEDA",
                ),
            card(
                dcc.Graph(
                    id="transaction-top-kerani-transport-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Top Kerani Transport",
            ),
        ], className="grid two transaction-breakdown-grid"),
        html.Div([
            card(
                dcc.Graph(
                    id="transaction-estate-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Breakdown Estate",
            ),
            card(
                dcc.Graph(
                    id="transaction-afdeling-chart",
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                ),
                "Breakdown Afdeling",
            ),
        ], className="grid two transaction-breakdown-grid"),
        card([
            table(
                pd.DataFrame(columns=[column["id"] for column in TRANSACTION_TABLE_COLUMNS]),
                table_id="transaction-table",
                selectable=True,
                page_size=20,
                style_data_conditional_extra=[
                    {
                        "if": {"filter_query": '{decision} = "BERBEDA"', "column_id": "decision"},
                        "backgroundColor": "#fff1f2", "color": "#9f1239", "fontWeight": "900",
                    },
                    {
                        "if": {"filter_query": '{decision} = "SAMA"', "column_id": "decision"},
                        "backgroundColor": "#ecfdf5", "color": "#047857", "fontWeight": "900",
                    },
                    {
                        "if": {"filter_query": '{transaction_status} = "TIDAK ADA FOTO TRANSPORT"', "column_id": "transaction_status"},
                        "backgroundColor": "#fff7ed", "color": "#c2410c", "fontWeight": "850",
                    },
                ],
            ),
            dcc.Store(id="transaction-rows-store"),
        ], "Detail Transaksi – Similarity Terendah", class_name="transaction-table-card"),
        html.Div(
            harmony_transaction_detail(None),
            id="transaction-detail-panel",
        ),
    ], id="module-harmony-transaction", style={"display": "none"}),


], className="page")


@app.callback(
    Output("module-reused-dashboard", "style"),
    Output("module-harmony-transaction", "style"),
    Input("module-tabs", "value"),
)
def switch_module(module):
    module = module or "reused-dashboard"
    return (
        {"display": "block"} if module == "reused-dashboard" else {"display": "none"},
        {"display": "block"} if module == "harmony-transaction" else {"display": "none"},
    )


@app.callback(
    Output("page-overview", "style"),
    Output("page-groups", "style"),
    Output("page-similarity", "style"),
    Output("page-detail", "style"),
    Input("page-tabs", "value"),
)
def switch_page(page):
    page = page or "overview"
    def show(name):
        return {"display": "block"} if page == name else {"display": "none"}
    return show("overview"), show("groups"), show("similarity"), show("detail")


@app.callback(
    Output("transaction-month-filter", "options"),
    Output("transaction-estate-filter", "options"),
    Output("transaction-afdeling-filter", "options"),
    Output("transaction-block-filter", "options"),
    Output("transaction-status-filter", "options"),
    Output("transaction-summary", "children"),
    Output("transaction-kpi-row", "children"),
    Output("transaction-decision-chart", "figure"),
    Output("transaction-company-chart", "figure"),
    Output("transaction-month-chart", "figure"),
    Output("transaction-estate-chart", "figure"),
    Output("transaction-afdeling-chart", "figure"),
    Output("transaction-status-chart", "figure"),
    Output("transaction-top-kerani-panen-chart", "figure"),
    Output("transaction-top-kerani-transport-chart", "figure"),
    Output("transaction-table", "data"),
    Output("transaction-table", "columns"),
    Output("transaction-rows-store", "data"),
    Input("transaction-company-filter", "value"),
    Input("transaction-year-filter", "value"),
    Input("transaction-month-filter", "value"),
    Input("transaction-estate-filter", "value"),
    Input("transaction-afdeling-filter", "value"),
    Input("transaction-block-filter", "value"),
    Input("transaction-decision-filter", "value"),
    Input("transaction-status-filter", "value"),
    Input("transaction-search", "value"),
    Input("transaction-refresh", "n_clicks"),
    Input("module-tabs", "value"),
)
def render_harmony_transaction_validation(
    companies,
    period_year,
    months,
    estates,
    afdelings,
    blocks,
    decisions,
    statuses,
    search,
    _refresh_clicks,
    active_module,
):
    # A Dash callback runs even when its module is hidden with CSS.
    # Avoid the 2026 OPH-wide transaction reconciliation on Reused Dashboard.
    if str(active_module or "reused-dashboard") != "harmony-transaction":
        return (no_update,) * 18
    if ctx.triggered_id == "transaction-refresh":
        clear_data_cache()
    companies = companies or ["PWP", "DIN"]
    period_year = int(period_year or 2026)
    base_rows = load_harmony_transaction_validation(companies, period_year)
    month_options = _transaction_filter_options(base_rows, "month")
    estate_options = _transaction_filter_options(base_rows, "estate")

    def selected_values(value):
        raw = value if isinstance(value, (list, tuple, set)) else [value]
        return {
            str(item).strip()
            for item in raw
            if item not in (None, "") and str(item).strip()
        }

    selected_months = selected_values(months)
    selected_estates = selected_values(estates)
    selected_afdelings = selected_values(afdelings)
    selected_blocks = selected_values(blocks)
    selected_statuses = selected_values(statuses)

    sap_total = None

    location_scope = base_rows.copy()
    if selected_estates and not location_scope.empty:
        location_scope = location_scope[
            location_scope["estate"].fillna("").astype(str).isin(selected_estates)
        ].copy()
    afdeling_options = _transaction_filter_options(location_scope, "afdeling")
    if selected_afdelings and not location_scope.empty:
        location_scope = location_scope[
            location_scope["afdeling"].fillna("").astype(str).isin(selected_afdelings)
        ].copy()
    block_options = _transaction_filter_options(location_scope, "blok")
    status_options = [
        item for item in _transaction_filter_options(base_rows, "transaction_status")
        if str(item.get("value", "")).upper() != "SIAP TRANSAKSI"
    ]

    rows = base_rows.copy()

    if selected_months and not rows.empty:
        rows = rows[rows["month"].astype(str).isin(selected_months)].copy()
    if selected_estates and not rows.empty:
        rows = rows[
            rows["estate"].fillna("").astype(str).isin(selected_estates)
        ].copy()
    if selected_afdelings and not rows.empty:
        rows = rows[
            rows["afdeling"].fillna("").astype(str).isin(selected_afdelings)
        ].copy()
    if selected_blocks and not rows.empty:
        rows = rows[
            rows["blok"].fillna("").astype(str).isin(selected_blocks)
        ].copy()
    if selected_statuses and not rows.empty:
        rows = rows[
            rows["transaction_status"].fillna("").astype(str).isin(selected_statuses)
        ].copy()

    selected_decisions = {
        str(value).upper()
        for value in (
            decisions
            if isinstance(decisions, (list, tuple, set))
            else [decisions]
        )
        if value not in (None, "")
    }
    if selected_decisions and not rows.empty:
        rows = rows[
            rows["decision"].astype(str).str.upper().isin(selected_decisions)
        ].copy()

    needle = str(search or "").strip().lower()
    if needle and not rows.empty:
        searchable = [
            column for column in (
                "oph", "oph_key", "panen_file_name", "transport_file_name",
                "kerani_panen", "kerani_transport", "mandor", "estate", "afdeling", "blok", "tph",
            ) if column in rows.columns
        ]
        mask = pd.Series(False, index=rows.index)
        for column in searchable:
            mask |= rows[column].fillna("").astype(str).str.lower().str.contains(
                needle, regex=False
            )
        rows = rows[mask].copy()

    ready_mask = rows.get(
        "similarity_eligible", pd.Series(False, index=rows.index)
    ).fillna(False).astype(bool)
    has_panen_photo = rows.get(
        "panen_has_embedding", pd.Series(False, index=rows.index)
    ).fillna(False).astype(bool)
    has_transport = rows.get(
        "transport_file_uid", pd.Series(dtype="object")
    ).fillna("").astype(str).str.strip().ne("")
    granular_filter_active = bool(
        selected_estates
        or selected_afdelings
        or selected_blocks
        or selected_decisions
        or selected_statuses
        or str(search or "").strip()
    )

    # Snapshot SAP memberi denominator cepat. Saat scope hanya company/tahun/
    # bulan, lakukan rekonsiliasi key OPH read-only agar angka "dengan foto"
    # benar-benar merupakan irisan OPH SAP dan Panen, bukan seluruh Context.
    oph_coverage = (
        load_harmony_oph_coverage(companies, period_year, selected_months)
        if not granular_filter_active
        else {"source_ready": False}
    )
    coverage_source_ready = bool(oph_coverage.get("source_ready"))
    if coverage_source_ready:
        exact_total = oph_coverage.get("sap_total")
        if exact_total is not None:
            sap_total = int(exact_total)
        panen_available_count = int(oph_coverage.get("sap_with_photo") or 0)
        no_panen_count = int(
            (oph_coverage.get("sap_missing_context") or 0)
            + (oph_coverage.get("sap_context_without_photo") or 0)
        )
        panen_embedding_pending_count = int(
            oph_coverage.get("sap_photo_without_embedding") or 0
        )
    else:
        panen_available_count = None
        no_panen_count = None
        panen_embedding_pending_count = None
    same_count = int(rows.get("decision", pd.Series(dtype="object")).eq("SAMA").sum())
    different_count = int(rows.get("decision", pd.Series(dtype="object")).eq("BERBEDA").sum())
    if no_panen_count is None:
        no_panen_count = int((~has_panen_photo).sum())
    if panen_available_count is None:
        panen_available_count = int(has_panen_photo.sum())

    # Tiga populasi harus bisa direkonsiliasi:
    # SAP -> Foto Panen -> Foto Transport -> Similarity.
    # Foto Transport memakai existence file (bukan embedding/similarity) agar
    # tidak tercampur dengan status proses feature/similarity.
    transport_available_count = int(has_transport.sum())
    ready_count = int(ready_mask.sum())
    transport_missing_count = max(int(panen_available_count) - transport_available_count, 0)
    transport_pending_similarity_count = max(transport_available_count - ready_count, 0)

    panen_coverage_pct = (
        100.0 * int(panen_available_count) / int(sap_total)
        if sap_total not in (None, 0)
        and not granular_filter_active
        and coverage_source_ready
        else None
    )
    no_panen_pct = (
        100.0 * int(no_panen_count) / int(sap_total)
        if sap_total not in (None, 0)
        and not granular_filter_active
        and coverage_source_ready
        else None
    )
    transport_coverage_pct = (
        100.0 * transport_available_count / int(panen_available_count)
        if panen_available_count else 0.0
    )
    transport_missing_pct = (
        100.0 * transport_missing_count / int(panen_available_count)
        if panen_available_count else 0.0
    )
    similarity_coverage_pct = (
        100.0 * ready_count / int(panen_available_count)
        if panen_available_count else 0.0
    )
    similarity_from_transport_pct = (
        100.0 * ready_count / transport_available_count
        if transport_available_count else 0.0
    )
    pending_similarity_pct = (
        100.0 * transport_pending_similarity_count / transport_available_count
        if transport_available_count else 0.0
    )
    same_pct = 100.0 * same_count / ready_count if ready_count else 0.0
    different_pct = 100.0 * different_count / ready_count if ready_count else 0.0

    score_values = pd.to_numeric(
        rows.get("similarity_score", pd.Series(dtype="float64")),
        errors="coerce",
    )
    avg_score = score_values.where(ready_mask).mean()

    sap_cards = [
        transaction_kpi_card(
            "OPH SAP dengan Foto Panen",
            f"{int(panen_available_count):,}" if coverage_source_ready else f"{int(panen_available_count):,}",
            tone="blue",
        ),
    ]

    transport_cards = [
        transaction_kpi_card(
            "OPH dengan Foto Transport",
            f"{transport_available_count:,}",
            tone="teal",
        ),
        transaction_kpi_card(
            "OPH Tanpa Foto Transport",
            f"{transport_missing_count:,}",
            tone="orange",
        ),
        transaction_kpi_card(
            "OPH Similarity",
            f"{ready_count:,}",
            tone="purple",
        ),
        transaction_kpi_card(
            "Transport Ada, Belum Similarity",
            f"{transport_pending_similarity_count:,}",
            tone="orange",
        ),
        transaction_kpi_card(
            "Coverage Foto Transport / Foto Panen",
            f"{transport_coverage_pct:.1f}%",
            tone="teal",
        ),
        transaction_kpi_card(
            "Coverage Similarity / Foto Panen",
            f"{similarity_coverage_pct:.1f}%",
            tone="purple",
        ),
    ]

    result_cards = [
        transaction_kpi_card(
            "SAMA",
            f"{same_count:,}",
            tone="green",
        ),
        transaction_kpi_card(
            "BERBEDA",
            f"{different_count:,}",
            tone="red",
        ),
        transaction_kpi_card(
            "Rata-rata Similarity",
            "N/A" if pd.isna(avg_score) else f"{float(avg_score):.2f}%",
            tone="dark",
        ),
    ]

    kpi_children = transaction_journey(
        panen_count=int(panen_available_count),
        transport_count=transport_available_count,
        ready_count=ready_count,
        same_count=same_count,
        different_count=different_count,
        transport_missing_count=transport_missing_count,
        pending_similarity_count=transport_pending_similarity_count,
        panen_coverage_pct=panen_coverage_pct,
        transport_coverage_pct=transport_coverage_pct,
        similarity_coverage_pct=similarity_coverage_pct,
    )
    unavailable_labels = {
        "TIDAK ADA FOTO PANEN", "TIDAK ADA FOTO TRANSPORT",
        "SKOR TIDAK TERSEDIA",
    }
    selected_unavailable = bool(
        set(selected_decisions).intersection(unavailable_labels)
        or set(selected_statuses).intersection(unavailable_labels)
    )
    table_rows = rows if (selected_unavailable or needle) else rows[ready_mask].copy()
    shown_count = min(len(table_rows), HARMONY_TRANSACTION_TABLE_LIMIT)
    photo_kpi_label = f"{panen_available_count:,}"
    summary = (
        f"Foto Panen {photo_kpi_label}"
        + f" | Foto Transport {transport_available_count:,} ({transport_coverage_pct:.1f}%)"
        + f" | Similarity {ready_count:,} ({similarity_coverage_pct:.1f}%)"
        + f" | SAMA {same_count:,} ({same_pct:.1f}%) | BERBEDA {different_count:,} ({different_pct:.1f}%)"
        + f" | {shown_count:,} baris"
    )
    display_rows = table_rows.copy()
    display_rows["_similarity_sort"] = pd.to_numeric(
        display_rows.get("similarity_score", pd.Series(dtype="float64")),
        errors="coerce",
    )
    display_rows = display_rows.sort_values(
        ["_similarity_sort", "tanggal", "company", "oph_key"],
        ascending=[True, False, True, True],
        na_position="last",
        kind="mergesort",
    ).head(HARMONY_TRANSACTION_TABLE_LIMIT).drop(
        columns=["_similarity_sort"], errors="ignore"
    ).copy()
    if "tanggal" in display_rows.columns:
        parsed_dates = pd.to_datetime(display_rows["tanggal"], errors="coerce")
        display_rows["tanggal"] = parsed_dates.dt.strftime("%Y-%m-%d").fillna("")
    if "similarity_score" in display_rows.columns:
        display_rows["similarity_score"] = pd.to_numeric(
            display_rows["similarity_score"], errors="coerce"
        ).round(2)
    data = records(display_rows)
    different_rows = (
        rows[
            rows.get("decision", pd.Series(dtype="object"))
            .astype(str)
            .str.upper()
            .eq("BERBEDA")
        ].copy()
        if not rows.empty
        else rows
    )
    return (
        month_options,
        estate_options,
        afdeling_options,
        block_options,
        status_options,
        summary,
        kpi_children,
        harmony_transaction_decision_fig(rows),
        harmony_transaction_breakdown_fig(
            rows, "company", "Breakdown Company", top_n=5
        ),
        harmony_transaction_breakdown_fig(
            rows, "month", "Breakdown Bulanan", top_n=24
        ),
        harmony_transaction_breakdown_fig(
            rows, "estate", "Breakdown Estate", top_n=15, horizontal=True
        ),
        harmony_transaction_breakdown_fig(
            rows, "afdeling", "Breakdown Afdeling", top_n=18, horizontal=True
        ),
        harmony_transaction_status_fig(rows),
        harmony_transaction_people_fig(
            different_rows,
            "kerani_panen",
            "Top Kerani Panen pada BERBEDA",
            top_n=15,
        ),
        harmony_transaction_people_fig(
            rows, "kerani_transport", "Top Kerani Transport", top_n=15
        ),
        data,
        TRANSACTION_TABLE_COLUMNS,
        # transaction-rows-store has no consumer in V187. Returning another
        # 5,000 full transaction records nearly doubles the browser JSON.
        [],
    )


@app.callback(
    Output("transaction-detail-panel", "children"),
    Input("transaction-table", "selected_rows"),
    Input("transaction-table", "active_cell"),
    State("transaction-table", "derived_virtual_data"),
    State("transaction-table", "data"),
)
def show_harmony_transaction_detail(selected_rows, active_cell, virtual_data, data):
    rows = virtual_data or data or []
    row_index = None
    if active_cell and isinstance(active_cell, dict):
        row_index = active_cell.get("row")
    elif selected_rows:
        row_index = selected_rows[0]
    if row_index is None or not rows or not 0 <= int(row_index) < len(rows):
        return harmony_transaction_detail(None)
    return harmony_transaction_detail(rows[int(row_index)])


@app.callback(
    Output("transaction-company-filter", "value"),
    Output("transaction-month-filter", "value"),
    Output("transaction-estate-filter", "value"),
    Output("transaction-afdeling-filter", "value"),
    Output("transaction-decision-filter", "value"),
    Output("transaction-status-filter", "value"),
    Input("transaction-decision-chart", "clickData"),
    Input("transaction-company-chart", "clickData"),
    Input("transaction-month-chart", "clickData"),
    Input("transaction-estate-chart", "clickData"),
    Input("transaction-afdeling-chart", "clickData"),
    Input("transaction-status-chart", "clickData"),
    prevent_initial_call=True,
)
def drill_harmony_transaction_breakdown(
    decision_click,
    company_click,
    month_click,
    estate_click,
    afdeling_click,
    status_click,
):
    trigger = ctx.triggered_id
    payload_by_id = {
        "transaction-decision-chart": decision_click,
        "transaction-company-chart": company_click,
        "transaction-month-chart": month_click,
        "transaction-estate-chart": estate_click,
        "transaction-afdeling-chart": afdeling_click,
        "transaction-status-chart": status_click,
    }
    payload = payload_by_id.get(trigger)
    if not payload or not payload.get("points"):
        return (no_update,) * 6
    point = payload["points"][0]
    company = month = estate = afdeling = decision = status = no_update

    if trigger == "transaction-decision-chart":
        value = str(point.get("x") or "").strip().upper()
        if value:
            decision = [value]
    elif trigger == "transaction-status-chart":
        value = point.get("customdata") or point.get("y")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        value = str(value or "").strip()
        if value:
            status = [value]
    else:
        custom = point.get("customdata")
        if not isinstance(custom, (list, tuple)) or not custom:
            return (no_update,) * 6
        value = str(custom[0] or "").strip()
        clicked_decision = (
            str(custom[2] or "").strip().upper()
            if len(custom) > 2 else ""
        )
        custom_company = str(custom[6] or "").strip() if len(custom) > 6 else ""
        custom_estate = str(custom[7] or "").strip() if len(custom) > 7 else ""
        custom_afdeling = str(custom[8] or "").strip() if len(custom) > 8 else ""
        if trigger == "transaction-company-chart" and value:
            company = [value]
        elif trigger == "transaction-month-chart" and value:
            month = [value]
        elif trigger == "transaction-estate-chart":
            if custom_company:
                company = [custom_company]
            if custom_estate:
                estate = [custom_estate]
        elif trigger == "transaction-afdeling-chart":
            if custom_company:
                company = [custom_company]
            if custom_estate:
                estate = [custom_estate]
            if custom_afdeling:
                afdeling = [custom_afdeling]
        if clicked_decision:
            decision = [clicked_decision]
    return company, month, estate, afdeling, decision, status


# V38: setelah row similarity diklik, detail panel langsung terlihat tanpa perlu scroll manual.
app.clientside_callback(
    """
    function(data) {
        if (data && data.index !== undefined) {
            setTimeout(function(){
                var el = document.getElementById('similarity-detail-anchor') || document.getElementById('similarity-example-wrap');
                if (el) { el.scrollIntoView({behavior: 'smooth', block: 'start'}); }
            }, 120);
        }
        return "";
    }
    """,
    Output("similarity-scroll-dummy", "children"),
    Input("selected-similarity-store", "data"),
)


@app.callback(
    Output("company-filter", "options"),
    Output("company-filter", "value"),
    Input("system-filter", "value"),
    State("company-filter", "value"),
)
def update_company_filter(system, current_company):
    """Company has one owner callback so a user selection is never overwritten."""
    system = str(system or DEFAULT_SYSTEM).upper().strip()
    try:
        options = get_company_options(system)
        value = _choose_company_value(system, current_company, options)
        return options, value
    except Exception as exc:
        print(
            "[DASHBOARD][COMPANY FILTER ERROR] "
            f"system={system!r} company={current_company!r}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        return no_update, no_update


@app.callback(
    Output("estate-filter", "options"),
    Output("estate-filter", "value"),
    Output("afdeling-filter", "options"),
    Output("afdeling-filter", "value"),
    Output("block-filter", "options"),
    Output("block-filter", "value"),
    Output("month-filter", "options"),
    Output("month-filter", "value"),
    Output("reuse-filter", "options"),
    Output("reuse-filter", "value"),
    Output("date-filter", "options"),
    Output("date-filter", "value"),
    Output("similarity-filter", "options"),
    Output("similarity-filter", "value"),
    Input("system-filter", "value"),
    Input("company-filter", "value"),
    Input("estate-filter", "value"),
    Input("afdeling-filter", "value"),
    Input("block-filter", "value"),
    Input("month-filter", "value"),
    Input("reuse-filter", "value"),
    Input("date-filter", "value"),
    Input("similarity-filter", "value"),
)
def cascade_filters(system, company, estate, afdeling, block, months, reuse, datecat, similarity):
    # Company sengaja tidak menjadi Output callback ini. Jika Input sekaligus
    # Output, pilihan manual dapat tertimpa oleh update berantai dan dropdown
    # tampak mati. Callback ini hanya memiliki filter turunan.
    system = str(system or DEFAULT_SYSTEM).upper().strip()
    try:
        company_val = _choose_company_value(system, company, get_company_options(system))

        opts1 = get_filter_options(system, company_val, {})
        estate_val = _sanitize_value(estate, opts1.get("estates", []), multi=True)

        opts2 = get_filter_options(system, company_val, {"estate": estate_val})
        afdeling_val = _sanitize_value(afdeling, opts2.get("afdelings", []), multi=True)

        opts3 = get_filter_options(system, company_val, {"estate": estate_val, "afdeling": afdeling_val})
        block_val = _sanitize_value(block, opts3.get("blocks", []), multi=True)

        opts4 = get_filter_options(system, company_val, {"estate": estate_val, "afdeling": afdeling_val, "block": block_val, "months": months})
        month_val = _sanitize_value(months, opts4.get("months", []), multi=True)
        reuse_val = _sanitize_value(reuse, opts4.get("reuse_categories", []), multi=True)
        date_val = _sanitize_value(datecat, opts4.get("date_categories", []), multi=True)
        sim_val = _sanitize_value(similarity, opts4.get("similarities", []), multi=True)

        return (
            opts1.get("estates", []), estate_val,
            opts2.get("afdelings", []), afdeling_val,
            opts3.get("blocks", []), block_val,
            opts4.get("months", []), month_val,
            opts4.get("reuse_categories", []), reuse_val,
            opts4.get("date_categories", []), date_val,
            opts4.get("similarities", []), sim_val,
        )
    except Exception as exc:
        print(
            "[DASHBOARD][CASCADE ERROR] "
            f"system={system!r} company={company!r}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        return tuple(no_update for _ in range(14))


@app.callback(
    Output("selected-group-store", "data"),
    Output("page-tabs", "value"),
    Input("top-reused-chart", "clickData"),
    Input("group-table", "selected_rows"),
    Input("group-table", "active_cell"),
    Input("reset-selection", "n_clicks"),
    State("group-table", "data"),
    State("group-table", "derived_virtual_data"),
    prevent_initial_call=True,
)
def pick_group(top_click, selected_rows, active_cell, reset_clicks, group_data, virtual_data):
    trigger = ctx.triggered_id
    if trigger == "reset-selection":
        return None, "overview"
    if trigger == "top-reused-chart" and top_click:
        try:
            return str(top_click["points"][0]["customdata"][0]), "detail"
        except Exception:
            return None, no_update
    if trigger == "group-table":
        rows = virtual_data or group_data or []
        row_idx = None
        if active_cell and isinstance(active_cell, dict):
            row_idx = active_cell.get("row")
        elif selected_rows:
            row_idx = selected_rows[0]
        if row_idx is not None and rows and 0 <= int(row_idx) < len(rows):
            try:
                picked = rows[int(row_idx)]
                return str(picked.get("group_key") or picked.get("group_id")), "detail"
            except Exception:
                return None, no_update
    return no_update, no_update


@app.callback(
    Output("reuse-filter", "value", allow_duplicate=True),
    Input("reuse-category-chart", "clickData"),
    prevent_initial_call=True,
)
def click_reuse_category(click):
    if not click:
        return no_update
    try:
        val = str(click["points"][0].get("label") or click["points"][0].get("customdata", [""])[0])
        return [val]
    except Exception:
        return no_update


@app.callback(
    Output("date-filter", "value", allow_duplicate=True),
    Input("date-category-chart", "clickData"),
    prevent_initial_call=True,
)
def click_date_category(click):
    if not click:
        return no_update
    try:
        val = str(click["points"][0].get("label") or click["points"][0].get("customdata", [""])[0])
        return [val]
    except Exception:
        return no_update


@app.callback(
    Output("estate-filter", "value", allow_duplicate=True),
    Input("estate-chart", "clickData"),
    prevent_initial_call=True,
)
def click_estate(click):
    if not click:
        return no_update
    try:
        val = str(click["points"][0]["customdata"][0] if "customdata" in click["points"][0] else click["points"][0]["y"])
        return [val]
    except Exception:
        return no_update


@app.callback(
    Output("selected-similarity-store", "data"),
    Input({"type": "similarity-group-card", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def pick_similarity_pair(_clicks):
    # Klik card Similarity Transport hanya membuka preview cepat di halaman yang sama.
    tid = ctx.triggered_id
    if isinstance(tid, dict) and tid.get("type") == "similarity-group-card":
        try:
            return {"group_key": str(tid.get("index", ""))}
        except Exception:
            return no_update
    return no_update


@app.callback(
    Output("similarity-example-wrap", "children", allow_duplicate=True),
    Input("selected-similarity-store", "data"),
    State("similarity-rows-store", "data"),
    prevent_initial_call=True,
)
def render_similarity_fast_preview(selected, similarity_rows):
    if not isinstance(selected, dict):
        return html.Div()
    group_key = selected.get("group_key")
    if not group_key or not similarity_rows:
        return html.Div()
    try:
        return similarity_fast_preview(pd.DataFrame(similarity_rows), str(group_key))
    except Exception as e:
        return card(html.Div(f"Preview similarity gagal dimuat: {e}", className="muted big-empty"), "Preview Similarity Transport")


@app.callback(
    Output("kpi-row", "children"),
    Output("source-summary-chart", "figure"),
    Output("top-reused-chart", "figure"),
    Output("monthly-pattern-chart", "figure"),
    Output("estate-monthly-chart", "figure"),
    Output("reuse-category-chart", "figure"),
    Output("date-category-chart", "figure"),
    Output("reuse-date-heatmap-chart", "figure"),
    Output("afdeling-monthly-chart", "figure"),
    Output("estate-chart", "figure"),
    Output("similarity-chart", "figure"),
    Output("similarity-card-wrap", "style"),
    Output("similarity-harmony-info", "children"),
    Output("similarity-page-kpi-row", "children"),
    Output("similarity-page-chart", "figure"),
    Output("similarity-example-wrap", "children"),
    Output("similarity-pair-table-wrap", "children"),
    Output("usage-dist-chart", "figure"),
    Output("top-pemanen-chart", "figure"),
    Output("top-kerani-chart", "figure"),
    Output("top-mandor-chart", "figure"),
    Output("group-list-summary", "children"),
    Output("group-table", "data"),
    Output("group-table", "columns"),
    Output("groups-current-store", "data"),
    Output("similarity-rows-store", "data"),
    Output("selected-detail-panel", "children"),
    Output("detail-table-wrap", "children"),
    Input("system-filter", "value"),
    Input("company-filter", "value"),
    Input("month-filter", "value"),
    Input("estate-filter", "value"),
    Input("afdeling-filter", "value"),
    Input("block-filter", "value"),
    Input("reuse-filter", "value"),
    Input("date-filter", "value"),
    Input("similarity-filter", "value"),
    Input("search-box", "value"),
    Input("page-tabs", "value"),
    Input("selected-group-store", "data"),
    Input("module-tabs", "value"),
)
def render_all(system, company, months, estate, afdeling, block, reuse_category, date_category, similarity, search, active_page, selected_group_id, active_module):
    if str(active_module or "reused-dashboard") != "reused-dashboard":
        return (no_update,) * 28
    try:
        system = str(system or DEFAULT_SYSTEM).upper().strip()
        company = _choose_company_value(system, company, get_company_options(system))
        active_page_name = str(active_page or "overview")
        filters = filter_payload(months, estate, afdeling, block, reuse_category, date_category, [], similarity, search)
        all_g = load_groups(system, company)
        monthly_base = load_dashboard_monthly(system, company)

        # Apply location/category filters at group level, but resolve month
        # membership from the exact Gold monthly aggregate. `group.month` is
        # only the first date in sql_date_list and must not drive month filters.
        filters_without_month = dict(filters)
        filters_without_month["months"] = []
        g = apply_filters(all_g, filters_without_month)
        selected_months = {
            str(value)
            for value in (
                months if isinstance(months, (list, tuple, set)) else [months]
            )
            if value not in (None, "")
        }
        if selected_months and not monthly_base.empty and "group_key" in monthly_base.columns:
            month_rows = monthly_base[
                monthly_base["month"].astype(str).isin(selected_months)
            ].copy()
            month_keys = set(month_rows["group_key"].astype(str))
            g = g[g["group_key"].astype(str).isin(month_keys)].copy()
            monthly_base = month_rows
        elif selected_months and "month" in g.columns:
            # Compatibility fallback for a database that has not applied
            # 006_dashboard_aggregates.sql yet.
            g = g[g["month"].astype(str).isin(selected_months)].copy()

        if not monthly_base.empty and not g.empty and "group_key" in monthly_base.columns:
            visible_keys = set(g["group_key"].astype(str))
            monthly_for_visible_groups = monthly_base[
                monthly_base["group_key"].astype(str).isin(visible_keys)
            ].copy()
        else:
            monthly_for_visible_groups = pd.DataFrame()

        # Tanpa pilihan bulan eksplisit, denominator SAP harus mengikuti bulan
        # yang memang tersedia pada Gold aktif (misalnya Januari-Juli), bukan
        # otomatis seluruh tahun kalender yang belum sebanding dengan usage.
        sap_scope_months = set(selected_months)
        if (
            not sap_scope_months
            and not monthly_for_visible_groups.empty
            and "month" in monthly_for_visible_groups.columns
        ):
            sap_scope_months = {
                str(value)[:7]
                for value in monthly_for_visible_groups["month"].tolist()
                if str(value or "")[:7]
            }

        # Chart Ringkasan Similarity Transport dan halaman Similarity harus
        # memakai usage KP-vs-KT yang sama. Detail Gold reused berukuran kecil
        # dan tetap tidak membawa BYTEA; payload gambar dimuat on-demand.
        is_harmony = str(system or "").upper() == "HARMONY"
        # Detail transactions are only needed for Overview's *exact* transport
        # similarity/bunch-pay KPIs and the Similarity page. Group List and
        # Detail Group must never SELECT the whole-company detail relation.
        # Detail Group already has load_group_details() below.
        needs_all_detail = is_harmony and active_page_name in {"overview", "similarity"}
        all_d_base = load_details(system, company) if needs_all_detail else pd.DataFrame()
        d_for_visible_groups = detail_for_groups(all_d_base, g) if not g.empty else pd.DataFrame()
        # Preserve the exact Bunch Pay KPI on Group List/Detail without
        # fetching all detail columns and all rows. Most Gold summaries already
        # contain total_bunch_pay_group; only zero/missing values need fallback.
        if is_harmony and not needs_all_detail and not g.empty:
            bunch = pd.to_numeric(g.get("total_bunch_pay_group", 0), errors="coerce").fillna(0)
            missing_ids = g.loc[bunch <= 0, "group_id"].astype(int).tolist()
            if missing_ids:
                d_for_visible_groups = load_group_bunch_pay_aggregate(system, company, missing_ids)
        g = enrich_bunch_pay(g, d_for_visible_groups)
        k = _group_kpis(g)
        sap_kpi = load_sap_kpis(
            system, company, g, sap_scope_months, estate
        )
        # Cache/snapshot SAP lama boleh belum mempunyai field denominator.
        # KPI menjadi N/A tanpa menggagalkan seluruh halaman dashboard.
        sap_total = sap_kpi.get("total_sap_oph")
        finding_usage_pct = (
            100.0 * float(k["total_usage"]) / float(sap_total)
            if sap_total not in (None, 0)
            else None
        )
        similarity_page_active = active_page_name == "similarity"
        similarity_pairs = (
            build_similarity_transport_rows(
                g, d_for_visible_groups, limit=SIMILARITY_LIST_LIMIT
            )
            if is_harmony and similarity_page_active
            else pd.DataFrame()
        )

        kpis_html = [
            kpi_card(
                "Total Transaksi SAP",
                "N/A" if sap_total is None else sap_total,
                (
                    "estate SAP terpilih & bulan terpilih"
                    if estate and selected_months
                    else "estate SAP terpilih & cakupan bulan Gold aktif"
                    if estate
                    else "estate SAP sah & bulan terpilih"
                    if selected_months
                    else "estate SAP sah & cakupan bulan Gold aktif"
                ),
                tone="dark",
            ),
            kpi_card("Reused Group", k["total_group"], tone="blue"),
            kpi_card("Total Reused", k["total_usage"], tone="purple"),
            kpi_card(
                "Total Reused / Transaksi SAP",
                "N/A" if finding_usage_pct is None else f"{finding_usage_pct:.2f}%",
                (
                    f"{int(k['total_usage']):,} usage / {int(sap_total):,} transaksi"
                    if sap_total not in (None, 0)
                    else "jalankan refresh SAP"
                ),
                tone="orange",
            ),
            kpi_card("Max Reused per Group", k["max_usage"], tone="red"),
            kpi_card("Avg Reused per Group", k["avg_usage"], tone="green"),
            kpi_card("Total Bunch Pay", k["total_bunch_pay"], tone="green"),
        ]

        # V186: avoid constructing hidden charts and serializing them to the browser.
        # Original V185 logic is preserved on the Overview page only.
        if not LAZY_PAGE_FIGURES or active_page_name == "overview":
            source_df = source_breakdown(g)
            fig_source = build_hbar(
                source_df,
                "source_company",
                "total_usage",
                "Company Summary",
                top_n=10,
                compact=True,
            )
            # Pass every visible group; _top_month_stack applies a balanced TOP_N
            # per month so high-volume May/June cannot hide January-April.
            top_df = top_reused(g, 0)
            monthly_long = _month_usage_long(g, monthly_for_visible_groups)
            # HTML lama: Top Reused satu bar per group, tetapi bar dipecah warna per bulan.
            # Pecahan bulan dihitung dari detail transaksi agar terlihat reused terjadi di bulan apa saja.
            fig_top = build_top_reused_fig(top_df, monthly_for_visible_groups, months)
            fig_monthly = build_monthly_pattern_fig(
                g, monthly_for_visible_groups, precomputed_long=monthly_long
            )
            fig_estate_monthly = build_entity_monthly_fig(
                g, monthly_for_visible_groups, "estate", "Estate Monthly Pattern",
                top_n=8, precomputed_long=monthly_long,
            )
            fig_reuse = build_pie(agg_count(g, "reuse_category"), "reuse_category", "total_usage", "Reuse Category")
            fig_date = build_pie(agg_count(g, "date_category"), "date_category", "total_usage", "Date Category")
            fig_reuse_date = build_reuse_date_heatmap(g)
            fig_afdeling_monthly = build_entity_monthly_fig(
                g, monthly_for_visible_groups, "afdeling", "Afdeling Monthly Pattern",
                top_n=10, precomputed_long=monthly_long,
            )
            fig_estate = build_hbar(agg_count(g, "estate_primary"), "estate_primary", "total_usage", "Estate Summary", top_n=30)
            fig_similarity = build_similarity_fig(
                g, d_for_visible_groups
            ) if is_harmony else empty_fig("Similarity Usage Transport hanya untuk Harmony")
            similarity_card_style = {} if is_harmony else {"display": "none"}
            fig_dist = build_usage_dist(usage_distribution(g))
            fig_pemanen = build_top_people_fig(
                g, monthly_for_visible_groups, "pemanen", "Top Pemanen Reused",
                top_n=15, precomputed_long=monthly_long,
            )
            fig_kerani = build_top_people_fig(
                g, monthly_for_visible_groups, "kerani_panen", "Top Kerani Panen",
                top_n=15, precomputed_long=monthly_long,
            )
            fig_mandor = build_top_people_fig(
                g, monthly_for_visible_groups, "mandor", "Top Mandor",
                top_n=15, precomputed_long=monthly_long,
            )

        else:
            fig_source = fig_top = fig_monthly = fig_estate_monthly = empty_fig("Buka Ringkasan")
            fig_reuse = fig_date = fig_reuse_date = fig_afdeling_monthly = empty_fig("Buka Ringkasan")
            fig_estate = fig_similarity = fig_dist = empty_fig("Buka Ringkasan")
            fig_pemanen = fig_kerani = fig_mandor = empty_fig("Buka Ringkasan")
            similarity_card_style = {} if is_harmony else {"display": "none"}

        if not LAZY_PAGE_FIGURES or active_page_name == "similarity":
            sim_info = similarity_page_info(system, similarity_pairs)
            sim_kpis = []
            try:
                sim_page_chart = build_similarity_page_fig(
                    similarity_pairs, d_for_visible_groups
                ) if is_harmony else empty_fig("Similarity Transport hanya untuk Harmony")
            except Exception as _sim_err:
                sim_page_chart = empty_fig(f"Similarity Transport error: {_sim_err}")
            # V40: Similarity Transport memakai card list per group yang estetik dan ada thumbnail.
            # Klik card langsung membuka Detail Group.
            if is_harmony:
                sim_table = similarity_group_card_list(similarity_pairs)
            else:
                sim_table = card(html.Div("Similarity Transport hanya tersedia untuk Harmony.", className="muted big-empty"), "Group Similarity Transport")
            sim_examples = html.Div()
        else:
            sim_info = html.Div()
            sim_kpis = []
            sim_page_chart = empty_fig("Buka Similarity Transport")
            sim_examples = html.Div()
            sim_table = html.Div()

        # The old callback normalized/serialized ALL group-list rows on EVERY
        # page. A hidden table does not need an expensive Pandas formatting pass.
        if active_page_name == "groups":
            sorted_g = g.sort_values(
                ["usage_count", "group_id"], ascending=[False, True]
            ) if not g.empty else g
            total_group_count = len(sorted_g)
            limited_g = (
                sorted_g.head(GROUP_TABLE_ROW_LIMIT)
                if GROUP_TABLE_ROW_LIMIT > 0 else sorted_g
            )
            group_show = prepare_group_list(limited_g)
        else:
            total_group_count = len(g)
            group_show = prepare_group_list(pd.DataFrame())
        visible_group_cols = [
            "no", "source", "group_id", "usage_count", "quick_read", "indicator",
            "location", "similarity", "transport", "sample_oph",
        ]
        group_columns = [
            {"name": {
                "no": "No",
                "source": "System / Company",
                "group_id": "Group ID",
                "usage_count": "Usage",
                "quick_read": "Baca Cepat",
                "indicator": "Indikasi",
                "location": "Lokasi",
                "similarity": "Similarity",
                "transport": "Transport",
                "sample_oph": "OPH",
            }.get(c, c.replace("_", " ").title()), "id": c} for c in visible_group_cols if c in group_show.columns
        ]
        group_summary_html = html.Div()
        if len(group_show) < total_group_count and active_page_name == "groups":
            group_summary_html = html.Div(
                f"Tabel menampilkan {len(group_show):,} dari {total_group_count:,} "
                "group. Gunakan filter untuk mempersempit data.",
                className="section-note",
            )

        sid = None
        if selected_group_id and "group_key" in g.columns and str(selected_group_id) in set(g["group_key"].astype(str).tolist()):
            sid = str(selected_group_id)

        row = None
        det = pd.DataFrame()
        if sid and active_page_name == "detail":
            rowdf = g[g["group_key"].astype(str) == str(sid)]
            row = rowdf.iloc[0] if not rowdf.empty else None
            if row is not None:
                gid = int(row.get("group_id", 0))
                det = load_group_details(system, company, gid)
                if "group_key" in det.columns:
                    det_for_sid = det[det["group_key"].astype(str) == str(sid)].copy()
                    if not det_for_sid.empty:
                        det = det_for_sid

        if row is not None and active_page_name == "detail":
            image_key = str(row.get("representative_image", "") or row.get("image_key", ""))
            selected_panel = card([
                reused_case_readout(row),
                html.Div([
                    html.Div([thumb_img(image_key, "main-image")], className="main-image-box"),
                    html.Div([
                        html.Div(f"{row.get('source_label', '')} | Group ID: {int(row.get('group_id', 0))}", className="detail-title"),
                        html.Div(str(row.get("representative_image", "")), className="detail-file"),
                        html.Div([
                            kpi_card("Usage", row.get("usage_count", 0)),
                            kpi_card("Group Similarity", fmt_similarity(row.get("mean_embedding_similarity"))),
                            kpi_card("Min Member", fmt_similarity(row.get("min_member_mean_similarity"))),
                            kpi_card("Total Bunch Pay", row.get("total_bunch_pay_effective", row.get("total_bunch_pay_group", 0))),
                            kpi_card("Reuse", row.get("reuse_category", "")),
                            kpi_card("Date", row.get("date_category", "")),
                            kpi_card("Estate", row.get("estate_primary", "")),
                        ], className="mini-kpi-row"),
                        html.Div([
                            html.B("OPH: "), html.Span(display_text(row.get("oph_list", ""))), html.Br(),
                            html.B("Tanggal: "), html.Span(display_text(row.get("sql_date_list", ""))), html.Br(),
                            html.B("Lokasi: "), html.Span(f"{display_text(row.get('estate_list',''))} / {display_text(row.get('afdeling_list',''))} / {display_text(row.get('blok_list',''))} / TPH {display_text(row.get('tph_list',''))}"), html.Br(),
                            html.B("Kerani: "), html.Span(display_text(row.get("kerani_panen_list", ""))), html.Br(),
                            html.B("Mandor: "), html.Span(display_text(row.get("mandor_list", ""))),
                        ], className="meta-box"),
                    ], className="detail-meta"),
                ], className="detail-layout"),
                reused_group_location_panel(company, det, row),
                html.H4("Frame transaksi"),
                frame_gallery(det, image_key, limit=FRAME_GALLERY_LIMIT, group_row=row),
            ], "Detail Group Terpilih", "", "selected-card")
        else:
            selected_panel = card(html.Div("Pilih group dari Top Reused Photo atau Reused Group List untuk membuka detail.", className="muted big-empty"), "Detail Group")

        detail_cols = ["group_id", "image_name", "estate", "afdeling", "blok", "tph", "oph", "tanggal", "pemanen", "kerani_panen", "jjg_kerani_panen", "mandor", "kerani_ft", "jjg_ft", "kerani_dt", "jjg_dt", "similarity", "bunch_pay", "transport_status", "foto_kt_before", "foto_dt", "foto_ft", "latitude", "longitude", "match_source"]
        det_show = det[[c for c in detail_cols if c in det.columns]].copy() if det is not None and not det.empty else pd.DataFrame(columns=detail_cols)
        detail_table = table(det_show, table_id="detail-table", page_size=18)

        store_cols = ["group_key", "group_id", "representative_image", "usage_count"]
        # Keep lightweight group identity for cross-page selection; the visible
        # group list (with formatted text) is only materialized on its own page.
        store_df = g[[c for c in store_cols if c in g.columns]].copy() if not g.empty else pd.DataFrame(columns=store_cols)
        return (
            kpis_html,
            fig_source,
            fig_top,
            fig_monthly,
            fig_estate_monthly,
            fig_reuse,
            fig_date,
            fig_reuse_date,
            fig_afdeling_monthly,
            fig_estate,
            fig_similarity,
            similarity_card_style,
            sim_info,
            sim_kpis,
            sim_page_chart,
            sim_examples,
            sim_table,
        fig_dist,
        fig_pemanen,
        fig_kerani,
            fig_mandor,
            group_summary_html,
            records(group_show),
            group_columns,
            records(store_df),
            records(similarity_pairs),
            selected_panel,
            detail_table,
        )
    except Exception as e:
        print(
            "[DASHBOARD][RENDER ERROR] "
            f"system={system!r} company={company!r} page={active_page!r}: {e}",
            flush=True,
        )
        traceback.print_exc()
        err = html.Div([html.H3("Dashboard error"), html.Pre(str(e))], className="error-box")
        return (
            [kpi_card("ERROR", 0, tone="red")],
            empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)),
            empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)),
            {}, err, [], empty_fig(str(e)), err, err, empty_fig(str(e)), empty_fig(str(e)), empty_fig(str(e)),
            empty_fig(str(e)), err, [], [], [], [], err, err,
        )


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG)
