"""
Migrasi DB60 SQL Server -> PostgreSQL Local
Tabel: db_audit.bronze.zpay_harvesting
Target: raw.zpay_harvesting

VERSI EXACT SCHEMA berdasarkan struktur DBeaver yang Bapak kirim:
- Source string VARCHAR mengikuti length source, tetapi minimal target VARCHAR(24).
- Source FLOAT(53) -> PostgreSQL DOUBLE PRECISION.
- ID -> VARCHAR(128) NOT NULL PRIMARY KEY.
- Tidak memakai ODBC/admin, memakai mssql-python atau pymssql.
- Kolom GENERATED_AT dihapus karena tidak ada di struktur zpay_harvesting terbaru.
- Kolom Estate, Block_lapangan, CC_Receiver dimasukkan.
- ZBRONDOLAN posisinya setelah LFBAG.

Install:
    py -m pip install --user mssql-python psycopg2-binary python-dotenv

Set password:
    setx VALID_OPH_SQL_PASSWORD "password_sql_server_db60"
    setx PG_PASSWORD "password_postgres"

Tutup PowerShell, buka ulang.

Run pertama kali / reset table:
    py migrate_db60_zpay_harvesting_exact_schema.py --mode recreate-table

Run normal tahun 2026:
    py migrate_db60_zpay_harvesting_exact_schema.py --budat-year 2026

Run semua tahun:
    py migrate_db60_zpay_harvesting_exact_schema.py --budat-year all

Scheduling data terbaru:
    py migrate_db60_zpay_harvesting_exact_schema.py --budat-year 2026 --recent-days 7
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError as exc:
    raise SystemExit(
        "Module PostgreSQL belum terinstall. Jalankan:\n"
        "  py -m pip install --user psycopg2-binary"
    ) from exc

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# =============================================================================
# 1) CONFIG
# =============================================================================

@dataclass(frozen=True)
class SqlServerConfig:
    enabled: bool = os.getenv("VALID_OPH_SQL_ENABLED", "True").lower() in {"1", "true", "yes", "y"}
    host: str = os.getenv("VALID_OPH_SQL_SERVER", "192.168.202.60")
    port: int = int(os.getenv("VALID_OPH_SQL_PORT", "1433"))
    database: str = os.getenv("VALID_OPH_SQL_DATABASE", "db_audit")
    user: str = os.getenv("VALID_OPH_SQL_USER", "audit")
    password: str = os.getenv("VALID_OPH_SQL_PASSWORD", os.getenv("DB60_PASSWORD", ""))


@dataclass(frozen=True)
class PostgresConfig:
    host: str = os.getenv("PG_HOST", "localhost")
    port: int = int(os.getenv("PG_PORT", "5432"))
    user: str = os.getenv("PG_USER", "postgres")
    password: str = os.getenv("PG_PASSWORD", "")
    database: str = os.getenv("PG_DATABASE", "golden_eye")
    schema_name: str = os.getenv("PG_SCHEMA", "raw")
    table_name: str = os.getenv("PG_ZPAY_TABLE", "V_zpay_harvesting")


SOURCE_TABLE = os.getenv("VALID_OPH_SQL_TABLE", "db_audit.bronze.V_zpay_harvesting")


# =============================================================================
# 2) EXACT SCHEMA
# =============================================================================

# Format tuple:
# (source_name, target_name, source_type_note, pg_type, select_expr)
# Untuk VARCHAR: PostgreSQL memakai max(source_length, 24) sesuai permintaan.
COLUMN_DEFS = [
    ("MANDT", "mandt", "varchar(3)", "VARCHAR(24)", "CAST([MANDT] AS NVARCHAR(MAX))"),
    ("PRFNR", "prfnr", "varchar(30)", "VARCHAR(30)", "CAST([PRFNR] AS NVARCHAR(MAX))"),
    ("EMPNR", "empnr", "varchar(30)", "VARCHAR(30)", "CAST([EMPNR] AS NVARCHAR(MAX))"),
    ("BUDAT", "budat", "varchar(20)", "VARCHAR(24)", "CAST([BUDAT] AS NVARCHAR(MAX))"),
    ("POSNR", "posnr", "varchar(6)", "VARCHAR(24)", "CAST([POSNR] AS NVARCHAR(MAX))"),
    ("POSNRT", "posnrt", "varchar(6)", "VARCHAR(24)", "CAST([POSNRT] AS NVARCHAR(MAX))"),
    ("ENAME", "ename", "varchar(50)", "VARCHAR(50)", "CAST([ENAME] AS NVARCHAR(MAX))"),
    ("JBCDE", "jbcde", "varchar(30)", "VARCHAR(30)", "CAST([JBCDE] AS NVARCHAR(MAX))"),
    ("JBTYP", "jbtyp", "varchar(2)", "VARCHAR(24)", "CAST([JBTYP] AS NVARCHAR(MAX))"),
    ("ESTNR", "estnr", "varchar(2)", "VARCHAR(24)", "CAST([ESTNR] AS NVARCHAR(MAX))"),
    ("DIVNR", "divnr", "varchar(1)", "VARCHAR(24)", "CAST([DIVNR] AS NVARCHAR(MAX))"),
    ("PHASE", "phase", "varchar(7)", "VARCHAR(24)", "CAST([PHASE] AS NVARCHAR(MAX))"),
    ("BUKRS", "bukrs", "varchar(4)", "VARCHAR(24)", "CAST([BUKRS] AS NVARCHAR(MAX))"),
    ("BLOCK", "block", "varchar(3)", "VARCHAR(24)", "CAST([BLOCK] AS NVARCHAR(MAX))"),
    ("BANJIR", "banjir", "varchar(1)", "VARCHAR(24)", "CAST([BANJIR] AS NVARCHAR(MAX))"),
    ("BUKIT", "bukit", "varchar(1)", "VARCHAR(24)", "CAST([BUKIT] AS NVARCHAR(MAX))"),
    ("CHECK_OER", "check_oer", "varchar(1)", "VARCHAR(24)", "CAST([CHECK_OER] AS NVARCHAR(MAX))"),
    ("YPLANP", "yplanp", "varchar(4)", "VARCHAR(24)", "CAST([YPLANP] AS NVARCHAR(MAX))"),
    ("WERKS", "werks", "varchar(4)", "VARCHAR(24)", "CAST([WERKS] AS NVARCHAR(MAX))"),
    ("XBLNR", "xblnr", "varchar(16)", "VARCHAR(24)", "CAST([XBLNR] AS NVARCHAR(MAX))"),
    ("EMPNR_M", "empnr_m", "varchar(30)", "VARCHAR(30)", "CAST([EMPNR_M] AS NVARCHAR(MAX))"),
    ("ENAME_M", "ename_m", "varchar(50)", "VARCHAR(50)", "CAST([ENAME_M] AS NVARCHAR(MAX))"),
    ("EMPNR_K", "empnr_k", "varchar(30)", "VARCHAR(30)", "CAST([EMPNR_K] AS NVARCHAR(MAX))"),
    ("ENAME_K", "ename_k", "varchar(50)", "VARCHAR(50)", "CAST([ENAME_K] AS NVARCHAR(MAX))"),
    ("ZMASAK", "zmasak", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZMASAK])"),
    ("ZKMASAK", "zkmasak", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZKMASAK])"),
    ("ZBUSUK", "zbusuk", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZBUSUK])"),
    ("ZHITAM", "zhitam", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZHITAM])"),
    ("ZABNORMAL", "zabnormal", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZABNORMAL])"),
    ("DELIV", "deliv", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [DELIV])"),
    ("BUNCH_TOTAL", "bunch_total", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [BUNCH_TOTAL])"),
    ("BUNCH_NPAY", "bunch_npay", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [BUNCH_NPAY])"),
    ("BUNCH_PAY", "bunch_pay", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [BUNCH_PAY])"),
    ("ZTPANJANG", "ztpanjang", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZTPANJANG])"),
    ("STLFR", "stlfr", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [STLFR])"),
    ("LSTFR", "lstfr", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [LSTFR])"),
    ("BRDTPH", "brdtph", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [BRDTPH])"),
    ("SPB", "spb", "varchar(25)", "VARCHAR(25)", "CAST([SPB] AS NVARCHAR(MAX))"),
    ("LFBAG", "lfbag", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [LFBAG])"),
    ("ZBRONDOLAN", "zbrondolan", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZBRONDOLAN])"),
    ("AQUAN", "aquan", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [AQUAN])"),
    ("MEINS", "meins", "varchar(3)", "VARCHAR(24)", "CAST([MEINS] AS NVARCHAR(MAX))"),
    ("NWEIG", "nweig", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [NWEIG])"),
    ("KUNNR", "kunnr", "varchar(10)", "VARCHAR(24)", "CAST([KUNNR] AS NVARCHAR(MAX))"),
    ("OPH", "oph", "varchar(25)", "VARCHAR(25)", "CAST([OPH] AS NVARCHAR(MAX))"),
    ("TPH", "tph", "varchar(3)", "VARCHAR(24)", "CAST([TPH] AS NVARCHAR(MAX))"),
    ("METHD", "methd", "varchar(1)", "VARCHAR(24)", "CAST([METHD] AS NVARCHAR(MAX))"),
    ("WRTOF", "wrtof", "varchar(1)", "VARCHAR(24)", "CAST([WRTOF] AS NVARCHAR(MAX))"),
    ("HKBSC", "hkbsc", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [HKBSC])"),
    ("HKALW", "hkalw", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [HKALW])"),
    ("ZTONASE", "ztonase", "float(53)", "DOUBLE PRECISION", "TRY_CONVERT(FLOAT, [ZTONASE])"),
    ("ERNAM", "ernam", "varchar(12)", "VARCHAR(24)", "CAST([ERNAM] AS NVARCHAR(MAX))"),
    ("ERDAT", "erdat", "varchar(20)", "VARCHAR(24)", "CAST([ERDAT] AS NVARCHAR(MAX))"),
    ("ERZET", "erzet", "varchar(20)", "VARCHAR(24)", "CAST([ERZET] AS NVARCHAR(MAX))"),
    ("AENAM", "aenam", "varchar(12)", "VARCHAR(24)", "CAST([AENAM] AS NVARCHAR(MAX))"),
    ("AEDAT", "aedat", "varchar(20)", "VARCHAR(24)", "CAST([AEDAT] AS NVARCHAR(MAX))"),
    ("AEZET", "aezet", "varchar(20)", "VARCHAR(24)", "CAST([AEZET] AS NVARCHAR(MAX))"),
    ("WRDAT", "wrdat", "varchar(20)", "VARCHAR(24)", "CAST([WRDAT] AS NVARCHAR(MAX))"),
    ("WRNAM", "wrnam", "varchar(12)", "VARCHAR(24)", "CAST([WRNAM] AS NVARCHAR(MAX))"),
    ("Estate", "estate", "varchar(128)", "VARCHAR(128)", "CAST([Estate] AS NVARCHAR(MAX))"),
    ("Block_lapangan", "block_lapangan", "varchar(64)", "VARCHAR(64)", "CAST([Block_lapangan] AS NVARCHAR(MAX))"),
    ("CC_Receiver", "cc_receiver", "varchar(10)", "VARCHAR(24)", "CAST([CC_Receiver] AS NVARCHAR(MAX))"),
    ("ID", "id", "varchar(128) not null", "VARCHAR(128)", "CAST([ID] AS NVARCHAR(MAX))"),
]

TARGET_COLUMNS = [c[1] for c in COLUMN_DEFS]
POSTGRES_COLUMN_TYPES = {c[1]: c[3] for c in COLUMN_DEFS}

SELECT_COLUMNS_SQL = ",\n    ".join(f"{select_expr} AS [{target_name}]" for _, target_name, _, _, select_expr in COLUMN_DEFS)


# =============================================================================
# 3) LOGGING + CONNECTION
# =============================================================================

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _mssql_brace_value(value: str) -> str:
    value = "" if value is None else str(value)
    return "{" + value.replace("}", "}}") + "}"


def connect_sqlserver_mssql_python(cfg: SqlServerConfig):
    try:
        from mssql_python import connect
    except ImportError as exc:
        raise RuntimeError(
            "Package mssql-python belum terinstall.\n"
            "Jalankan:\n"
            "  py -m pip install --user mssql-python\n"
            "Atau pakai fallback:\n"
            "  py migrate_db60_zpay_harvesting_exact_schema.py --mssql-driver pymssql"
        ) from exc

    if not cfg.enabled:
        raise RuntimeError("VALID_OPH_SQL_ENABLED=False, migrasi dibatalkan.")

    if not cfg.password:
        raise RuntimeError(
            "VALID_OPH_SQL_PASSWORD masih kosong.\n"
            'Set dengan: setx VALID_OPH_SQL_PASSWORD "password_sql_server_db60"\n'
            "Lalu tutup PowerShell dan buka ulang."
        )

    conn_str = (
        f"Server={cfg.host},{cfg.port};"
        f"Database={cfg.database};"
        f"UID={_mssql_brace_value(cfg.user)};"
        f"PWD={_mssql_brace_value(cfg.password)};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )

    logging.info(
        "Connect SQL Server DB60 via mssql-python %s:%s database=%s user=%s",
        cfg.host,
        cfg.port,
        cfg.database,
        cfg.user,
    )
    return connect(conn_str)


def connect_sqlserver_pymssql(cfg: SqlServerConfig):
    try:
        import pymssql
    except ImportError as exc:
        raise RuntimeError(
            "Package pymssql belum terinstall.\n"
            "Jalankan:\n"
            "  py -m pip install --user pymssql"
        ) from exc

    if not cfg.enabled:
        raise RuntimeError("VALID_OPH_SQL_ENABLED=False, migrasi dibatalkan.")

    if not cfg.password:
        raise RuntimeError(
            "VALID_OPH_SQL_PASSWORD masih kosong.\n"
            'Set dengan: setx VALID_OPH_SQL_PASSWORD "password_sql_server_db60"\n'
            "Lalu tutup PowerShell dan buka ulang."
        )

    logging.info(
        "Connect SQL Server DB60 via pymssql %s:%s database=%s user=%s",
        cfg.host,
        cfg.port,
        cfg.database,
        cfg.user,
    )
    return pymssql.connect(
        server=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        login_timeout=30,
        timeout=120,
        charset="UTF-8",
        as_dict=False,
    )


def connect_sqlserver(cfg: SqlServerConfig, driver: str):
    driver = driver.lower().strip()
    if driver == "mssql-python":
        return connect_sqlserver_mssql_python(cfg)
    if driver == "pymssql":
        return connect_sqlserver_pymssql(cfg)
    raise ValueError("--mssql-driver harus mssql-python atau pymssql")


def connect_postgres(cfg: PostgresConfig):
    logging.info(
        "Connect PostgreSQL %s:%s database=%s user=%s",
        cfg.host,
        cfg.port,
        cfg.database,
        cfg.user,
    )
    return psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        dbname=cfg.database,
        connect_timeout=30,
    )


# =============================================================================
# 4) POSTGRES DDL
# =============================================================================

def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def create_schema_if_needed(pg_conn, cfg: PostgresConfig) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(cfg.schema_name)};")
    pg_conn.commit()


def drop_target_table(pg_conn, cfg: PostgresConfig) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {qident(cfg.schema_name)}.{qident(cfg.table_name)};")
    pg_conn.commit()
    logging.warning("Target table %s.%s di-drop untuk recreate schema.", cfg.schema_name, cfg.table_name)


def create_target_table(pg_conn, cfg: PostgresConfig) -> None:
    col_defs = []
    for source_name, target_name, source_type, pg_type, _ in COLUMN_DEFS:
        if target_name == "id":
            col_defs.append(f"{qident(target_name)} {pg_type} NOT NULL PRIMARY KEY")
        else:
            col_defs.append(f"{qident(target_name)} {pg_type}")

    col_defs.append('"migrated_at" TIMESTAMP NOT NULL DEFAULT NOW()')

    ddl = f"""
    CREATE TABLE IF NOT EXISTS {qident(cfg.schema_name)}.{qident(cfg.table_name)} (
        {",\n        ".join(col_defs)}
    );
    """

    with pg_conn.cursor() as cur:
        cur.execute(ddl)
    pg_conn.commit()


def ensure_indexes(pg_conn, cfg: PostgresConfig) -> None:
    indexes = [
        ("idx_zpay_budat", "budat"),
        ("idx_zpay_prfnr", "prfnr"),
        ("idx_zpay_oph", "oph"),
        ("idx_zpay_bukrs", "bukrs"),
        ("idx_zpay_estate", "estate"),
        ("idx_zpay_block_lapangan", "block_lapangan"),
        ("idx_zpay_cc_receiver", "cc_receiver"),
    ]
    with pg_conn.cursor() as cur:
        for idx, col in indexes:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {qident(idx)} "
                f"ON {qident(cfg.schema_name)}.{qident(cfg.table_name)}({qident(col)});"
            )
    pg_conn.commit()


def normalize_pg_type(data_type: str, char_len: Optional[int], precision: Optional[int], scale: Optional[int]) -> str:
    data_type = str(data_type).lower()
    if data_type == "character varying":
        return f"VARCHAR({char_len})" if char_len else "VARCHAR"
    if data_type == "integer":
        return "INTEGER"
    if data_type == "bigint":
        return "BIGINT"
    if data_type == "double precision":
        return "DOUBLE PRECISION"
    if data_type == "numeric":
        return f"NUMERIC({precision},{scale})" if precision is not None and scale is not None else "NUMERIC"
    if data_type in {"timestamp without time zone", "timestamp with time zone"}:
        return "TIMESTAMP"
    if data_type == "text":
        return "TEXT"
    return data_type.upper()


def check_target_schema(pg_conn, cfg: PostgresConfig) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                column_name,
                data_type,
                character_maximum_length,
                numeric_precision,
                numeric_scale,
                is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position;
            """,
            (cfg.schema_name, cfg.table_name),
        )
        rows = cur.fetchall()

    by_col = {
        r[0]: {
            "data_type": r[1],
            "character_maximum_length": r[2],
            "numeric_precision": r[3],
            "numeric_scale": r[4],
            "is_nullable": r[5],
        }
        for r in rows
    }

    mismatches = []
    for _, target_name, _, expected_type, _ in COLUMN_DEFS:
        if target_name not in by_col:
            mismatches.append(f"- {target_name}: kolom belum ada, expected {expected_type}")
            continue

        info = by_col[target_name]
        actual = normalize_pg_type(
            info["data_type"],
            info["character_maximum_length"],
            info["numeric_precision"],
            info["numeric_scale"],
        )

        if actual != expected_type:
            mismatches.append(f"- {target_name}: actual {actual}, expected {expected_type}")

        if target_name == "id" and info["is_nullable"] != "NO":
            mismatches.append("- id: harus NOT NULL")

    if mismatches:
        detail = "\n".join(mismatches[:80])
        more = "" if len(mismatches) <= 80 else f"\n... dan {len(mismatches) - 80} mismatch lain"
        raise RuntimeError(
            "Struktur target PostgreSQL belum sesuai schema terbaru.\n"
            "Kemungkinan table lama masih memakai schema script lama.\n\n"
            f"{detail}{more}\n\n"
            "Solusi paling bersih untuk raw local:\n"
            "  py migrate_db60_zpay_harvesting_exact_schema.py --mode recreate-table\n"
        )


