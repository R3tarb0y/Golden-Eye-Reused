from __future__ import annotations

import json
import os
import re
import hashlib
from typing import Any

import numpy as np
import pandas as pd

from db import (
    read_table,
    resolve_dashboard_relation,
    read_df,
    read_df_resilient,
    qname,
    ident,
    table_exists,
    get_columns,
    TABLE_GROUP,
    TABLE_DETAIL,
    TABLE_DASHBOARD_MONTHLY,
    TABLE_SAP_KPI_SUMMARY,
    SAP_SCHEMA,
    SAP_TABLE,
    TABLE_TRANSPORT_STATUS,
    TABLE_OPH_VALIDATION_EXCEL,
    TABLE_FALSE_ALARM,
    TABLE_AUDIT_DOWNLOAD_REPORT,
    TABLE_AUDIT_NO_IMAGE,
    TABLE_ELHP_GROUP,
    TABLE_ELHP_DETAIL,
    TABLE_ELHP_DASHBOARD_MONTHLY,
    TABLE_ELHP_TRANSPORT_STATUS,
    TABLE_ELHP_OPH_VALIDATION,
    TABLE_ELHP_AUDIT_DOWNLOAD_REPORT,
    RECOVERY_SCHEMA,
    TABLE_BYTEA_RECOVERY,
    TABLE_TRANSPORT_KT_CONTEXT,
    TABLE_TRANSPORT_KT_SIMILARITY,
    TABLE_PANEN_NAS_INDEX,
)

TOP_N = int(os.getenv("TOP_N", "60"))
FILTER_OPTION_LIMIT = int(os.getenv("FILTER_OPTION_LIMIT", "2000"))
INVALID = {"", "nan", "none", "null", "nat", "unknown", "-", "--", "n/a", "na", "#n/a", "tidak ada", "kosong"}
TRANSPORT_NA_ELHP = "N/A - ELHP Tidak Ada Transport"

# V10: System, Company, Estate are separate concepts.
SYSTEM_OPTIONS = [
    {"label": "Semua System", "value": "ALL"},
    {"label": "Harmony", "value": "HARMONY"},
    {"label": "ELHP", "value": "ELHP"},
]
DATASET_OPTIONS = SYSTEM_OPTIONS  # backward compatible import name
COMPANY_ORDER = [x.strip().upper() for x in os.getenv("COMPANY_ORDER", "PWP,DIN,DAN,DWT").split(",") if x.strip()]
ELHP_COMPANY_VALUES = {"DAN", "DWT"}
INCLUDE_ELHP_UNKNOWN = os.getenv("INCLUDE_ELHP_UNKNOWN", "false").lower() in {"1", "true", "yes", "y"}
DASHBOARD_REQUIRE_ESTATE = os.getenv("DASHBOARD_REQUIRE_ESTATE", "true").lower() in {"1", "true", "yes", "y"}
MIN_VISIBLE_GROUP_USAGE = max(2, int(os.getenv("MIN_VISIBLE_GROUP_USAGE", "2")))
HARMONY_TRANSACTION_SAME_THRESHOLD = float(
    os.getenv("HARMONY_TRANSACTION_SAME_THRESHOLD", "83.0")
)
DIN_TRANSPORT_START_MONTH = "2026-03"
DASHBOARD_MODEL_VERSION = os.getenv("MODEL_VERSION", "resnet18_imagenet_v1")

# SAP/DB60 company codes are not the same as Golden Eye company labels.
# Keep this mapping local to dashboard/report queries so direct SAP coverage
# does not accidentally compare BUKRS=DIN/PWP instead of DI/PW.
def _parse_sap_company_code_map() -> dict[str, str]:
    raw = os.getenv(
        "SAP_COMPANY_CODE_MAP",
        "PWP:PW,DIN:DI,DAN:DA,DWT:DW,ELHP_DIN:DI,ELHP_DAN:DA,ELHP_DWT:DW",
    )
    out: dict[str, str] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip().upper()
        value = value.strip().upper()
        if key and value:
            out[key] = value
    return out


SAP_COMPANY_CODE_MAP = _parse_sap_company_code_map()

# Normalized dataset cache. This is the main speed fix.
# PostgreSQL is read into memory once, normalized once, then all filter/chart
# callbacks work against RAM. BYTEA image data is still on-demand only.
import time
DATA_CACHE_SECONDS = int(os.getenv("DATA_CACHE_SECONDS", "120"))
_norm_cache: dict[str, tuple[float, pd.DataFrame]] = {}

def clear_data_cache() -> None:
    _norm_cache.clear()

def _norm_cache_get(key: str) -> pd.DataFrame | None:
    if DATA_CACHE_SECONDS <= 0:
        return None
    item = _norm_cache.get(key)
    if not item:
        return None
    ts, df = item
    if time.time() - ts > DATA_CACHE_SECONDS:
        _norm_cache.pop(key, None)
        return None
    return df.copy()

def _norm_cache_set(key: str, df: pd.DataFrame) -> None:
    if DATA_CACHE_SECONDS > 0:
        _norm_cache[key] = (time.time(), df.copy())


def norm_key(x: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(x or "").strip().lower()).strip("_")


def clean_text(x: Any) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.lower() in INVALID else s


def is_valid(x: Any) -> bool:
    return bool(clean_text(x))


def split_values(x: Any) -> list[str]:
    s = clean_text(x)
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"\s*,\s*", s) if clean_text(p)]
    return parts or [s]


def first_value(x: Any, fallback: str = "UNKNOWN") -> str:
    vals = split_values(x)
    return vals[0] if vals else fallback


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    if df is None or df.empty:
        return None
    cmap = {norm_key(c): c for c in df.columns}
    for cand in candidates:
        key = norm_key(cand)
        if key in cmap:
            return cmap[key]
    return None


def _value_has_token(value: Any, token: str) -> bool:
    s = clean_text(value).upper().replace("\\", "/")
    token = token.upper()
    if not s:
        return False
    return bool(re.search(rf"(^|[^A-Z0-9]){token}([^A-Z0-9]|$)", s)) or f"ELHP_{token}" in s or f"/{token}/" in s or f"_{token}" in s


def _infer_company_from_estate(value: Any) -> str:
    vals = [v.upper() for v in split_values(value)]
    if any(v.startswith("PU") for v in vals):
        return "DAN"
    if any(v.startswith("ME") for v in vals):
        return "DWT"
    # BP rows appearing in ELHP table are usually Harmony/PWP contamination from old output.
    # Do not classify BP as ELHP, so System=ELHP stays clean.
    if any(v.startswith("BP") for v in vals):
        return "PWP"
    return "UNKNOWN"


def estate_matches_elhp_company(company: Any, value: Any) -> bool:
    """Aturan ownership ELHP: DAN hanya PU*, DWT hanya ME*."""
    company_key = clean_text(company).upper()
    if company_key not in ELHP_COMPANY_VALUES:
        return True
    tokens = [
        token.strip().upper()
        for token in re.split(r"\s*[,;|]\s*", clean_text(value))
        if token.strip()
    ]
    if not tokens:
        return False
    expected_prefix = "PU" if company_key == "DAN" else "ME"
    return all(token.startswith(expected_prefix) for token in tokens)


def _filter_elhp_estate_scope(
    df: pd.DataFrame,
    estate_column: str,
) -> pd.DataFrame:
    if df is None or df.empty or estate_column not in df.columns:
        return df
    if "source_company" not in df.columns:
        return df
    valid = df.apply(
        lambda row: estate_matches_elhp_company(
            row.get("source_company", ""),
            row.get(estate_column, ""),
        ),
        axis=1,
    )
    return df[valid].copy()


def detect_elhp_company_from_row(row: Any) -> str:
    """Detect ELHP company per row.

    Priority:
    1. explicit company code/source path: DAN/DWT/PWP
    2. estate prefix: PU*=DAN, ME*=DWT, BP*=PWP/Harmony contamination
    """
    explicit_cols = [
        "download_report_company_code", "source_company", "elhp_company", "company", "company_code",
        "source_group", "source_name", "source_key", "source", "dataset", "file_source",
        "source_file", "input_file", "file_path", "path", "folder", "output_dir", "source_lhp",
    ]
    try:
        keys = list(row.index) if hasattr(row, "index") else list(row.keys())
    except Exception:
        keys = []
    key_map = {norm_key(k): k for k in keys}

    text_parts: list[str] = []
    for cand in explicit_cols:
        col = key_map.get(norm_key(cand))
        if col is None:
            continue
        try:
            val = clean_text(row.get(col, ""))
        except Exception:
            val = ""
        if val:
            text_parts.append(val)

    txt = " | ".join(text_parts).upper().replace("\\", "/")
    if _value_has_token(txt, "DAN"):
        return "DAN"
    if _value_has_token(txt, "DWT"):
        return "DWT"
    if _value_has_token(txt, "PWP") or _value_has_token(txt, "HARMONY"):
        return "PWP"

    # Fallback from estate-like columns.
    estate_cols = ["estate", "estate_list", "department_code", "department_code_list", "estate_primary"]
    for cand in estate_cols:
        col = key_map.get(norm_key(cand))
        if col is None:
            continue
        try:
            comp = _infer_company_from_estate(row.get(col, ""))
        except Exception:
            comp = "UNKNOWN"
        if comp != "UNKNOWN":
            return comp
    return "UNKNOWN"


def add_source_columns(df: pd.DataFrame, source_table_kind: str) -> pd.DataFrame:
    if df.empty:
        return df
    kind = str(source_table_kind or "HARMONY").upper()
    if kind == "HARMONY":
        company_col = find_col(df, ["source_company", "company_code", "company", "source_group"])
        if company_col:
            company = df[company_col].apply(lambda value: clean_text(value).upper() or "PWP")
        else:
            company = pd.Series(["PWP"] * len(df), index=df.index)
        df["system"] = "HARMONY"
        df["source_system"] = "HARMONY"
        df["source_company"] = company
        df["company"] = company
        df["source_label"] = "Harmony / " + company.astype(str)
        return df

    # ELHP table: classify only DAN/DWT as valid ELHP. PWP/BP contamination is excluded later.
    if "source_company" not in df.columns:
        df["source_company"] = df.apply(detect_elhp_company_from_row, axis=1)
    else:
        empty = df["source_company"].apply(lambda v: clean_text(v) == "")
        if empty.any():
            df.loc[empty, "source_company"] = df[empty].apply(detect_elhp_company_from_row, axis=1)
        df["source_company"] = df["source_company"].apply(lambda v: clean_text(v).upper() if clean_text(v) else "UNKNOWN")
        # Re-detect invalid or ambiguous values from the row.
        bad = ~df["source_company"].isin(["DAN", "DWT", "PWP"])
        if bad.any():
            df.loc[bad, "source_company"] = df[bad].apply(detect_elhp_company_from_row, axis=1)

    df["source_company"] = df["source_company"].apply(lambda v: clean_text(v).upper() if clean_text(v) else "UNKNOWN")
    df["company"] = df["source_company"]
    df["system"] = df["source_company"].apply(lambda c: "ELHP" if c in ELHP_COMPANY_VALUES else ("HARMONY" if c == "PWP" else "UNKNOWN"))
    df["source_system"] = df["source_company"].apply(lambda c: f"ELHP_{c}" if c in ELHP_COMPANY_VALUES else ("HARMONY" if c == "PWP" else "ELHP_UNKNOWN"))
    df["source_label"] = df["source_company"].apply(lambda c: f"ELHP {c}" if c in ELHP_COMPANY_VALUES else ("Harmony / PWP" if c == "PWP" else "Unknown"))
    return df


def ensure_alias(df: pd.DataFrame, target: str, candidates: list[str], default: Any = "") -> pd.DataFrame:
    if target not in df.columns:
        df[target] = default
    for cand in candidates:
        src = find_col(df, [cand])
        if not src or src == target:
            continue
        try:
            target_empty = df[target].apply(lambda v: clean_text(v) == "")
            src_valid = df[src].apply(lambda v: clean_text(v) != "")
            mask = target_empty & src_valid
            if mask.any():
                df.loc[mask, target] = df.loc[mask, src]
        except Exception:
            pass
    try:
        target_empty = df[target].apply(lambda v: clean_text(v) == "")
        if target_empty.any() and default != "":
            df.loc[target_empty, target] = default
    except Exception:
        pass
    return df


def parse_date_list(x: Any) -> list[pd.Timestamp]:
    out = []
    for v in split_values(x):
        dt = pd.to_datetime(v, errors="coerce")
        if pd.notna(dt):
            out.append(dt)
    return out


def first_month(x: Any) -> str:
    dates = parse_date_list(x)
    if dates:
        return sorted(dates)[0].strftime("%Y-%m")
    return "Tanpa Bulan"


def date_category_from_list(x: Any) -> str:
    dates = parse_date_list(x)
    if not dates:
        return "Tidak Ada Tanggal"
    days = {d.strftime("%Y-%m-%d") for d in dates}
    months = {d.strftime("%Y-%m") for d in dates}
    if len(days) <= 1:
        return "Tanggal Sama"
    if len(months) <= 1:
        return "Bulan Sama"
    return "Bulan Berbeda"


def reuse_category_from_lists(afdeling: Any, blok: Any, tph: Any, estate: Any = "") -> str:
    estates = set(split_values(estate))
    afds = set(split_values(afdeling))
    bloks = set(split_values(blok))
    tphs = set(split_values(tph))
    if len(estates) > 1:
        return "Estate Berbeda"
    if len(afds) > 1:
        return "Afdeling Berbeda"
    if len(bloks) > 1:
        return "Blok Berbeda"
    if len(tphs) > 1:
        return "Blok Sama / TPH Berbeda"
    return "TPH Sama"


