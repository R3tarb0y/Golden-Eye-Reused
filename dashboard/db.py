from __future__ import annotations

import hashlib
import os
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()

APP_SCHEMA = os.getenv("APP_SCHEMA", "gold")
IMAGE_SCHEMA = os.getenv("IMAGE_SCHEMA", "silver")
RECOVERY_SCHEMA = os.getenv("RECOVERY_SCHEMA", "processing")

TABLE_GROUP = os.getenv("TABLE_GROUP_CLEANED", "reused_photo_group_summary_cleaned")
TABLE_DETAIL = os.getenv("TABLE_DETAIL_TRANSACTION", "reused_photo_detail_transaction")
TABLE_DASHBOARD_MONTHLY = os.getenv(
    "TABLE_DASHBOARD_MONTHLY", "reused_photo_dashboard_monthly_cleaned"
)
TABLE_SAP_KPI_SUMMARY = os.getenv(
    "TABLE_SAP_KPI_SUMMARY", "sap_oph_kpi_summary"
)
# Relation sumber SAP yang dipakai untuk rekonsiliasi OPH. Snapshot Gold tetap
# menjadi sumber KPI cepat, sedangkan relation ini hanya dibaca untuk
# memastikan berapa OPH SAP yang benar-benar mempunyai konteks/foto Panen.
# v152: dashboard must read the live SAP bridge maintained from DB60.
# Legacy SAP_SCHEMA/SAP_TABLE may point to an old local snapshot and are no
# longer authoritative for KPI/coverage.
SAP_SCHEMA = os.getenv("SAP_KPI_SCHEMA", "silver")
SAP_TABLE = os.getenv("SAP_KPI_TABLE", "v_zpay_harvesting_live")
TABLE_TRANSPORT_STATUS = os.getenv("TABLE_TRANSPORT_STATUS", "reused_photo_transport_status_summary")
TABLE_OPH_VALIDATION_EXCEL = os.getenv("TABLE_OPH_VALIDATION_EXCEL", "reused_photo_oph_validation_from_excel")
TABLE_FALSE_ALARM = os.getenv("TABLE_FALSE_ALARM", "reused_photo_false_alarm_raw")
TABLE_AUDIT_DOWNLOAD_REPORT = os.getenv("TABLE_AUDIT_DOWNLOAD_REPORT", "audit_download_report_vs_sap_total")
TABLE_AUDIT_NO_IMAGE = os.getenv("TABLE_AUDIT_NO_IMAGE", "audit_harmony_no_image_final")
TABLE_IMAGE_STORE = os.getenv("TABLE_IMAGE_STORE", "image_store")
TABLE_IMAGE_ALL = os.getenv("TABLE_IMAGE_ALL", "image_store_all_files")
TABLE_IMAGE_VIEW = os.getenv("TABLE_IMAGE_VIEW", "image_store_from_all_files_view")
# Hot store hanya berisi gambar yang benar-benar dipakai Gold aktif. .env lama
# yang masih menunjuk recovery v1/v2 dipaksa ke gambar_reused agar dashboard
# dan PDF tidak pernah menyentuh relation/TOAST lama yang rusak.
_configured_bytea_recovery = os.getenv(
    "TABLE_BYTEA_RECOVERY", "gambar_reused"
).strip()
TABLE_BYTEA_RECOVERY = (
    "gambar_reused"
    if _configured_bytea_recovery
    in {"", "source_bytea_recovery", "source_bytea_recovery_v2"}
    else _configured_bytea_recovery
)
TABLE_BYTEA_TARGETED_RECOVERY = os.getenv("TABLE_BYTEA_TARGETED_RECOVERY", "gambar_reused_targeted_recovery")
TABLE_BYTEA_EXCEPTION = os.getenv("TABLE_BYTEA_EXCEPTION", "source_bytea_exception")
TABLE_TRANSPORT_KT_CONTEXT = os.getenv(
    "TABLE_TRANSPORT_KT_CONTEXT", "transport_kt_before_context"
)
TABLE_TRANSPORT_KT_SIMILARITY = os.getenv(
    "TABLE_TRANSPORT_KT_SIMILARITY", "transport_kt_similarity"
)
TABLE_PANEN_NAS_INDEX = os.getenv(
    "TABLE_PANEN_NAS_INDEX", "panen_nas_path_index"
)
ALLOW_SILVER_IMAGE_FALLBACK = os.getenv(
    "ALLOW_SILVER_IMAGE_FALLBACK", "false"
).lower() in {"1", "true", "yes", "y"}
NAS_IMAGE_FIRST = os.getenv(
    "NAS_IMAGE_FIRST", "false"
).lower() in {"1", "true", "yes", "y"}
NAS_IMAGE_MAX_BYTES = max(
    1, int(os.getenv("NAS_IMAGE_MAX_BYTES", str(64 * 1024 * 1024)))
)

