from __future__ import annotations

import calendar
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import execute_values

from .config import Settings
from .db import ident, qname


# DB60 SAP harvesting source previously used by the project.
# Keep credentials exclusively in .env / environment variables.
@dataclass(frozen=True)
class Db60Config:
    host: str = os.getenv("DB60_ZPAY_SERVER", os.getenv("VALID_OPH_SQL_SERVER", ""))
    port: int = int(os.getenv("DB60_ZPAY_PORT", os.getenv("VALID_OPH_SQL_PORT", "1433")))
    database: str = os.getenv("DB60_ZPAY_DATABASE", os.getenv("VALID_OPH_SQL_DATABASE", "db_audit"))
    user: str = os.getenv("DB60_ZPAY_USER", os.getenv("VALID_OPH_SQL_USER", "audit"))
    password: str = os.getenv("DB60_ZPAY_PASSWORD", os.getenv("VALID_OPH_SQL_PASSWORD", os.getenv("DB60_PASSWORD", "")))
    source_table: str = os.getenv("DB60_ZPAY_TABLE", "db_audit.bronze.v_zpay_harvesting")
    driver: str = os.getenv("DB60_ZPAY_DRIVER", "mssql-python").strip().lower()


TARGET_SCHEMA = os.getenv("DB60_ZPAY_TARGET_SCHEMA", "silver")
TARGET_TABLE = os.getenv("DB60_ZPAY_TARGET_TABLE", "zpay_harvesting_db60")
LIVE_SCHEMA = os.getenv("SAP_KPI_SCHEMA", "silver")
LIVE_TABLE = os.getenv("SAP_KPI_TABLE", "v_zpay_harvesting_live")

TARGET_COLUMNS = [
    "mandt", "prfnr", "empnr", "budat", "posnr", "posnrt", "ename", "jbcde",
    "jbtyp", "estnr", "divnr", "phase", "bukrs", "block", "banjir", "bukit",
    "check_oer", "yplanp", "werks", "xblnr", "empnr_m", "ename_m", "empnr_k",
    "ename_k", "zmasak", "zkmasak", "zbusuk", "zhitam", "zabnormal", "deliv",
    "bunch_total", "bunch_npay", "bunch_pay", "ztpanjang", "stlfr", "lstfr",
    "brdtph", "spb", "lfbag", "aquan", "meins", "nweig", "kunnr", "oph", "tph",
    "methd", "wrtof", "hkbsc", "hkalw", "ztonase", "ernam", "erdat", "erzet",
    "aenam", "aedat", "aezet", "wrdat", "wrnam", "generated_at", "id", "zbrondolan",
]