def ensure_pg_objects(pg_conn, cfg: PostgresConfig, mode: str) -> None:
    create_schema_if_needed(pg_conn, cfg)

    if mode == "recreate-table":
        drop_target_table(pg_conn, cfg)

    create_target_table(pg_conn, cfg)
    check_target_schema(pg_conn, cfg)
    ensure_indexes(pg_conn, cfg)


def truncate_target_scope(pg_conn, cfg: PostgresConfig, budat_year: Optional[str]) -> None:
    with pg_conn.cursor() as cur:
        if not budat_year or str(budat_year).lower() == "all":
            cur.execute(f"TRUNCATE TABLE {qident(cfg.schema_name)}.{qident(cfg.table_name)};")
            logging.warning("Target table dikosongkan seluruhnya.")
        else:
            cur.execute(
                f"DELETE FROM {qident(cfg.schema_name)}.{qident(cfg.table_name)} WHERE LEFT({qident('budat')}, 4) = %s;",
                (str(budat_year),),
            )
            logging.warning("Target PostgreSQL BUDAT tahun %s sudah dihapus untuk refresh ulang.", budat_year)
    pg_conn.commit()


# =============================================================================
# 5) EXTRACT SQL SERVER
# =============================================================================

def build_where_clause(budat_year: Optional[str], recent_days: Optional[int]) -> str:
    parts = []

    if budat_year and str(budat_year).lower() != "all":
        y = str(budat_year).strip()
        if not (len(y) == 4 and y.isdigit()):
            raise ValueError("--budat-year harus format tahun 4 digit, contoh 2026, atau all.")
        parts.append(f"LEFT(CAST([BUDAT] AS VARCHAR(24)), 4) = '{y}'")

    if recent_days is not None and int(recent_days) > 0:
        days = int(recent_days)
        date_checks = []
        for col in ["BUDAT", "ERDAT", "AEDAT", "WRDAT"]:
            date_checks.append(
                f"COALESCE(TRY_CONVERT(date, CAST([{col}] AS VARCHAR(24)), 112), "
                f"TRY_CONVERT(date, CAST([{col}] AS VARCHAR(24)))) >= DATEADD(day, -{days}, CONVERT(date, GETDATE()))"
            )
        parts.append("(" + " OR ".join(date_checks) + ")")

    return " AND ".join(parts) if parts else "1 = 1"


