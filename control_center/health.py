from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from golden_eye.config import Settings
from golden_eye.db import connection
from golden_eye.sap_db60_sync import get_local_max_budat

from .config import CONFIG

SETTINGS = Settings()


def _pg_relations() -> list[str]:
    schema = os.getenv("SILVER_SCHEMA", "silver").strip() or "silver"
    sap_schema = os.getenv("SAP_KPI_SCHEMA", "silver").strip() or "silver"
    app_schema = os.getenv("APP_SCHEMA", "gold").strip() or "gold"
    rec_schema = os.getenv("RECOVERY_SCHEMA", "processing").strip() or "processing"
    raw_schema = os.getenv("RAW_BUSINESS_SCHEMA", "raw").strip() or "raw"
    return [
        f"{schema}.{os.getenv('BUSINESS_TABLE', 'production_plantation_clean').strip()}",
        f"{sap_schema}.{os.getenv('SAP_KPI_TABLE', 'v_zpay_harvesting_live').strip()}",
        f"{os.getenv('DB60_ZPAY_TARGET_SCHEMA', 'raw').strip() or 'raw'}.{os.getenv('DB60_ZPAY_TARGET_TABLE', 'zpay_harvesting').strip() or 'zpay_harvesting'}",
        f"{schema}.{os.getenv('IMAGE_TABLE', 'image_store_all_files').strip()}",
        f"{raw_schema}.{os.getenv('RAW_BUSINESS_TABLE', 'production_plantation').strip()}",
        "processing.image_context",
        "processing.image_feature",
        f"{rec_schema}.{os.getenv('TABLE_PANEN_NAS_INDEX', 'panen_nas_path_index').strip()}",
        f"{rec_schema}.{os.getenv('TABLE_TRANSPORT_KT_CONTEXT', 'transport_kt_before_context').strip()}",
        f"{rec_schema}.{os.getenv('TABLE_TRANSPORT_KT_SIMILARITY', 'transport_kt_similarity').strip()}",
        f"{app_schema}.{os.getenv('TABLE_GROUP_CLEANED', 'reused_photo_group_summary_cleaned').strip()}",
        f"{app_schema}.{os.getenv('TABLE_DETAIL_TRANSACTION', 'reused_photo_detail_transaction').strip()}",
        f"{app_schema}.{os.getenv('TABLE_SAP_KPI_SUMMARY', 'sap_oph_kpi_summary').strip()}",
        f"{app_schema}.{os.getenv('TABLE_TRANSPORT_STATUS', 'reused_photo_transport_status_summary').strip()}",
    ]


def postgres_health() -> dict[str, Any]:
    relations = _pg_relations()
    result: dict[str, Any] = {
        "id": "postgres",
        "label": "PostgreSQL golden_eye",
        "active": False,
        "status": "OFFLINE",
        "message": "Belum diperiksa",
        "relations": [],
        "missing": [],
    }
    try:
        with connection(SETTINGS) as conn, conn.cursor() as cur:
            checked = []
            missing = []
            for rel in relations:
                cur.execute("SELECT to_regclass(%s)", (rel,))
                exists = cur.fetchone()[0] is not None
                checked.append({"name": rel, "exists": exists})
                if not exists:
                    missing.append(rel)
            result.update({
                "active": True,
                "status": "READY" if not missing else "WARNING",
                "message": "PostgreSQL aktif" if not missing else f"PostgreSQL aktif, {len(missing)} relation belum ditemukan",
                "relations": checked,
                "missing": missing,
            })
    except Exception as exc:
        result.update({"status": "OFFLINE", "message": str(exc)[:220]})
    return result