# ELHP tables loaded by load_elhp_data_to_silver_v1/v2.
TABLE_ELHP_GROUP = os.getenv("TABLE_ELHP_GROUP_CLEANED", "elhp_gt_group_summary_cleaned")
TABLE_ELHP_DETAIL = os.getenv("TABLE_ELHP_DETAIL_TRANSACTION", "elhp_gt_detail_transaction")
TABLE_ELHP_DASHBOARD_MONTHLY = os.getenv(
    "TABLE_ELHP_DASHBOARD_MONTHLY", "elhp_dashboard_monthly_cleaned"
)
TABLE_ELHP_TRANSPORT_STATUS = os.getenv("TABLE_ELHP_TRANSPORT_STATUS", "elhp_gt_transport_status_summary")
TABLE_ELHP_OPH_VALIDATION = os.getenv("TABLE_ELHP_OPH_VALIDATION", "elhp_oph_visual_vs_sap_validation_file")
TABLE_ELHP_AUDIT_DOWNLOAD_REPORT = os.getenv("TABLE_ELHP_AUDIT_DOWNLOAD_REPORT", "elhp_download_report_vs_sap_validation_file")

# V23 safety: kalau .env lama tidak sengaja mengarahkan tabel ELHP ke tabel Harmony,
# paksa kembali ke tabel ELHP default agar System=ELHP tidak menampilkan PWP/BP.
if TABLE_ELHP_GROUP == TABLE_GROUP:
    TABLE_ELHP_GROUP = "elhp_gt_group_summary_cleaned"
if TABLE_ELHP_DETAIL == TABLE_DETAIL:
    TABLE_ELHP_DETAIL = "elhp_gt_detail_transaction"

QUERY_CACHE_SECONDS = int(os.getenv("QUERY_CACHE_SECONDS", "30"))
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "12"))
LOG_SQL_TIMING = os.getenv("LOG_SQL_TIMING", "false").lower() in {"1", "true", "yes", "y"}
SQL_SLOW_QUERY_MS = max(0, int(os.getenv("DASHBOARD_SQL_SLOW_MS", "1500")))

_cache: dict[str, tuple[float, Any]] = {}
_columns_cache: dict[str, list[str]] = {}
_pool: ThreadedConnectionPool | None = None
_unreadable_image_relations: set[str] = set()
_image_storage_warnings: dict[str, str] = {}


def ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")):
        raise ValueError(f"Identifier tidak aman: {name!r}")
    return '"' + str(name) + '"'


def qname(table: str, schema: str | None = None) -> str:
    return f"{ident(schema or APP_SCHEMA)}.{ident(table)}"


def _pg_kwargs() -> dict[str, Any]:
    password = os.getenv("PG_PASSWORD", "")
    if not password:
        raise RuntimeError("PG_PASSWORD kosong. Isi .env atau set environment variable.")
    kwargs = dict(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        user=os.getenv("PG_USER", "postgres"),
        password=password,
        dbname=os.getenv("PG_DATABASE", "golden_eye"),
        connect_timeout=10,
    )
    timeout_ms = max(0, int(os.getenv("DASHBOARD_QUERY_TIMEOUT_MS", "0")))
    if timeout_ms:
        kwargs["options"] = f"-c statement_timeout={timeout_ms}"
    return kwargs


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(PG_POOL_MIN, PG_POOL_MAX, **_pg_kwargs())
    return _pool


def _borrow_conn():
    try:
        return _get_pool().getconn(), True
    except Exception:
        # Fallback kalau pool gagal dibuat di environment tertentu.
        return psycopg2.connect(**_pg_kwargs()), False


def _return_conn(conn, pooled: bool) -> None:
    try:
        # Pastikan transaksi SELECT tidak menggantung dan connection siap dipakai lagi.
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        if pooled and _pool is not None:
            _pool.putconn(conn)
        else:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def connect_pg():
    """Compatibility helper for scripts that expect a normal psycopg2 connection."""
    return psycopg2.connect(**_pg_kwargs())


def ping() -> str:
    conn, pooled = _borrow_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_schema(), now()")
            db, schema, now = cur.fetchone()
            return f"PostgreSQL OK | db={db} | schema={schema} | time={now}"
    finally:
        _return_conn(conn, pooled)


def cache_get(key: str):
    if QUERY_CACHE_SECONDS <= 0:
        return None
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > QUERY_CACHE_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any):
    if QUERY_CACHE_SECONDS > 0:
        _cache[key] = (time.time(), value)


def clear_query_cache() -> None:
    _cache.clear()


def _df_from_cursor(cur) -> pd.DataFrame:
    cols = [d.name if hasattr(d, "name") else d[0] for d in (cur.description or [])]
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame.from_records(rows, columns=cols)