def normalize_mssql_row(row: Any, ncols: int) -> Tuple:
    if isinstance(row, tuple):
        return row
    if isinstance(row, list):
        return tuple(row)

    values = []
    for idx in range(ncols):
        try:
            values.append(row[idx])
            continue
        except Exception:
            pass
        raise RuntimeError(f"Gagal membaca index {idx} dari row SQL Server. Tipe row={type(row)} row={row}")
    return tuple(values)


def count_source_rows(sql_conn, where_clause: str) -> int:
    sql = f"SELECT COUNT(1) AS total_rows FROM {SOURCE_TABLE} WHERE {where_clause};"
    cur = sql_conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    try:
        return int(row[0])
    except Exception:
        return int(getattr(row, "total_rows"))


def count_source_distinct_id(sql_conn, where_clause: str) -> int:
    sql = f"SELECT COUNT(DISTINCT [ID]) AS total_rows FROM {SOURCE_TABLE} WHERE {where_clause};"
    cur = sql_conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    try:
        return int(row[0])
    except Exception:
        return int(getattr(row, "total_rows"))


def count_source_null_id(sql_conn, where_clause: str) -> int:
    sql = f"SELECT COUNT(1) AS total_rows FROM {SOURCE_TABLE} WHERE {where_clause} AND [ID] IS NULL;"
    cur = sql_conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    try:
        return int(row[0])
    except Exception:
        return int(getattr(row, "total_rows"))