def mssql_health() -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "mssql",
        "label": "SQL Server / DASHBOARD",
        "active": False,
        "status": "OFFLINE",
        "message": "Belum diperiksa",
        "tables": [],
        "missing": [],
    }
    try:
        import pyodbc  # type: ignore

        server = os.getenv("MSSQL_SERVER", "").strip()
        port = os.getenv("MSSQL_PORT", "1433").strip()
        database = os.getenv("MSSQL_DATABASE", "DASHBOARD").strip()
        user = os.getenv("MSSQL_USER", "").strip()
        password = os.getenv("MSSQL_PASSWORD", "")
        driver = os.getenv("MSSQL_DRIVER", "SQL Server").strip()
        if not server or not user or not password:
            raise RuntimeError("Konfigurasi MSSQL_SERVER / USER / PASSWORD belum lengkap")
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={server},{port};DATABASE={database};"
            f"UID={user};PWD={password};TrustServerCertificate=yes;Connection Timeout=4;"
        )
        raw_table = os.getenv("MSSQL_RAW_TABLE", "dbo.PRODUCTION_PLANTATION").strip()
        tr_table = os.getenv("MSSQL_TRANSPORT_TABLE", "dbo.PRODUCTION_PLANTATION").strip()
        tables = []
        missing = []
        with pyodbc.connect(conn_str, timeout=4) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            for table in dict.fromkeys([raw_table, tr_table]):
                # OBJECT_ID accepts schema-qualified table names.
                cur.execute("SELECT OBJECT_ID(?, 'U')", table)
                exists = cur.fetchone()[0] is not None
                tables.append({"name": table, "exists": exists})
                if not exists:
                    missing.append(table)
        result.update({
            "active": True,
            "status": "READY" if not missing else "WARNING",
            "message": "SQL Server aktif" if not missing else f"SQL Server aktif, {len(missing)} table belum ditemukan",
            "tables": tables,
            "missing": missing,
        })
    except Exception as exc:
        result.update({"status": "OFFLINE", "message": str(exc)[:220]})
    return result