def read_df(sql: str, params: list | tuple | None = None, cache_key: str | None = None) -> pd.DataFrame:
    """Read SQL into DataFrame without pandas.read_sql_query.

    This removes the repeated pandas DBAPI warning and is faster for this Dash
    workload because it reuses a PostgreSQL connection pool instead of opening a
    new connection on every dropdown/chart callback.
    """
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached.copy()

    started = time.perf_counter()
    execute_ms = fetch_ms = frame_ms = 0.0
    conn, pooled = _borrow_conn()
    try:
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(sql, params or [])
            execute_ms = (time.perf_counter() - t0) * 1000.0
            t0 = time.perf_counter()
            cols = [d.name if hasattr(d, "name") else d[0] for d in (cur.description or [])]
            rows = cur.fetchall()
            fetch_ms = (time.perf_counter() - t0) * 1000.0
            t0 = time.perf_counter()
            df = pd.DataFrame.from_records(rows, columns=cols)
            frame_ms = (time.perf_counter() - t0) * 1000.0
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        print(f"[DASHBOARD SQL ERROR] ms={elapsed:.0f} sql_id={hashlib.sha1(sql.encode()).hexdigest()[:10]}", flush=True)
        raise
    finally:
        _return_conn(conn, pooled)

    elapsed = (time.perf_counter() - started) * 1000
    if LOG_SQL_TIMING or elapsed >= SQL_SLOW_QUERY_MS:
        # Timing components distinguish SQL execution, network/fetch, and Pandas.
        # Show only SQL identifiers/relation names: never interpolate parameters
        # (which can contain business IDs) into logs.
        rels = re.findall(
            r'\b(?:FROM|JOIN)\s+((?:"[A-Za-z_][\w]*"|[A-Za-z_][\w]*)(?:\.(?:"[A-Za-z_][\w]*"|[A-Za-z_][\w]*))?)',
            sql, flags=re.I,
        )
        relations = ",".join(dict.fromkeys(rels))[:220] or "-"
        print(
            f"[DASHBOARD SQL] ms={elapsed:.0f} rows={len(df)} "
            f"execute_ms={execute_ms:.0f} fetch_ms={fetch_ms:.0f} "
            f"frame_ms={frame_ms:.0f} "
            f"sql_id={hashlib.sha1(sql.encode()).hexdigest()[:10]} "
            f"relations={relations} cache_key={cache_key or '-'}",
            flush=True,
        )
        if elapsed >= SQL_SLOW_QUERY_MS and os.getenv("DASHBOARD_LOG_SQL_PREVIEW", "false").lower() in ("1", "true", "yes"):
            # Developer opt-in. Never use mogrify or print bound parameters.
            print("[DASHBOARD SQL PREVIEW] " + " ".join(sql.split())[:700], flush=True)
    if cache_key:
        cache_set(cache_key, df.copy())
    return df


def read_df_resilient(
    sql: str,
    params: list | tuple | None = None,
    *,
    operation: str = "dashboard metadata",
) -> pd.DataFrame:
    """Retry one metadata query without indexes after a physical index error.

    This does not pretend to repair PostgreSQL.  It keeps a dashboard callback
    usable while an administrator rebuilds the damaged index.  A damaged
    heap/TOAST still raises on the retry and must be recovered normally.
    """
    try:
        return read_df(sql, params)
    except Exception as first_exc:
        if not _is_relation_storage_error(first_exc):
            raise
        message = " ".join(str(first_exc).split())
        print(
            f"[DASHBOARD STORAGE WARN] {operation}: index/relation error; "
            f"retry sequential scan. detail={message[:300]}",
            flush=True,
        )

    conn, pooled = _borrow_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_indexscan=off")
            cur.execute("SET LOCAL enable_indexonlyscan=off")
            cur.execute("SET LOCAL enable_bitmapscan=off")
            cur.execute(sql, params or [])
            return _df_from_cursor(cur)
    finally:
        _return_conn(conn, pooled)


def read_one(sql: str, params: list | tuple | None = None) -> dict[str, Any] | None:
    conn, pooled = _borrow_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or [])
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _return_conn(conn, pooled)


def table_exists(table: str, schema: str | None = None) -> bool:
    key = f"exists:{schema or APP_SCHEMA}.{table}"
    cached = _columns_cache.get(key)
    if cached is not None:
        return bool(cached)
    sql = """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=%s AND table_name=%s
    ) AS ok
    """
    row = read_one(sql, [schema or APP_SCHEMA, table])
    ok = bool(row and row.get("ok"))
    _columns_cache[key] = ["1"] if ok else []
    return ok