def normalize_group(df: pd.DataFrame, source_table_kind: str = "HARMONY") -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return pd.DataFrame(columns=["system", "source_system", "source_company", "group_id", "group_key"])
    df.columns = [str(c).strip() for c in df.columns]
    for target, cands, default in [
        ("group_id", ["group_id", "Group ID"], 0),
        ("representative_image", ["representative_image", "Nama Gambar Representatif", "Representative Image", "image", "Image"], ""),
        ("usage_count", ["usage_count", "Usage Count", "Total Usage", "usage"], 0),
        ("matched_image_count", ["matched_image_count", "matched_download_report_count"], 0),
        ("mean_embedding_similarity", ["mean_embedding_similarity", "group_mean_similarity"], np.nan),
        ("min_member_mean_similarity", ["min_member_mean_similarity", "minimum_member_similarity"], np.nan),
        ("estate_list", ["estate_list", "Estate", "department_code", "estate"], ""),
        ("afdeling_list", ["afdeling_list", "Afdeling", "afdeling"], ""),
        ("blok_list", ["blok_list", "Blok", "block", "blok"], ""),
        ("tph_list", ["tph_list", "TPH", "tph"], ""),
        ("oph_list", ["oph_list", "OPH", "harvest_ticket"], ""),
        ("sql_date_list", ["sql_date_list", "tanggal_list", "Tanggal", "period_date", "report_date_list"], ""),
        ("pemanen_list", ["pemanen_list", "ename_list", "Pemanen"], ""),
        ("kerani_panen_list", ["kerani_panen_list", "nama_kerani_panen_list", "Kerani Panen"], ""),
        ("mandor_list", ["mandor_list", "ename_m_list", "Mandor"], ""),
        ("kerani_ft_list", ["kerani_ft_list", "nama_kerani_transport_ft_list", "Kerani FT"], ""),
        ("kerani_dt_list", ["kerani_dt_list", "nama_kerani_transport_dt_list", "Kerani DT"], ""),
        ("status_transport_list", ["status_transport_list", "Status Transport", "transport_status"], ""),
        ("similarity_list", ["similarity_list", "similarity_category_list", "Similarity"], ""),
        ("total_bunch_pay_group", ["total_bunch_pay_group", "group_total_bunch_pay", "total_bunch_pay", "bunch_pay_total", "total_bunch", "sum_bunch_pay", "Total Bunch Pay", "Bunch Pay"], 0),
        ("reuse_category", ["reuse_category", "Reuse Category"], ""),
        ("date_category", ["date_category", "Date Category"], ""),
        ("reuse_date_category", ["reuse_date_category", "Reuse Date Category"], ""),
    ]:
        ensure_alias(df, target, cands, default)

    for c in ["group_id", "usage_count", "matched_image_count", "total_bunch_pay_group"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["group_id"] = df["group_id"].astype(int)
    df["usage_count"] = df["usage_count"].astype(int)
    df["matched_image_count"] = df["matched_image_count"].astype(int)
    df["mean_embedding_similarity"] = pd.to_numeric(
        df["mean_embedding_similarity"], errors="coerce"
    )
    df["min_member_mean_similarity"] = pd.to_numeric(
        df["min_member_mean_similarity"], errors="coerce"
    )

    if DASHBOARD_REQUIRE_ESTATE:
        df = df[df["estate_list"].apply(is_valid)].copy()

    df = add_source_columns(df, source_table_kind)
    df = _filter_elhp_estate_scope(df, "estate_list")
    df["group_key"] = df["source_system"].astype(str) + "|" + df["group_id"].astype(str)
    df["representative_image"] = df["representative_image"].apply(clean_text)
    df["image_key"] = df["representative_image"].str.upper()
    df["month"] = df["sql_date_list"].apply(first_month)
    df["estate_primary"] = df["estate_list"].apply(first_value)
    df["afdeling_primary"] = df["afdeling_list"].apply(first_value)
    df["blok_primary"] = df["blok_list"].apply(first_value)
    df["tph_primary"] = df["tph_list"].apply(first_value)
    df["kerani_primary"] = df["kerani_panen_list"].apply(first_value)
    df["mandor_primary"] = df["mandor_list"].apply(first_value)
    df["pemanen_primary"] = df["pemanen_list"].apply(first_value)
    df["similarity_primary"] = df["similarity_list"].apply(lambda x: first_value(x, "Tidak Ada Gambar"))

    is_elhp = df["system"].astype(str).eq("ELHP")
    df.loc[is_elhp, "status_transport_list"] = TRANSPORT_NA_ELHP
    df.loc[is_elhp, "kerani_ft_list"] = ""
    df.loc[is_elhp, "kerani_dt_list"] = ""

    mask_empty_reuse = df["reuse_category"].astype(str).str.strip().eq("")
    df.loc[mask_empty_reuse, "reuse_category"] = df.loc[mask_empty_reuse].apply(
        lambda r: reuse_category_from_lists(r.get("afdeling_list"), r.get("blok_list"), r.get("tph_list"), r.get("estate_list")), axis=1,
    )
    mask_empty_date = df["date_category"].astype(str).str.strip().eq("")
    df.loc[mask_empty_date, "date_category"] = df.loc[mask_empty_date, "sql_date_list"].apply(date_category_from_list)
    mask_empty_rd = df["reuse_date_category"].astype(str).str.strip().eq("")
    df.loc[mask_empty_rd, "reuse_date_category"] = df.loc[mask_empty_rd, "reuse_category"].astype(str) + " | " + df.loc[mask_empty_rd, "date_category"].astype(str)
    return df


def normalize_detail(df: pd.DataFrame, source_table_kind: str = "HARMONY") -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return pd.DataFrame(columns=["system", "source_system", "source_company", "group_id", "group_key"])
    df.columns = [str(c).strip() for c in df.columns]
    for target, cands, default in [
        ("group_id", ["group_id", "Group ID"], 0),
        ("image_name", ["image_name", "Image", "Nama Gambar", "representative_image", "nama_gambar_representatif", "file_name", "output_file", "ffb_evidence_kp", "foto_panen"], ""),
        ("is_representative", ["is_representative", "Representative"], 0),
        ("estate", ["estate", "Estate", "estate_list", "department_code", "kebun"], ""),
        ("afdeling", ["afdeling", "Afdeling", "afdeling_code", "afdeling_id", "afd", "afd_code", "afdeling_list"], ""),
        ("blok", ["blok", "Blok", "block", "blok_list", "block_code"], ""),
        ("tph", ["tph", "TPH", "tph_no", "tph_list", "tph_code"], ""),
        ("oph", ["oph", "OPH", "harvest_ticket", "harvest_ticket_sample", "oph_list", "sql_oph", "tiket_panen", "ticket", "harvest ticket"], ""),
        ("tanggal", ["tanggal", "Tanggal", "period_date", "sql_date_list", "report_date", "date", "tanggal_panen", "first_period_date"], ""),
        ("pemanen", ["pemanen", "Pemanen", "ename", "name", "pemanen_list", "nama_pemanen", "employee_name"], ""),
        ("kerani_panen", ["kerani_panen", "Kerani Panen", "nama_kerani_panen", "name_kerani", "kerani_panen_list", "nama_kerani", "kerani"], ""),
        ("jjg_kerani_panen", ["jjg_kerani_panen", "JJG Kerani Panen", "jjg_kerani_panen_lookup"], ""),
        ("mandor", ["mandor", "Mandor", "ename_m", "name_mandor", "mandor_list", "nama_mandor"], ""),
        ("kerani_ft", ["kerani_ft", "Kerani FT", "nama_kerani_transport_ft"], ""),
        ("jjg_ft", ["jjg_ft", "JJG FT", "jjg_transport_ft"], ""),
        ("kerani_dt", ["kerani_dt", "Kerani DT", "nama_kerani_transport_dt"], ""),
        ("jjg_dt", ["jjg_dt", "JJG DT", "jjg_transport_dt"], ""),
        ("similarity", ["similarity", "Similarity", "similarity_category"], ""),
        ("bunch_pay", ["bunch_pay", "Bunch Pay / OPH", "bunch_pay_total_per_oph", "total_bunch_pay_per_oph", "jjg_kerani_panen_lookup", "jjg_kerani_panen", "bunch", "jjg", "std_bunch", "qty_jjg", "janjang"], 0),
        ("foto_dt", ["foto_dt", "Foto DT", "transport_dt_image"], ""),
        ("foto_ft", ["foto_ft", "Foto FT", "transport_ft_image"], ""),
        ("foto_kt_before", ["foto_kt_before", "FFB_EVIDENCE_KT_BEFORE", "transport_kt_before_image"], ""),
        ("transport_status", ["transport_status", "Status Transport"], ""),
        ("latitude", ["latitude", "Latitude", "lat"], ""),
        ("longitude", ["longitude", "Longitude", "long", "lng"], ""),
        ("match_source", ["match_source", "download_report_match_source"], ""),
    ]:
        ensure_alias(df, target, cands, default)
    df["group_id"] = pd.to_numeric(df["group_id"], errors="coerce").fillna(0).astype(int)
    if DASHBOARD_REQUIRE_ESTATE:
        df = df[df["estate"].apply(is_valid)].copy()
    df = add_source_columns(df, source_table_kind)
    df = _filter_elhp_estate_scope(df, "estate")
    df["group_key"] = df["source_system"].astype(str) + "|" + df["group_id"].astype(str)
    df["image_key"] = df["image_name"].apply(clean_text).str.upper()
    df["month"] = df["tanggal"].apply(first_month)
    df["bunch_pay"] = pd.to_numeric(df["bunch_pay"], errors="coerce").fillna(0)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    is_elhp = df["system"].astype(str).eq("ELHP")
    df.loc[is_elhp, "transport_status"] = TRANSPORT_NA_ELHP
    df.loc[is_elhp, "foto_dt"] = ""
    df.loc[is_elhp, "foto_ft"] = ""
    df.loc[is_elhp, "foto_kt_before"] = ""
    df.loc[is_elhp, "kerani_ft"] = ""
    df.loc[is_elhp, "kerani_dt"] = ""
    return df


def _oph_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean_text(value).upper())


def _attach_kt_before_transport(df: pd.DataFrame) -> pd.DataFrame:
    """Tempelkan foto KT Before ke detail melalui OPH tanpa mengubah Gold."""
    if df is None or df.empty or "oph" not in df.columns:
        return df
    out = df.copy()
    if "foto_kt_before" not in out.columns:
        out["foto_kt_before"] = ""
    if "transport_similarity_score" not in out.columns:
        out["transport_similarity_score"] = pd.NA
    if "transport_similarity_category" not in out.columns:
        out["transport_similarity_category"] = ""
    if not table_exists(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA):
        return out
    source_company = out.get(
        "source_company",
        pd.Series([""] * len(out), index=out.index),
    ).fillna("").astype(str).str.upper()
    harmony_transport = source_company.isin({"PWP", "DIN"})
    out["_transport_oph_key"] = out["oph"].apply(_oph_key)
    companies = sorted(set(source_company.loc[harmony_transport].tolist()))
    if not companies:
        return out.drop(columns=["_transport_oph_key"])
    mapping_frames: list[pd.DataFrame] = []
    similarity_available = table_exists(
        TABLE_TRANSPORT_KT_SIMILARITY, RECOVERY_SCHEMA
    )
    for company in companies:
        keys = sorted({
            value
            for value in out.loc[
                source_company.eq(company), "_transport_oph_key"
            ].tolist()
            if value
        })
        if not keys:
            continue
        if similarity_available:
            mapping_sql = f"""
            SELECT DISTINCT ON (c.oph_key, s.panen_file_uid)
                   c.source_group,
                   c.oph_key,
                   c.evidence_file_name AS foto_kt_before,
                   s.panen_file_uid,
                   s.similarity_score AS transport_similarity_score,
                   s.similarity_category AS transport_similarity_category
            FROM {qname(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)} c
            JOIN {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)} g
              ON g.source_group=c.source_group
             AND g.file_uid=c.file_uid
             AND g.is_active=true
            LEFT JOIN {qname(TABLE_TRANSPORT_KT_SIMILARITY, RECOVERY_SCHEMA)} s
              ON s.source_group=c.source_group
             AND s.period_year=c.period_year
             AND s.oph_key=c.oph_key
             AND s.transport_file_uid=c.file_uid
             AND s.is_active=true
            WHERE c.source_group=%s
              AND c.period_year=2026
              AND c.is_active=true
              AND c.load_status='READY'
              AND c.oph_key=ANY(%s)
            ORDER BY c.oph_key, s.panen_file_uid,
                     c.period_date DESC NULLS LAST, c.evidence_key
            """
        else:
            mapping_sql = f"""
            SELECT DISTINCT ON (c.oph_key)
                   c.source_group,
                   c.oph_key,
                   c.evidence_file_name AS foto_kt_before,
                   NULL::text AS panen_file_uid,
                   NULL::real AS transport_similarity_score,
                   NULL::text AS transport_similarity_category
            FROM {qname(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)} c
            JOIN {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)} g
              ON g.source_group=c.source_group
             AND g.file_uid=c.file_uid
             AND g.is_active=true
            WHERE c.source_group=%s
              AND c.period_year=2026
              AND c.is_active=true
              AND c.load_status='READY'
              AND c.oph_key=ANY(%s)
            ORDER BY c.oph_key, c.period_date DESC NULLS LAST, c.evidence_key
            """
        try:
            frame = read_df_resilient(
                mapping_sql,
                [company, keys],
                operation=f"transport KT detail {company}",
            )
        except Exception as exc:
            # Kerusakan heap/TOAST tidak dapat diatasi dengan sequential scan.
            # Detail Gold tetap ditampilkan tanpa lampiran KT agar satu storage
            # relation tidak menjatuhkan seluruh callback Dash.
            message = " ".join(str(exc).split())
            print(
                f"[TRANSPORT KT WARN] {company}: attachment dilewati; "
                f"jalankan repair index/storage. detail={message[:300]}",
                flush=True,
            )
            continue
        if not frame.empty:
            mapping_frames.append(frame)
    if not mapping_frames:
        return out.drop(columns=["_transport_oph_key"])
    mapping = pd.concat(mapping_frames, ignore_index=True, sort=False)
    if mapping.empty:
        return out.drop(columns=["_transport_oph_key"])
    file_by_oph = {
        (str(row.source_group).upper(), str(row.oph_key)): str(row.foto_kt_before)
        for row in mapping.itertuples()
    }
    company_oph_keys = list(zip(
        source_company.astype(str), out["_transport_oph_key"].astype(str)
    ))
    attached = pd.Series(
        [file_by_oph.get(key, "") for key in company_oph_keys],
        index=out.index,
    )
    current = out["foto_kt_before"].fillna("").astype(str).str.strip()
    out["foto_kt_before"] = current.where(current.ne(""), attached)
    scored = mapping[
        mapping.get("panen_file_uid", pd.Series(dtype="object"))
        .fillna("").astype(str).str.strip().ne("")
    ].copy()
    if not scored.empty and "file_uid" in out.columns:
        score_by_pair = {
            (
                str(row.source_group).upper(),
                str(row.oph_key),
                str(row.panen_file_uid),
            ): row.transport_similarity_score
            for row in scored.itertuples()
        }
        category_by_pair = {
            (
                str(row.source_group).upper(),
                str(row.oph_key),
                str(row.panen_file_uid),
            ):
                str(row.transport_similarity_category or "")
            for row in scored.itertuples()
        }
        pair_keys = list(zip(
            source_company.astype(str),
            out["_transport_oph_key"].astype(str),
            out["file_uid"].fillna("").astype(str),
        ))
        out["transport_similarity_score"] = [
            score_by_pair.get(key, pd.NA) for key in pair_keys
        ]
        out["transport_similarity_category"] = [
            category_by_pair.get(key, "") for key in pair_keys
        ]
    has_kt = out["foto_kt_before"].fillna("").astype(str).str.strip().ne("")
    if "transport_status" in out.columns:
        status = out["transport_status"].fillna("").astype(str).str.strip()
        replace = has_kt & (
            status.eq("")
            | status.str.contains("Tidak Ada", case=False, na=False)
        )
        out.loc[replace, "transport_status"] = "Ada Foto Transport KT Before"
    return out.drop(columns=["_transport_oph_key"])