def open_source_cursor(sql_conn, where_clause: str):
    sql = f"""
    SELECT
        {SELECT_COLUMNS_SQL}
    FROM {SOURCE_TABLE}
    WHERE {where_clause};
    """
    cur = sql_conn.cursor()
    cur.execute(sql)
    return cur


# =============================================================================
# 6) LOAD POSTGRES
# =============================================================================

def clean_rows_before_insert(rows: Sequence[Tuple]) -> List[Tuple]:
    id_index = TARGET_COLUMNS.index("id")
    by_id = {}
    null_skipped = 0
    dup_count = 0

    for row in rows:
        row = tuple(row)
        row_id = row[id_index]
        if row_id is None or str(row_id).strip() == "":
            null_skipped += 1
            continue

        row_id = str(row_id)
        row = row[:id_index] + (row_id,) + row[id_index + 1:]

        if row_id in by_id:
            dup_count += 1
        by_id[row_id] = row

    if null_skipped:
        logging.warning("Skip %s rows karena ID NULL/kosong.", null_skipped)
    if dup_count:
        logging.warning("Dedup %s duplicate ID dalam batch yang sama. Row terakhir dipakai.", dup_count)

    return list(by_id.values())


def upsert_postgres_batch(pg_conn, cfg: PostgresConfig, rows: Sequence[Tuple]) -> int:
    rows = clean_rows_before_insert(rows)
    if not rows:
        return 0

    columns_sql = ", ".join(qident(c) for c in TARGET_COLUMNS)
    update_columns = [c for c in TARGET_COLUMNS if c != "id"]
    update_sql = ", ".join([f"{qident(c)} = EXCLUDED.{qident(c)}" for c in update_columns])

    insert_sql = f"""
    INSERT INTO {qident(cfg.schema_name)}.{qident(cfg.table_name)} ({columns_sql})
    VALUES %s
    ON CONFLICT ({qident('id')}) DO UPDATE SET
        {update_sql},
        {qident('migrated_at')} = NOW();
    """

    with pg_conn.cursor() as cur:
        execute_values(cur, insert_sql, rows, page_size=min(len(rows), 5000))
    pg_conn.commit()
    return len(rows)