def table_exists_uncached(table: str, schema: str | None = None) -> bool:
    """Existence check without negative process cache; used for late-created repair overlays."""
    row = read_one(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s
        ) AS ok
        """,
        [schema or APP_SCHEMA, table],
    )
    return bool(row and row.get("ok"))


def get_columns(table: str, schema: str | None = None) -> list[str]:
    """Return table columns with a process-local cache."""
    key = f"{schema or APP_SCHEMA}.{table}"
    if key in _columns_cache:
        return list(_columns_cache[key])
    if not table_exists(table, schema):
        _columns_cache[key] = []
        return []
    sql = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema=%s AND table_name=%s
    ORDER BY ordinal_position
    """
    df = read_df(sql, [schema or APP_SCHEMA, table])
    cols = df["column_name"].astype(str).tolist() if not df.empty else []
    _columns_cache[key] = cols
    return list(cols)


# Source aliases are metadata-only and do not change the SAP-verified view contract.
# The company predicate is applied AFTER the original cleaned view's SAP gate.
_DASHBOARD_SOURCE_ALIASES = {
    "PWP": ("PWP", "HARMONY_PWP"),
    "DIN": ("DIN", "ELHP_DIN", "HARMONY_DIN"),
    "DAN": ("DAN", "ELHP_DAN"),
    "DWT": ("DWT", "ELHP_DWT"),
}