def _attach_group_kt_before_status(df: pd.DataFrame) -> pd.DataFrame:
    """Samakan status Ringkasan dengan bukti KT Before yang dipakai Detail.

    Gold group summary dibuat saat PUBLISH dan status legacy-nya hanya mengenal
    DT/FT.  KT Before dapat disinkronkan/di-score setelah publish.  Query kecil
    ini tidak membaca BYTEA; foto dianggap tersedia hanya jika OPH Gold aktif
    cocok dengan Context KT READY dan payload hot-store masih aktif.  Dengan
    demikian pasangan similarity lama yang OPH-nya kosong/berbeda tidak dapat
    mengubah status Ringkasan.
    """
    if df is None or df.empty or "group_id" not in df.columns:
        return df
    if not (
        table_exists(TABLE_DETAIL)
        and table_exists(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)
        and table_exists(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)
    ):
        return df

    out = df.copy()
    companies = sorted({
        clean_text(value).upper()
        for value in out.get(
            "source_company", pd.Series(dtype="object")
        ).tolist()
        if clean_text(value).upper() in {"PWP", "DIN"}
    })
    if not companies:
        return out

    cache_key = "group-kt-status:" + ",".join(companies)
    evidence = _norm_cache_get(cache_key)
    if evidence is None:
        sql = f"""
            SELECT upper(d.source_group) AS source_group,
                   d.group_id,
                   count(*)::bigint AS member_count,
                   count(*) FILTER (
                       WHERE EXISTS (
                           SELECT 1
                           FROM {qname(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)} c
                           JOIN {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)} g
                             ON g.source_group=c.source_group
                            AND g.file_uid=c.file_uid
                            AND g.is_active=true
                           WHERE upper(c.source_group)=upper(d.source_group)
                             AND c.period_year=extract(year FROM d.tanggal)::integer
                             AND c.is_active=true
                             AND c.load_status='READY'
                             AND c.oph_key=regexp_replace(
                                 upper(btrim(coalesce(d.oph, ''))),
                                 '[^A-Z0-9]', '', 'g'
                             )
                       )
                   )::bigint AS kt_photo_count
            FROM {qname(TABLE_DETAIL)} d
            WHERE d.is_active=true
              AND upper(d.source_group)=ANY(%s)
            GROUP BY upper(d.source_group), d.group_id
        """
        try:
            evidence = read_df_resilient(
                sql,
                [companies],
                operation="Ringkasan Harmony KT Before status",
            )
        except Exception as exc:
            print(
                "[TRANSPORT KT WARN] status Ringkasan tidak dapat direkonsiliasi; "
                f"detail={' '.join(str(exc).split())[:300]}",
                flush=True,
            )
            return out
        _norm_cache_set(cache_key, evidence)

    if evidence is None or evidence.empty:
        return out
    evidence_map = {
        (str(row.source_group).upper(), int(row.group_id)): (
            int(row.member_count or 0), int(row.kt_photo_count or 0)
        )
        for row in evidence.itertuples()
    }

    def reconciled_status(row: pd.Series) -> str:
        company = clean_text(row.get("source_company", "")).upper()
        try:
            group_id = int(row.get("group_id", 0) or 0)
        except (TypeError, ValueError):
            return clean_text(row.get("status_transport_list", ""))
        total, with_kt = evidence_map.get((company, group_id), (0, 0))
        current = [
            clean_text(value)
            for value in split_values(row.get("status_transport_list", ""))
            if clean_text(value)
        ]
        if with_kt <= 0:
            return ", ".join(dict.fromkeys(current))

        # Placeholder legacy tidak boleh tetap menyatakan seluruh group tidak
        # punya transport bila KT Before aktif sudah terbukti.
        current = [
            value for value in current
            if "TIDAK ADA" not in value.upper()
        ]
        current.append("Ada Foto Transport KT Before")
        if total > 0 and with_kt < total:
            current.append("Tidak Ada Foto Transport")
        return ", ".join(dict.fromkeys(current))

    if "status_transport_list" not in out.columns:
        out["status_transport_list"] = ""
    out["status_transport_list"] = out.apply(reconciled_status, axis=1)
    return out