def count_target_rows(pg_conn, cfg: PostgresConfig, budat_year: Optional[str]) -> int:
    with pg_conn.cursor() as cur:
        if budat_year and str(budat_year).lower() != "all":
            cur.execute(
                f"SELECT COUNT(1) FROM {qident(cfg.schema_name)}.{qident(cfg.table_name)} WHERE LEFT({qident('budat')}, 4) = %s",
                (str(budat_year),),
            )
        else:
            cur.execute(f"SELECT COUNT(1) FROM {qident(cfg.schema_name)}.{qident(cfg.table_name)}")
        return int(cur.fetchone()[0])


# =============================================================================
# 7) MIGRATE
# =============================================================================

def migrate(
    budat_year: Optional[str],
    batch_size: int,
    mode: str,
    mssql_driver: str,
    recent_days: Optional[int],
) -> None:
    sql_cfg = SqlServerConfig()
    pg_cfg = PostgresConfig()

    sql_conn = None
    pg_conn = None
    source_cur = None

    started = time.time()
    total_fetched = 0
    total_loaded = 0

    try:
        sql_conn = connect_sqlserver(sql_cfg, mssql_driver)
        pg_conn = connect_postgres(pg_cfg)

        ensure_pg_objects(pg_conn, pg_cfg, mode)

        if mode == "truncate-scope":
            truncate_target_scope(pg_conn, pg_cfg, budat_year)

        where_clause = build_where_clause(budat_year, recent_days)
        source_total = count_source_rows(sql_conn, where_clause)
        distinct_id_total = count_source_distinct_id(sql_conn, where_clause)
        null_id_total = count_source_null_id(sql_conn, where_clause)

        logging.info("Source table       : %s", SOURCE_TABLE)
        logging.info("Target table       : %s.%s", pg_cfg.schema_name, pg_cfg.table_name)
        logging.info("BUDAT year filter  : %s", budat_year if budat_year else "all")
        logging.info("Source rows        : %s", f"{source_total:,}")
        logging.info("Distinct ID source : %s", f"{distinct_id_total:,}")

        if source_total != distinct_id_total:
            logging.warning(
                "Source memiliki duplicate ID. Karena target memakai ID sebagai PRIMARY KEY, jumlah target bisa lebih kecil dari source. "
                "Jika Bapak ingin semua row duplicate ID tetap masuk, minta versi raw_key."
            )
        if recent_days:
            logging.info("Filter recent_days aktif: %s hari terakhir berdasarkan BUDAT/ERDAT/AEDAT/WRDAT", recent_days)
        if null_id_total:
            logging.warning("Source rows dengan ID NULL: %s", f"{null_id_total:,}")

        source_cur = open_source_cursor(sql_conn, where_clause)

        while True:
            rows_raw = source_cur.fetchmany(batch_size)
            if not rows_raw:
                break

            rows = [normalize_mssql_row(row, len(TARGET_COLUMNS)) for row in rows_raw]
            total_fetched += len(rows)

            loaded = upsert_postgres_batch(pg_conn, pg_cfg, rows)
            total_loaded += loaded

            elapsed = max(time.time() - started, 1)
            rate = total_loaded / elapsed
            logging.info(
                "Batch fetched=%s | loaded=%s | total_fetched=%s | total_loaded=%s | rate=%.2f rows/sec",
                f"{len(rows):,}",
                f"{loaded:,}",
                f"{total_fetched:,}",
                f"{total_loaded:,}",
                rate,
            )

        target_total = count_target_rows(pg_conn, pg_cfg, budat_year)
        elapsed = time.time() - started

        logging.info("=" * 80)
        logging.info("MIGRASI SELESAI")
        logging.info("MSSQL driver       : %s", mssql_driver)
        logging.info("Mode               : %s", mode)
        logging.info("BUDAT year filter  : %s", budat_year if budat_year else "all")
        logging.info("Recent days        : %s", recent_days if recent_days else "-")
        logging.info("Source count       : %s", f"{source_total:,}")
        logging.info("Distinct ID source : %s", f"{distinct_id_total:,}")
        logging.info("Fetched rows       : %s", f"{total_fetched:,}")
        logging.info("Loaded rows        : %s", f"{total_loaded:,}")
        logging.info("Target count scope : %s", f"{target_total:,}")
        logging.info("Elapsed            : %.2f detik", elapsed)
        logging.info("=" * 80)

    except Exception:
        logging.exception("Migrasi gagal.")
        if pg_conn:
            pg_conn.rollback()
        raise
    finally:
        if source_cur:
            source_cur.close()
        if sql_conn:
            sql_conn.close()
        if pg_conn:
            pg_conn.close()