def _table_filter_sql(
    columns: set[str], companies: tuple[str, ...] | None, active_only: bool
) -> tuple[str, list[Any]]:
    """Build a parameterized predicate. Unknown/legacy schemas stay readable.

    Do not filter by first-month: reused groups can span multiple months, and
    exact month membership belongs to Gold monthly aggregates.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if active_only and "is_active" in columns:
        clauses.append("is_active=true")
    if companies:
        normalized = sorted({str(c).strip().upper() for c in companies if str(c).strip()})
        aliases = sorted({v for c in normalized for v in _DASHBOARD_SOURCE_ALIASES.get(c, (c,))})
        company_parts: list[str] = []
        if "source_company" in columns:
            company_parts.append('upper(btrim("source_company"::text))=ANY(%s)')
            params.append(normalized)
        if "source_group" in columns:
            company_parts.append('upper(btrim("source_group"::text))=ANY(%s)')
            params.append(aliases)
        if company_parts:
            clauses.append("(" + " OR ".join(company_parts) + ")")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def resolve_dashboard_relation(table: str) -> tuple[str, str]:
    """Select a physical PostgreSQL snapshot only when explicitly enabled.

    Source views remain the truth; never fall back to raw Gold and never guess
    a snapshot name. The version is part of every SQL/Pandas cache identity.
    """
    default = (qname(table), f"gold:{table}")
    if os.getenv("DASHBOARD_READ_MODEL", "false").lower() not in ("1", "true", "yes"):
        return default
    allowed = {
        "reused_photo_group_summary_cleaned", "elhp_gt_group_summary_cleaned",
        "reused_photo_dashboard_monthly_cleaned", "elhp_dashboard_monthly_cleaned",
        "reused_photo_detail_transaction", "elhp_gt_detail_transaction",
    }
    if table not in allowed:
        return default
    try:
        row = read_one(
            """SELECT snapshot_name FROM dashboard_cache.active_snapshot
               WHERE source_name=%s""", [table]
        )
        snapshot = (row or {}).get("snapshot_name", "")
        if re.fullmatch(r"snapshot_[a-z0-9_]+", str(snapshot)):
            return (qname(snapshot, "dashboard_cache"), snapshot)
    except psycopg2.Error:
        # Read-only fallback for first install; no physical cache exists yet.
        pass
    return default


def read_table(
    table: str,
    limit: int | None = None,
    active_only: bool = True,
    *,
    source_companies: tuple[str, ...] | list[str] | None = None,
) -> pd.DataFrame:
    """Read Gold view, filtering company in PostgreSQL before materializing Pandas.

    The view's existing ownership / SAP-gating stays authoritative. Never
    substitute a base Gold relation for a cleaned view on missing rows.
    """
    if not table_exists(table):
        return pd.DataFrame()
    cols = set(get_columns(table))
    comps = tuple(sorted({str(c).strip().upper() for c in (source_companies or ()) if str(c).strip()}))
    where, params = _table_filter_sql(cols, comps, active_only)
    lim = "" if not limit or limit <= 0 else f" LIMIT {int(limit)}"
    relation, generation = resolve_dashboard_relation(table)
    cache_key = f"table:{table}:{generation}:{limit or 0}:active={int(active_only)}:company={','.join(comps)}"
    return read_df(f"SELECT * FROM {relation}{where}{lim}", params, cache_key=cache_key)


def _basename(value: str) -> str:
    s = str(value or "").strip().strip('"').strip("'").replace("\\", "/")
    if not s:
        return ""
    return Path(s).name


def _is_relation_storage_error(exc: Exception) -> bool:
    """Bedakan kerusakan relation/index dari satu payload gambar yang invalid."""
    message = str(exc or "").lower()
    markers = (
        "could not open file",
        "previous segment is only",
        "unexpected zero page",
        "invalid page in block",
        "invalid page header",
        "checksum verification failed",
        "could not read block",
    )
    return any(marker in message for marker in markers)


def _remember_image_storage_error(
    schema: str,
    table: str,
    exc: Exception,
    *,
    disable_relation: bool,
) -> None:
    relation = f"{schema}.{table}"
    message = " ".join(str(exc or "").split())
    if disable_relation:
        _unreadable_image_relations.add(relation)
    if relation not in _image_storage_warnings:
        _image_storage_warnings[relation] = message[:500]
        print(
            f"[IMAGE STORAGE WARN] {relation} tidak dapat dibaca; "
            "report dilanjutkan dengan fallback/placeholder. "
            f"detail={message[:300]}",
            flush=True,
        )


def get_image_storage_warnings() -> dict[str, str]:
    """Ringkasan relation gambar yang dilewati selama proses report."""
    return dict(_image_storage_warnings)


def _safe_image_read_one(
    schema: str,
    table: str,
    sql: str,
    params: list | tuple | None = None,
) -> dict[str, Any] | None:
    relation = f"{schema}.{table}"
    if relation in _unreadable_image_relations:
        return None
    try:
        return read_one(sql, params)
    except Exception as exc:
        # gambar_reused dapat mempunyai satu/lebih payload TOAST rusak sementara
        # ribuan key lain masih sehat. Jangan karantina seluruh hot store hanya
        # karena satu key transport gagal; request panen berikutnya harus tetap
        # boleh mencoba key-nya sendiri. Relation Silver/recovery legacy tetap
        # dikarantina penuh karena fallback ke sana memang tidak diizinkan.
        disable_relation = (
            _is_relation_storage_error(exc)
            and table != TABLE_BYTEA_RECOVERY
        )
        _remember_image_storage_error(
            schema,
            table,
            exc,
            disable_relation=disable_relation,
        )
        return None


def _is_known_corrupt_source(candidates: list[str]) -> bool:
    """Jangan membaca ulang BYTEA Silver yang sudah dikarantina sebagai korup."""
    if not table_exists(TABLE_BYTEA_EXCEPTION, RECOVERY_SCHEMA):
        return False
    upper_candidates = [value.upper() for value in candidates]
    row = _safe_image_read_one(
        RECOVERY_SCHEMA,
        TABLE_BYTEA_EXCEPTION,
        f"""
        SELECT 1 AS found
        FROM {qname(TABLE_BYTEA_EXCEPTION, RECOVERY_SCHEMA)}
        WHERE is_active=true
          AND (
                upper(file_uid)=ANY(%s)
                OR upper(coalesce(file_name, ''))=ANY(%s)
              )
        LIMIT 1
        """,
        (upper_candidates, upper_candidates),
    )
    return bool(row and row.get("found"))


def get_image_recovery_status(image_key: str) -> dict[str, Any] | None:
    """Cari metadata hot store reused tanpa membaca payload BYTEA.

    Dipakai oleh dashboard_diagnose.py untuk membuktikan sumber gambar. Fungsi
    ini tidak pernah membuka relation recovery v1 atau BYTEA Silver.
    """
    if not image_key:
        return None

    requested = str(image_key).strip()
    base = _basename(requested)
    candidates = []
    for value in [requested, requested.upper(), base, base.upper()]:
        if value and value not in candidates:
            candidates.append(value)
    upper_candidates = [value.upper() for value in candidates]

    # V110: targeted repair overlay selalu menang. Ini membuat beberapa image DWT
    # yang rusak dapat dipulihkan tanpa menyalin ulang seluruh hot store.
    for table in [TABLE_BYTEA_TARGETED_RECOVERY, TABLE_BYTEA_RECOVERY]:
        exists = table_exists_uncached(table, RECOVERY_SCHEMA) if table == TABLE_BYTEA_TARGETED_RECOVERY else table_exists(table, RECOVERY_SCHEMA)
        if not exists:
            continue
        row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            table,
            f"""
            SELECT source_group, file_uid, file_name, bytea_len,
                   recovery_source, match_method, last_verified_at
            FROM {qname(table, RECOVERY_SCHEMA)}
            WHERE is_active=true
              AND (
                    upper(file_uid)=ANY(%s)
                    OR upper(file_name)=ANY(%s)
                  )
            ORDER BY last_verified_at DESC
            LIMIT 1
            """,
            (upper_candidates, upper_candidates),
        )
        if row:
            return row
    return None


def _fetch_silver_image_bytes(
    candidates: list[str],
    requested: str,
    base: str,
):
    """Baca tepat satu payload corpus Silver melalui key berindeks."""
    sources = [TABLE_IMAGE_STORE, TABLE_IMAGE_VIEW, TABLE_IMAGE_ALL]
    for table in dict.fromkeys(sources):
        if not table_exists(table, IMAGE_SCHEMA):
            continue
        cols = set(get_columns(table, IMAGE_SCHEMA))
        byte_col = next(
            (c for c in ["image_bytes", "bytes", "file_bytes"] if c in cols),
            None,
        )
        if not byte_col:
            continue

        mime_col = "mime_type" if "mime_type" in cols else None
        fname_col = (
            "file_name"
            if "file_name" in cols
            else "image_key"
            if "image_key" in cols
            else "file_uid"
            if "file_uid" in cols
            else None
        )
        key_cols = [
            c
            for c in ["image_key", "file_name", "file_name_upper", "file_uid"]
            if c in cols
        ]
        if not key_cols:
            continue

        where_parts = []
        params = []
        upper_candidates = [value.upper() for value in candidates]
        for column in key_cols:
            where_parts.append(f"upper(CAST({ident(column)} AS TEXT)) = ANY(%s)")
            params.append(upper_candidates)

        sql = f"""
        SELECT
            {ident(byte_col)} AS image_bytes,
            {ident(mime_col) if mime_col else "NULL"} AS mime_type,
            {ident(fname_col) if fname_col else "NULL"} AS file_name
        FROM {qname(table, IMAGE_SCHEMA)}
        WHERE {ident(byte_col)} IS NOT NULL
          AND ({' OR '.join(where_parts)})
        LIMIT 1
        """
        row = _safe_image_read_one(IMAGE_SCHEMA, table, sql, params)
        if row and row.get("image_bytes") is not None:
            return (
                row["image_bytes"],
                row.get("mime_type") or "image/jpeg",
                row.get("file_name") or base or requested,
            )
    return None


def _read_nas_image(
    path_value: Any,
    fallback_name: str,
    expected_sha256: str = "",
):
    """Baca satu file exact yang path-nya berasal dari metadata database.

    URL tidak pernah diterjemahkan menjadi filesystem path. Dengan demikian
    request dashboard tidak dapat dipakai untuk membaca path sembarang.
    """
    raw_path = str(path_value or "").strip().strip('"').strip("'")
    if not raw_path:
        return None
    try:
        path = Path(raw_path)
        if path.is_dir() and fallback_name:
            path = path / _basename(fallback_name)
        if not path.is_file():
            return None
        size = int(path.stat().st_size)
        if size <= 0 or size > NAS_IMAGE_MAX_BYTES:
            return None
        payload = path.read_bytes()
        if not payload or len(payload) != size:
            return None
        expected = str(expected_sha256 or "").strip().lower()
        if expected and hashlib.sha256(payload).hexdigest().lower() != expected:
            return None
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return payload, mime_type, path.name or fallback_name
    except (OSError, ValueError):
        # NAS putus/tidak termount bukan error fatal; hot-store tetap dicoba.
        return None


def _fetch_nas_image_bytes(
    candidates: list[str],
    requested: str,
    base: str,
):
    """Resolve exact NAS path tanpa scan folder, lalu baca file on-demand."""
    candidates = list(candidates)

    # Halaman transaksi mengirim panen_file_uid, sedangkan Silver dapat pernah
    # direload dengan UID berbeda akibat perubahan source_root. Embedding yang
    # menghasilkan skor tetap menyimpan filename canonical. Resolve UID ke
    # filename dahulu agar NAS masih dapat ditemukan tanpa scan folder.
    feature_source_group = ""
    feature_expected_sha256 = ""
    feature_is_transport = False
    if table_exists("image_feature", RECOVERY_SCHEMA):
        original_upper = [value.upper() for value in candidates]
        feature_row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            "image_feature",
            f"""
            SELECT file_name, source_group, file_sha256, is_transport
            FROM {qname("image_feature", RECOVERY_SCHEMA)}
            WHERE (
                    upper(file_uid)=ANY(%s)
                    OR upper(file_name)=ANY(%s)
                  )
              AND nullif(trim(coalesce(file_name, '')), '') IS NOT NULL
            ORDER BY
                CASE WHEN upper(file_uid)=ANY(%s) THEN 0 ELSE 1 END,
                is_valid DESC,
                embedded_at DESC
            LIMIT 1
            """,
            (original_upper, original_upper, original_upper),
        )
        feature_name = str((feature_row or {}).get("file_name") or "").strip()
        feature_source_group = str(
            (feature_row or {}).get("source_group") or ""
        ).upper().strip()
        feature_expected_sha256 = str(
            (feature_row or {}).get("file_sha256") or ""
        ).lower().strip()
        feature_is_transport = bool(
            (feature_row or {}).get("is_transport") or False
        )
        feature_base = _basename(feature_name)
        for value in (feature_name, feature_name.upper(), feature_base, feature_base.upper()):
            if value and value not in candidates:
                candidates.append(value)
        if feature_base:
            base = feature_base
    upper_candidates = [value.upper() for value in candidates]

    # Registry metadata-only untuk seluruh foto panen, termasuk non-reused.
    # Dibangun satu kali oleh ``index-panen-nas``; dashboard tidak scan NAS.
    if (
        not feature_is_transport
        and feature_source_group
        and table_exists(TABLE_PANEN_NAS_INDEX, RECOVERY_SCHEMA)
    ):
        row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            TABLE_PANEN_NAS_INDEX,
            f"""
            SELECT nas_path, file_name, expected_sha256
            FROM {qname(TABLE_PANEN_NAS_INDEX, RECOVERY_SCHEMA)}
            WHERE is_active=true
              AND source_group=%s
              AND file_name_upper=ANY(%s)
            ORDER BY period_year DESC, indexed_at DESC
            LIMIT 1
            """,
            (feature_source_group, upper_candidates),
        )
        direct = _read_nas_image(
            row.get("nas_path") if row else None,
            str((row or {}).get("file_name") or base or requested),
            feature_expected_sha256
            or str((row or {}).get("expected_sha256") or ""),
        )
        if direct:
            return direct

    # KT Before sudah menyimpan path exact hasil satu kali index NAS saat sync.
    if table_exists(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA):
        row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            TABLE_TRANSPORT_KT_CONTEXT,
            f"""
            SELECT nas_path, evidence_file_name AS file_name
            FROM {qname(TABLE_TRANSPORT_KT_CONTEXT, RECOVERY_SCHEMA)}
            WHERE is_active=true
              AND load_status='READY'
              AND nullif(trim(coalesce(nas_path, '')), '') IS NOT NULL
              AND (
                    upper(file_uid)=ANY(%s)
                    OR upper(evidence_file_name)=ANY(%s)
                  )
            ORDER BY
                CASE WHEN upper(file_uid)=ANY(%s) THEN 0 ELSE 1 END,
                synced_at DESC
            LIMIT 1
            """,
            (upper_candidates, upper_candidates, upper_candidates),
        )
        direct = _read_nas_image(
            row.get("nas_path") if row else None,
            str((row or {}).get("file_name") or base or requested),
            feature_expected_sha256,
        )
        if direct:
            return direct

    # Foto panen memakai file_path exact hasil index/load metadata Silver.
    # BYTEA Silver tidak dibaca pada jalur ini.
    sources = [TABLE_IMAGE_STORE, TABLE_IMAGE_VIEW, TABLE_IMAGE_ALL]
    for table in dict.fromkeys(sources):
        if not table_exists(table, IMAGE_SCHEMA):
            continue
        cols = set(get_columns(table, IMAGE_SCHEMA))
        path_col = next(
            (column for column in ("file_path", "source_path", "nas_path") if column in cols),
            None,
        )
        if not path_col:
            continue
        fname_col = next(
            (column for column in ("file_name", "image_key", "file_uid") if column in cols),
            None,
        )
        key_cols = [
            column
            for column in ("file_uid", "file_name", "file_name_upper", "image_key")
            if column in cols
        ]
        if not key_cols:
            continue
        where_parts: list[str] = []
        params: list[Any] = []
        for column in key_cols:
            where_parts.append(
                f"upper(CAST({ident(column)} AS TEXT))=ANY(%s)"
            )
            params.append(upper_candidates)
        row = _safe_image_read_one(
            IMAGE_SCHEMA,
            table,
            f"""
            SELECT {ident(path_col)} AS nas_path,
                   {ident(fname_col) if fname_col else 'NULL'} AS file_name,
                   {ident('source_root') if 'source_root' in cols else 'NULL'} AS source_root,
                   {ident('relative_path') if 'relative_path' in cols else 'NULL'} AS relative_path
            FROM {qname(table, IMAGE_SCHEMA)}
            WHERE nullif(trim(CAST({ident(path_col)} AS TEXT)), '') IS NOT NULL
              AND ({' OR '.join(where_parts)})
            LIMIT 1
            """,
            params,
        )
        direct = _read_nas_image(
            row.get("nas_path") if row else None,
            str((row or {}).get("file_name") or base or requested),
            feature_expected_sha256,
        )
        if direct:
            return direct
        if row and row.get("source_root") and row.get("relative_path"):
            direct = _read_nas_image(
                Path(str(row["source_root"])) / str(row["relative_path"]),
                str(row.get("file_name") or base or requested),
                feature_expected_sha256,
            )
            if direct:
                return direct
    return None


def fetch_image_bytes(image_key: str):
    """Ambil gambar NAS-first, lalu fallback ke hot-store canonical."""
    if not image_key:
        return None

    requested = str(image_key).strip()
    base = _basename(requested)
    candidates = []
    for x in [requested, requested.upper(), base, base.upper()]:
        if x and x not in candidates:
            candidates.append(x)

    if NAS_IMAGE_FIRST:
        nas_image = _fetch_nas_image_bytes(candidates, requested, base)
        if nas_image:
            return nas_image

    # V110 targeted overlay: beberapa evidence (mis. DWT/Dewata) yang rusak
    # dipulihkan ke relation kecil dan sehat. Cek ini SEBELUM hot store utama
    # agar page TOAST korup di processing.gambar_reused tidak tersentuh untuk key itu.
    if table_exists_uncached(TABLE_BYTEA_TARGETED_RECOVERY, RECOVERY_SCHEMA):
        upper_candidates = [x.upper() for x in candidates]
        targeted_row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            TABLE_BYTEA_TARGETED_RECOVERY,
            f"""
            SELECT image_bytes, mime_type, file_name
            FROM {qname(TABLE_BYTEA_TARGETED_RECOVERY, RECOVERY_SCHEMA)}
            WHERE is_active=true
              AND (
                    upper(file_uid)=ANY(%s)
                    OR upper(file_name)=ANY(%s)
                  )
            ORDER BY last_verified_at DESC
            LIMIT 1
            """,
            (upper_candidates, upper_candidates),
        )
        if targeted_row and targeted_row.get("image_bytes") is not None:
            return (
                targeted_row["image_bytes"],
                targeted_row.get("mime_type") or "image/jpeg",
                targeted_row.get("file_name") or base or requested,
            )

    # Fallback saat NAS tidak termount/putus. Dashboard tetap tidak memindai
    # folder: lookup NAS di atas memakai exact path dari metadata database.
    if table_exists(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA):
        upper_candidates = [x.upper() for x in candidates]
        recovery_row = _safe_image_read_one(
            RECOVERY_SCHEMA,
            TABLE_BYTEA_RECOVERY,
            f"""
            SELECT image_bytes, mime_type, file_name
            FROM {qname(TABLE_BYTEA_RECOVERY, RECOVERY_SCHEMA)}
            WHERE is_active=true
              AND (
                    upper(file_uid)=ANY(%s)
                    OR upper(file_name)=ANY(%s)
                  )
            ORDER BY last_verified_at DESC
            LIMIT 1
            """,
            (upper_candidates, upper_candidates),
        )
        if recovery_row and recovery_row.get("image_bytes") is not None:
            return (
                recovery_row["image_bytes"],
                recovery_row.get("mime_type") or "image/jpeg",
                recovery_row.get("file_name") or base or requested,
            )

    # Native dashboard default: HOT FIRST. If Gold hot evidence is absent,
    # still resolve an exact indexed NAS path; never enumerate NAS folders.
    # This preserves image availability while avoiding avoidable SMB round trips
    # for active reused-photo evidence already cached in PostgreSQL.
    if not NAS_IMAGE_FIRST:
        nas_image = _fetch_nas_image_bytes(candidates, requested, base)
        if nas_image:
            return nas_image

    # Dashboard/PDF produksi hanya membaca hot store gambar_reused. Fallback
    # Silver default dimatikan karena tabelnya sangat besar dan sebagian TOAST
    # telah terbukti rusak. Missing hot row menjadi "Gambar perlu recovery".
    if not ALLOW_SILVER_IMAGE_FALLBACK:
        return None

    # File yang sudah terbukti korup hanya boleh dibaca dari overlay recovery.
    # Jika overlay belum sehat/tersedia, kembalikan None agar PDF memakai
    # placeholder dan proses keseluruhan tetap selesai.
    if _is_known_corrupt_source(candidates):
        return None

    return _fetch_silver_image_bytes(candidates, requested, base)


def fetch_corpus_image_bytes(image_key: str):
    """Ambil satu foto seluruh corpus untuk modul KP-vs-KT.

    Modul Panen-vs-Transport wajib mencoba registry NAS, termasuk ketika .env
    lama masih mempunyai NAS_IMAGE_FIRST=false. Flag tersebut hanya mengatur
    urutan pembacaan dashboard reused umum; foto corpus non-reused tidak boleh
    bergantung pada keberadaan BYTEA di hot store/Silver.
    """
    if not image_key:
        return None
    requested = str(image_key).strip()
    base = _basename(requested)
    candidates: list[str] = []
    for value in [requested, requested.upper(), base, base.upper()]:
        if value and value not in candidates:
            candidates.append(value)

    # Jika NAS_IMAGE_FIRST=true, fetch_image_bytes sudah mencoba registry NAS
    # sebelum hot store. Jika flag lama false, lakukan lookup NAS secara wajib
    # di sini agar /api/corpus-thumb tetap dapat menampilkan panen non-reused.
    if NAS_IMAGE_FIRST:
        hot = fetch_image_bytes(image_key)
        if hot:
            return hot
    else:
        nas_image = _fetch_nas_image_bytes(candidates, requested, base)
        if nas_image:
            return nas_image
        hot = fetch_image_bytes(image_key)
        if hot:
            return hot

    if _is_known_corrupt_source(candidates):
        return None
    return _fetch_silver_image_bytes(candidates, requested, base)