POSTGRES_COLUMN_TYPES = {
    "mandt": "VARCHAR(24)", "prfnr": "VARCHAR(30)", "empnr": "VARCHAR(30)",
    "budat": "VARCHAR(24)", "posnr": "VARCHAR(24)", "posnrt": "VARCHAR(24)",
    "ename": "VARCHAR(50)", "jbcde": "VARCHAR(30)", "jbtyp": "VARCHAR(24)",
    "estnr": "VARCHAR(24)", "divnr": "VARCHAR(24)", "phase": "VARCHAR(24)",
    "bukrs": "VARCHAR(24)", "block": "VARCHAR(24)", "banjir": "VARCHAR(24)",
    "bukit": "VARCHAR(24)", "check_oer": "VARCHAR(24)", "yplanp": "VARCHAR(24)",
    "werks": "VARCHAR(24)", "xblnr": "VARCHAR(24)", "empnr_m": "VARCHAR(30)",
    "ename_m": "VARCHAR(50)", "empnr_k": "VARCHAR(30)", "ename_k": "VARCHAR(50)",
    "zmasak": "DOUBLE PRECISION", "zkmasak": "DOUBLE PRECISION", "zbusuk": "DOUBLE PRECISION",
    "zhitam": "DOUBLE PRECISION", "zabnormal": "DOUBLE PRECISION", "deliv": "DOUBLE PRECISION",
    "bunch_total": "DOUBLE PRECISION", "bunch_npay": "DOUBLE PRECISION", "bunch_pay": "DOUBLE PRECISION",
    "ztpanjang": "DOUBLE PRECISION", "stlfr": "DOUBLE PRECISION", "lstfr": "DOUBLE PRECISION",
    "brdtph": "DOUBLE PRECISION", "spb": "VARCHAR(25)", "lfbag": "DOUBLE PRECISION",
    "aquan": "DOUBLE PRECISION", "meins": "VARCHAR(24)", "nweig": "DOUBLE PRECISION",
    "kunnr": "VARCHAR(24)", "oph": "VARCHAR(25)", "tph": "VARCHAR(24)",
    "methd": "VARCHAR(24)", "wrtof": "VARCHAR(24)", "hkbsc": "DOUBLE PRECISION",
    "hkalw": "DOUBLE PRECISION", "ztonase": "DOUBLE PRECISION", "ernam": "VARCHAR(24)",
    "erdat": "VARCHAR(24)", "erzet": "VARCHAR(24)", "aenam": "VARCHAR(24)",
    "aedat": "VARCHAR(24)", "aezet": "VARCHAR(24)", "wrdat": "VARCHAR(24)",
    "wrnam": "VARCHAR(24)", "generated_at": "TIMESTAMP", "id": "VARCHAR(128)",
    "zbrondolan": "DOUBLE PRECISION",
}