def sap_db60_health() -> dict[str, Any]:
    """Read-only SAP freshness status from local audit history.

    Do not query DB60 on every browser poll: the explicit sync step owns remote
    connectivity and records remote/local watermarks in control.sap_db60_sync_audit.
    """
    result: dict[str, Any] = {
        "id": "sap_db60",
        "label": "SAP DB60 / ZPAY",
        "active": False,
        "status": "WARNING",
        "message": "Belum ada histori sync DB60",
        "remote_max_budat": None,
        "local_max_budat": None,
    }
    try:
        local_max = get_local_max_budat(SETTINGS, "ALL")
        remote_max = None
        last_sync = None
        with connection(SETTINGS) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('control.sap_db60_sync_audit')")
            if cur.fetchone()[0] is not None:
                cur.execute(
                    """
                    SELECT remote_max_budat, local_max_budat, finished_at, target_month, status
                    FROM control.sap_db60_sync_audit
                    WHERE source_group='ALL'
                      AND target_relation='silver.zpay_harvesting_db60'
                    ORDER BY finished_at DESC, audit_id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if row:
                    remote_max, audit_local, finished_at, target_month, audit_status = row
                    last_sync = finished_at
                    if audit_local and (local_max is None or audit_local > local_max):
                        local_max = audit_local
        lag_days = None
        if local_max and remote_max:
            lag_days = (remote_max - local_max).days
        if not last_sync:
            status = "WARNING"
            message = f"Belum pernah sync DB60 langsung ke Silver · local={local_max or '-'}"
        elif remote_max and local_max and local_max < remote_max:
            status = "WARNING"
            message = f"SAP LOCAL TERTINGGAL {lag_days} hari · DB60={remote_max} · local={local_max}"
        elif remote_max and local_max:
            status = "READY"
            message = f"SAP sync terakhir OK · DB60={remote_max} · local={local_max}"
        else:
            status = "WARNING"
            message = f"Histori sync ada tetapi watermark belum lengkap · local={local_max or '-'}"
        result.update({
            "active": bool(last_sync),
            "status": status,
            "message": message,
            "remote_max_budat": str(remote_max or ""),
            "local_max_budat": str(local_max or ""),
            "lag_days": lag_days,
            "last_sync": str(last_sync or ""),
        })
    except Exception as exc:
        result.update({"status": "WARNING", "message": str(exc)[:220]})
    return result


def nas_health() -> dict[str, Any]:
    roots = []
    missing = []
    for company in ("PWP", "DIN", "DAN", "DWT"):
        for kind, vals in (("PANEN", CONFIG.panen_roots(company)), ("TRANSPORT", CONFIG.transport_roots(company) if company in {"PWP", "DIN"} else [])):
            for value in vals:
                exists = Path(value).exists()
                row = {"company": company, "kind": kind, "path": value, "exists": exists}
                roots.append(row)
                if not exists:
                    missing.append(row)
    configured = bool(roots)
    return {
        "id": "nas",
        "label": "NAS Evidence",
        "active": configured and not missing,
        "status": "READY" if configured and not missing else ("WARNING" if configured else "OFFLINE"),
        "message": (
            "Semua root NAS tersedia" if configured and not missing
            else f"{len(missing)} root NAS tidak tersedia" if configured
            else "Root NAS belum dikonfigurasi"
        ),
        "roots": roots,
        "missing": missing,
    }


def geospatial_health() -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "geospatial",
        "label": "Geospatial Master",
        "active": False,
        "status": "WARNING",
        "message": "Boundary belum diperiksa",
        "missing": [],
        "companies": [],
    }
    try:
        with connection(SETTINGS) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('reference.geo_block_boundary')")
            if cur.fetchone()[0] is None:
                result["message"] = "reference.geo_block_boundary belum dibuat"
                result["missing"] = ["reference.geo_block_boundary"]
                return result
            cur.execute(
                """
                SELECT upper(company), count(*)
                FROM reference.geo_block_boundary
                WHERE is_active=true
                GROUP BY upper(company)
                ORDER BY upper(company)
                """
            )
            rows = cur.fetchall()
            counts = {str(company): int(count) for company, count in rows}
            configured = [c for c in ("PWP", "DIN", "DAN", "DWT") if os.getenv(f"GEO_{c}_BOUNDARY_FILE", "").strip()]
            missing = [c for c in configured if counts.get(c, 0) <= 0]
            result.update({
                "active": bool(counts),
                "status": "READY" if counts and not missing else "WARNING",
                "message": (
                    "Boundary aktif: " + ", ".join(f"{c} {n:,}" for c, n in counts.items())
                    if counts else "Belum ada boundary aktif yang di-import"
                ),
                "missing": missing,
                "companies": [{"company": c, "features": counts.get(c, 0)} for c in ("PWP", "DIN", "DAN", "DWT")],
            })
    except Exception as exc:
        result.update({"status": "WARNING", "message": str(exc)[:220]})
    return result


def source_health() -> dict[str, Any]:
    pg = postgres_health()
    ms = mssql_health()
    sap60 = sap_db60_health()
    nas = nas_health()
    geo = geospatial_health()
    return {
        "postgres": pg,
        "mssql": ms,
        "sap_db60": sap60,
        "nas": nas,
        "geospatial": geo,
        "scrapeops": {
            "id": "scrapeops",
            "label": "ScrapeOps / Downloader",
            "active": None,
            "status": "EXTERNAL",
            "message": "Upstream eksternal: Control Center tidak menjalankan scraper; kesiapan dinilai dari file bulan target di NAS.",
        },
        "overall": "READY" if pg["status"] == "READY" and ms["status"] in {"READY", "WARNING"} and sap60["status"] == "READY" and nas["status"] == "READY" else "CHECK",
    }


_HEALTH_CACHE: dict[str, Any] | None = None
_HEALTH_CACHE_AT = 0.0

def source_health_cached(ttl_seconds: float = 30.0) -> dict[str, Any]:
    global _HEALTH_CACHE, _HEALTH_CACHE_AT
    now = time.monotonic()
    if _HEALTH_CACHE is not None and (now - _HEALTH_CACHE_AT) < ttl_seconds:
        return _HEALTH_CACHE
    _HEALTH_CACHE = source_health()
    _HEALTH_CACHE_AT = now
    return _HEALTH_CACHE