# =============================================================================
# 8) CLI
# =============================================================================

def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrasi DB60 db_audit.bronze.zpay_harvesting ke PostgreSQL raw sesuai schema DBeaver."
    )
    parser.add_argument(
        "--budat-year",
        default=os.getenv("VALID_OPH_BUDAT_YEAR", "2026"),
        help="Filter BUDAT tahun. Default: 2026. Gunakan all untuk semua tahun.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help="Jumlah row per batch. Default: 10000.",
    )
    parser.add_argument(
        "--mode",
        choices=["upsert", "truncate-scope", "recreate-table"],
        default="upsert",
        help=(
            "upsert=aman berulang berdasarkan ID, "
            "truncate-scope=hapus data sesuai scope BUDAT lalu tarik ulang, "
            "recreate-table=drop table dan buat ulang sesuai schema terbaru."
        ),
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=None,
        help="Opsional untuk scheduling. Contoh --recent-days 7 berdasarkan BUDAT/ERDAT/AEDAT/WRDAT.",
    )
    parser.add_argument(
        "--mssql-driver",
        choices=["mssql-python", "pymssql"],
        default=os.getenv("MSSQL_PY_DRIVER", "mssql-python"),
        help="Driver SQL Server tanpa ODBC. Default: mssql-python. Fallback: pymssql.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> None:
    setup_logging()
    args = parse_args(argv)

    if args.batch_size < 100:
        raise SystemExit("--batch-size terlalu kecil. Gunakan minimal 100.")

    migrate(
        budat_year=args.budat_year,
        batch_size=args.batch_size,
        mode=args.mode,
        mssql_driver=args.mssql_driver,
        recent_days=args.recent_days,
    )


if __name__ == "__main__":
    main()