SELECT_COLUMNS_SQL = """
    CAST(MANDT AS NVARCHAR(MAX)) AS mandt,
    CAST(PRFNR AS NVARCHAR(MAX)) AS prfnr,
    CAST(EMPNR AS NVARCHAR(MAX)) AS empnr,
    CAST(BUDAT AS NVARCHAR(MAX)) AS budat,
    CAST(POSNR AS NVARCHAR(MAX)) AS posnr,
    CAST(POSNRT AS NVARCHAR(MAX)) AS posnrt,
    CAST(ENAME AS NVARCHAR(MAX)) AS ename,
    CAST(JBCDE AS NVARCHAR(MAX)) AS jbcde,
    CAST(JBTYP AS NVARCHAR(MAX)) AS jbtyp,
    CAST(ESTNR AS NVARCHAR(MAX)) AS estnr,
    CAST(DIVNR AS NVARCHAR(MAX)) AS divnr,
    CAST(PHASE AS NVARCHAR(MAX)) AS phase,
    CAST(BUKRS AS NVARCHAR(MAX)) AS bukrs,
    CAST(BLOCK AS NVARCHAR(MAX)) AS block,
    CAST(BANJIR AS NVARCHAR(MAX)) AS banjir,
    CAST(BUKIT AS NVARCHAR(MAX)) AS bukit,
    CAST(CHECK_OER AS NVARCHAR(MAX)) AS check_oer,
    CAST(YPLANP AS NVARCHAR(MAX)) AS yplanp,
    CAST(WERKS AS NVARCHAR(MAX)) AS werks,
    CAST(XBLNR AS NVARCHAR(MAX)) AS xblnr,
    CAST(EMPNR_M AS NVARCHAR(MAX)) AS empnr_m,
    CAST(ENAME_M AS NVARCHAR(MAX)) AS ename_m,
    CAST(EMPNR_K AS NVARCHAR(MAX)) AS empnr_k,
    CAST(ENAME_K AS NVARCHAR(MAX)) AS ename_k,
    TRY_CONVERT(FLOAT, ZMASAK) AS zmasak,
    TRY_CONVERT(FLOAT, ZKMASAK) AS zkmasak,
    TRY_CONVERT(FLOAT, ZBUSUK) AS zbusuk,
    TRY_CONVERT(FLOAT, ZHITAM) AS zhitam,
    TRY_CONVERT(FLOAT, ZABNORMAL) AS zabnormal,
    TRY_CONVERT(FLOAT, DELIV) AS deliv,
    TRY_CONVERT(FLOAT, BUNCH_TOTAL) AS bunch_total,
    TRY_CONVERT(FLOAT, BUNCH_NPAY) AS bunch_npay,
    TRY_CONVERT(FLOAT, BUNCH_PAY) AS bunch_pay,
    TRY_CONVERT(FLOAT, ZTPANJANG) AS ztpanjang,
    TRY_CONVERT(FLOAT, STLFR) AS stlfr,
    TRY_CONVERT(FLOAT, LSTFR) AS lstfr,
    TRY_CONVERT(FLOAT, BRDTPH) AS brdtph,
    CAST(SPB AS NVARCHAR(MAX)) AS spb,
    TRY_CONVERT(FLOAT, LFBAG) AS lfbag,
    TRY_CONVERT(FLOAT, AQUAN) AS aquan,
    CAST(MEINS AS NVARCHAR(MAX)) AS meins,
    TRY_CONVERT(FLOAT, NWEIG) AS nweig,
    CAST(KUNNR AS NVARCHAR(MAX)) AS kunnr,
    CAST(OPH AS NVARCHAR(MAX)) AS oph,
    CAST(TPH AS NVARCHAR(MAX)) AS tph,
    CAST(METHD AS NVARCHAR(MAX)) AS methd,
    CAST(WRTOF AS NVARCHAR(MAX)) AS wrtof,
    TRY_CONVERT(FLOAT, HKBSC) AS hkbsc,
    TRY_CONVERT(FLOAT, HKALW) AS hkalw,
    TRY_CONVERT(FLOAT, ZTONASE) AS ztonase,
    CAST(ERNAM AS NVARCHAR(MAX)) AS ernam,
    CAST(ERDAT AS NVARCHAR(MAX)) AS erdat,
    CAST(ERZET AS NVARCHAR(MAX)) AS erzet,
    CAST(AENAM AS NVARCHAR(MAX)) AS aenam,
    CAST(AEDAT AS NVARCHAR(MAX)) AS aedat,
    CAST(AEZET AS NVARCHAR(MAX)) AS aezet,
    CAST(WRDAT AS NVARCHAR(MAX)) AS wrdat,
    CAST(WRNAM AS NVARCHAR(MAX)) AS wrnam,
    TRY_CONVERT(DATETIME2, GENERATED_AT) AS generated_at,
    CAST(ID AS NVARCHAR(MAX)) AS id,
    TRY_CONVERT(FLOAT, ZBRONDOLAN) AS zbrondolan
"""

_DATE_EXPR = "COALESCE(TRY_CONVERT(date, CAST(BUDAT AS VARCHAR(32)), 112), TRY_CONVERT(date, CAST(BUDAT AS VARCHAR(32))))"
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9_\.\[\]]+$")


def _month_start(value: str) -> date:
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(value or "")):
        raise ValueError("Bulan harus YYYY-MM, contoh 2026-08")
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def _month_text(value: date) -> str:
    return value.strftime("%Y-%m")


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _month_range(start_month: str, end_month: str) -> list[str]:
    start = _month_start(start_month)
    end = _month_start(end_month)
    if start > end:
        return []
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(_month_text(cur))
        cur = _next_month(cur)
    return out


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _scope_values(settings: Settings, source_group: str) -> tuple[str, ...]:
    source = str(source_group or "ALL").upper().strip()
    if source == "ALL":
        # Ingestion ALL means a true copy of all DB60 ZPAY rows in the date scope.
        # Company/estate filtering belongs downstream in SAP KPI, not in ingestion.
        return tuple()
    if source not in {"PWP", "DIN", "DAN", "DWT"}:
        raise ValueError("--source-group harus PWP, DIN, DAN, DWT, atau ALL")
    return settings.sap_estate_scope_values(source)