def transport_similarity_usage_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Hitung kategori KP-vs-KT pada level usage dari detail Harmony aktif.

    Ini adalah satu-satunya basis chart similarity transport untuk Ringkasan,
    halaman Similarity, dan PDF.  Kolom ``similarity`` Gold tidak dipakai
    karena merupakan snapshot transport legacy saat PUBLISH.
    """
    columns = ["label", "total_usage"]
    if detail is None or detail.empty:
        return pd.DataFrame(columns=columns)

    data = detail.copy()
    category = data.get(
        "transport_similarity_category",
        pd.Series([""] * len(data), index=data.index, dtype="object"),
    ).fillna("").astype(str).str.strip()
    category = category.replace({
        "BELUM SIAP": "Skor Tidak Tersedia",
        "BELUM DIHITUNG": "Skor Tidak Tersedia",
        "Belum Dihitung": "Skor Tidak Tersedia",
        "TIDAK ADA TRANSPORT": "Tidak Ada Foto Transport",
        "Tidak Ada Transport": "Tidak Ada Foto Transport",
    })

    transport_columns = [
        column for column in (
            "foto_kt_before",
            "transport_kt_before_image",
            "transport_image",
        )
        if column in data.columns
    ]
    has_transport = pd.Series(False, index=data.index, dtype=bool)
    for column in transport_columns:
        values = data[column].fillna("").astype(str).str.strip()
        valid = values.ne("") & ~values.str.upper().str.contains(
            r"TIDAK ADA|NO IMAGE|NO FOTO|NO PHOTO|MISSING|EMPTY|KOSONG",
            regex=True,
            na=False,
        )
        has_transport |= valid

    label = category.copy()
    label = label.where(label.ne(""), "Skor Tidak Tersedia")
    label = label.where(has_transport, "Tidak Ada Foto Transport")
    # Jangan percaya category tanpa evidence transport aktif yang sudah
    # ditempelkan melalui OPH Gold + Context READY + hot-store aktif.
    data["_transport_similarity_label"] = label
    result = (
        data.groupby("_transport_similarity_label", dropna=False)
        .size()
        .reset_index(name="total_usage")
        .rename(columns={"_transport_similarity_label": "label"})
    )
    result["total_usage"] = pd.to_numeric(
        result["total_usage"], errors="coerce"
    ).fillna(0).astype(int)
    return result.sort_values(
        ["total_usage", "label"], ascending=[False, True]
    ).reset_index(drop=True)


def load_harmony_transaction_validation(
    companies: Any = None,
    period_year: int = 2026,
) -> pd.DataFrame:
    """Muat metadata pasangan foto Kerani Panen vs Transport Harmony.

    Query ini tidak membaca BYTEA. Foto dimuat on-demand saat user memilih
    transaksi: NAS exact-path untuk panen/transport, lalu hot store sebagai
    fallback. Unit menu ini adalah satu ``(company, oph_key)`` dan bukan satu
    file_uid. Jika satu OPH memiliki beberapa foto KP/KT, query memilih satu
    pasangan canonical secara deterministik dan membawa jumlah kandidatnya
    sebagai audit; dengan demikian KPI tidak menggandakan transaksi SAP.
    Keputusan dihitung dari skor tersimpan memakai threshold bisnis 83% agar
    hasil lama langsung mengikuti aturan baru tanpa harus menunggu re-score.
    """
    columns = [
        "company", "period_year", "month", "tanggal", "oph", "oph_key",
        "estate", "afdeling", "blok", "tph", "kerani_panen", "mandor",
        "kerani_transport",
        "panen_file_uid", "panen_file_name", "panen_has_embedding",
        "transport_file_uid", "transport_file_name", "similarity_score",
        "panen_candidate_count", "transport_candidate_count",
        "transport_has_embedding", "decision", "transaction_status",
        "similarity_eligible", "calculated_at",
    ]
    if not table_exists("image_context", RECOVERY_SCHEMA):
        return pd.DataFrame(columns=columns)

    if isinstance(companies, str):
        requested = [companies]
    elif isinstance(companies, (list, tuple, set)):
        requested = list(companies)
    else:
        requested = ["PWP", "DIN"]
    requested = sorted({
        clean_text(value).upper()
        for value in requested
        if clean_text(value).upper() in {"PWP", "DIN"}
    }) or ["PWP", "DIN"]
    cache_key = (
        "harmony_transaction_validation:"
        + ",".join(requested)
        + f":{int(period_year)}"
    )
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        cached = cached.copy()
        if int(period_year) == 2026 and not cached.empty and {"company", "month"}.issubset(cached.columns):
            fair_mask = ~((cached["company"].astype(str).str.upper().eq("DIN")) & (cached["month"].astype(str) < DIN_TRANSPORT_START_MONTH))
            cached = cached.loc[fair_mask].copy()
        return cached

    transport_available = (
        table_exists(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)
        and table_exists(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)
    )
    similarity_available = table_exists(
        TABLE_TRANSPORT_KT_SIMILARITY, RECOVERY_SCHEMA
    )
    params: list[Any] = []
    transport_people_cte = """
        transport_people AS (
            SELECT NULL::text AS source_group,
                   NULL::text AS oph_key,
                   NULL::text AS kerani_transport
            WHERE false
        ),
    """
    transport_people_params: list[Any] = []
    if transport_available:
        if table_exists(TABLE_DETAIL):
            transport_people_cte = f"""
        transport_people AS (
            SELECT upper(d.source_group) AS source_group,
                   regexp_replace(upper(btrim(d.oph::text)),
                                  '[^A-Z0-9]', '', 'g') AS oph_key,
                   nullif(concat_ws(' / ',
                       string_agg(
                           DISTINCT nullif(trim(d.kerani_ft), ''),
                           ', ' ORDER BY nullif(trim(d.kerani_ft), '')
                       ),
                       string_agg(
                           DISTINCT nullif(trim(d.kerani_dt), ''),
                           ', ' ORDER BY nullif(trim(d.kerani_dt), '')
                       )
                   ), '') AS kerani_transport
            FROM {qname(TABLE_DETAIL)} d
            WHERE d.is_active=true
              AND upper(d.source_group)=ANY(%s)
            GROUP BY upper(d.source_group),
                     regexp_replace(upper(btrim(d.oph::text)),
                                    '[^A-Z0-9]', '', 'g')
        ),
        """
            transport_people_params = [requested]
        transport_cte = f"""
        {transport_people_cte}
        transport_ranked AS (
            SELECT upper(t.source_group) AS source_group,
                   t.period_year,
                   t.oph_key,
                   t.file_uid AS transport_file_uid,
                   t.evidence_file_name AS transport_file_name,
                   tp.kerani_transport,
                   (
                       tf.file_uid IS NOT NULL
                       AND tf.is_valid=true
                       AND tf.full_embedding IS NOT NULL
                       AND coalesce(tf.embedding_dim, 0) > 0
                   ) AS transport_has_embedding,
                   count(*) OVER (
                       PARTITION BY upper(t.source_group), t.period_year, t.oph_key
                   )::bigint AS transport_candidate_count,
                   row_number() OVER (
                       PARTITION BY upper(t.source_group), t.period_year, t.oph_key
                       ORDER BY t.period_date DESC NULLS LAST,
                                t.evidence_key, t.file_uid
                   ) AS transport_rank
            FROM {qname(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)} t
            JOIN {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)} g
              ON upper(g.source_group)=upper(t.source_group)
             AND g.file_uid=t.file_uid
             AND g.is_active=true
            LEFT JOIN {qname("image_feature", RECOVERY_SCHEMA)} tf
              ON tf.file_uid=t.file_uid
             AND tf.model_version=%s
             AND tf.is_transport=true
            LEFT JOIN transport_people tp
              ON tp.source_group=upper(t.source_group)
             AND tp.oph_key=t.oph_key
            WHERE t.is_active=true
              AND t.load_status='READY'
              AND t.period_year=%s
              AND upper(t.source_group)=ANY(%s)
        ),
        ready_transport AS (
            SELECT source_group, period_year, oph_key,
                   transport_file_uid, transport_file_name,
                   kerani_transport,
                   transport_candidate_count, transport_has_embedding
            FROM transport_ranked
            WHERE transport_rank=1
        )
        """
        params.extend(transport_people_params)
        params.extend([DASHBOARD_MODEL_VERSION, int(period_year), requested])
    else:
        transport_cte = """
        transport_people AS (
            SELECT NULL::text AS source_group,
                   NULL::text AS oph_key,
                   NULL::text AS kerani_transport
            WHERE false
        ),
        transport_ranked AS (
            SELECT NULL::text AS source_group, NULL::integer AS period_year,
                   NULL::text AS oph_key, NULL::text AS transport_file_uid,
                   NULL::text AS transport_file_name,
                   NULL::text AS kerani_transport,
                   false AS transport_has_embedding,
                   0::bigint AS transport_candidate_count,
                   0::bigint AS transport_rank
            WHERE false
        ),
        ready_transport AS (
            SELECT source_group, period_year, oph_key,
                   transport_file_uid, transport_file_name,
                   kerani_transport,
                   transport_candidate_count, transport_has_embedding
            FROM transport_ranked
            WHERE false
        )
        """

    if similarity_available:
        similarity_cte = f"""
        similarity_best AS (
            SELECT DISTINCT ON (
                       upper(s.source_group), s.period_year, s.oph_key,
                       s.panen_file_uid, s.transport_file_uid
                   )
                   upper(s.source_group) AS source_group,
                   s.period_year, s.oph_key,
                   s.panen_file_uid, s.transport_file_uid,
                   s.similarity_score, s.calculated_at
            FROM {qname(TABLE_TRANSPORT_KT_SIMILARITY, RECOVERY_SCHEMA)} s
            WHERE s.is_active=true
              AND s.period_year=%s
              AND upper(s.source_group)=ANY(%s)
            ORDER BY upper(s.source_group), s.period_year, s.oph_key,
                     s.panen_file_uid, s.transport_file_uid,
                     s.calculated_at DESC NULLS LAST
        )
        """
        params.extend([int(period_year), requested])
    else:
        similarity_cte = """
        similarity_best AS (
            SELECT NULL::text AS source_group, NULL::integer AS period_year,
                   NULL::text AS oph_key, NULL::text AS panen_file_uid,
                   NULL::text AS transport_file_uid,
                   NULL::real AS similarity_score,
                   NULL::timestamptz AS calculated_at
            WHERE false
        )
        """

    sql = f"""
        WITH {transport_cte},
        {similarity_cte},
        panen_base AS (
            SELECT upper(c.source_group) AS company,
                   extract(year FROM c.period_date)::integer AS period_year,
                   c.period_date AS tanggal,
                   c.oph,
                   c.oph_key,
                   c.estate,
                   c.afdeling,
                   c.blok,
                   c.tph,
                   c.kerani_panen,
                   c.mandor,
                   c.file_uid AS panen_file_uid,
                   coalesce(nullif(trim(f.file_name), ''), c.file_uid)
                       AS panen_file_name,
                   (
                       f.file_uid IS NOT NULL
                       AND f.is_valid=true
                       AND f.full_embedding IS NOT NULL
                       AND coalesce(f.embedding_dim, 0) > 0
                   )
                       AS panen_has_embedding
            FROM {qname("image_context", RECOVERY_SCHEMA)} c
            LEFT JOIN {qname("image_feature", RECOVERY_SCHEMA)} f
              ON f.file_uid=c.file_uid
             AND f.model_version=%s
            WHERE upper(c.source_group)=ANY(%s)
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=%s
              AND (
                    extract(year FROM c.period_date)::integer <> 2026
                    OR upper(c.source_group) <> 'DIN'
                    OR to_char(c.period_date, 'YYYY-MM') >= '{DIN_TRANSPORT_START_MONTH}'
                  )
              AND nullif(trim(coalesce(c.oph_key, '')), '') IS NOT NULL
        ),
        panen_ranked AS (
            SELECT p.*,
                   t.transport_file_uid,
                   t.transport_file_name,
                   t.kerani_transport,
                   coalesce(t.transport_candidate_count, 0)::bigint
                       AS transport_candidate_count,
                   coalesce(t.transport_has_embedding, false)
                       AS transport_has_embedding,
                   s.similarity_score,
                   s.calculated_at,
                   count(*) OVER (
                       PARTITION BY p.company, p.oph_key
                   )::bigint AS panen_candidate_count,
                   row_number() OVER (
                       PARTITION BY p.company, p.oph_key
                       ORDER BY
                           (s.similarity_score IS NOT NULL) DESC,
                           p.panen_has_embedding DESC,
                           p.tanggal DESC NULLS LAST,
                           p.panen_file_uid
                   ) AS panen_rank
            FROM panen_base p
            LEFT JOIN ready_transport t
              ON t.source_group=p.company
             AND t.period_year=p.period_year
             AND t.oph_key=p.oph_key
            LEFT JOIN similarity_best s
              ON s.source_group=p.company
             AND s.period_year=p.period_year
             AND s.oph_key=p.oph_key
             AND s.panen_file_uid=p.panen_file_uid
             AND s.transport_file_uid=t.transport_file_uid
        )
        SELECT
            company, period_year, tanggal, oph, oph_key,
            estate, afdeling, blok, tph, kerani_panen, mandor,
            kerani_transport,
            panen_file_uid, panen_file_name, panen_has_embedding,
            transport_file_uid, transport_file_name,
            transport_has_embedding, similarity_score, calculated_at,
            panen_candidate_count, transport_candidate_count
        FROM panen_ranked
        WHERE panen_rank=1
        ORDER BY tanggal DESC, company, oph_key
    """
    params.extend([DASHBOARD_MODEL_VERSION, requested, int(period_year)])
    try:
        out = read_df_resilient(
            sql,
            params,
            operation="Harmony KP vs Transport transaction metadata",
        )
    except Exception as exc:
        message = " ".join(str(exc).split())
        print(
            "[HARMONY TRANSACTION WARN] metadata pasangan tidak dapat dibaca; "
            f"detail={message[:300]}",
            flush=True,
        )
        return pd.DataFrame(columns=columns)
    if out.empty:
        return pd.DataFrame(columns=columns)

    out["tanggal"] = pd.to_datetime(out["tanggal"], errors="coerce")
    out["month"] = out["tanggal"].dt.strftime("%Y-%m").fillna("Tanpa Bulan")
    if int(period_year) == 2026:
        fair_mask = ~((out["company"].astype(str).str.upper().eq("DIN")) & (out["month"].astype(str) < DIN_TRANSPORT_START_MONTH))
        out = out.loc[fair_mask].copy()
    out["similarity_score"] = pd.to_numeric(
        out["similarity_score"], errors="coerce"
    )
    out["panen_candidate_count"] = pd.to_numeric(
        out["panen_candidate_count"], errors="coerce"
    ).fillna(0).astype(int)
    out["transport_candidate_count"] = pd.to_numeric(
        out["transport_candidate_count"], errors="coerce"
    ).fillna(0).astype(int)
    has_oph = out["oph_key"].fillna("").astype(str).str.strip().ne("")
    has_transport_photo = (
        out["transport_file_uid"].fillna("").astype(str).str.strip().ne("")
    )
    has_transport_embedding = out.get(
        "transport_has_embedding", pd.Series(False, index=out.index)
    ).fillna(False).astype(bool)
    has_embedding = out["panen_has_embedding"].fillna(False).astype(bool)
    has_panen_photo = has_embedding
    has_transport = has_transport_photo & has_transport_embedding
    ready = (
        out["similarity_score"].notna()
        & has_oph
        & has_transport
        & has_transport_embedding
        & has_panen_photo
    )
    same = ready & out["similarity_score"].ge(
        HARMONY_TRANSACTION_SAME_THRESHOLD
    )
    out["decision"] = "SKOR TIDAK TERSEDIA"
    out.loc[~has_panen_photo, "decision"] = "TIDAK ADA FOTO PANEN"
    out.loc[
        ~has_transport & has_panen_photo,
        "decision",
    ] = "TIDAK ADA FOTO TRANSPORT"
    out.loc[ready & ~same, "decision"] = "BERBEDA"
    out.loc[same, "decision"] = "SAMA"
    out["transaction_status"] = "SKOR TIDAK TERSEDIA"
    out.loc[~has_panen_photo, "transaction_status"] = "TIDAK ADA FOTO PANEN"
    out.loc[
        ~has_transport & has_panen_photo,
        "transaction_status",
    ] = "TIDAK ADA FOTO TRANSPORT"
    out.loc[ready, "transaction_status"] = "SIAP TRANSAKSI"
    out["similarity_eligible"] = ready
    for column in columns:
        if column not in out.columns:
            out[column] = ""
    result = out[columns].copy()
    _norm_cache_set(cache_key, result)
    return result


def normalize_dashboard_monthly(
    df: pd.DataFrame,
    source_table_kind: str = "HARMONY",
) -> pd.DataFrame:
    """Normalize compact exact monthly aggregates produced by PUBLISH."""
    df = df.copy()
    if df.empty:
        return pd.DataFrame(
            columns=[
                "system", "source_system", "source_company", "group_id",
                "group_key", "month", "estate", "afdeling", "kerani_panen",
                "mandor", "pemanen", "usage_weight",
            ]
        )
    df.columns = [str(column).strip() for column in df.columns]
    usage_source = find_col(df, ["usage_weight", "usage_count"])
    for target, candidates, default in [
        ("group_id", ["group_id"], 0),
        ("month", ["month", "tanggal", "period_date"], ""),
        ("estate", ["estate"], ""),
        ("afdeling", ["afdeling"], ""),
        ("kerani_panen", ["kerani_panen"], ""),
        ("mandor", ["mandor"], ""),
        ("pemanen", ["pemanen"], ""),
        ("usage_weight", ["usage_weight", "usage_count"], 0),
    ]:
        ensure_alias(df, target, candidates, default)
    df["group_id"] = pd.to_numeric(df["group_id"], errors="coerce").fillna(0).astype(int)
    if usage_source:
        df["usage_weight"] = pd.to_numeric(
            df[usage_source], errors="coerce"
        ).fillna(0)
    else:
        df["usage_weight"] = 0
    df["month"] = df["month"].apply(first_month)
    if DASHBOARD_REQUIRE_ESTATE:
        df = df[df["estate"].apply(is_valid)].copy()
    df = add_source_columns(df, source_table_kind)
    df = _filter_elhp_estate_scope(df, "estate")
    df["group_key"] = df["source_system"].astype(str) + "|" + df["group_id"].astype(str)
    return df


def _system_value(system: str | None) -> str:
    s = str(system or os.getenv("DEFAULT_SYSTEM", "HARMONY")).upper().strip()
    if s in {"PWP"}:
        s = "HARMONY"
    if s not in {"HARMONY", "ELHP", "ALL"}:
        s = "HARMONY"
    return s


def _company_values(company: Any) -> list[str]:
    if company is None or company == "":
        return []
    if isinstance(company, (list, tuple, set)):
        vals = [clean_text(x).upper() for x in company if clean_text(x)]
    else:
        vals = [clean_text(company).upper()] if clean_text(company) else []
    return [x for x in vals if x and x != "ALL"]


def _load_group_table(table: str, source_table_kind: str, company: Any = None) -> pd.DataFrame:
    comps = tuple(_company_values(company))
    cache_key = f"group:{source_table_kind}:{table}:{resolve_dashboard_relation(table)[1]}:{','.join(comps)}"
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        return cached
    if not table_exists(table):
        df = normalize_group(pd.DataFrame(), source_table_kind)
    else:
        df = normalize_group(read_table(table, source_companies=comps), source_table_kind)
    _norm_cache_set(cache_key, df)
    return df.copy()


def _load_detail_table(table: str, source_table_kind: str, company: Any = None) -> pd.DataFrame:
    comps = tuple(_company_values(company))
    cache_key = f"detail:{source_table_kind}:{table}:{resolve_dashboard_relation(table)[1]}:{','.join(comps)}"
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        return cached
    if not table_exists(table):
        df = normalize_detail(pd.DataFrame(), source_table_kind)
    else:
        df = normalize_detail(read_table(table, source_companies=comps), source_table_kind)
    _norm_cache_set(cache_key, df)
    return df.copy()


def _load_dashboard_monthly_table(table: str, source_table_kind: str, company: Any = None) -> pd.DataFrame:
    comps = tuple(_company_values(company))
    cache_key = f"dashboard_monthly:{source_table_kind}:{table}:{resolve_dashboard_relation(table)[1]}:{','.join(comps)}"
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        return cached
    if not table_exists(table):
        df = normalize_dashboard_monthly(pd.DataFrame(), source_table_kind)
    else:
        df = normalize_dashboard_monthly(read_table(table, source_companies=comps), source_table_kind)
    _norm_cache_set(cache_key, df)
    return df.copy()


def preload_all_data() -> None:
    """Preload only compact Gold summaries at app startup.

    Detail transaction can contain tens of thousands of rows per company.  It
    is intentionally loaded lazily only when the user opens Detail Group or
    Similarity.  Overview/filter callbacks therefore stay on the compact Gold
    summary path and never touch BYTEA images.
    """
    try:
        _load_group_table(TABLE_GROUP, "HARMONY")
        _load_dashboard_monthly_table(TABLE_DASHBOARD_MONTHLY, "HARMONY")
        _load_group_table(TABLE_ELHP_GROUP, "ELHP")
        _load_dashboard_monthly_table(TABLE_ELHP_DASHBOARD_MONTHLY, "ELHP")
    except Exception as exc:
        print(f"PRELOAD WARNING: {exc}")


def _filter_elhp_valid(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "source_company" not in df.columns:
        return df.iloc[0:0].copy()
    allowed = set(ELHP_COMPANY_VALUES)
    if INCLUDE_ELHP_UNKNOWN:
        allowed.add("UNKNOWN")
    return df[df["source_company"].astype(str).str.upper().isin(allowed)].copy()


def _filter_by_system_company(df: pd.DataFrame, system: str, company: Any) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    s = _system_value(system)
    comps = _company_values(company)
    out = df.copy()
    if s != "ALL" and "system" in out.columns:
        out = out[out["system"].astype(str).str.upper().eq(s)].copy()
    if comps and "source_company" in out.columns:
        out = out[out["source_company"].astype(str).str.upper().isin(comps)].copy()
    return out


def _joined_detail_values(values: pd.Series) -> str:
    unique: set[str] = set()
    for value in values.tolist():
        unique.update(clean_text(item) for item in split_values(value) if clean_text(item))
    return ", ".join(sorted(unique))


def reconcile_groups_with_visible_details(
    groups: pd.DataFrame,
    detail: pd.DataFrame,
) -> pd.DataFrame:
    """Bangun metadata dashboard hanya dari detail yang mempunyai estate.

    Gold tetap utuh untuk audit. Group dengan kurang dari dua usage valid tidak
    lagi dikategorikan sebagai reused group pada dashboard.
    """
    if not DASHBOARD_REQUIRE_ESTATE or groups is None or groups.empty:
        return groups
    if detail is None or detail.empty or "group_key" not in detail.columns:
        return groups

    records: list[dict[str, Any]] = []
    list_columns = {
        "estate_list": "estate",
        "afdeling_list": "afdeling",
        "blok_list": "blok",
        "tph_list": "tph",
        "oph_list": "oph",
        "sql_date_list": "tanggal",
        "pemanen_list": "pemanen",
        "kerani_panen_list": "kerani_panen",
        "mandor_list": "mandor",
        "kerani_ft_list": "kerani_ft",
        "kerani_dt_list": "kerani_dt",
        "status_transport_list": "transport_status",
        "similarity_list": "similarity",
    }
    for group_key, rows in detail.groupby(detail["group_key"].astype(str), sort=False):
        usage_count = int(len(rows))
        if usage_count < MIN_VISIBLE_GROUP_USAGE:
            continue
        representative_rows = rows[
            pd.to_numeric(rows.get("is_representative", 0), errors="coerce").fillna(0) > 0
        ]
        representative = representative_rows.iloc[0] if not representative_rows.empty else rows.iloc[0]
        match_sources = rows.get(
            "match_source",
            pd.Series([""] * usage_count, index=rows.index),
        ).apply(clean_text)
        item: dict[str, Any] = {
            "group_key": str(group_key),
            "usage_count": usage_count,
            "representative_image": clean_text(representative.get("image_name", "")),
            "matched_image_count": int(
                (match_sources.ne("") & match_sources.ne("NOT_MATCH_DB_CONTEXT")).sum()
            ),
            "total_bunch_pay_group": float(
                pd.to_numeric(rows.get("bunch_pay", 0), errors="coerce").fillna(0).sum()
            ),
        }
        for target, source in list_columns.items():
            item[target] = _joined_detail_values(rows[source]) if source in rows.columns else ""
        item["reuse_category"] = reuse_category_from_lists(
            item["afdeling_list"],
            item["blok_list"],
            item["tph_list"],
            item["estate_list"],
        )
        item["date_category"] = date_category_from_list(item["sql_date_list"])
        item["reuse_date_category"] = f"{item['reuse_category']} | {item['date_category']}"
        records.append(item)

    if not records:
        return groups.iloc[0:0].copy()

    visible = pd.DataFrame(records)
    replace_columns = [column for column in visible.columns if column != "group_key"]
    base = groups.drop(columns=[column for column in replace_columns if column in groups.columns])
    out = base.merge(visible, on="group_key", how="inner")
    out["image_key"] = out["representative_image"].apply(clean_text).str.upper()
    out["month"] = out["sql_date_list"].apply(first_month)
    out["estate_primary"] = out["estate_list"].apply(first_value)
    out["afdeling_primary"] = out["afdeling_list"].apply(first_value)
    out["blok_primary"] = out["blok_list"].apply(first_value)
    out["tph_primary"] = out["tph_list"].apply(first_value)
    out["kerani_primary"] = out["kerani_panen_list"].apply(first_value)
    out["mandor_primary"] = out["mandor_list"].apply(first_value)
    out["pemanen_primary"] = out["pemanen_list"].apply(first_value)
    out["similarity_primary"] = out["similarity_list"].apply(
        lambda value: first_value(value, "Tidak Ada Gambar")
    )
    return out


def load_groups(system: str | None = None, company: Any = None) -> pd.DataFrame:
    s = _system_value(system)
    frames = []
    comps = _company_values(company)
    include_harmony = s in {"HARMONY", "ALL"}
    include_elhp = s in {"ELHP", "ALL"}
    if include_harmony:
        frames.append(_load_group_table(TABLE_GROUP, "HARMONY", comps))
    if include_elhp:
        frames.append(_filter_elhp_valid(_load_group_table(TABLE_ELHP_GROUP, "ELHP", comps)))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = _filter_by_system_company(out, s, company)
    if include_harmony and not out.empty:
        out = _attach_group_kt_before_status(out)
    if DASHBOARD_REQUIRE_ESTATE:
        # Gold strict sudah mengagregasi hanya member ber-estate saat PUBLISH.
        # Jangan groupby seluruh detail lagi pada setiap callback dashboard.
        # Ini menjaga jalur Ringkasan benar-benar membaca summary Gold saja.
        estate_col = "estate_list" if "estate_list" in out.columns else "estate_primary"
        if estate_col in out.columns:
            out = out[out[estate_col].apply(is_valid)].copy()
    return out


def load_details(system: str | None = None, company: Any = None) -> pd.DataFrame:
    s = _system_value(system)
    frames = []
    comps = _company_values(company)
    include_harmony = s in {"HARMONY", "ALL"}
    include_elhp = s in {"ELHP", "ALL"}
    if include_harmony:
        frames.append(_load_detail_table(TABLE_DETAIL, "HARMONY", comps))
    if include_elhp:
        frames.append(_filter_elhp_valid(_load_detail_table(TABLE_ELHP_DETAIL, "ELHP", comps)))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = _filter_by_system_company(out, s, company)
    if DASHBOARD_REQUIRE_ESTATE and "estate" in out.columns:
        out = out[out["estate"].apply(is_valid)].copy()
    return _attach_kt_before_transport(out)


def load_group_details(
    system: str | None,
    company: Any,
    group_id: int,
) -> pd.DataFrame:
    """Load only one Gold group for the Detail page.

    The old callback loaded every detail row for a company before it could
    display a single selected group.  Gold already has an index on
    ``(source_group, is_active, group_id)``, so a targeted query is both faster
    and keeps the Overview/Company filter independent from the large detail
    table.
    """
    s = _system_value(system)
    comps = _company_values(company)
    frames: list[pd.DataFrame] = []
    table_specs: list[tuple[str, str]] = []
    if s in {"HARMONY", "ALL"}:
        table_specs.append((TABLE_DETAIL, "HARMONY"))
    if s in {"ELHP", "ALL"}:
        table_specs.append((TABLE_ELHP_DETAIL, "ELHP"))

    for table_name, table_kind in table_specs:
        if not table_exists(table_name):
            continue
        params: list[Any] = [int(group_id)]
        where = ["group_id=%s"]
        columns = set(get_columns(table_name))
        if comps and "source_group" in columns:
            where.append("upper(CAST(source_group AS TEXT))=ANY(%s)")
            params.append(comps)
        elif comps and "source_company" in columns:
            where.append("upper(CAST(source_company AS TEXT))=ANY(%s)")
            params.append(comps)
        raw = read_df(
            f"SELECT * FROM {resolve_dashboard_relation(table_name)[0]} WHERE {' AND '.join(where)} ",
            params,
            cache_key=f"detail-group:{table_name}:{resolve_dashboard_relation(table_name)[1]}:{group_id}:{','.join(comps)}",
        )
        if not raw.empty:
            frames.append(normalize_detail(raw, table_kind))

    if not frames:
        return normalize_detail(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = _filter_by_system_company(out, s, company)
    if DASHBOARD_REQUIRE_ESTATE and "estate" in out.columns:
        out = out[out["estate"].apply(is_valid)].copy()
    return _attach_kt_before_transport(out)


def load_group_bunch_pay_aggregate(
    system: str | None, company: Any, group_ids: list[int]
) -> pd.DataFrame:
    """Compact Bunch Pay fallback without SELECT * on every detail transaction.

    Called only if Gold group summary has missing/zero Bunch Pay on a page
    without detail. Uses the same *cleaned* Gold view as Detail Group, so ELHP
    ownership/SAP restrictions can never be bypassed.
    """
    s = _system_value(system)
    if s != "HARMONY" or not group_ids or not table_exists(TABLE_DETAIL):
        return pd.DataFrame(columns=["group_key", "bunch_pay"])
    cols = set(get_columns(TABLE_DETAIL))
    if not {"group_id", "bunch_pay", "source_company"}.issubset(cols):
        return pd.DataFrame(columns=["group_key", "bunch_pay"])
    from db import _table_filter_sql  # pure parameterized builder
    company_filter = tuple(_company_values(company))
    where, params = _table_filter_sql(cols, company_filter, True)
    clause = (where + " AND " if where else " WHERE ") + '"group_id"=ANY(%s)'
    numeric_ids = sorted({int(value) for value in group_ids})
    params.append(numeric_ids)
    sql = (
        'SELECT "group_id", coalesce(sum("bunch_pay"),0) AS bunch_pay '
        f'FROM {resolve_dashboard_relation(TABLE_DETAIL)[0]}{clause} GROUP BY "group_id"'
    )
    group_signature = hashlib.sha1(",".join(map(str, numeric_ids)).encode("ascii")).hexdigest()[:16]
    key = f"bunch-pay:{TABLE_DETAIL}:{resolve_dashboard_relation(TABLE_DETAIL)[1]}:{','.join(company_filter)}:{group_signature}"
    rows = read_df(sql, params, cache_key=key)
    if rows.empty:
        return pd.DataFrame(columns=["group_key", "bunch_pay"])
    rows["group_key"] = "HARMONY|" + rows["group_id"].astype(str)
    return rows[["group_key", "bunch_pay"]]


def load_dashboard_monthly(
    system: str | None = None,
    company: Any = None,
) -> pd.DataFrame:
    s = _system_value(system)
    frames = []
    comps = _company_values(company)
    if s in {"HARMONY", "ALL"}:
        frames.append(
            _load_dashboard_monthly_table(TABLE_DASHBOARD_MONTHLY, "HARMONY", comps)
        )
    if s in {"ELHP", "ALL"}:
        frames.append(
            _filter_elhp_valid(
                _load_dashboard_monthly_table(
                    TABLE_ELHP_DASHBOARD_MONTHLY,
                    "ELHP",
                    comps,
                )
            )
        )
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    return _filter_by_system_company(out, s, company)


def get_company_options(system: str | None = None) -> list[dict]:
    """Static company catalog; never scan expensive Gold views for a dropdown.

    Users can opt in a new canonical company with COMPANY_ORDER and
    HARMONY_COMPANIES/ELHP_COMPANIES. Presence of Gold data is validated by
    the actual page query, not by this option list.
    """
    s = _system_value(system)
    harmony = {
        value.strip().upper()
        for value in os.getenv("HARMONY_COMPANIES", "PWP,DIN").split(",")
        if value.strip()
    }
    elhp = {
        value.strip().upper()
        for value in os.getenv("ELHP_COMPANIES", "DAN,DWT").split(",")
        if value.strip()
    }
    allowed = (harmony if s == "HARMONY" else elhp if s == "ELHP" else harmony | elhp)
    vals = [value for value in COMPANY_ORDER if value in allowed]
    vals += sorted(allowed.difference(vals))
    return [{"label": value, "value": value} for value in vals]


def _as_list(x):
    if x is None or x == "":
        return []
    if isinstance(x, (list, tuple, set)):
        return [str(v) for v in x if clean_text(v)]
    return [str(x)]


def contains_any(series: pd.Series, values: list[str]) -> pd.Series:
    if not values:
        return pd.Series([True] * len(series), index=series.index)
    pattern = "|".join(re.escape(v) for v in values)
    return series.fillna("").astype(str).str.contains(pattern, case=False, na=False, regex=True)


def _opt_values(df: pd.DataFrame, col: str, splitter: bool = False, limit: int | None = None, exclude: set[str] | None = None) -> list[dict]:
    vals: list[str] = []
    if df is None or df.empty or col not in df.columns:
        return []
    for x in df[col].dropna().tolist():
        if splitter:
            vals.extend(split_values(x))
        else:
            v = clean_text(x)
            if v:
                vals.append(v)
    exclude = exclude or set()
    uniq = sorted({v for v in vals if clean_text(v) and v not in exclude})
    if limit and limit > 0:
        uniq = uniq[:limit]
    return [{"label": v, "value": v} for v in uniq]


def _filter_for_options(df: pd.DataFrame, *, months=None, estate=None, afdeling=None, block=None, reuse_category=None, date_category=None, transport_status=None, similarity=None, search=None) -> pd.DataFrame:
    """Apply only provided filters for cascading option lists."""
    filters = {
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
    return apply_filters(df, filters)


def get_filter_options(system: str | None = None, company: Any = None, current_filters: dict[str, Any] | None = None) -> dict[str, list[dict]]:
    """Build strong cascading filter options.

    Cascade order:
      System -> Company -> Estate -> Afdeling -> Blok -> Bulan -> Categories.

    This prevents BP/PWP choices appearing under ELHP and makes lower filters
    follow the locked upper-level selection.
    """
    current_filters = current_filters or {}
    base = load_groups(system, company)

    # Primary hierarchy: company is already applied by load_groups().
    estate_df = base
    afdeling_df = _filter_for_options(base, estate=current_filters.get("estate"))
    block_df = _filter_for_options(base, estate=current_filters.get("estate"), afdeling=current_filters.get("afdeling"))
    month_df = _filter_for_options(base, estate=current_filters.get("estate"), afdeling=current_filters.get("afdeling"), block=current_filters.get("block"))

    # Month options must come from the exact Gold monthly aggregate. A group
    # summary stores a list of dates and its derived `month` is only the first
    # month; using it here hid later/earlier months for cross-month groups.
    monthly_exact = load_dashboard_monthly(system, company)
    if not monthly_exact.empty and "group_key" in monthly_exact.columns:
        allowed_keys = set(month_df.get("group_key", pd.Series(dtype=str)).astype(str))
        monthly_exact = monthly_exact[
            monthly_exact["group_key"].astype(str).isin(allowed_keys)
        ].copy()
        estate_values = _as_list(current_filters.get("estate"))
        afdeling_values = _as_list(current_filters.get("afdeling"))
        if estate_values and "estate" in monthly_exact.columns:
            monthly_exact = monthly_exact[
                monthly_exact["estate"].astype(str).isin(estate_values)
            ].copy()
        if afdeling_values and "afdeling" in monthly_exact.columns:
            monthly_exact = monthly_exact[
                monthly_exact["afdeling"].astype(str).isin(afdeling_values)
            ].copy()

    # Analytical filters follow primary location/month filters.
    analytic_base = _filter_for_options(
        base,
        estate=current_filters.get("estate"),
        afdeling=current_filters.get("afdeling"),
        block=current_filters.get("block"),
    )
    selected_months = set(_as_list(current_filters.get("months")))
    if selected_months and not monthly_exact.empty and "group_key" in monthly_exact.columns:
        month_keys = set(
            monthly_exact[
                monthly_exact["month"].astype(str).isin(selected_months)
            ]["group_key"].astype(str)
        )
        analytic_base = analytic_base[
            analytic_base["group_key"].astype(str).isin(month_keys)
        ].copy()
    elif selected_months and "month" in analytic_base.columns:
        analytic_base = analytic_base[
            analytic_base["month"].astype(str).isin(selected_months)
        ].copy()

    s = _system_value(system)
    comps = _company_values(company)
    elhp_only = s == "ELHP" or (comps and all(c in ELHP_COMPANY_VALUES for c in comps))
    transport_opts = [] if elhp_only else _opt_values(analytic_base, "status_transport_list", True, 200, exclude={TRANSPORT_NA_ELHP})
    if elhp_only:
        transport_opts = [{"label": TRANSPORT_NA_ELHP, "value": TRANSPORT_NA_ELHP, "disabled": True}]

    return {
        "systems": SYSTEM_OPTIONS,
        "companies": get_company_options(system),
        "estates": _opt_values(estate_df, "estate_list", True, FILTER_OPTION_LIMIT),
        "afdelings": _opt_values(afdeling_df, "afdeling_list", True, FILTER_OPTION_LIMIT),
        "blocks": _opt_values(block_df, "blok_list", True, FILTER_OPTION_LIMIT),
        "months": _opt_values(
            monthly_exact if not monthly_exact.empty else month_df,
            "month",
            False,
            300,
        ),
        "reuse_categories": _opt_values(analytic_base, "reuse_category", False, 100),
        "date_categories": _opt_values(analytic_base, "date_category", False, 100),
        "transport_statuses": transport_opts,
        "similarities": _opt_values(analytic_base, "similarity_primary", False, 200),
    }


def apply_filters(groups: pd.DataFrame, filters: dict[str, Any]) -> pd.DataFrame:
    df = groups.copy()
    if df.empty:
        return df
    months = _as_list(filters.get("months"))
    if months:
        df = df[df["month"].astype(str).isin(months)]
    for fkey, col, splitter in [
        ("estate", "estate_list", True),
        ("afdeling", "afdeling_list", True),
        ("block", "blok_list", True),
        ("reuse_category", "reuse_category", False),
        ("date_category", "date_category", False),
        ("transport_status", "status_transport_list", True),
        ("similarity", "similarity_primary", False),
    ]:
        vals = _as_list(filters.get(fkey))
        if vals and col in df.columns:
            if fkey == "transport_status" and TRANSPORT_NA_ELHP in vals:
                continue
            if splitter:
                df = df[contains_any(df[col], vals)]
            else:
                df = df[df[col].astype(str).isin(vals)]
    search = clean_text(filters.get("search"))
    if search:
        s_cols = ["representative_image", "oph_list", "pemanen_list", "kerani_panen_list", "mandor_list", "blok_list", "tph_list", "source_label", "source_company"]
        mask = pd.Series([False] * len(df), index=df.index)
        for c in s_cols:
            if c in df.columns:
                mask |= df[c].fillna("").astype(str).str.contains(re.escape(search), case=False, na=False)
        df = df[mask]
    return df


def detail_for_groups(detail: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    if detail.empty or groups.empty:
        return detail.iloc[0:0].copy()
    gids = set(groups["group_key"].dropna().astype(str).tolist()) if "group_key" in groups.columns else set(groups["group_id"].dropna().astype(str).tolist())
    key_col = "group_key" if "group_key" in detail.columns else "group_id"
    return detail[detail[key_col].astype(str).isin(gids)].copy()


def kpis(groups: pd.DataFrame, detail: pd.DataFrame) -> dict[str, Any]:
    transport_missing = 0
    if not detail.empty and "transport_status" in detail.columns:
        td = detail[detail.get("system", pd.Series(dtype=str)).astype(str).str.upper().eq("HARMONY")].copy() if "system" in detail.columns else detail.copy()
        if not td.empty:
            transport_missing = int(td["transport_status"].fillna("").astype(str).str.contains("Tidak Ada", case=False, na=False).sum())
    return {
        "total_group": int(len(groups)),
        "pwp_group": int((groups.get("source_company", pd.Series(dtype=str)).astype(str).str.upper() == "PWP").sum()) if not groups.empty else 0,
        "dan_group": int((groups.get("source_company", pd.Series(dtype=str)).astype(str).str.upper() == "DAN").sum()) if not groups.empty else 0,
        "dwt_group": int((groups.get("source_company", pd.Series(dtype=str)).astype(str).str.upper() == "DWT").sum()) if not groups.empty else 0,
        "total_usage": int(pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0).sum()) if not groups.empty else 0,
        "max_usage": int(pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0).max()) if not groups.empty else 0,
        "avg_usage": float(pd.to_numeric(groups.get("usage_count", 0), errors="coerce").fillna(0).mean()) if not groups.empty else 0,
        "detail_rows": int(len(detail)),
        "unique_oph": int(detail["oph"].nunique()) if "oph" in detail.columns and not detail.empty else 0,
        "total_bunch_pay": float(pd.to_numeric(detail.get("bunch_pay", 0), errors="coerce").fillna(0).sum()) if not detail.empty else 0,
        "unique_images": int(groups["representative_image"].nunique()) if not groups.empty else 0,
        "transport_missing_harmony_only": transport_missing,
    }


def top_reused(groups: pd.DataFrame, top_n: int = TOP_N) -> pd.DataFrame:
    cols = ["source_label", "source_company", "system", "source_system", "group_key", "group_id", "representative_image", "image_key", "usage_count", "month", "reuse_category", "date_category", "estate_primary", "afdeling_primary", "blok_primary", "tph_primary"]
    if groups is None or groups.empty or "usage_count" not in groups.columns:
        return pd.DataFrame(columns=cols)
    out = groups.sort_values("usage_count", ascending=False)[[c for c in cols if c in groups.columns]].copy()
    return out.head(top_n) if top_n else out


def agg_count(groups: pd.DataFrame, col: str, value_col: str = "usage_count") -> pd.DataFrame:
    if groups.empty or col not in groups.columns:
        return pd.DataFrame(columns=[col, "total_usage", "total_group"])
    return (
        groups.groupby(col, dropna=False)
        .agg(total_usage=(value_col, "sum"), total_group=("group_key", "count"))
        .reset_index()
        .sort_values("total_usage", ascending=False)
    )


def usage_distribution(groups: pd.DataFrame) -> pd.DataFrame:
    if groups.empty:
        return pd.DataFrame(columns=["bucket", "total_group"])
    bins = [0, 2, 3, 5, 10, 20, 50, 10**9]
    labels = ["2", "3", "4-5", "6-10", "11-20", "21-50", ">50"]
    tmp = groups.copy()
    tmp["bucket"] = pd.cut(tmp["usage_count"], bins=bins, labels=labels, include_lowest=True, right=True)
    return tmp.groupby("bucket", observed=False).size().reset_index(name="total_group")


def source_breakdown(groups: pd.DataFrame) -> pd.DataFrame:
    if groups is None or groups.empty:
        return pd.DataFrame(columns=["source_company", "source_label", "total_group", "total_usage", "max_usage"])
    tmp = groups.copy()
    if "source_company" not in tmp.columns:
        tmp["source_company"] = "UNKNOWN"
    if "source_label" not in tmp.columns:
        tmp["source_label"] = tmp["source_company"]
    return (
        tmp.groupby(["source_company", "source_label"], dropna=False)
        .agg(total_group=("group_key", "count"), total_usage=("usage_count", "sum"), max_usage=("usage_count", "max"))
        .reset_index()
        .sort_values("total_usage", ascending=False)
    )


def selected_detail(group_key: str | None, system: str | None = None, company: Any = None) -> tuple[pd.Series | None, pd.DataFrame]:
    g = load_groups(system, company)
    d = load_details(system, company)
    if not group_key:
        if g.empty:
            return None, d.iloc[0:0]
        group_key = str(g.sort_values("usage_count", ascending=False).iloc[0]["group_key"])
    rowdf = g[g["group_key"].astype(str) == str(group_key)]
    if rowdf.empty:
        return None, d.iloc[0:0]
    return rowdf.iloc[0], d[d["group_key"].astype(str) == str(group_key)].copy()


def load_transport_status_summary(system: str | None = None, company: Any = None) -> pd.DataFrame:
    s = _system_value(system)
    comps = _company_values(company)
    elhp_only = s == "ELHP" or (comps and all(c in ELHP_COMPANY_VALUES for c in comps))
    if elhp_only:
        return pd.DataFrame([{"system": "ELHP", "company": ",".join(comps) if comps else "DAN,DWT", "status_transport": TRANSPORT_NA_ELHP, "total": 0}])
    frames = []
    include_harmony = s in {"HARMONY", "ALL"}
    if include_harmony and table_exists(TABLE_TRANSPORT_STATUS):
        x = read_table(TABLE_TRANSPORT_STATUS)
        if not x.empty:
            x["system"] = "HARMONY"
            company_col = find_col(x, ["company", "source_company", "company_code", "source_group"])
            if company_col:
                x["company"] = x[company_col].apply(lambda value: clean_text(value).upper())
            else:
                x["company"] = "PWP"
            if comps:
                x = x[x["company"].isin(comps)].copy()
            frames.append(x)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def load_audit_summary(system: str | None = None, company: Any = None) -> dict[str, Any]:
    s = _system_value(system)
    comps = _company_values(company)
    candidates = []
    if s in {"HARMONY", "ALL"}:
        candidates += [
            ("harmony_download_report_vs_sap", TABLE_AUDIT_DOWNLOAD_REPORT),
            ("harmony_no_image_final", TABLE_AUDIT_NO_IMAGE),
            ("harmony_oph_validation_excel", TABLE_OPH_VALIDATION_EXCEL),
            ("harmony_false_alarm", TABLE_FALSE_ALARM),
        ]
    if s in {"ELHP", "ALL"} and (not comps or bool(set(comps) & ELHP_COMPANY_VALUES)):
        candidates += [
            ("elhp_download_report_vs_sap", TABLE_ELHP_AUDIT_DOWNLOAD_REPORT),
            ("elhp_oph_validation", TABLE_ELHP_OPH_VALIDATION),
        ]
    out = {}
    for name, table in candidates:
        if table_exists(table):
            try:
                table_cols = set(get_columns(table))
                source_col = next((c for c in ["source_company", "company_code", "company", "source_group"] if c in table_cols), None)
                if comps and source_col:
                    df = read_df(
                        f"SELECT COUNT(*)::bigint AS n FROM {qname(table)} WHERE upper(CAST({ident(source_col)} AS TEXT)) = ANY(%s)",
                        [comps],
                        cache_key=f"count:{table}:{','.join(comps)}",
                    )
                else:
                    df = read_df(f"SELECT COUNT(*)::bigint AS n FROM {qname(table)}", cache_key=f"count:{table}")
                out[name] = int(df.iloc[0]["n"])
            except Exception:
                out[name] = 0
        else:
            out[name] = 0
    return out


def _normalize_oph_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean_text(value).upper())


def _as_bool_series(values: pd.Series) -> pd.Series:
    if values.dtype == bool:
        return values.fillna(False)
    return values.astype(str).str.strip().str.lower().isin(
        {"1", "t", "true", "y", "yes"}
    )


def _load_sap_audit_rows() -> pd.DataFrame:
    """Cache the compact active OPH audit; this never reads the large SAP table."""
    cache_key = f"sap-audit:{TABLE_AUDIT_DOWNLOAD_REPORT}"
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        return cached
    if not table_exists(TABLE_AUDIT_DOWNLOAD_REPORT):
        empty = pd.DataFrame(
            columns=["source_group", "oph", "oph_key", "in_sap", "status"]
        )
        _norm_cache_set(cache_key, empty)
        return empty
    columns = set(get_columns(TABLE_AUDIT_DOWNLOAD_REPORT))
    wanted = [
        column
        for column in ("source_group", "oph", "oph_key", "in_sap", "status")
        if column in columns
    ]
    if not {"source_group", "in_sap"}.issubset(columns):
        return pd.DataFrame(
            columns=["source_group", "oph", "oph_key", "in_sap", "status"]
        )
    raw = read_df(
        f"SELECT {', '.join(ident(column) for column in wanted)} "
        f"FROM {qname(TABLE_AUDIT_DOWNLOAD_REPORT)}",
        cache_key=f"sap-audit-rows:{TABLE_AUDIT_DOWNLOAD_REPORT}",
    )
    if "oph_key" not in raw.columns:
        raw["oph_key"] = raw.get("oph", "").apply(_normalize_oph_key)
    else:
        raw["oph_key"] = raw["oph_key"].apply(_normalize_oph_key)
    raw["source_group"] = raw["source_group"].apply(clean_text).str.upper()
    raw["in_sap"] = _as_bool_series(raw["in_sap"])
    if "status" not in raw.columns:
        raw["status"] = ""
    _norm_cache_set(cache_key, raw)
    return raw.copy()


def _normalize_sap_kpi_summary(raw: pd.DataFrame | None) -> pd.DataFrame:
    """Kembalikan snapshot KPI dengan schema stabil, termasuk dari cache lama.

    ``read_table`` dan cache dashboard dapat berasal dari proses yang dimulai
    sebelum migration SAP terbaru diterapkan. Kolom yang belum ada berarti KPI
    belum tersedia, bukan alasan untuk menggagalkan seluruh render dashboard.
    """
    if raw is None:
        raw = pd.DataFrame()
    else:
        raw = raw.copy()

    row_count = len(raw.index)
    for column in ("source_group", "source_system", "source_company"):
        if column not in raw.columns:
            raw[column] = pd.Series([""] * row_count, index=raw.index)
        raw[column] = raw[column].apply(clean_text).str.upper()

    if "period_year" not in raw.columns:
        raw["period_year"] = pd.Series(
            [np.nan] * row_count, index=raw.index, dtype="float64"
        )
    if "sap_evaluated" not in raw.columns:
        raw["sap_evaluated"] = pd.Series(
            [False] * row_count, index=raw.index, dtype="bool"
        )
    else:
        raw["sap_evaluated"] = _as_bool_series(raw["sap_evaluated"])
    if "is_active" not in raw.columns:
        raw["is_active"] = pd.Series(
            [True] * row_count, index=raw.index, dtype="bool"
        )
    else:
        raw["is_active"] = _as_bool_series(raw["is_active"])
    if "total_sap_oph" not in raw.columns:
        raw["total_sap_oph"] = pd.Series(
            [np.nan] * row_count, index=raw.index, dtype="float64"
        )
    else:
        raw["total_sap_oph"] = pd.to_numeric(
            raw["total_sap_oph"], errors="coerce"
        )
    if "monthly_sap_oph" not in raw.columns:
        raw["monthly_sap_oph"] = pd.Series(
            [{} for _ in range(row_count)], index=raw.index, dtype="object"
        )
    if "estate_sap_oph" not in raw.columns:
        raw["estate_sap_oph"] = pd.Series(
            [{} for _ in range(row_count)], index=raw.index, dtype="object"
        )
    if "estate_monthly_sap_oph" not in raw.columns:
        raw["estate_monthly_sap_oph"] = pd.Series(
            [{} for _ in range(row_count)], index=raw.index, dtype="object"
        )
    if "prfnr_scope" not in raw.columns:
        raw["prfnr_scope"] = pd.Series(
            [[] for _ in range(row_count)], index=raw.index, dtype="object"
        )
    return raw


def _load_sap_kpi_summary_rows() -> pd.DataFrame:
    cache_key = f"sap-kpi-summary:{TABLE_SAP_KPI_SUMMARY}"
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        # Cache bisa masih berbentuk schema lama ketika kode/migration diganti
        # saat proses dashboard belum direstart.
        return _normalize_sap_kpi_summary(cached)
    if not table_exists(TABLE_SAP_KPI_SUMMARY):
        empty = _normalize_sap_kpi_summary(pd.DataFrame())
        _norm_cache_set(cache_key, empty)
        return empty
    raw = read_table(TABLE_SAP_KPI_SUMMARY)
    raw = _normalize_sap_kpi_summary(raw)
    _norm_cache_set(cache_key, raw)
    return raw.copy()


def load_sap_kpis(
    system: str | None = None,
    company: Any = None,
    visible_groups: pd.DataFrame | None = None,
    months: Any = None,
    estates: Any = None,
) -> dict[str, Any]:
    """Return SAP denominator following company + explicit estate + month filters.

    The dashboard never reads the large SAP table in callbacks. v103 stores
    company/estate/month aggregates in ``gold.sap_oph_kpi_summary``. If an
    Estate filter is explicitly selected, a legacy snapshot without the new
    estate aggregate is rejected as N/A rather than silently using the whole
    company denominator.
    """
    result: dict[str, Any] = {
        "total_sap_oph": None,
        "period_year": None,
        "prfnr_scope": [],
        "scope_ready": False,
    }

    def as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
        return value if isinstance(value, dict) else {}

    s = _system_value(system)
    comps = set(_company_values(company))
    selected_months = {
        clean_text(value)
        for value in (
            months if isinstance(months, (list, tuple, set)) else [months]
        )
        if clean_text(value)
    }
    selected_estates = {
        clean_text(value).upper()
        for value in (
            estates if isinstance(estates, (list, tuple, set)) else [estates]
        )
        if clean_text(value)
    }
    if visible_groups is not None and not visible_groups.empty:
        source_col = next(
            (
                column
                for column in ("source_group", "source_company")
                if column in visible_groups.columns
            ),
            None,
        )
        if source_col:
            visible_sources = {
                clean_text(value).upper()
                for value in visible_groups[source_col].tolist()
                if clean_text(value)
            }
            if visible_sources:
                comps = visible_sources

    summary = _load_sap_kpi_summary_rows()
    if summary.empty:
        return result
    summary = summary[summary["is_active"]].copy()
    if comps:
        summary = summary[summary["source_group"].isin(comps)].copy()
    elif s == "ELHP":
        summary = summary[summary["source_group"].isin(ELHP_COMPANY_VALUES)].copy()
    elif s == "HARMONY":
        summary = summary[~summary["source_group"].isin(ELHP_COMPANY_VALUES)].copy()
    evaluated_summary = summary[summary["sap_evaluated"]].copy()
    if "prfnr_scope" in evaluated_summary.columns:
        evaluated_summary = evaluated_summary[
            ~evaluated_summary["prfnr_scope"].apply(lambda value: "*" in str(value))
        ].copy()

    selected_total = 0
    snapshot_ready = False
    if selected_estates:
        # Explicit estate filter must use the estate-level snapshot. Never
        # substitute the whole-company total, because that recreates the bug.
        for row in evaluated_summary.itertuples(index=False):
            estate_totals = {
                clean_text(key).upper(): int(value or 0)
                for key, value in as_dict(getattr(row, "estate_sap_oph", {})).items()
                if clean_text(key)
            }
            estate_monthly_raw = as_dict(
                getattr(row, "estate_monthly_sap_oph", {})
            )
            estate_monthly = {
                clean_text(key).upper(): as_dict(value)
                for key, value in estate_monthly_raw.items()
                if clean_text(key)
            }
            if selected_months:
                if not estate_monthly:
                    continue
                snapshot_ready = True
                for estate in selected_estates:
                    month_map = estate_monthly.get(estate, {})
                    selected_total += sum(
                        int(month_map.get(month, 0) or 0)
                        for month in selected_months
                    )
            else:
                if not estate_totals:
                    continue
                snapshot_ready = True
                selected_total += sum(
                    int(estate_totals.get(estate, 0) or 0)
                    for estate in selected_estates
                )
    elif selected_months:
        for value in evaluated_summary.get(
            "monthly_sap_oph", pd.Series(dtype=object)
        ).tolist():
            monthly = as_dict(value)
            if not monthly:
                continue
            snapshot_ready = True
            selected_total += sum(
                int(monthly.get(month, 0) or 0)
                for month in selected_months
            )
    else:
        total_values = evaluated_summary.get(
            "total_sap_oph", pd.Series(dtype="float64")
        ).dropna()
        if not total_values.empty:
            selected_total = int(total_values.sum())
            snapshot_ready = True
        else:
            for value in evaluated_summary.get(
                "monthly_sap_oph", pd.Series(dtype=object)
            ).tolist():
                monthly = as_dict(value)
                if not monthly:
                    continue
                snapshot_ready = True
                selected_total += sum(int(item or 0) for item in monthly.values())

    if snapshot_ready:
        result["total_sap_oph"] = selected_total
        result["scope_ready"] = True

    if "prfnr_scope" in evaluated_summary.columns:
        scopes: set[str] = set()
        for value in evaluated_summary["prfnr_scope"].tolist():
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    value = parsed if isinstance(parsed, list) else [value]
                except (TypeError, ValueError):
                    value = [value]
            if isinstance(value, (list, tuple, set)):
                scopes.update(clean_text(item) for item in value if clean_text(item))
            elif clean_text(value):
                scopes.add(clean_text(value))
        result["prfnr_scope"] = sorted(scopes)
    if "period_year" in evaluated_summary.columns and not evaluated_summary.empty:
        years = sorted(
            {
                int(value)
                for value in pd.to_numeric(
                    evaluated_summary["period_year"], errors="coerce"
                ).dropna()
            }
        )
        result["period_year"] = ", ".join(map(str, years)) if years else None
    return result


def load_harmony_oph_coverage(
    companies: Any = None,
    period_year: int = 2026,
    months: Any = None,
) -> dict[str, int | bool | None]:
    """Reconcile scoped SAP OPH with Panen context and photo evidence.

    ``sap_oph_kpi_summary`` is intentionally a compact count snapshot, so it
    cannot explain a gap such as ``484,012`` SAP OPH versus ``373,313`` OPH
    with Panen.  This read-only query uses the same PRFNR scope as the active
    snapshot, normalises OPH on both sides, and returns counts only.  A photo
    is considered present when it is indexed on NAS, in the canonical hot
    store, or has a valid Panen embedding; a valid embedding is tracked
    separately so a missing vector is not mislabeled as a missing photo.
    """
    requested = (
        [companies]
        if isinstance(companies, str)
        else list(companies)
        if isinstance(companies, (list, tuple, set))
        else ["PWP", "DIN"]
    )
    requested = sorted({
        clean_text(value).upper()
        for value in requested
        if clean_text(value).upper() in {"PWP", "DIN"}
    }) or ["PWP", "DIN"]
    selected_months = sorted({
        clean_text(value)
        for value in (
            months if isinstance(months, (list, tuple, set)) else [months]
        )
        if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", clean_text(value))
    })
    cache_key = (
        "harmony-oph-coverage:din-march-v2-bukrs:" + ",".join(requested)
        + f":{int(period_year)}:" + ",".join(selected_months)
    )
    cached = _norm_cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            return {
                str(key): (
                    int(value) if key not in {"source_ready"} and value is not None
                    else bool(value) if key == "source_ready" else value
                )
                for key, value in cached.iloc[0].to_dict().items()
            }

    empty: dict[str, int | bool | None] = {
        "sap_total": None,
        "sap_with_context": None,
        "sap_with_photo": None,
        "sap_with_embedding": None,
        "sap_missing_context": None,
        "sap_context_without_photo": None,
        "sap_photo_without_embedding": None,
        "source_ready": False,
    }
    if not table_exists(SAP_TABLE, SAP_SCHEMA):
        return empty

    summary = _load_sap_kpi_summary_rows()
    if summary.empty:
        return empty
    summary = summary[
        summary["is_active"]
        & summary["sap_evaluated"]
        & summary["source_group"].isin(requested)
        & pd.to_numeric(summary["period_year"], errors="coerce").eq(int(period_year))
    ].copy()
    if summary.empty:
        return empty

    scope_rows: list[tuple[str, str]] = []
    for row in summary.itertuples(index=False):
        value = getattr(row, "prfnr_scope", [])
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = [value]
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        source = clean_text(getattr(row, "source_group", "")).upper()
        for prfnr in value:
            clean_prfnr = clean_text(prfnr).upper()
            if source in requested and clean_prfnr and clean_prfnr != "*":
                scope_rows.append((source, clean_prfnr))
    scope_rows = sorted(set(scope_rows))
    if not scope_rows:
        return empty

    sap_columns = {str(column).lower(): str(column) for column in get_columns(SAP_TABLE, SAP_SCHEMA)}
    oph_col = next((sap_columns[name] for name in ("oph", "harvest_ticket", "tiket_panen", "no_bcc") if name in sap_columns), None)
    prfnr_col = next((sap_columns[name] for name in ("prfnr", "estate", "estate_name") if name in sap_columns), None)
    company_col = next(
        (
            sap_columns[name]
            for name in ("company_code", "company_code_sap", "company", "bukrs", "companycode", "co_code")
            if name in sap_columns
        ),
        None,
    )
    date_col = next((sap_columns[name] for name in ("budat", "posting_date", "period_date") if name in sap_columns), None)
    if not oph_col or not prfnr_col or not date_col:
        return empty

    scope_rows_with_company = [(source, SAP_COMPANY_CODE_MAP.get(source, source), prfnr) for source, prfnr in scope_rows]
    value_sql = ",".join(["(%s,%s,%s)"] * len(scope_rows_with_company))
    params: list[Any] = [item for row in scope_rows_with_company for item in row]
    date_digits = (
        f"regexp_replace(btrim(s.{ident(date_col)}::text), "
        "'[^0-9]', '', 'g')"
    )
    month_expr = (
        f"CASE WHEN length({date_digits}) >= 6 THEN "
        f"substring({date_digits} from 1 for 4) || '-' || "
        f"substring({date_digits} from 5 for 2) END"
    )
    sap_filter = [
        f"s.{ident(oph_col)} IS NOT NULL",
        f"btrim(s.{ident(oph_col)}::text) <> ''",
        f"left({date_digits}, 4)=%s",
    ]
    if int(period_year) == 2026:
        sap_filter.append(
            f"NOT (upper(cs.source_group)='DIN' AND {month_expr} < '{DIN_TRANSPORT_START_MONTH}')"
        )
    params.append(str(int(period_year)))
    if selected_months:
        params.append(selected_months)
    nas_join = ""
    nas_photo = "false"
    if table_exists(TABLE_PANEN_NAS_INDEX, RECOVERY_SCHEMA):
        nas_join = f"""
        LEFT JOIN {qname(TABLE_PANEN_NAS_INDEX, RECOVERY_SCHEMA)} n
          ON n.source_group=upper(c.source_group)
         AND n.period_year=%s
         AND n.is_active=true
         AND nullif(trim(n.nas_path), '') IS NOT NULL
         AND n.file_name_upper=upper(btrim(f.file_name))
        """
        nas_photo = "n.file_name_upper IS NOT NULL"
    hot_join = ""
    hot_photo = "false"
    if table_exists(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA):
        hot_join = f"""
        LEFT JOIN {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)} g
          ON upper(g.source_group)=upper(c.source_group)
         AND g.file_uid=c.file_uid
         AND g.is_active=true
        """
        hot_photo = "g.file_uid IS NOT NULL"

    params.append(DASHBOARD_MODEL_VERSION)
    if nas_join:
        params.append(int(period_year))
    params.extend([requested, int(period_year)])
    if selected_months:
        month_filter = "WHERE sap_month=ANY(%s)"
    else:
        month_filter = ""
    company_join = (
        f" AND upper(btrim(s.{ident(company_col)}::text))=cs.source_company"
        if company_col
        else ""
    )
    sql = f"""
        WITH company_scope(source_group, source_company, prfnr) AS (
            VALUES {value_sql}
        ), sap_raw AS (
            SELECT upper(cs.source_group) AS source_group,
                   regexp_replace(upper(btrim(s.{ident(oph_col)}::text)),
                                  '[^A-Z0-9]', '', 'g') AS oph_key,
                   {month_expr} AS sap_month
            FROM {qname(SAP_TABLE, SAP_SCHEMA)} s
            JOIN company_scope cs
              ON upper(btrim(s.{ident(prfnr_col)}::text))=cs.prfnr
             {company_join}
            WHERE {' AND '.join(sap_filter)}
        ), sap_keys AS (
            SELECT source_group, oph_key, max(sap_month) AS sap_month
            FROM sap_raw
            WHERE nullif(oph_key, '') IS NOT NULL
            {month_filter}
            GROUP BY source_group, oph_key
        ), context_keys AS (
            SELECT upper(c.source_group) AS source_group,
                   regexp_replace(upper(btrim(c.oph_key)),
                                  '[^A-Z0-9]', '', 'g') AS oph_key,
                   bool_or(
                       {nas_photo}
                       OR {hot_photo}
                       OR (
                           f.file_uid IS NOT NULL
                           AND f.is_transport=false
                           AND f.is_valid=true
                           AND f.full_embedding IS NOT NULL
                           AND coalesce(f.embedding_dim, 0) > 0
                       )
                   ) AS has_photo,
                   bool_or(
                       f.file_uid IS NOT NULL
                       AND f.is_transport=false
                       AND f.is_valid=true
                       AND f.full_embedding IS NOT NULL
                       AND coalesce(f.embedding_dim, 0) > 0
                   ) AS has_embedding
            FROM {qname("image_context", RECOVERY_SCHEMA)} c
            LEFT JOIN {qname("image_feature", RECOVERY_SCHEMA)} f
              ON f.file_uid=c.file_uid
             AND f.model_version=%s
            {nas_join}
            {hot_join}
            WHERE upper(c.source_group)=ANY(%s)
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=%s
              AND (
                    extract(year FROM c.period_date)::integer <> 2026
                    OR upper(c.source_group) <> 'DIN'
                    OR to_char(c.period_date, 'YYYY-MM') >= '{DIN_TRANSPORT_START_MONTH}'
                  )
              AND nullif(trim(coalesce(c.oph_key, '')), '') IS NOT NULL
            GROUP BY upper(c.source_group),
                     regexp_replace(upper(btrim(c.oph_key)),
                                    '[^A-Z0-9]', '', 'g')
        )
        SELECT count(*)::bigint AS sap_total,
               count(*) FILTER (WHERE c.oph_key IS NOT NULL)::bigint
                   AS sap_with_context,
               count(*) FILTER (WHERE coalesce(c.has_photo, false))::bigint
                   AS sap_with_photo,
               count(*) FILTER (WHERE coalesce(c.has_embedding, false))::bigint
                   AS sap_with_embedding,
               count(*) FILTER (WHERE c.oph_key IS NULL)::bigint
                   AS sap_missing_context,
               count(*) FILTER (
                   WHERE c.oph_key IS NOT NULL
                     AND NOT coalesce(c.has_photo, false)
               )::bigint AS sap_context_without_photo,
               count(*) FILTER (
                   WHERE coalesce(c.has_photo, false)
                     AND NOT coalesce(c.has_embedding, false)
               )::bigint AS sap_photo_without_embedding
        FROM sap_keys s
        LEFT JOIN context_keys c
          ON c.source_group=s.source_group
         AND c.oph_key=s.oph_key
    """
    try:
        frame = read_df_resilient(
            sql,
            params,
            operation="Harmony SAP OPH versus Panen coverage",
        )
    except Exception as exc:
        print(
            "[HARMONY OPH COVERAGE WARN] rekonsiliasi tidak dapat dibaca; "
            f"detail={' '.join(str(exc).split())[:260]}",
            flush=True,
        )
        return empty
    if frame.empty:
        return empty
    row = frame.iloc[0].to_dict()
    result: dict[str, int | bool | None] = {
        key: int(row.get(key) or 0)
        for key in (
            "sap_total", "sap_with_context", "sap_with_photo",
            "sap_with_embedding", "sap_missing_context",
            "sap_context_without_photo", "sap_photo_without_embedding",
        )
    }
    result["source_ready"] = True
    _norm_cache_set(
        cache_key,
        pd.DataFrame([result]),
    )
    return result

# -----------------------------------------------------------------------------
# Geospatial reference layer
# -----------------------------------------------------------------------------
REFERENCE_SCHEMA = os.getenv("REFERENCE_SCHEMA", "reference")
TABLE_GEO_BOUNDARY = os.getenv("TABLE_GEO_BOUNDARY", "geo_block_boundary")
TABLE_GEO_TPH_POINT = os.getenv("TABLE_GEO_TPH_POINT", "geo_tph_point")


def load_geo_boundaries(company: str | None = None) -> pd.DataFrame:
    """Load active canonical block boundaries without requiring PostGIS.

    Geometry is stored as GeoJSON JSONB so the dashboard remains portable on
    Windows PostgreSQL installations that do not have the PostGIS extension.
    """
    columns = [
        "boundary_id", "company", "estate", "afdeling", "blok",
        "company_norm", "estate_norm", "afdeling_norm", "blok_norm",
        "geometry_geojson", "centroid_lat", "centroid_lon", "area_ha",
        "source_path", "imported_at",
    ]
    if not table_exists(TABLE_GEO_BOUNDARY, REFERENCE_SCHEMA):
        return pd.DataFrame(columns=columns)
    where = ["is_active=true"]
    params: list[Any] = []
    if clean_text(company):
        where.append("company_norm=%s")
        params.append(re.sub(r"[^A-Z0-9]", "", clean_text(company).upper()))
    sql = f"""
        SELECT boundary_id, company, estate, afdeling, blok,
               company_norm, estate_norm, afdeling_norm, blok_norm,
               geometry_geojson, centroid_lat, centroid_lon, area_ha,
               source_path, imported_at
        FROM {qname(TABLE_GEO_BOUNDARY, REFERENCE_SCHEMA)}
        WHERE {' AND '.join(where)}
        ORDER BY company, estate, afdeling, blok, boundary_id
    """
    df = read_df(sql, params, cache_key=f"geo_boundaries:{clean_text(company).upper() or 'ALL'}")
    if df.empty:
        return pd.DataFrame(columns=columns)
    for col in ["company", "estate", "afdeling", "blok", "company_norm", "estate_norm", "afdeling_norm", "blok_norm"]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    return df



def load_geo_tph_points(company: str | None = None) -> pd.DataFrame:
    """Load canonical/inferred TPH anchors built from historical transaction GPS."""
    columns = [
        "tph_point_id", "company", "estate", "afdeling", "blok", "tph",
        "company_norm", "estate_norm", "afdeling_norm", "blok_norm", "tph_norm",
        "latitude", "longitude", "observation_count", "dispersion_p90_m",
        "confidence", "first_observed_date", "last_observed_date", "source_relation",
    ]
    if not table_exists(TABLE_GEO_TPH_POINT, REFERENCE_SCHEMA):
        return pd.DataFrame(columns=columns)
    where = ["is_active=true"]
    params: list[Any] = []
    if clean_text(company):
        where.append("company_norm=%s")
        params.append(re.sub(r"[^A-Z0-9]", "", clean_text(company).upper()))
    sql = f"""
        SELECT tph_point_id, company, estate, afdeling, blok, tph,
               company_norm, estate_norm, afdeling_norm, blok_norm, tph_norm,
               latitude, longitude, observation_count, dispersion_p90_m, confidence,
               first_observed_date, last_observed_date, source_relation
        FROM {qname(TABLE_GEO_TPH_POINT, REFERENCE_SCHEMA)}
        WHERE {' AND '.join(where)}
        ORDER BY company, estate, afdeling, blok, tph
    """
    df = read_df(sql, params, cache_key=f"geo_tph_points:{clean_text(company).upper() or 'ALL'}")
    if df.empty:
        return pd.DataFrame(columns=columns)
    for col in ["company", "estate", "afdeling", "blok", "tph", "company_norm", "estate_norm", "afdeling_norm", "blok_norm", "tph_norm", "confidence"]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    for col in ["latitude", "longitude", "dispersion_p90_m"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "observation_count" in df.columns:
        df["observation_count"] = pd.to_numeric(df["observation_count"], errors="coerce").fillna(0).astype(int)
    return df

def geospatial_boundary_status() -> pd.DataFrame:
    if not table_exists(TABLE_GEO_BOUNDARY, REFERENCE_SCHEMA):
        return pd.DataFrame(columns=["company", "boundary_count", "estate_count", "afdeling_count", "block_count", "last_import"])
    return read_df(
        f"""
        SELECT company,
               count(*)::bigint AS boundary_count,
               count(DISTINCT nullif(estate_norm,''))::bigint AS estate_count,
               count(DISTINCT nullif(afdeling_norm,''))::bigint AS afdeling_count,
               count(DISTINCT nullif(blok_norm,''))::bigint AS block_count,
               max(imported_at) AS last_import
        FROM {qname(TABLE_GEO_BOUNDARY, REFERENCE_SCHEMA)}
        WHERE is_active=true
        GROUP BY company
        ORDER BY company
        """,
        cache_key="geo_boundary_status",
    )

# =============================================================================
# V162 - Visual audit kept separate from ELHP SAP-locked main dashboard
# =============================================================================

def _visual_audit_companies(system: str | None, company: Any) -> list[str]:
    """Resolve canonical companies for the visual audit panel."""
    selected = _company_values(company)
    if selected:
        return [c for c in selected if c in {"PWP", "DIN", "DAN", "DWT"}]
    s = _system_value(system)
    if s == "HARMONY":
        return ["PWP", "DIN"]
    if s == "ELHP":
        return ["DAN", "DWT"]
    return ["PWP", "DIN", "DAN", "DWT"]


def load_visual_reused_audit_monthly(
    system: str | None = None,
    company: Any = None,
    period_year: int = 2026,
) -> pd.DataFrame:
    """Read-only funnel from base visual Gold to main display and SAP verification.

    v162 policy:
    * HARMONY/PWP/DIN main dashboard uses visual findings (group still >=2).
    * ELHP/DAN/DWT main dashboard is SAP-locked (exact OPH + same month + group>=2)
      and strict ownership remains active (DAN=PU*, DWT=ME*).
    * Visual findings that do not pass SAP remain available here for audit only.
    * DAN January 2026 has no main-dashboard bypass; investigate it separately.
    """
    companies = _visual_audit_companies(system, company)
    columns = [
        "company", "month", "raw_visual_usage", "estate_visual_usage",
        "visual_usage", "visual_groups", "with_oph", "unverified_oph",
        "sap_any_month", "sap_same_month", "sap_other_month", "not_in_sap",
        "wrong_estate", "policy_bypass_usage", "calculated_verified_usage",
        "dashboard_usage", "official_dashboard_usage", "sap_oph",
        "dashboard_delta", "official_delta", "official_over_sap",
    ]
    if not companies:
        return pd.DataFrame(columns=columns)

    sql = """
    WITH visual AS (
        SELECT
            processing.canonical_source_group(d.source_group) AS company,
            d.run_id,
            d.group_id,
            d.file_uid,
            d.tanggal,
            d.estate,
            nullif(processing.normalize_oph(d.oph), '') AS oph_key,
            coalesce(d.match_source, '') AS match_source,
            CASE
                WHEN processing.canonical_source_group(d.source_group)='DAN'
                    THEN processing.elhp_estate_matches_company('DAN', d.estate)
                WHEN processing.canonical_source_group(d.source_group)='DWT'
                    THEN processing.elhp_estate_matches_company('DWT', d.estate)
                ELSE nullif(btrim(coalesce(d.estate, '')), '') IS NOT NULL
            END AS strict_estate_ok,
            processing.reused_visual_display_scope_ok(
                d.source_group, d.tanggal, d.estate
            ) AS display_scope_ok
        FROM gold.reused_photo_detail d
        WHERE d.is_active=true
          AND d.tanggal IS NOT NULL
          AND extract(year FROM d.tanggal)::integer=%s
          AND processing.canonical_source_group(d.source_group)=ANY(%s)
    ), flags AS (
        SELECT
            v.*,
            to_char(v.tanggal, 'YYYY-MM') AS month,
            (v.oph_key IS NULL OR upper(v.match_source) LIKE 'UNVERIFIED_OPH_%%') AS is_unverified_oph,
            EXISTS (
                SELECT 1 FROM gold.sap_oph_scope_key k
                WHERE k.source_group=v.company
                  AND k.period_year=%s
                  AND k.oph_key=v.oph_key
            ) AS sap_any_month,
            EXISTS (
                SELECT 1 FROM gold.sap_oph_scope_key k
                WHERE k.source_group=v.company
                  AND k.period_year=%s
                  AND k.sap_month=to_char(v.tanggal, 'YYYY-MM')
                  AND k.oph_key=v.oph_key
            ) AS sap_same_month
        FROM visual v
    ), group_gate AS (
        SELECT
            company, run_id, group_id,
            count(*) FILTER (WHERE display_scope_ok)::integer AS display_members,
            count(*) FILTER (WHERE sap_same_month)::integer AS same_month_members
        FROM flags
        GROUP BY company, run_id, group_id
    ), classified AS (
        SELECT
            f.*,
            coalesce(g.display_members, 0) AS display_members,
            coalesce(g.same_month_members, 0) AS same_month_members,
            (f.display_scope_ok AND coalesce(g.display_members, 0) >= 2) AS display_visual_member,
            (f.sap_same_month AND coalesce(g.same_month_members, 0) >= 2) AS calculated_verified_member,
            (f.display_scope_ok AND NOT f.strict_estate_ok) AS policy_bypass_member
        FROM flags f
        LEFT JOIN group_gate g
          ON g.company=f.company
         AND g.run_id=f.run_id
         AND g.group_id=f.group_id
    ), visual_month AS (
        SELECT
            company,
            month,
            count(*)::bigint AS raw_visual_usage,
            count(*) FILTER (WHERE display_scope_ok)::bigint AS estate_visual_usage,
            count(*) FILTER (WHERE display_visual_member)::bigint AS visual_usage,
            count(DISTINCT (run_id, group_id)) FILTER (WHERE display_visual_member)::bigint AS visual_groups,
            count(*) FILTER (WHERE display_visual_member AND oph_key IS NOT NULL)::bigint AS with_oph,
            count(*) FILTER (WHERE display_visual_member AND is_unverified_oph)::bigint AS unverified_oph,
            count(*) FILTER (WHERE display_visual_member AND sap_any_month)::bigint AS sap_any_month,
            count(*) FILTER (WHERE display_visual_member AND sap_same_month)::bigint AS sap_same_month,
            count(*) FILTER (WHERE display_visual_member AND sap_any_month AND NOT sap_same_month)::bigint AS sap_other_month,
            count(*) FILTER (WHERE display_visual_member AND oph_key IS NOT NULL AND NOT sap_any_month)::bigint AS not_in_sap,
            count(*) FILTER (WHERE NOT display_scope_ok)::bigint AS wrong_estate,
            count(*) FILTER (WHERE display_visual_member AND policy_bypass_member)::bigint AS policy_bypass_usage,
            count(*) FILTER (WHERE calculated_verified_member)::bigint AS calculated_verified_usage
        FROM classified
        GROUP BY company, month
    ), main_dashboard_month AS (
        SELECT upper(source_company) AS company,
               to_char(month, 'YYYY-MM') AS month,
               sum(usage_count)::bigint AS dashboard_usage
        FROM gold.reused_photo_dashboard_monthly_cleaned
        WHERE is_active=true
          AND upper(source_company)=ANY(%s)
          AND extract(year FROM month)::integer=%s
        GROUP BY upper(source_company), to_char(month, 'YYYY-MM')
        UNION ALL
        SELECT upper(source_company) AS company,
               to_char(month, 'YYYY-MM') AS month,
               sum(usage_count)::bigint AS dashboard_usage
        FROM gold.elhp_dashboard_monthly_cleaned
        WHERE is_active=true
          AND upper(source_company)=ANY(%s)
          AND extract(year FROM month)::integer=%s
        GROUP BY upper(source_company), to_char(month, 'YYYY-MM')
    ), main_rollup AS (
        SELECT company, month, sum(dashboard_usage)::bigint AS dashboard_usage
        FROM main_dashboard_month
        GROUP BY company, month
    ), official_month AS (
        SELECT processing.canonical_source_group(source_group) AS company,
               to_char(month, 'YYYY-MM') AS month,
               sum(usage_count)::bigint AS official_dashboard_usage
        FROM gold.reused_photo_dashboard_monthly_sap_verified
        WHERE is_active=true
          AND processing.canonical_source_group(source_group)=ANY(%s)
          AND extract(year FROM month)::integer=%s
        GROUP BY processing.canonical_source_group(source_group), to_char(month, 'YYYY-MM')
    ), sap_month AS (
        SELECT source_group AS company,
               sap_month AS month,
               count(DISTINCT oph_key)::bigint AS sap_oph
        FROM gold.sap_oph_scope_key
        WHERE period_year=%s
          AND source_group=ANY(%s)
        GROUP BY source_group, sap_month
    ), months AS (
        SELECT company, month FROM visual_month
        UNION SELECT company, month FROM main_rollup
        UNION SELECT company, month FROM official_month
        UNION SELECT company, month FROM sap_month
    )
    SELECT
        m.company,
        m.month,
        coalesce(v.raw_visual_usage, 0)::bigint AS raw_visual_usage,
        coalesce(v.estate_visual_usage, 0)::bigint AS estate_visual_usage,
        coalesce(v.visual_usage, 0)::bigint AS visual_usage,
        coalesce(v.visual_groups, 0)::bigint AS visual_groups,
        coalesce(v.with_oph, 0)::bigint AS with_oph,
        coalesce(v.unverified_oph, 0)::bigint AS unverified_oph,
        coalesce(v.sap_any_month, 0)::bigint AS sap_any_month,
        coalesce(v.sap_same_month, 0)::bigint AS sap_same_month,
        coalesce(v.sap_other_month, 0)::bigint AS sap_other_month,
        coalesce(v.not_in_sap, 0)::bigint AS not_in_sap,
        coalesce(v.wrong_estate, 0)::bigint AS wrong_estate,
        coalesce(v.policy_bypass_usage, 0)::bigint AS policy_bypass_usage,
        coalesce(v.calculated_verified_usage, 0)::bigint AS calculated_verified_usage,
        coalesce(d.dashboard_usage, 0)::bigint AS dashboard_usage,
        coalesce(o.official_dashboard_usage, 0)::bigint AS official_dashboard_usage,
        coalesce(s.sap_oph, 0)::bigint AS sap_oph,
        (coalesce(d.dashboard_usage, 0) - coalesce(v.visual_usage, 0))::bigint AS dashboard_delta,
        (coalesce(o.official_dashboard_usage, 0) - coalesce(v.calculated_verified_usage, 0))::bigint AS official_delta,
        greatest(coalesce(o.official_dashboard_usage, 0) - coalesce(s.sap_oph, 0), 0)::bigint AS official_over_sap
    FROM months m
    LEFT JOIN visual_month v USING (company, month)
    LEFT JOIN main_rollup d USING (company, month)
    LEFT JOIN official_month o USING (company, month)
    LEFT JOIN sap_month s USING (company, month)
    ORDER BY m.company, m.month
    """
    params = [
        int(period_year), companies,
        int(period_year), int(period_year),
        companies, int(period_year),
        companies, int(period_year),
        companies, int(period_year),
        int(period_year), companies,
    ]
    try:
        out = read_df_resilient(sql, params, operation="visual reused monthly audit v162")
    except Exception as exc:
        print(f"[VISUAL AUDIT WARN] monthly audit gagal: {exc}", flush=True)
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in out.columns:
            out[col] = 0 if col not in {"company", "month"} else ""
    return out[columns].copy()

def load_visual_reused_audit_groups(
    system: str | None = None,
    company: Any = None,
    period_year: int = 2026,
    months: Any = None,
    limit: int = 2000,
) -> pd.DataFrame:
    """Group-level visual audit; independent from the v162 ELHP SAP-locked main view."""
    companies = _visual_audit_companies(system, company)
    selected_months = [str(x) for x in _as_list(months) if str(x).strip()]
    columns = [
        "company", "group_id", "representative_image", "months", "estate_list",
        "visual_usage", "with_oph", "sap_same_month", "verified_usage",
        "unverified_oph", "sap_other_month", "not_in_sap", "wrong_estate",
        "policy_bypass_usage", "verification_status",
    ]
    if not companies:
        return pd.DataFrame(columns=columns)

    month_clause = ""
    params: list[Any] = [int(period_year), companies]
    if selected_months:
        month_clause = "AND to_char(d.tanggal, 'YYYY-MM')=ANY(%s)"
        params.append(selected_months)
    params.extend([int(period_year), int(period_year), max(1, int(limit))])

    sql = f"""
    WITH visual AS (
        SELECT
            processing.canonical_source_group(d.source_group) AS company,
            d.run_id, d.group_id, d.file_uid, d.image_name, d.is_representative,
            d.tanggal, d.estate,
            nullif(processing.normalize_oph(d.oph), '') AS oph_key,
            coalesce(d.match_source, '') AS match_source,
            CASE
                WHEN processing.canonical_source_group(d.source_group)='DAN'
                    THEN processing.elhp_estate_matches_company('DAN', d.estate)
                WHEN processing.canonical_source_group(d.source_group)='DWT'
                    THEN processing.elhp_estate_matches_company('DWT', d.estate)
                ELSE nullif(btrim(coalesce(d.estate, '')), '') IS NOT NULL
            END AS strict_estate_ok,
            processing.reused_visual_display_scope_ok(
                d.source_group, d.tanggal, d.estate
            ) AS display_scope_ok
        FROM gold.reused_photo_detail d
        WHERE d.is_active=true
          AND d.tanggal IS NOT NULL
          AND extract(year FROM d.tanggal)::integer=%s
          AND processing.canonical_source_group(d.source_group)=ANY(%s)
          {month_clause}
    ), flags AS (
        SELECT
            v.*,
            to_char(v.tanggal, 'YYYY-MM') AS month,
            (v.oph_key IS NULL OR upper(v.match_source) LIKE 'UNVERIFIED_OPH_%%') AS is_unverified_oph,
            EXISTS (
                SELECT 1 FROM gold.sap_oph_scope_key k
                WHERE k.source_group=v.company
                  AND k.period_year=%s
                  AND k.oph_key=v.oph_key
            ) AS sap_any_month,
            EXISTS (
                SELECT 1 FROM gold.sap_oph_scope_key k
                WHERE k.source_group=v.company
                  AND k.period_year=%s
                  AND k.sap_month=to_char(v.tanggal, 'YYYY-MM')
                  AND k.oph_key=v.oph_key
            ) AS sap_same_month
        FROM visual v
    ), gated AS (
        SELECT f.*,
               count(*) FILTER (WHERE display_scope_ok)
                   OVER (PARTITION BY company, run_id, group_id) AS display_members,
               count(*) FILTER (WHERE sap_same_month)
                   OVER (PARTITION BY company, run_id, group_id) AS same_month_members
        FROM flags f
    ), agg AS (
        SELECT
            company, run_id, group_id,
            (array_agg(image_name ORDER BY is_representative DESC, tanggal NULLS LAST, file_uid))[1]
                AS representative_image,
            string_agg(DISTINCT month, ', ' ORDER BY month) AS months,
            string_agg(DISTINCT btrim(estate), ', ' ORDER BY btrim(estate))
                FILTER (WHERE display_scope_ok AND nullif(btrim(estate), '') IS NOT NULL) AS estate_list,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2)::bigint AS visual_usage,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND oph_key IS NOT NULL)::bigint AS with_oph,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND sap_same_month)::bigint AS sap_same_month,
            count(*) FILTER (WHERE sap_same_month AND same_month_members >= 2)::bigint AS verified_usage,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND is_unverified_oph)::bigint AS unverified_oph,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND sap_any_month AND NOT sap_same_month)::bigint AS sap_other_month,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND oph_key IS NOT NULL AND NOT sap_any_month)::bigint AS not_in_sap,
            count(*) FILTER (WHERE NOT display_scope_ok)::bigint AS wrong_estate,
            count(*) FILTER (WHERE display_scope_ok AND display_members >= 2 AND NOT strict_estate_ok)::bigint AS policy_bypass_usage
        FROM gated
        GROUP BY company, run_id, group_id
    )
    SELECT
        company, group_id, representative_image, months, coalesce(estate_list, '') AS estate_list,
        visual_usage, with_oph, sap_same_month, verified_usage, unverified_oph,
        sap_other_month, not_in_sap, wrong_estate, policy_bypass_usage,
        CASE
            WHEN visual_usage < 2 THEN 'NOT DISPLAYED - <2 DISPLAY MEMBERS'
                        WHEN verified_usage >= 2 AND verified_usage = visual_usage THEN 'SAP VERIFIED'
            WHEN verified_usage >= 2 THEN 'VISUAL SHOWN - PARTIAL SAP VERIFIED'
            WHEN unverified_oph > 0 THEN 'VISUAL SHOWN - OPH UNVERIFIED'
            WHEN sap_other_month > 0 THEN 'VISUAL SHOWN - SAP OTHER MONTH'
            WHEN not_in_sap > 0 THEN 'VISUAL SHOWN - OPH NOT IN SAP'
            WHEN sap_same_month = 1 THEN 'VISUAL SHOWN - ONLY 1 SAP VERIFIED MEMBER'
            WHEN wrong_estate > 0 THEN 'VISUAL SHOWN - OUT-OF-SCOPE MEMBER EXCLUDED'
            ELSE 'VISUAL SHOWN'
        END AS verification_status
    FROM agg
    WHERE visual_usage > 0 OR wrong_estate > 0
    ORDER BY visual_usage DESC, company, group_id
    LIMIT %s
    """
    try:
        out = read_df_resilient(sql, params, operation="visual reused group audit v162")
    except Exception as exc:
        print(f"[VISUAL AUDIT WARN] group audit gagal: {exc}", flush=True)
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in out.columns:
            out[col] = "" if col in {"company", "representative_image", "months", "estate_list", "verification_status"} else 0
    return out[columns].copy()