def _scope_sql(settings: Settings, source_group: str) -> str:
    if str(source_group or "ALL").upper().strip() == "ALL":
        return "1 = 1"
    parts: list[str] = []
    for value in _scope_values(settings, source_group):
        clean = str(value).upper().strip()
        if not clean:
            continue
        if "*" in clean:
            pattern = clean.replace("%", "[%]").replace("_", "[_]").replace("*", "%")
            parts.append(f"UPPER(LTRIM(RTRIM(CAST(PRFNR AS NVARCHAR(MAX))))) LIKE {_sql_quote(pattern)}")
        else:
            parts.append(f"UPPER(LTRIM(RTRIM(CAST(PRFNR AS NVARCHAR(MAX))))) = {_sql_quote(clean)}")
    if not parts:
        raise RuntimeError(f"SAP scope kosong untuk {source_group}")
    return "(" + " OR ".join(parts) + ")"


def _mssql_brace_value(value: str) -> str:
    return "{" + str(value or "").replace("}", "}}") + "}"


def connect_db60(cfg: Db60Config):
    if not cfg.password:
        raise RuntimeError(
            "DB60_ZPAY_PASSWORD belum diisi. Isi di .env; jangan simpan password di source code."
        )
    if not _SAFE_SOURCE.fullmatch(cfg.source_table):
        raise ValueError(f"DB60_ZPAY_TABLE tidak aman: {cfg.source_table!r}")

    if cfg.driver in {"mssql-python", "mssql_python"}:
        try:
            from mssql_python import connect  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Package mssql-python belum terinstall. Jalankan: py -m pip install mssql-python"
            ) from exc
        conn_str = (
            f"Server={cfg.host},{cfg.port};Database={cfg.database};"
            f"UID={_mssql_brace_value(cfg.user)};PWD={_mssql_brace_value(cfg.password)};"
            "Encrypt=no;TrustServerCertificate=yes;"
        )
        return connect(conn_str)

    if cfg.driver == "pyodbc":
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pyodbc belum terinstall") from exc
        driver = os.getenv("DB60_ZPAY_ODBC_DRIVER", os.getenv("MSSQL_DRIVER", "SQL Server"))
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={cfg.host},{cfg.port};DATABASE={cfg.database};"
            f"UID={cfg.user};PWD={cfg.password};TrustServerCertificate=yes;Encrypt=no;Connection Timeout=30;"
        )
        return pyodbc.connect(conn_str, timeout=30)

    if cfg.driver == "pymssql":
        try:
            import pymssql  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pymssql belum terinstall") from exc
        return pymssql.connect(
            server=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
            database=cfg.database, login_timeout=30, timeout=180, charset="UTF-8", as_dict=False,
        )
    raise ValueError("DB60_ZPAY_DRIVER harus mssql-python, pyodbc, atau pymssql")


def _pg_connect(settings: Settings):
    return psycopg2.connect(**settings.connection_kwargs())


def ensure_target_table(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(TARGET_SCHEMA)}")
        defs = []
        for col in TARGET_COLUMNS:
            suffix = " NOT NULL" if col == "id" else ""
            defs.append(f"{ident(col)} {POSTGRES_COLUMN_TYPES[col]}{suffix}")
        defs.append("migrated_at timestamptz NOT NULL DEFAULT now()")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {qname(TARGET_SCHEMA, TARGET_TABLE)} ({', '.join(defs)})"
        )
        # Existing historical tables may be missing newer columns. Add only; never drop.
        for col in TARGET_COLUMNS:
            cur.execute(
                f"ALTER TABLE {qname(TARGET_SCHEMA, TARGET_TABLE)} ADD COLUMN IF NOT EXISTS {ident(col)} {POSTGRES_COLUMN_TYPES[col]}"
            )
        cur.execute(
            f"ALTER TABLE {qname(TARGET_SCHEMA, TARGET_TABLE)} ADD COLUMN IF NOT EXISTS migrated_at timestamptz NOT NULL DEFAULT now()"
        )
        # ON CONFLICT requires a unique key. Old migration already used ID as PK; this keeps legacy DBs safe.
        cur.execute(
            f"SELECT count(*) FROM (SELECT id FROM {qname(TARGET_SCHEMA, TARGET_TABLE)} "
            "WHERE id IS NOT NULL GROUP BY id HAVING count(*)>1 LIMIT 1) d"
        )
        if int(cur.fetchone()[0]) > 0:
            raise RuntimeError(
                f"{TARGET_SCHEMA}.{TARGET_TABLE} mempunyai duplicate ID; tidak aman membuat UPSERT key."
            )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{TARGET_TABLE}_id ON {qname(TARGET_SCHEMA, TARGET_TABLE)} (id)"
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{TARGET_TABLE}_budat ON {qname(TARGET_SCHEMA, TARGET_TABLE)} (budat)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{TARGET_TABLE}_prfnr ON {qname(TARGET_SCHEMA, TARGET_TABLE)} (prfnr)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{TARGET_TABLE}_oph ON {qname(TARGET_SCHEMA, TARGET_TABLE)} (oph)")
    pg_conn.commit()


def ensure_live_view(pg_conn) -> None:
    """Create/refresh the SAP live view without changing its public column types.

    Historical Golden Eye installations exposed ``migrated_at`` as
    ``timestamp without time zone`` while newer DB60 storage uses
    ``timestamptz`` internally. PostgreSQL does not allow CREATE OR REPLACE
    VIEW to silently change a column type, so preserve the existing live-view
    signature when it already exists.
    """
    with pg_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(LIVE_SCHEMA)}")

        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND column_name = 'migrated_at'
            LIMIT 1
            """,
            (LIVE_SCHEMA, LIVE_TABLE),
        )
        row = cur.fetchone()
        existing_type = str(row[0]).lower() if row and row[0] else ""

        if existing_type == "timestamp with time zone":
            migrated_expr = "migrated_at::timestamptz AS migrated_at"
        else:
            # Backward-compatible public contract. This is also the default
            # for a fresh live view so downstream dashboards stay consistent
            # with the historical Golden Eye schema.
            migrated_expr = "migrated_at::timestamp without time zone AS migrated_at"

        cur.execute(
            f"""
            CREATE OR REPLACE VIEW {qname(LIVE_SCHEMA, LIVE_TABLE)} AS
            SELECT
                prfnr, budat, oph, bukrs, tph, estnr, block,
                NULL::text AS estate, NULL::text AS block_lapangan,
                id, {migrated_expr}
            FROM {qname(TARGET_SCHEMA, TARGET_TABLE)}
            WHERE nullif(btrim(oph), '') IS NOT NULL
              AND nullif(btrim(prfnr), '') IS NOT NULL
              AND nullif(btrim(budat), '') IS NOT NULL
            """
        )
    pg_conn.commit()


def _normalize_row(row: Any) -> tuple[Any, ...]:
    try:
        return tuple(row[i] for i in range(len(TARGET_COLUMNS)))
    except Exception as exc:
        raise RuntimeError(f"Gagal membaca row DB60 tipe={type(row)}") from exc


def _dedup_rows(rows: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    id_idx = TARGET_COLUMNS.index("id")
    by_id: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        rid = row[id_idx]
        if rid is None or not str(rid).strip():
            continue
        rid = str(rid).strip()
        row = row[:id_idx] + (rid,) + row[id_idx + 1:]
        by_id[rid] = row
    return list(by_id.values())


def _insert_new_batch(pg_conn, rows: Sequence[tuple[Any, ...]]) -> tuple[int, int]:
    """Append DB60 rows into Silver without rewriting existing IDs.

    Returns (inserted_new, skipped_existing_or_duplicate).
    """
    rows = _dedup_rows(rows)
    if not rows:
        return 0, 0
    columns_sql = ", ".join(ident(c) for c in TARGET_COLUMNS)
    sql = f"""
        INSERT INTO {qname(TARGET_SCHEMA, TARGET_TABLE)} ({columns_sql})
        VALUES %s
        ON CONFLICT (id) DO NOTHING
        RETURNING id
    """
    with pg_conn.cursor() as cur:
        inserted = execute_values(
            cur, sql, rows, page_size=min(5000, len(rows)), fetch=True
        )
    pg_conn.commit()
    inserted_new = len(inserted or [])
    return inserted_new, max(0, len(rows) - inserted_new)


def _local_max_budat(pg_conn, settings: Settings, source_group: str) -> date | None:
    scope = _scope_values(settings, source_group)
    exact = [v for v in scope if "*" not in v]
    prefixes = [v[:-1] for v in scope if v.endswith("*")]
    clauses: list[str] = []
    params: list[Any] = []
    if exact:
        clauses.append("upper(btrim(prfnr))=ANY(%s)")
        params.append([v.upper() for v in exact])
    for prefix in prefixes:
        clauses.append("upper(btrim(prfnr)) LIKE %s")
        params.append(prefix.upper() + "%")
    where = " OR ".join(clauses) if clauses else "true"
    with pg_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT max(
                CASE
                    WHEN btrim(budat) ~ '^\\d{{8}}$' THEN to_date(btrim(budat), 'YYYYMMDD')
                    WHEN btrim(budat) ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN left(btrim(budat),10)::date
                    ELSE NULL
                END
            )
            FROM {qname(TARGET_SCHEMA, TARGET_TABLE)}
            WHERE ({where})
            """,
            tuple(params),
        )
        return cur.fetchone()[0]


def get_local_max_budat(settings: Settings, source_group: str = "ALL") -> date | None:
    try:
        with _pg_connect(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"{TARGET_SCHEMA}.{TARGET_TABLE}",))
                if cur.fetchone()[0] is None:
                    return None
            return _local_max_budat(conn, settings, source_group)
    except Exception:
        return None


def _remote_max_budat(conn, cfg: Db60Config, settings: Settings, source_group: str) -> date | None:
    scope_sql = _scope_sql(settings, source_group)
    sql = f"SELECT MAX({_DATE_EXPR}) FROM {cfg.source_table} WHERE {_DATE_EXPR} IS NOT NULL AND {scope_sql}"
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    value = row[0] if row else None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def get_remote_max_budat(settings: Settings, source_group: str = "ALL") -> date | None:
    cfg = Db60Config()
    conn = connect_db60(cfg)
    try:
        return _remote_max_budat(conn, cfg, settings, source_group)
    finally:
        conn.close()


def _month_where(settings: Settings, source_group: str, month: str) -> str:
    first = _month_start(month)
    last_day = calendar.monthrange(first.year, first.month)[1]
    last = date(first.year, first.month, last_day)
    return (
        f"{_DATE_EXPR} >= {_sql_quote(first.isoformat())} AND "
        f"{_DATE_EXPR} <= {_sql_quote(last.isoformat())} AND "
        f"{_scope_sql(settings, source_group)}"
    )


def _count_remote_month(conn, cfg: Db60Config, settings: Settings, source_group: str, month: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(1) FROM {cfg.source_table} WHERE {_month_where(settings, source_group, month)}")
    row = cur.fetchone()
    cur.close()
    return int(row[0] if row else 0)


def _open_remote_month(conn, cfg: Db60Config, settings: Settings, source_group: str, month: str):
    cur = conn.cursor()
    cur.execute(
        f"SELECT {SELECT_COLUMNS_SQL} FROM {cfg.source_table} WHERE {_month_where(settings, source_group, month)}"
    )
    return cur


def _count_local_month(pg_conn, settings: Settings, source_group: str, month: str) -> int:
    scope = _scope_values(settings, source_group)
    exact = [v.upper() for v in scope if "*" not in v]
    prefixes = [v[:-1].upper() for v in scope if v.endswith("*")]
    clauses: list[str] = []
    params: list[Any] = []
    if exact:
        clauses.append("upper(btrim(prfnr))=ANY(%s)")
        params.append(exact)
    for prefix in prefixes:
        clauses.append("upper(btrim(prfnr)) LIKE %s")
        params.append(prefix + "%")
    where_scope = " OR ".join(clauses) if clauses else "true"
    params.append(month)
    with pg_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) FROM {qname(TARGET_SCHEMA, TARGET_TABLE)}
            WHERE ({where_scope})
              AND CASE
                    WHEN btrim(budat) ~ '^\\d{{8}}$' THEN left(btrim(budat),6)
                    WHEN btrim(budat) ~ '^\\d{{4}}-\\d{{2}}' THEN replace(left(btrim(budat),7),'-','')
                    ELSE ''
                  END = replace(%s,'-','')
            """,
            tuple(params),
        )
        return int(cur.fetchone()[0])


def _write_sync_audit(
    pg_conn, *, source_group: str, month: str, remote_rows: int, fetched_rows: int,
    loaded_rows: int, local_rows: int, local_max: date | None, remote_max: date | None,
    status: str = "SUCCESS", message: str = "",
) -> None:
    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('control.sap_db60_sync_audit')")
            if cur.fetchone()[0] is None:
                return
            cur.execute(
                """
                INSERT INTO control.sap_db60_sync_audit
                    (source_group, target_month, remote_rows, fetched_rows,
                     upserted_rows, local_rows_after, local_max_budat, remote_max_budat,
                     status, started_at, finished_at, message, source_relation,
                     target_relation, sync_mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),%s,%s,%s,%s)
                """,
                (source_group, month, remote_rows, fetched_rows, loaded_rows, local_rows,
                 local_max, remote_max, status, message[:1000], Db60Config().source_table,
                 f"{TARGET_SCHEMA}.{TARGET_TABLE}", "INSERT_NEW"),
            )
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()


def sync_db60_zpay(
    settings: Settings,
    *,
    through_month: str,
    source_group: str = "ALL",
    start_month: str | None = None,
    batch_size: int = 10000,
    apply: bool = False,
) -> dict[str, Any]:
    through_month = _month_text(_month_start(through_month))
    source_group = str(source_group or "ALL").upper().strip()
    if batch_size < 100:
        raise ValueError("batch_size minimal 100")

    cfg = Db60Config()
    started = time.time()
    sql_conn = connect_db60(cfg)
    pg_conn = _pg_connect(settings)
    try:
        ensure_target_table(pg_conn)
        local_max_before = _local_max_budat(pg_conn, settings, source_group)
        remote_max = _remote_max_budat(sql_conn, cfg, settings, source_group)
        if start_month:
            first_month = _month_text(_month_start(start_month))
        elif local_max_before:
            # Redo the whole watermark month to close partial-month gaps. UPSERT makes this safe.
            first_month = local_max_before.strftime("%Y-%m")
        else:
            first_month = os.getenv("SAP_DB60_INITIAL_START_MONTH", f"{_month_start(through_month).year}-01")
            first_month = _month_text(_month_start(first_month))

        months = _month_range(first_month, through_month)
        if not months:
            months = [through_month]

        print("=" * 78, flush=True)
        print("SAP DB60 -> LOCAL POSTGRES CATCH-UP", flush=True)
        print("=" * 78, flush=True)
        print(f"[SAP DB60] source={cfg.source_table}", flush=True)
        print(f"[SAP DB60] target={TARGET_SCHEMA}.{TARGET_TABLE}", flush=True)
        print(f"[SAP DB60] live_view={LIVE_SCHEMA}.{LIVE_TABLE}", flush=True)
        print(f"[SAP DB60] scope={source_group} through={through_month}", flush=True)
        print(
            f"[SAP DB60] local_max_before={local_max_before or '-'} remote_max={remote_max or '-'} "
            f"catchup_months={months}",
            flush=True,
        )

        total_remote = 0
        total_fetched = 0
        total_inserted = 0
        total_skipped = 0
        monthly: dict[str, dict[str, int]] = {}
        for month in months:
            remote_count = _count_remote_month(sql_conn, cfg, settings, source_group, month)
            total_remote += remote_count
            print(f"[SAP DB60 MONTH] {month} remote_rows={remote_count:,} status={'LOAD' if apply else 'DRY_RUN'}", flush=True)
            fetched = 0
            inserted = 0
            skipped = 0
            if apply and remote_count:
                cur = _open_remote_month(sql_conn, cfg, settings, source_group, month)
                try:
                    while True:
                        raw = cur.fetchmany(batch_size)
                        if not raw:
                            break
                        rows = [_normalize_row(row) for row in raw]
                        fetched += len(rows)
                        ins, skp = _insert_new_batch(pg_conn, rows)
                        inserted += ins
                        skipped += skp
                        print(
                            f"[SAP DB60 LOAD] {month} fetched={fetched:,}/{remote_count:,} "
                            f"inserted_new={inserted:,} skipped_existing={skipped:,}",
                            flush=True,
                        )
                finally:
                    cur.close()
            local_count = _count_local_month(pg_conn, settings, source_group, month)
            monthly[month] = {"remote": remote_count, "fetched": fetched, "inserted": inserted, "skipped": skipped, "local": local_count}
            total_fetched += fetched
            total_inserted += inserted
            total_skipped += skipped
            print(
                f"[SAP DB60 VERIFY] {month} remote_rows={remote_count:,} local_rows={local_count:,} "
                f"delta={local_count-remote_count:+,}",
                flush=True,
            )
            if apply:
                _write_sync_audit(
                    pg_conn, source_group=source_group, month=month,
                    remote_rows=remote_count, fetched_rows=fetched, loaded_rows=inserted,
                    local_rows=local_count, local_max=_local_max_budat(pg_conn, settings, source_group),
                    remote_max=remote_max, status="SUCCESS",
                    message="month catch-up completed",
                )

        if apply:
            ensure_live_view(pg_conn)
        local_max_after = _local_max_budat(pg_conn, settings, source_group)
        elapsed = time.time() - started
        print(
            f"[SAP DB60 DONE] remote_rows={total_remote:,} fetched={total_fetched:,} "
            f"inserted_new={total_inserted:,} skipped_existing={total_skipped:,} "
            f"local_max_after={local_max_after or '-'} elapsed={elapsed:.1f}s",
            flush=True,
        )
        if apply and remote_max and local_max_after and local_max_after < min(remote_max, date(_month_start(through_month).year, _month_start(through_month).month, calendar.monthrange(_month_start(through_month).year, _month_start(through_month).month)[1])):
            print(
                f"[SAP DB60 WARNING] local watermark {local_max_after} masih di bawah remote watermark {remote_max}.",
                flush=True,
            )
        return {
            "source": cfg.source_table,
            "target": f"{TARGET_SCHEMA}.{TARGET_TABLE}",
            "live_view": f"{LIVE_SCHEMA}.{LIVE_TABLE}",
            "source_group": source_group,
            "through_month": through_month,
            "months": monthly,
            "local_max_before": str(local_max_before or ""),
            "local_max_after": str(local_max_after or ""),
            "inserted_new": total_inserted,
            "skipped_existing": total_skipped,
            "remote_max": str(remote_max or ""),
            "total_remote": total_remote,
            "total_fetched": total_fetched,
            "total_loaded": total_inserted,
            "apply": bool(apply),
        }
    finally:
        try:
            sql_conn.close()
        finally:
            pg_conn.close()
